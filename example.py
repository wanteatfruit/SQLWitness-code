"""Find a small counterexample; run with --dialect mysql or postgresql as needed."""
import argparse
from pprint import pprint
import random

from sqlwitness import counterexample


SCHEMA = [{
    "TableName": "employees",
    "PKeys": [{"Name": "id", "Type": "int"}],
    "FKeys": [],
    "Others": [{"Name": "age", "Type": "int"}],
}]
GROUNDTRUTH = "SELECT id FROM employees WHERE age >= 18"
CANDIDATE = "SELECT id FROM employees WHERE age > 18"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialect", choices=["sqlite", "mysql", "postgresql"], default="sqlite")
    args = parser.parse_args()
    random.seed(0)
    refuted, data, elapsed, iterations = counterexample(
        schema=SCHEMA,
        constraints="",
        groundtruth_query=GROUNDTRUTH,
        candidate_query=CANDIDATE,
        dialect=args.dialect,
        iteration=100,
        use_multiprocessing=False,
    )
    print(f"\nCounterexample found: {refuted}")
    print(f"Iterations: {iterations}; elapsed: {elapsed:.3f}s")
    if refuted:
        pprint(data)
    else:
        print("No counterexample found within the search budget; equivalence is not proven.")
    return 0 if refuted else 1


if __name__ == "__main__":
    raise SystemExit(main())
