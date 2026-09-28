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
    assert parameters['coverage'].default == 1
    assert parameters['termination_method'].default == 'laplace'
    assert 'sqlfpc_coverage' not in parameters
    for coverage in (0, 2, 3):
        with pytest.raises(ValueError, match='Only 1-way coverage'):
            search(SCHEMA, '', GT, CD, coverage=coverage)
    with pytest.raises(ValueError, match='termination_method'):
        search(SCHEMA, '', GT, CD, termination_method='good_turing')


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


@pytest.mark.parametrize('method', ['laplace', None])
@pytest.mark.parametrize('boolean_coverage', [True, False])
def test_search_with_and_without_stopping(tmp_path, monkeypatch, method, boolean_coverage):
    monkeypatch.chdir(tmp_path)
    # Equivalent but textually distinct queries must run the search.
    result = counterexample(
        SCHEMA, '', GT, 'SELECT id FROM employees WHERE 18 <= age', dialect='sqlite',
        termination_method=method, boolean_coverage=boolean_coverage, iteration=2,
    )
    assert result[0] is False
    assert result[3] == 2
