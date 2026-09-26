"""Optional integration tests; use URLs pointing to disposable test servers."""
import os
import random

import pytest

from sqlwitness import counterexample, counterexample_multidialect
from sqlwitness.online.runner import DatabaseRunner
from sqlwitness.online.schema_handler import SchemaHandler

from test_core import SCHEMA, GT, CD


def require_backend(dialect):
    variable = "SQLWITNESS_MYSQL_URL" if dialect == "mysql" else "SQLWITNESS_POSTGRES_URL"
    if not os.environ.get(variable):
        pytest.skip(f"Set {variable} to run this integration test")


def replay(data, dialect, query):
    runner = DatabaseRunner(SchemaHandler(SCHEMA), data, dialect_name=dialect,
                            worker_id=f"replay_{os.getpid()}")
    try:
        runner.setup_tables()
        runner.insert_tables()
        result = runner.execute_query(query)
        assert result is not None
        return result
    finally:
        runner.cleanup_database()


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize("parallel", [False, True])
def test_external_witness(dialect, parallel):
    require_backend(dialect)
    random.seed(0)
    refuted, data, _, _ = counterexample(
        SCHEMA, "", GT, CD, dialect=dialect, timeout=15, iteration=100,
        use_multiprocessing=parallel,
    )
    assert refuted and data
    assert replay(data, dialect, GT) != replay(data, dialect, CD)


def test_cross_backend_witness():
    require_backend("mysql")
    require_backend("postgresql")
    random.seed(0)
    refuted, data, _, _ = counterexample_multidialect(
        SCHEMA, "", GT, CD, dialect_gt="mysql", dialect_cd="postgresql",
        timeout=15, iteration=100,
    )
    assert refuted and data
    assert replay(data, "mysql", GT) != replay(data, "postgresql", CD)
