import random
import sqlite3
from unittest.mock import patch

import pytest

from sqlwitness import counterexample, build_boolean_coverage
from sqlwitness.online.constraints import ConstraintParser
from sqlwitness.online.runner import DatabaseRunner
from sqlwitness.online.schema_handler import SchemaHandler


SCHEMA = [{
    "TableName": "employees",
    "PKeys": [{"Name": "id", "Type": "int"}],
    "FKeys": [],
    "Others": [{"Name": "age", "Type": "int"}],
}]
GT = "SELECT id FROM employees WHERE age >= 18"
CD = "SELECT id FROM employees WHERE age > 18"


@pytest.mark.parametrize("multiprocessing", [False, True])
def test_witness_distinguishes_queries(tmp_path, monkeypatch, multiprocessing):
    monkeypatch.chdir(tmp_path)
    random.seed(0)
    refuted, data, elapsed, iterations = counterexample(
        SCHEMA, "", GT, CD, dialect="sqlite", iteration=100, timeout=10,
        use_multiprocessing=multiprocessing,
    )
    assert refuted and data and iterations > 0 and elapsed >= 0
    # Replay the witness independently of DatabaseRunner.
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, age INTEGER)")
        conn.executemany("INSERT INTO employees VALUES (?, ?)", data["employees"][1:])
        assert conn.execute(GT).fetchall() != conn.execute(CD).fetchall()
    assert not list(tmp_path.glob("*.db"))


def test_identical_queries():
    assert counterexample(SCHEMA, "", GT, GT, dialect="sqlite") == (False, None, 0.0, 0)


def test_constraints_and_coverage(tmp_path):
    parser = ConstraintParser()
    constraint = "employees.age >= 18"
    expected = parser.parse(constraint)
    assert expected
    path = tmp_path / "age.constraint"
    path.write_text(constraint)
    assert parser.parse_from_file(path) == expected
    yaml_path = tmp_path / "age.yml"
    yaml_path.write_text(f"age: {constraint}\n")
    assert parser.parse_from_yml(yaml_path) == expected
    coverage = build_boolean_coverage(GT, dialect="sqlite")
    assert coverage and coverage["row_coverage"]
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE employees (id INTEGER, age INTEGER)")
        conn.executemany("INSERT INTO employees VALUES (?, ?)", [(1, 17), (2, 18), (3, None)])
        assert len(set(conn.execute(coverage["row_coverage"]).fetchall())) == 3


@pytest.mark.parametrize("dialect,variable,url,driver", [
    ("mysql", "SQLWITNESS_MYSQL_URL", "mysql://user:p%40ss@localhost:3306/testdb?charset=utf8mb4", "mysql+pymysql"),
    ("postgresql", "SQLWITNESS_POSTGRES_URL", "postgresql://user:p%40ss@localhost:5432/testdb?sslmode=require", "postgresql+psycopg2"),
])
def test_connection_configuration(monkeypatch, dialect, variable, url, driver):
    monkeypatch.setenv(variable, url)
    configured = DatabaseRunner.connection_url(dialect)
    assert configured.drivername == driver
    assert configured.password == "p@ss"
    runner = DatabaseRunner(SchemaHandler(SCHEMA), {}, dialect_name=dialect, worker_id="123")
    with patch("sqlwitness.online.runner.create_engine") as create:
        runner._get_worker_engine()
        worker_url = create.call_args.args[0]
        assert worker_url.database == ("worker_db_123" if dialect == "mysql" else "testdb")
        assert worker_url.query == configured.query
        assert worker_url.password == configured.password
        if dialect == "mysql":
            runner._get_admin_engine()
            assert create.call_args.args[0].database is None
    monkeypatch.delenv(variable)
    with pytest.raises(ValueError, match=variable):
        counterexample(SCHEMA, "", GT, CD, dialect=dialect)


@pytest.mark.parametrize("url", ["postgresql://user:pass@localhost/db", "mysql://user:pass@localhost"])
def test_invalid_mysql_configuration(monkeypatch, url):
    monkeypatch.setenv("SQLWITNESS_MYSQL_URL", url)
    with pytest.raises(ValueError, match="SQLWITNESS_MYSQL_URL"):
        DatabaseRunner.connection_url("mysql")
