import inspect

import pytest

from sqlwitness import counterexample, counterexample_multidialect
from sqlwitness.coverage import CoverageAccumulator
from sqlwitness.estimator import create_terminator
from test_core import SCHEMA, GT, CD


@pytest.mark.parametrize('search', [counterexample, counterexample_multidialect])
def test_search_defaults_and_removed_options(search):
    parameters = inspect.signature(search).parameters
    assert parameters['timeout'].default == 10
    for removed in ('boolean_coverage', 'termination_method', 'coverage', 'sqlfpc_coverage'):
        assert removed not in parameters
        with pytest.raises(TypeError, match=removed):
            search(SCHEMA, '', GT, CD, **{removed: None})


def test_independent_predicates_and_aggregate_group_keys():
    tracker = CoverageAccumulator(2, 1, 1, 1)
    results = {
        'gt_outcomes': [('row', (0, 1)), ('row', (1, 0)), ('agg', ('group-a', 1))],
        'cd_outcomes': [('row', (-1,)), ('agg', ('group-a', 0))],
    }
    assert tracker.update(results)
    assert tracker.compute_signal() == (5, 2)
    # New combinations or GROUP BY keys are not new per-predicate outcomes.
    assert not tracker.update({'gt_outcomes': [('row', (1, 1)), ('agg', ('group-b', 1))]})
    assert not tracker.is_saturated()
    for value in (-1, 0, 1):
        tracker.update({
            'gt_outcomes': [('row', (value, value)), ('agg', ('group-c', value))],
            'cd_outcomes': [('row', (value,)), ('agg', ('group-c', value))],
        })
    assert tracker.compute_signal() == (9, 6)
    assert tracker.is_saturated()
    assert not CoverageAccumulator(0, 0, 0, 0).is_saturated()


def test_laplace_patience_and_reset():
    terminator = create_terminator()
    for _ in range(17):
        terminator.update(False)
    assert not terminator.should_terminate()
    terminator.update(True)
    assert not terminator.should_terminate()
    for _ in range(18):
        terminator.update(False)
    assert terminator.should_terminate()
    with pytest.raises(ValueError, match='laplace'):
        create_terminator('good_turing')


def test_search_always_uses_coverage_and_laplace(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    result = counterexample(
        SCHEMA, '', GT, 'SELECT id FROM employees WHERE 18 <= age', dialect='sqlite',
        iteration=100, termination_target_risk=0.5,
    )
    assert result[0] is False
    assert 0 < result[3] < 100
    output = capsys.readouterr().out
    assert 'Using laplace termination estimator' in output
    assert 'Boolean coverage enabled (1-way)' in output
    assert 'Statistical termination' in output


def test_coverage_failure_keeps_bounded_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sqlwitness.build_boolean_coverage', lambda *args, **kwargs: None)
    result = counterexample(
        SCHEMA, '', GT, 'SELECT id FROM employees WHERE 18 <= age',
        dialect='sqlite', iteration=100,
    )
    assert result[0] is False
    assert result[3] == 20
    assert not list(tmp_path.glob('*.db'))
