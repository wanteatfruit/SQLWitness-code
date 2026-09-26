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

Parameters shared by both functions:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `schema` | Required | List of table definitions with `TableName`, `PKeys`, `FKeys`, and `Others`; see `example.py`. |
| `constraints` | Required | Constraint text, a `.constraint`/`.yml` file path, or a parsed list. Pass `""`, `[]`, or `None` for no extra constraints. |
| `groundtruth_query` | Required | Reference SQL query. |
| `candidate_query` | Required | SQL query to compare against the reference. |
| `timeout` | `4` | Search time budget in seconds, checked between iterations; not a database statement timeout. `None` or `0` disables it. |
| `iteration` | `None` | Sequential iteration limit; `None` becomes one billion iterations, normally bounded by timeout or early stopping. Currently ignored in multiprocessing mode. |
| `boolean_coverage` | `True` | Track Boolean predicate outcomes locally to guide generation and stopping. `False` disables this coverage tracking. |
| `termination_method` | `"laplace"` | Coverage-based stopping estimator: `"laplace"` or `"good_turing"`. `None` disables the estimator, but other stopping conditions still apply. Used with Boolean coverage. |
| `termination_target_risk` | `0.05` | Estimator threshold for finding new coverage, between 0 and 1; lower values generally search longer. This is not the probability that the queries are equivalent. |
| `coverage` | `1` | Track outcomes for individual predicates (1-way); `2` tracks pairs, higher positive values track larger combinations, and `0` tracks full combinations. Used with Boolean coverage. |
| `remove_null` | `False` | Keep NULL candidates enabled. `True` removes NULLs from ordinary value candidate pools; it is not a schema-wide NOT NULL constraint. |
| `remove_one` | `False` | Keep both default non-NULL values per type. `True` removes the second default (e.g. numeric `1`); query-derived or constrained values may still include it. |
| `remove_literal` | `False` | Keep query-literal value heuristics enabled. `True` disables those heuristics; explicit constraints still apply. |

Parameters specific to each function:

| Function | Parameter | Default | Meaning |
| --- | --- | --- | --- |
| `counterexample` | `dialect` | `"mysql"` | Backend for both queries: `"sqlite"`, `"mysql"`, or `"postgresql"`. |
| `counterexample` | `use_multiprocessing` | `False` | Run sequentially; `True` starts eight independent worker processes. |
| `counterexample` | `sqlfpc_coverage` | `False` | Legacy compatibility parameter; currently unused, so changing it has no effect. |
| `counterexample_multidialect` | `dialect_gt` | `"mysql"` | Backend for the reference query. |
| `counterexample_multidialect` | `dialect_cd` | `"postgresql"` | Backend for the candidate query. |

Cross-backend search runs sequentially. The example overrides the API defaults
with SQLite, a 10-second timeout, and 100 iterations. No counterexample within the
search budget **does not prove equivalence**. The separate
`sqlwitness.online.coverage` client can call the external SQLFpc service.

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
