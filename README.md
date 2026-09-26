# SQLWitness

SQLWitness searches for a database on which two SQL queries return different
results. This repository contains the core rule-based search, constraints,
Boolean coverage, and termination estimators, without LLMs or benchmark data.

## Setup and run

Use Python 3.10+ (validated with Python 3.12). From this directory:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
python example.py
```

The example uses SQLite, so no database server or API key is needed. It compares
`age >= 18` with `age > 18` and prints `Counterexample found: True` followed by
generated rows containing the distinguishing age of 18. Temporary SQLite files
are created in the working directory and cleaned up after the search.

## Python API

See `example.py` for a complete inline schema and call to
`sqlwitness.counterexample(...)`. Both this function and
`sqlwitness.counterexample_multidialect(...)` return
`(refuted, data, elapsed_seconds, iterations)`.

Use `dialect="sqlite"`, `"mysql"`, or `"postgresql"` for a single backend;
use `dialect_gt` and `dialect_cd` for cross-backend comparison. The API's default
dialect is MySQL. Set `timeout` and `iteration` to bound the search. No counterexample
within that budget **does not prove equivalence**. Boolean coverage runs locally;
the separate `sqlwitness.online.coverage` client uses an external SQLFpc service.

## MySQL and PostgreSQL

Start your own database server, install its driver, and export a connection URL:

```sh
python -m pip install '.[mysql,postgres]'
export SQLWITNESS_MYSQL_URL='mysql+pymysql://USER:PASSWORD@localhost:3306/testdb'
export SQLWITNESS_POSTGRES_URL='postgresql+psycopg2://USER:PASSWORD@localhost:5432/testdb'
python example.py --dialect mysql
python example.py --dialect postgresql
```

Replace the placeholders and URL-encode special characters in credentials.
Use a dedicated test environment: MySQL needs permission to create/drop worker
databases; PostgreSQL needs an existing database and permission to create/drop
worker schemas and their tables. The runner removes these worker objects after
each search. Environment variables are read directly; `.env` files are not loaded.

## Development

```sh
python -m pip install -e '.[dev]'
python -m pytest
```

Database integration tests run when the corresponding connection URLs are set;
otherwise they are skipped. Use only disposable test databases for these tests.
