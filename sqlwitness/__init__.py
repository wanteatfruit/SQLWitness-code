from typing import Optional, List, Tuple, Dict, Any, Union, cast
from sqlwitness.online.schema_handler import SchemaHandler
from sqlwitness.online.heuristics import HeuristicsHandler
from sqlwitness.online.generator import DataGenerator
from sqlwitness.online.runner import DatabaseRunner
from sqlwitness.online.constraints import ConstraintParser
from sqlwitness.coverage import (
    build_boolean_coverage, count_predicates,
    CoverageAccumulator,
)
from sqlwitness.estimator import create_terminator
import time
import re
import random
import multiprocessing
import os
from sqlglot import parse, exp

# build_boolean_coverage is now imported from sqlwitness.coverage


# --- Helper function for running coverage query on existing database ---
def _run_coverage_query_on_existing_db(
    args: Tuple[str, str, SchemaHandler, Dict, List, str]
) -> bool:
    query, dialect, schema_handler, data, constraints_parsed, worker_id = args
    runner = None
    try:
        # Create runner pointing to the existing database (via worker_id)
        if dialect == 'sqlite':
            runner = DatabaseRunner(schema_handler, data, dialect_name='sqlite', constraints=constraints_parsed, worker_id=worker_id)
        elif dialect == 'mysql':
            runner = DatabaseRunner(schema_handler, data, dialect_name='mysql', constraints=constraints_parsed, worker_id=worker_id)
        elif dialect == 'postgresql':
            runner = DatabaseRunner(schema_handler, data, dialect_name='postgresql', constraints=constraints_parsed, worker_id=worker_id)
        elif dialect == 'tsql':
            runner = DatabaseRunner(schema_handler, data, dialect_name='tsql', constraints=constraints_parsed, worker_id=worker_id)
        
        if runner:
            # Don't setup/insert - just execute the query on existing data
            result = runner.execute_query(query)
            return result is not None and len(result) > 0
        return False
    except Exception as e:
        print(f"Coverage query execution error: {e}")
        return False
    # Note: No cleanup here - the main runner will clean up at the end


def collect_coverage_outcomes(
    runner: Any,
    gt_row_query: Optional[str],
    gt_agg_query: Optional[str],
    cd_row_query: Optional[str],
    cd_agg_query: Optional[str],
    num_gt_row_predicates: int,
    num_cd_row_predicates: int,
    worker_label: str = "",
) -> Dict:
    """
    Execute coverage queries on an active runner and return a coverage_results dict
    ready for CoverageAccumulator.update().

    Format returned:
      {
        'gt_outcomes': [('row'|'agg', row_tuple), ...],
        'gt_row_count': int, 'gt_agg_count': int,
        'cd_outcomes': [...],
        'cd_row_count': int, 'cd_agg_count': int,
        'num_gt_row_predicates': int,
        'num_cd_row_predicates': int,
      }
    """
    coverage_results: Dict = {}
    has_gt = gt_row_query or gt_agg_query
    has_cd = cd_row_query or cd_agg_query

    if has_gt:
        gt_row_outcomes: List = []
        gt_agg_outcomes: List = []
        if gt_row_query:
            row_result = runner.execute_query(gt_row_query)
            if row_result:
                gt_row_outcomes = list(row_result)
        if gt_agg_query:
            agg_result = runner.execute_query(gt_agg_query)
            if agg_result:
                gt_agg_outcomes = list(agg_result)
        combined_gt = (
            [('row', r) for r in gt_row_outcomes] + [('agg', a) for a in gt_agg_outcomes]
        )
        if combined_gt:
            coverage_results['gt_outcomes'] = combined_gt
            coverage_results['gt_row_count'] = len(gt_row_outcomes)
            coverage_results['gt_agg_count'] = len(gt_agg_outcomes)
            print(f"Worker {worker_label}: Boolean GT coverage - {len(gt_row_outcomes)} row outcome(s), {len(gt_agg_outcomes)} agg outcome(s)")

    if has_cd:
        cd_row_outcomes: List = []
        cd_agg_outcomes: List = []
        if cd_row_query:
            row_result = runner.execute_query(cd_row_query)
            if row_result:
                cd_row_outcomes = list(row_result)
        if cd_agg_query:
            agg_result = runner.execute_query(cd_agg_query)
            if agg_result:
                cd_agg_outcomes = list(agg_result)
        combined_cd = (
            [('row', r) for r in cd_row_outcomes] + [('agg', a) for a in cd_agg_outcomes]
        )
        if combined_cd:
            coverage_results['cd_outcomes'] = combined_cd
            coverage_results['cd_row_count'] = len(cd_row_outcomes)
            coverage_results['cd_agg_count'] = len(cd_agg_outcomes)
            print(f"Worker {worker_label}: Boolean CD coverage - {len(cd_row_outcomes)} row outcome(s), {len(cd_agg_outcomes)} agg outcome(s)")

    coverage_results['num_gt_row_predicates'] = num_gt_row_predicates
    coverage_results['num_cd_row_predicates'] = num_cd_row_predicates
    return coverage_results


# --- Helper function moved to top level ---
def _lowercase_column_names(obj: Any) -> Any:
    """Recursively find and lowercase all column names matching pattern table.column"""
    if isinstance(obj, str):
        # Match pattern: word.word (table.column)
        return re.sub(r'(\w+)\.(\w+)', lambda m: f"{m.group(1).lower()}.{m.group(2).lower()}", obj)
    elif isinstance(obj, list):
        return [_lowercase_column_names(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _lowercase_column_names(v) for k, v in obj.items()}
    else:
        return obj


def _lowercase_sql_preserve_literals(sql: str, dialect: str = '') -> str:
    """Lowercase SQL keywords/identifiers while preserving string literal content.

    Splits on quoted tokens (handling '' and "" SQL escapes), lowercases everything
    outside them. This avoids mangling case-sensitive format specifiers like %Y vs %y
    or string comparisons against mixed-case values.

    For PostgreSQL, double-quoted tokens are identifiers (not literals), so their
    content is lowercased too (preserving the surrounding quotes). For other dialects
    double-quoted tokens are treated as literals and left unchanged.
    """
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`)", sql)
    result = []
    for part in parts:
        if part.startswith("'") or part.startswith('`'):
            result.append(part)
        elif part.startswith('"'):
            # PostgreSQL: double-quotes delimit identifiers — lowercase the content
            if dialect == 'postgresql':
                result.append('"' + part[1:-1].lower() + '"')
            else:
                result.append(part)
        else:
            result.append(part.lower())
    return ''.join(result)


def _strip_order_limit(query: str) -> str:
    """Strip top-level ORDER BY and LIMIT/OFFSET clauses to avoid non-determinism."""
    q = query.strip().rstrip(';').strip()
    # Strip trailing LIMIT n [OFFSET m]
    q = re.sub(r'\s+LIMIT\s+\d+(?:\s+OFFSET\s+\d+)?\s*$', '', q, flags=re.IGNORECASE).strip()
    # Find and strip the last top-level ORDER BY (not inside parentheses)
    depth = 0
    last_pos = -1
    upper = q.upper()
    i = 0
    while i < len(q):
        c = q[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif depth == 0 and upper[i:i+8] == 'ORDER BY':
            last_pos = i
        i += 1
    if last_pos >= 0:
        q = q[:last_pos].strip()
    return q


def _has_percent_Y(results: List) -> bool:
    """Return True if any result value contains a literal %<letter> date format artifact.

    This catches cases where strftime/DATE_FORMAT format specifiers appear verbatim in
    query results because the DBMS did not recognize the specifier (e.g. SQLite returns
    '%J' for an unknown specifier, MySQL may return '%%J' for the same). Both forms are
    artifacts of unevaluated format strings and should not be treated as counterexamples.
    """
    import re
    _PERCENT_FORMAT_RE = re.compile(r'%%?[A-Za-z]')
    for row in results:
        vals = row if isinstance(row, (list, tuple)) else (row,)
        for v in vals:
            if isinstance(v, str) and _PERCENT_FORMAT_RE.search(v):
                return True
    return False


def _to_numeric(v: Any) -> Any:
    """Convert to float if possible for numeric tolerance comparison."""
    from decimal import Decimal
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except (ValueError, TypeError):
            pass
    return v


def _rows_equal_normalized(row1: Any, row2: Any, decimal_places: int = 3) -> bool:
    """Compare two result rows, rounding floats to `decimal_places` decimal places."""
    vals1 = row1 if isinstance(row1, (list, tuple)) else (row1,)
    vals2 = row2 if isinstance(row2, (list, tuple)) else (row2,)
    if len(vals1) != len(vals2):
        return False
    for v1, v2 in zip(vals1, vals2):
        c1, c2 = _to_numeric(v1), _to_numeric(v2)
        if isinstance(c1, float) and isinstance(c2, float):
            if round(c1, decimal_places) != round(c2, decimal_places):
                return False
        else:
            if c1 != c2:
                return False
    return True


def _has_outer_order_by(query: str, dialect: str = "") -> bool:
    """Check if a query has a top-level ORDER BY clause using sqlglot parsing.
    Returns False if parsing fails (fallback to multiset semantics).
    """
    sqlglot_dialect = "postgres" if dialect == "postgresql" else dialect or None
    try:
        parsed = parse(query, dialect=sqlglot_dialect)
        if not parsed:
            return False
        stmt = parsed[0]
        if stmt is None:
            return False
        # Check the outermost SELECT for an ORDER BY
        if isinstance(stmt, exp.Select):
            return stmt.args.get("order") is not None
        if isinstance(stmt, exp.Query):
            return stmt.args.get("order") is not None
        return False
    except Exception:
        return False


def _results_equal_normalized(r1: List, r2: List, decimal_places: int = 3) -> bool:
    """Bag-equal comparison, rounding floats to `decimal_places` decimal places."""
    if len(r1) != len(r2):
        return False
    try:
        s1 = sorted(r1, key=str)
        s2 = sorted(r2, key=str)
    except TypeError:
        s1, s2 = list(r1), list(r2)
    return all(_rows_equal_normalized(a, b, decimal_places) for a, b in zip(s1, s2))


def _results_equal_ordered_normalized(r1: List, r2: List, decimal_places: int = 3) -> bool:
    """Order-sensitive comparison, rounding floats to `decimal_places` decimal places."""
    if len(r1) != len(r2):
        return False
    return all(_rows_equal_normalized(a, b, decimal_places) for a, b in zip(r1, r2))


def _is_type_coercion(r1: List, r2: List) -> bool:
    """True if results have identical string representations but different Python types."""
    if len(r1) != len(r2):
        return False
    try:
        s1 = sorted(r1, key=str)
        s2 = sorted(r2, key=str)
    except TypeError:
        s1, s2 = list(r1), list(r2)
    any_type_diff = False
    for row1, row2 in zip(s1, s2):
        vals1 = row1 if isinstance(row1, (list, tuple)) else (row1,)
        vals2 = row2 if isinstance(row2, (list, tuple)) else (row2,)
        if len(vals1) != len(vals2):
            return False
        for v1, v2 in zip(vals1, vals2):
            if str(v1) != str(v2):
                return False
            if type(v1) != type(v2):
                any_type_diff = True
    return any_type_diff


# --- Worker function (loop body) moved to top level ---
def _run_one_iteration(
    i: int,
    start_time: float,
    timeout: Optional[int],
    dialect: str,
    schema_handler: SchemaHandler,
    heuristics: Any,
    constraints_parsed: List,
    groundtruth_query: str,
    candidate_query: str,
    boolean_gt_row_coverage_query: Optional[str] = None,
    boolean_cd_row_coverage_query: Optional[str] = None,
    boolean_gt_agg_coverage_query: Optional[str] = None,
    boolean_cd_agg_coverage_query: Optional[str] = None,
    num_gt_row_predicates: int = 0,
    num_cd_row_predicates: int = 0,
    cardinality_boost: int = 0,
    persistent_runner: Optional[DatabaseRunner] = None,
    skip_coverage: bool = False,
    remove_null: bool = False,
    remove_one: bool = False,
    remove_literal: bool = False,
    order_sensitive: bool = False,
) -> Tuple[str, Optional[Union[Tuple[Dict, List, List], Tuple[Dict, List, List, Dict], Tuple[Dict, List, List, Dict, Dict]]], float]:
    """
    Runs a single iteration of data generation and database execution.
    Returns a status string, optional result data, and execution time.

    Result tuple may be:
    - (data, result_q1, result_q2) for normal counterexamples
    - (data, result_q1, result_q2, metadata_dict) for counterexamples with query errors
    - (data, result_q1, result_q2, metadata_dict, coverage_dict) for iterations with coverage calculation
    """
    
    # 1. Check worker timeout
    current_time = time.time()
    exec_time = current_time - start_time
    if timeout and exec_time > timeout:
        print(f"Worker {i}: Timeout reached, stopping task.")
        return "timeout", None, exec_time

    print(f"Worker {i} (PID {os.getpid()}): ================== Data Generation =================")
    
    # 2. CPU-Bound Part
    data_generator = DataGenerator(schema_handler, heuristics, constraints=constraints_parsed, iter_num=i, gt_query=groundtruth_query, cd_query=candidate_query, cardinality_boost=cardinality_boost, remove_null=remove_null, remove_one=remove_one, remove_literal=remove_literal)
    data = data_generator.generate_data_main()
    relevant_tables = data_generator.relevant_tables

    # 3. I/O-Bound Part
    runner = persistent_runner
    owns_runner = runner is None
    coverage_results = {}
    try:
        if runner is None:
            runner = DatabaseRunner(schema_handler, data, dialect_name=dialect, constraints=constraints_parsed, worker_id=str(os.getpid()), relevant_tables=relevant_tables)
            runner.setup_tables()
            runner.cleanup_tables()
            runner.insert_tables()
        else:
            runner.refresh_data(data)
        # ####TODO: remove LIMIT for align with ParSEVal code
        # limit_pattern = re.compile(r'LIMIT\s+\d+\b(?:\s+OFFSET\s+\d+)?$', re.IGNORECASE)  #LIMIT\s+(\d+)(?:\s*,\s*(\d+))?\s*$

        # gold_limit_match = limit_pattern.search(groundtruth_query)
        # pred_limit_match = limit_pattern.search(candidate_query)
        # gold_limit = gold_limit_match.group(0) if gold_limit_match else None
        # pred_limit = pred_limit_match.group(0) if pred_limit_match else None
        # if gold_limit == pred_limit:
        #     groundtruth_query = re.sub(limit_pattern, '', groundtruth_query)
        #     candidate_query = re.sub(limit_pattern, '', candidate_query)
        # else:
        #     print(f"Worker {i} (PID {os.getpid()}): Different LIMIT clauses detected, skipping LIMIT removal.")


        result_q1 = runner.execute_query(groundtruth_query)
        result_q2 = runner.execute_query(candidate_query)
        
        # Track query errors - convert None to [] for compatibility
        q1_error = result_q1 is None
        q2_error = result_q2 is None
        
        if q1_error:
            print(f"Worker {i} (PID {os.getpid()}): Groundtruth query failed, treating as empty result")
            result_q1 = []
        if q2_error:
            print(f"Worker {i} (PID {os.getpid()}): Candidate query failed, treating as empty result")
            result_q2 = []
        
        print(f"Worker {i} (PID {os.getpid()}): Execution complete.")
        print("Groundtruth query:", groundtruth_query)
        print("Candidate query:", candidate_query)
        print("Groundtruth result:", result_q1)
        print("Candidate result:", result_q2)

        # 4. Check for counterexample (round floats to 3 decimal places)
        counterexample_found = False
        if order_sensitive:
            if not _results_equal_ordered_normalized(result_q1, result_q2):
                counterexample_found = True
        elif not _results_equal_normalized(result_q1, result_q2):
            counterexample_found = True

        if counterexample_found:
            print(f"Worker {i} (PID {os.getpid()}): Counterexample found!")
            if q1_error or q2_error:
                error_tags = []
                if q1_error:
                    error_tags.append("groundtruth_query_error")
                if q2_error:
                    error_tags.append("candidate_query_error")
                return "found", (data, result_q1, result_q2, {"query_errors": error_tags}, {}), time.time() - start_time
            else:
                return "found", (data, result_q1, result_q2, {}, {}), time.time() - start_time

        # 5. Calculate boolean coverage (only when no counterexample found)
        if skip_coverage:
            coverage_results = {}
        else:
            coverage_results = collect_coverage_outcomes(
                runner=runner,
                gt_row_query=boolean_gt_row_coverage_query,
                gt_agg_query=boolean_gt_agg_coverage_query,
                cd_row_query=boolean_cd_row_coverage_query,
                cd_agg_query=boolean_cd_agg_coverage_query,
                num_gt_row_predicates=num_gt_row_predicates,
                num_cd_row_predicates=num_cd_row_predicates,
                worker_label=str(i),
            )

    except Exception as e:
        print(f"Worker {i} (PID {os.getpid()}): ERROR during execution: {e}")
        return "error", None, time.time() - start_time
    finally:
        if owns_runner and runner:
            runner.cleanup_database()

    # 6. No counterexample found
    if coverage_results:
        return "not_found_with_coverage", (data, [], [], {}, coverage_results), time.time() - start_time
    else:
        return "not_found", None, time.time() - start_time


# --- Per-worker loop for multiprocessing mode ---
def _worker_loop(
    worker_id: int,
    result_queue: multiprocessing.Queue,
    start_time: float,
    timeout: Optional[int],
    dialect: str,
    schema_handler: SchemaHandler,
    heuristics: Any,
    constraints_parsed: List,
    groundtruth_query: str,
    candidate_query: str,
    gt_row_coverage_query: Optional[str],
    cd_row_coverage_query: Optional[str],
    gt_agg_coverage_query: Optional[str],
    cd_agg_coverage_query: Optional[str],
    num_gt_row_predicates: int,
    num_cd_row_predicates: int,
    num_gt_agg_predicates: int,
    num_cd_agg_predicates: int,
    num_gt_predicates: int,
    num_cd_predicates: int,
    termination_target_risk: float,
    coverage_available: bool,
    fallback_max_iters: int = 0,
    remove_null: bool = False,
    remove_one: bool = False,
    remove_literal: bool = False,
):
    """
    Long-running worker process. Owns its own coverage state, cardinality_boost,
    and terminator. Terminates itself when the terminator fires or timeout hits.
    Sends results back through result_queue.
    """
    start_time = time.time()  # reset here so timeout is measured from when this worker actually runs
    cov_accum = CoverageAccumulator(
        num_gt_row_predicates, num_gt_agg_predicates,
        num_cd_row_predicates, num_cd_agg_predicates,
    )
    previous_gt_signal: int = 0
    previous_cd_signal: int = 0
    cardinality_boost: int = 1

    terminator = None
    if coverage_available:
        terminator = create_terminator(
            target_risk=termination_target_risk,
        )

    # Create a persistent runner to reuse across iterations (avoids DDL per iteration)
    persistent_runner = DatabaseRunner(
        schema_handler, {},  # empty data initially
        dialect_name=dialect,
        constraints=constraints_parsed,
        worker_id=str(os.getpid()),
    )
    persistent_runner.setup_tables()
    order_sensitive = _has_outer_order_by(groundtruth_query, dialect) or _has_outer_order_by(candidate_query, dialect)
    try:
        i = 0
        while True:
            current_time = time.time()
            total_exec_time = current_time - start_time
            if timeout and total_exec_time > timeout:
                result_queue.put(("timeout", worker_id, None, i))
                return

            coverage_saturated = cov_accum.is_saturated() or cardinality_boost > 100
            status, result_data, _ = _run_one_iteration(
                i,
                start_time=start_time,
                timeout=timeout,
                dialect=dialect,
                schema_handler=schema_handler,
                heuristics=heuristics,
                constraints_parsed=constraints_parsed,
                groundtruth_query=groundtruth_query,
                candidate_query=candidate_query,
                boolean_gt_row_coverage_query=gt_row_coverage_query,
                boolean_cd_row_coverage_query=cd_row_coverage_query,
                boolean_gt_agg_coverage_query=gt_agg_coverage_query,
                boolean_cd_agg_coverage_query=cd_agg_coverage_query,
                num_gt_row_predicates=num_gt_row_predicates,
                num_cd_row_predicates=num_cd_row_predicates,
                cardinality_boost=cardinality_boost,
                persistent_runner=persistent_runner,
                skip_coverage=coverage_saturated,
                remove_null=remove_null,
                remove_one=remove_one,
                remove_literal=remove_literal,
                order_sensitive=order_sensitive,
            )

            if status == "found":
                result_queue.put(("found", worker_id, result_data, i + 1))
                return

            if status == "timeout":
                result_queue.put(("timeout", worker_id, None, i + 1))
                return

            # Update coverage and terminator
            if status == "not_found_with_coverage" and result_data is not None and len(result_data) == 5:
                _, _, _, _, coverage_results = result_data

                coverage_improved = cov_accum.update(coverage_results)
                current_gt_signal, current_cd_signal = cov_accum.compute_signal()

                if terminator is not None and coverage_available:
                    if coverage_improved:
                        previous_gt_signal = current_gt_signal
                        previous_cd_signal = current_cd_signal
                        cardinality_boost += 1
                    else:
                        cardinality_boost += 2

                    terminator.update(coverage_improved)

                    if terminator.should_terminate():
                        stats = terminator.get_stats()
                        label = "1-way"
                        print(f"  [Worker {worker_id}] Statistical termination ({label}): risk={stats['current_risk']:.4f}")
                        result_queue.put(("done", worker_id, None, i + 1))
                        return

            # When coverage is saturated, skip_coverage=True causes status="not_found".
            # Still feed no-improvement to terminator.
            elif coverage_saturated and status == "not_found" and coverage_available:
                cardinality_boost += 2
                if terminator is not None:
                    terminator.update(False)
                    if terminator.should_terminate():
                        stats = terminator.get_stats()
                        print(f"  [Worker {worker_id}] Statistical termination (saturated): risk={stats['current_risk']:.4f}")
                        result_queue.put(("done", worker_id, None, i + 1))
                        return

            elif not coverage_available and terminator is not None:
                terminator.update(False)
                cardinality_boost += 1
                if terminator.should_terminate():
                    result_queue.put(("done", worker_id, None, i + 1))
                    return

            if fallback_max_iters and i + 1 >= fallback_max_iters:
                print(f"  [Worker {worker_id}] Hard stop: both coverage queries failed after {i + 1} iterations.")
                result_queue.put(("done", worker_id, None, i + 1))
                return

            i += 1
    finally:
        persistent_runner.cleanup_database()


# --- Main function refactored for multiprocessing ---
def counterexample(
    schema: List[Dict],
    constraints: str,
    groundtruth_query: str,
    candidate_query: str,
    timeout: Optional[int] = 10,
    iteration: Optional[int] = None,
    dialect: str = 'mysql',
    use_multiprocessing: bool = False,
    termination_target_risk: float = 0.05,
    remove_null: bool = False,
    remove_one: bool = False,
    remove_literal: bool = False,
) -> Tuple[bool, Optional[Dict[str, List[Any]]], float, int]:

    if groundtruth_query.strip() == candidate_query.strip():
        return False, None, 0.0, 0

    DatabaseRunner.connection_url(dialect)

    # --- 1. SETUP (Runs ONCE in the main process) ---
    
    print("================== Schema Setup =================")
    schema_handler = SchemaHandler(schema)
    print("Extracted Schema:", schema_handler.schema)
    # ... (removed other print statements for brevity) ...

    print("================== Heuristics Setup =================")
    heuristics_handler = HeuristicsHandler(groundtruth_query, candidate_query, schema_handler, dialect=dialect, dialect_gt=None, dialect_cd=None)
    heuristics = heuristics_handler.get_applicable_heuristics()

    print("================== Constraints Setup =================")
    constraints_parsed: list = []
    if isinstance(constraints, list):
        constraints_parsed = constraints
    elif isinstance(constraints, str):
        constraint_str = constraints.strip()
        if constraint_str:
            constraint_parerser = ConstraintParser()
            if constraint_str.endswith('.constraint'):
                constraints_parsed = constraint_parerser.parse_from_file(constraint_str)
            elif constraint_str.endswith('.yml'):
                constraints_parsed = constraint_parerser.parse_from_yml(constraint_str)
            else:
                constraints_parsed = constraint_parerser.parse(constraint_str)
    elif constraints is None:
        constraints_parsed = []
    else:
        raise ValueError(f"Unsupported constraints input: {type(constraints)}")
    
    constraints_parsed = cast(list, _lowercase_column_names(constraints_parsed))
    print("Normalized Constraints:", constraints_parsed)

    groundtruth_query = _lowercase_sql_preserve_literals(groundtruth_query, dialect=dialect)
    candidate_query = _lowercase_sql_preserve_literals(candidate_query, dialect=dialect)

    if iteration is None:
        iteration = int(1e9)  # Effectively infinite iterations
    
    num_gt_predicates: int = 0
    num_cd_predicates: int = 0

    # Early termination tracking
    iterations_without_improvement: int = 0
    previous_gt_total_outcomes: int = 0
    previous_cd_total_outcomes: int = 0
    
    # Coverage-guided cardinality boost
    cardinality_boost: int = 1
    cardinality_boost_previous: int = 1
    
    # Laplace termination estimator
    terminator = create_terminator(target_risk=termination_target_risk)
    print(f"Using laplace termination estimator (target_risk={termination_target_risk})")

    coverage_enabled = True

    # Coverage query variables
    gt_row_coverage_query: Optional[str] = None
    gt_agg_coverage_query: Optional[str] = None
    cd_row_coverage_query: Optional[str] = None
    cd_agg_coverage_query: Optional[str] = None
    num_gt_row_predicates: int = 0
    num_gt_agg_predicates: int = 0
    num_cd_row_predicates: int = 0
    num_cd_agg_predicates: int = 0
    cov_accum: Optional[CoverageAccumulator] = None

    fallback_max_iters: int = 0
    gt_coverage_result = build_boolean_coverage(groundtruth_query, dialect=dialect)
    cd_coverage_result = build_boolean_coverage(candidate_query, dialect=dialect)

    gt_failed = gt_coverage_result is None
    cd_failed = cd_coverage_result is None
    if gt_failed:
        print(f"⚠️  PARSE ERROR: Boolean coverage construction failed for ground truth query")
        print(f"   Query: {groundtruth_query[:200]}...")
    if cd_failed:
        print(f"⚠️  PARSE ERROR: Boolean coverage construction failed for candidate query")
        print(f"   Query: {candidate_query[:200]}...")

    if gt_failed and cd_failed:
        print(f"   Both coverage queries failed – falling back to 20-iteration hard limit.")
        coverage_enabled = False
        fallback_max_iters = 20
    else:
        if gt_failed or cd_failed:
            print(f"   Partial coverage parse failure – continuing with available signal.")
        if gt_coverage_result is not None:
            gt_row_coverage_query = gt_coverage_result.get('row_coverage')
            gt_agg_coverage_query = gt_coverage_result.get('agg_coverage')
            gt_pred_counts = count_predicates(gt_coverage_result)
            num_gt_row_predicates = gt_pred_counts['row_predicates']
            num_gt_agg_predicates = gt_pred_counts['agg_predicates']
            num_gt_predicates = num_gt_row_predicates + num_gt_agg_predicates
        if cd_coverage_result is not None:
            cd_row_coverage_query = cd_coverage_result.get('row_coverage')
            cd_agg_coverage_query = cd_coverage_result.get('agg_coverage')
            cd_pred_counts = count_predicates(cd_coverage_result)
            num_cd_row_predicates = cd_pred_counts['row_predicates']
            num_cd_agg_predicates = cd_pred_counts['agg_predicates']
            num_cd_predicates = num_cd_row_predicates + num_cd_agg_predicates

        cov_accum = CoverageAccumulator(
            num_gt_row_predicates, num_gt_agg_predicates,
            num_cd_row_predicates, num_cd_agg_predicates,
        )

        cov_label = "1-way"
        print(f"Boolean coverage enabled ({cov_label}):")
        print(f"  GT: {num_gt_row_predicates} row predicates + {num_gt_agg_predicates} agg predicates = {num_gt_predicates} total")
        print(f"  CD: {num_cd_row_predicates} row predicates + {num_cd_agg_predicates} agg predicates = {num_cd_predicates} total")
    coverage_available = any([
        gt_row_coverage_query,
        gt_agg_coverage_query,
        cd_row_coverage_query,
        cd_agg_coverage_query
    ])
    if coverage_enabled and not coverage_available:
        print("⚠️  Boolean coverage enabled, but no coverage queries were generated (no predicates).")
        print(f"   Terminator will receive no-improvement signals each iteration.")
    start_time = time.time()

    # --- 2. EXECUTION ---

    task_iterable = range(iteration)

    if use_multiprocessing:
        num_workers = 8
        print(f"\nStarting parallel search with {num_workers} independent workers...")

        result_queue: multiprocessing.Queue = multiprocessing.Queue()

        worker_args = dict(
            result_queue=result_queue,
            start_time=start_time,
            timeout=timeout,
            dialect=dialect,
            schema_handler=schema_handler,
            heuristics=heuristics,
            constraints_parsed=constraints_parsed,
            groundtruth_query=groundtruth_query,
            candidate_query=candidate_query,
            gt_row_coverage_query=gt_row_coverage_query if coverage_enabled else None,
            cd_row_coverage_query=cd_row_coverage_query if coverage_enabled else None,
            gt_agg_coverage_query=gt_agg_coverage_query if coverage_enabled else None,
            cd_agg_coverage_query=cd_agg_coverage_query if coverage_enabled else None,
            num_gt_row_predicates=num_gt_row_predicates,
            num_cd_row_predicates=num_cd_row_predicates,
            num_gt_agg_predicates=num_gt_agg_predicates,
            num_cd_agg_predicates=num_cd_agg_predicates,
            num_gt_predicates=num_gt_predicates,
            num_cd_predicates=num_cd_predicates,
            termination_target_risk=termination_target_risk,
            coverage_available=coverage_available,
            fallback_max_iters=fallback_max_iters,
            remove_null=remove_null,
            remove_one=remove_one,
            remove_literal=remove_literal,
        )

        def cleanup_worker_sandboxes():
            # Terminated processes cannot execute their own finally blocks.
            for worker in workers:
                if worker.pid is not None:
                    DatabaseRunner(
                        schema_handler, {}, dialect_name=dialect,
                        constraints=constraints_parsed, worker_id=str(worker.pid),
                    ).cleanup_database()

        workers = [
            multiprocessing.Process(target=_worker_loop, kwargs={"worker_id": wid, **worker_args})
            for wid in range(num_workers)
        ]
        for w in workers:
            w.start()
        start_time = time.time()  # reset after spawn so timeout budget isn't eaten by process creation

        completed_iters = 0
        workers_done = 0

        while workers_done < num_workers:
            current_time = time.time()
            total_exec_time = current_time - start_time
            if timeout and total_exec_time > timeout:
                print("Main timeout reached, terminating all workers.")
                for w in workers:
                    w.terminate()
                for w in workers:
                    w.join()
                cleanup_worker_sandboxes()
                return False, None, total_exec_time, completed_iters

            # Block until next message (poll with timeout to re-check wall clock)
            try:
                msg = result_queue.get(timeout=1.0)
            except Exception:
                continue

            msg_status, wid, result_data, iters = msg
            completed_iters += iters

            if msg_status == "found" and result_data is not None:
                print(f"\n================== Counterexample Found (worker {wid})! ==================")
                for w in workers:
                    w.terminate()
                for w in workers:
                    w.join()
                cleanup_worker_sandboxes()

                total_exec_time = time.time() - start_time
                if len(result_data) == 5:
                    data, r1, r2, metadata, _ = result_data
                    if "query_errors" in metadata:
                        print(f"⚠ Warning: {', '.join(metadata['query_errors'])}")
                elif len(result_data) == 4:
                    data, r1, r2, metadata = result_data
                    if "query_errors" in metadata:
                        print(f"⚠ Warning: {', '.join(metadata['query_errors'])}")
                elif len(result_data) == 3:
                    data, r1, r2 = result_data
                else:
                    return False, None, total_exec_time, completed_iters

                print(f"Found in {total_exec_time:.2f} seconds.")
                print("Query 1:", groundtruth_query)
                print("Groundtruth result:", r1)
                print("Query 2:", candidate_query)
                print("Candidate result:", r2)
                print("Data:", data)
                return True, data, total_exec_time, completed_iters

            if msg_status == "done":
                print(f"  Worker {wid} coverage-saturated after {iters} iterations — terminating remaining workers.")
                for w in workers:
                    w.terminate()
                for w in workers:
                    w.join()
                cleanup_worker_sandboxes()
                total_exec_time = time.time() - start_time
                print(f"\nEarly exit on coverage saturation. Total iterations across workers: {completed_iters}")
                return False, None, total_exec_time, completed_iters

            if msg_status == "timeout":
                workers_done += 1
                print(f"  Worker {wid} timed out after {iters} iterations.")

        for w in workers:
            w.join()
        cleanup_worker_sandboxes()

        total_exec_time = time.time() - start_time
        print(f"\nAll workers finished. Total iterations across workers: {completed_iters}")
        return False, None, total_exec_time, completed_iters

    else:
        print(f"\nStarting sequential search for up to {iteration} iterations...")
        seq_iters = 0
        # Create a persistent runner to reuse across iterations (avoids DDL per iteration)
        persistent_runner = DatabaseRunner(
            schema_handler, {},  # empty data initially
            dialect_name=dialect,
            constraints=constraints_parsed,
            worker_id=str(os.getpid()),
        )
        persistent_runner.setup_tables()
        order_sensitive = _has_outer_order_by(groundtruth_query, dialect) or _has_outer_order_by(candidate_query, dialect)
        try:
            for i in task_iterable:
                current_time = time.time()
                total_exec_time = current_time - start_time
                if timeout and total_exec_time > timeout:
                    print("Main timeout reached, stopping search.")
                    return False, None, total_exec_time, seq_iters

                seq_iters += 1
                if fallback_max_iters and seq_iters >= fallback_max_iters:
                    print(f"\n📊 Hard stop: both coverage queries failed after {seq_iters} iterations.")
                    total_time = time.time() - start_time
                    return False, None, total_time, seq_iters
                # Call worker directly with current cardinality_boost (not using frozen partial)
                coverage_saturated = (cov_accum.is_saturated() if cov_accum is not None else False) or cardinality_boost > 100
                if coverage_saturated:
                    print(f"Worker {i}: Coverage saturated — skipping coverage queries")
                status, result_data, _ = _run_one_iteration(
                    i,
                    start_time=start_time,
                    timeout=timeout,
                    dialect=dialect,
                    schema_handler=schema_handler,
                    heuristics=heuristics,
                    constraints_parsed=constraints_parsed,
                    groundtruth_query=groundtruth_query,
                    candidate_query=candidate_query,
                    boolean_gt_row_coverage_query=gt_row_coverage_query if coverage_enabled else None,
                    boolean_cd_row_coverage_query=cd_row_coverage_query if coverage_enabled else None,
                    boolean_gt_agg_coverage_query=gt_agg_coverage_query if coverage_enabled else None,
                    boolean_cd_agg_coverage_query=cd_agg_coverage_query if coverage_enabled else None,
                    num_gt_row_predicates=num_gt_row_predicates,
                    num_cd_row_predicates=num_cd_row_predicates,
                    cardinality_boost=cardinality_boost,
                    persistent_runner=persistent_runner,
                    skip_coverage=coverage_saturated,
                    remove_null=remove_null,
                    remove_one=remove_one,
                    remove_literal=remove_literal,
                    order_sensitive=order_sensitive,
                )

                # Track boolean coverage if it was calculated
                if status == "not_found_with_coverage" and result_data is not None and len(result_data) == 5:
                    _, _, _, _, coverage_results = result_data

                    if cov_accum is not None:
                        coverage_improved = cov_accum.update(coverage_results)
                        cov_accum.print_iter_summary(i)
                        current_gt_signal, current_cd_signal = cov_accum.compute_signal()
                    else:
                        coverage_improved = False
                        current_gt_signal = current_cd_signal = 0

                    # Check for early termination
                    if coverage_enabled and coverage_available:
                        if coverage_improved:
                            iterations_without_improvement = 0
                            previous_gt_total_outcomes = current_gt_signal
                            previous_cd_total_outcomes = current_cd_signal
                            cardinality_boost += 1
                        else:
                            iterations_without_improvement += 1
                            cardinality_boost += 2
                            signal_label = "1-way"
                            print(f"  📈 {signal_label} coverage stalled, boosting cardinality (now +{cardinality_boost})")

                        if terminator is not None:
                            terminator.update(coverage_improved)

                            stats = terminator.get_stats()
                            print(f"  Terminator: risk={stats['current_risk']:.4f} (target={stats['target_risk']})")

                        if terminator is not None and terminator.should_terminate():
                            stats = terminator.get_stats()
                            print(f"\n📊 Statistical termination ({stats['estimator']})")
                            print(f"   Risk of missing coverage: {stats['current_risk']:.4f} < {stats['target_risk']}")
                            print(f"   Total iterations: {stats['total_iterations']}")
                            if 'consecutive_boring' in stats:
                                print(f"   Consecutive boring: {stats['consecutive_boring']}/{stats['patience_limit']}")
                            total_time = time.time() - start_time
                            return False, None, total_time, seq_iters

                    else:
                        cardinality_boost += 1

                # When coverage is saturated, skip_coverage=True causes status="not_found"
                # instead of "not_found_with_coverage". Still feed no-improvement to terminator.
                if coverage_saturated and status == "not_found" and coverage_enabled and coverage_available:
                    iterations_without_improvement += 1
                    cardinality_boost += 2
                    if terminator is not None:
                        terminator.update(False)
                        stats = terminator.get_stats()
                        print(f"  Terminator (saturated): risk={stats['current_risk']:.4f} (target={stats['target_risk']})")
                        if terminator.should_terminate():
                            print(f"\n📊 Statistical termination ({stats['estimator']}) — coverage saturated")
                            print(f"   Risk: {stats['current_risk']:.4f} < {stats['target_risk']}")
                            print(f"   Total iterations: {stats['total_iterations']}")
                            total_time = time.time() - start_time
                            return False, None, total_time, seq_iters

                if coverage_enabled and not coverage_available and terminator is not None:
                    terminator.update(False)
                    if terminator.should_terminate():
                        stats = terminator.get_stats()
                        print(f"\n📊 Termination - no coverage available (iterations={seq_iters})")
                        print(f"   Risk: {stats['current_risk']:.4f} < {stats['target_risk']}")
                        total_time = time.time() - start_time
                        return False, None, total_time, seq_iters

                if status == "found" and result_data is not None:
                    print("\n================== Counterexample Found! ==================")
                    if len(result_data) == 5:
                        data, r1, r2, metadata, cov_results = result_data
                        if cov_results and cov_accum is not None:
                            cov_accum.update(cov_results)
                            print("\n--- Boolean Coverage at Counterexample ---")
                            cov_accum.print_final_summary()
                        if "query_errors" in metadata:
                            print(f"⚠ Warning: Counterexample involves query errors: {', '.join(metadata['query_errors'])}")
                    elif len(result_data) == 4:
                        data, r1, r2, metadata = result_data
                        if "query_errors" in metadata:
                            print(f"⚠ Warning: Counterexample involves query errors: {', '.join(metadata['query_errors'])}")
                    elif len(result_data) == 3:
                        data, r1, r2 = result_data
                    else:
                        # Shouldn't happen with found status, but be defensive
                        continue
                    total_exec_time = time.time() - start_time
                    print(f"Found in {total_exec_time:.2f} seconds.")
                    print("Query 1:", groundtruth_query)
                    print("Groundtruth result:", r1)
                    print("Query 2:", candidate_query)
                    print("Candidate result:", r2)
                    print("Data:", data)
                    print("Dialect:", dialect)
                    return True, data, total_exec_time, seq_iters

                if status == "timeout":
                    print("Iteration timed out; stopping search.")
                    return False, None, total_exec_time, seq_iters
        finally:
            persistent_runner.cleanup_database()

    total_time = time.time() - start_time
    print(f"Search finished after {iteration} iterations. No counterexample found.")

    if coverage_enabled and cov_accum is not None:
        print("\n================== Final Boolean Coverage Summary =================")
        cov_accum.print_final_summary()
        print("===================================================================")

    return False, None, total_time, seq_iters


def counterexample_multidialect(
    schema: List[Dict],
    constraints: str,
    groundtruth_query: str,
    candidate_query: str,
    timeout: Optional[int] = 10,
    iteration: Optional[int] = None,
    dialect_gt: str = 'mysql',
    dialect_cd: str = 'postgresql',
    termination_target_risk: float = 0.05,
    remove_null: bool = False,
    remove_one: bool = False,
    remove_literal: bool = False,
) -> Tuple[bool, Optional[Dict[str, List[Any]]], float, int]:

    if groundtruth_query.strip() == candidate_query.strip():
        return False, None, 0.0, 0

    DatabaseRunner.connection_url(dialect_gt)
    DatabaseRunner.connection_url(dialect_cd)

    # --- 1. SETUP ---
    start_time = time.time()

    print("================== Schema Setup =================")
    schema_handler = SchemaHandler(schema)
    print("Extracted Schema:", schema_handler.schema)

    print("================== Heuristics Setup =================")
    heuristics_handler = HeuristicsHandler(groundtruth_query, candidate_query, schema_handler, dialect_cd=dialect_cd, dialect_gt=dialect_gt, dialect=None)
    heuristics = heuristics_handler.get_applicable_heuristics()

    print("================== Constraints Setup =================")
    constraints_parsed: list = []
    if isinstance(constraints, list):
        constraints_parsed = constraints
    elif isinstance(constraints, str):
        constraint_str = constraints.strip()
        if constraint_str:
            constraint_parerser = ConstraintParser()
            if constraint_str.endswith('.constraint'):
                constraints_parsed = constraint_parerser.parse_from_file(constraint_str)
            elif constraint_str.endswith('.yml'):
                constraints_parsed = constraint_parerser.parse_from_yml(constraint_str)
            else:
                constraints_parsed = constraint_parerser.parse(constraint_str)
    elif constraints is None:
        constraints_parsed = []
    else:
        raise ValueError(f"Unsupported constraints input: {type(constraints)}")

    constraints_parsed = cast(list, _lowercase_column_names(constraints_parsed))
    print("Normalized Constraints:", constraints_parsed)

    # groundtruth_query = _strip_order_limit(groundtruth_query)
    # candidate_query = _strip_order_limit(candidate_query)
    # print(f"Stripped GT query: {groundtruth_query[:120]}")
    # print(f"Stripped CD query: {candidate_query[:120]}")

    if iteration is None:
        iteration = int(1e9)

    num_gt_predicates: int = 0
    num_cd_predicates: int = 0

    # Early termination tracking
    iterations_without_improvement: int = 0
    previous_gt_total_outcomes: int = 0
    previous_cd_total_outcomes: int = 0

    # Coverage-guided cardinality boost
    cardinality_boost: int = 1

    # Statistical termination estimator
    terminator = create_terminator(target_risk=termination_target_risk)
    print(f"Using laplace termination estimator (target_risk={termination_target_risk})")

    coverage_enabled = True

    # Coverage query variables
    gt_row_coverage_query: Optional[str] = None
    gt_agg_coverage_query: Optional[str] = None
    cd_row_coverage_query: Optional[str] = None
    cd_agg_coverage_query: Optional[str] = None
    num_gt_row_predicates: int = 0
    num_gt_agg_predicates: int = 0
    num_cd_row_predicates: int = 0
    num_cd_agg_predicates: int = 0
    cov_accum: Optional[CoverageAccumulator] = None

    gt_coverage_result = build_boolean_coverage(groundtruth_query, dialect=dialect_gt)
    cd_coverage_result = build_boolean_coverage(candidate_query, dialect=dialect_cd)

    coverage_disabled = False
    if gt_coverage_result is None:
        print(f"⚠️  PARSE ERROR: Boolean coverage construction failed for ground truth query")
        print(f"   Query: {groundtruth_query[:200]}...")
        coverage_disabled = True
    if cd_coverage_result is None:
        print(f"⚠️  PARSE ERROR: Boolean coverage construction failed for candidate query")
        print(f"   Query: {candidate_query[:200]}...")
        coverage_disabled = True

    if coverage_disabled:
        print(f"   Skipping boolean coverage for this query pair (parse error only)")
        coverage_enabled = False
    else:
        assert gt_coverage_result is not None
        assert cd_coverage_result is not None
        gt_row_coverage_query = gt_coverage_result.get('row_coverage')
        gt_agg_coverage_query = gt_coverage_result.get('agg_coverage')
        cd_row_coverage_query = cd_coverage_result.get('row_coverage')
        cd_agg_coverage_query = cd_coverage_result.get('agg_coverage')

        gt_pred_counts = count_predicates(gt_coverage_result)
        num_gt_row_predicates = gt_pred_counts['row_predicates']
        num_gt_agg_predicates = gt_pred_counts['agg_predicates']
        num_gt_predicates = num_gt_row_predicates + num_gt_agg_predicates

        cd_pred_counts = count_predicates(cd_coverage_result)
        num_cd_row_predicates = cd_pred_counts['row_predicates']
        num_cd_agg_predicates = cd_pred_counts['agg_predicates']
        num_cd_predicates = num_cd_row_predicates + num_cd_agg_predicates

        cov_accum = CoverageAccumulator(
            num_gt_row_predicates, num_gt_agg_predicates,
            num_cd_row_predicates, num_cd_agg_predicates,
        )

        cov_label = "1-way"
        print(f"Boolean coverage enabled ({cov_label}):")
        print(f"  GT: {num_gt_row_predicates} row predicates + {num_gt_agg_predicates} agg predicates = {num_gt_predicates} total")
        print(f"  CD: {num_cd_row_predicates} row predicates + {num_cd_agg_predicates} agg predicates = {num_cd_predicates} total")

    coverage_available = any([
        gt_row_coverage_query,
        gt_agg_coverage_query,
        cd_row_coverage_query,
        cd_agg_coverage_query,
    ])
    if coverage_enabled and not coverage_available:
        print("⚠️  Boolean coverage enabled, but no coverage queries were generated (no predicates).")
        print(f"   Terminator will receive no-improvement signals each iteration.")

    # --- 2. EXECUTION ---

    groundtruth_query = _lowercase_sql_preserve_literals(groundtruth_query, dialect=dialect_gt)
    candidate_query = _lowercase_sql_preserve_literals(candidate_query, dialect=dialect_cd)

    # Create persistent runners for both dialects (avoids DDL per iteration)
    persistent_runner_gt = DatabaseRunner(
        schema_handler, {},
        dialect_name=dialect_gt,
        constraints=constraints_parsed,
        worker_id=str(os.getpid()),
    )
    persistent_runner_cd = DatabaseRunner(
        schema_handler, {},
        dialect_name=dialect_cd,
        constraints=constraints_parsed,
        worker_id=str(os.getpid()),
    )
    persistent_runner_gt.setup_tables()
    persistent_runner_cd.setup_tables()

    def _run_multidialect_iteration(
        i: int,
        data: Dict,
        relevant_tables: Any,
        skip_coverage: bool = False,
    ) -> Tuple[str, Optional[Any], float]:
        try:
            persistent_runner_gt.refresh_data(data)
            result_q1 = persistent_runner_gt.execute_query(groundtruth_query)

            persistent_runner_cd.refresh_data(data)
            result_q2 = persistent_runner_cd.execute_query(candidate_query)

            q1_error = result_q1 is None
            q2_error = result_q2 is None
            if q1_error:
                print(f"Iteration {i} (PID {os.getpid()}): Groundtruth query failed, treating as empty result")
                result_q1 = []
            if q2_error:
                print(f"Iteration {i} (PID {os.getpid()}): Candidate query failed, treating as empty result")
                result_q2 = []

            print(f"Iteration {i} (PID {os.getpid()}): Execution complete.")
            print("Groundtruth result:", result_q1)
            print("Candidate result:", result_q2)

            # Skip %Y date format artifacts (e.g. unevaluated DATE_FORMAT strings)
            if _has_percent_Y(result_q1) or _has_percent_Y(result_q2):
                print(f"Iteration {i} (PID {os.getpid()}): Skipping - %Y date format artifact in results")
            else:
                counterexample_found = not _results_equal_normalized(result_q1, result_q2)

                if counterexample_found:
                    print(f"Iteration {i} (PID {os.getpid()}): Counterexample found!")
                    error_tags = []
                    if q1_error:
                        error_tags.append("groundtruth_query_error")
                    if q2_error:
                        error_tags.append("candidate_query_error")
                    has_order_limit = bool(re.search(
                        r'\b(ORDER\s+BY|LIMIT)\b', groundtruth_query + ' ' + candidate_query, re.IGNORECASE
                    ))
                    if has_order_limit:
                        category = "non_deterministic"
                    elif _is_type_coercion(result_q1, result_q2):
                        category = "type_coercion"
                    else:
                        category = "genuine"
                    print(f"  Category: {category}")
                    metadata: Dict = {"category": category}
                    if error_tags:
                        metadata["query_errors"] = error_tags
                    return "found", (data, result_q1, result_q2, metadata, {}), time.time() - start_time

            # Collect coverage only when no counterexample found
            if skip_coverage:
                coverage_results = {}
            else:
                coverage_results_gt = collect_coverage_outcomes(
                    runner=persistent_runner_gt,
                    gt_row_query=gt_row_coverage_query if coverage_enabled else None,
                    gt_agg_query=gt_agg_coverage_query if coverage_enabled else None,
                    cd_row_query=None,
                    cd_agg_query=None,
                    num_gt_row_predicates=num_gt_row_predicates,
                    num_cd_row_predicates=0,
                    worker_label=str(i),
                )
                coverage_results_cd = collect_coverage_outcomes(
                    runner=persistent_runner_cd,
                    gt_row_query=None,
                    gt_agg_query=None,
                    cd_row_query=cd_row_coverage_query if coverage_enabled else None,
                    cd_agg_query=cd_agg_coverage_query if coverage_enabled else None,
                    num_gt_row_predicates=0,
                    num_cd_row_predicates=num_cd_row_predicates,
                    worker_label=str(i),
                )
                coverage_results = {**coverage_results_gt, **coverage_results_cd}

        except Exception as e:
            print(f"Iteration {i} (PID {os.getpid()}): ERROR during execution: {e}")
            return "error", None, time.time() - start_time

        if coverage_results:
            return "not_found_with_coverage", (data, [], [], {}, coverage_results), time.time() - start_time
        return "not_found", None, time.time() - start_time

    print(f"\nStarting sequential search for up to {iteration} iterations...")
    seq_iters = 0
    try:
        for i in range(iteration):
            current_time = time.time()
            total_exec_time = current_time - start_time
            if timeout and total_exec_time > timeout:
                print("Main timeout reached, stopping search.")
                return False, None, total_exec_time, seq_iters

            seq_iters += 1
            data_generator = DataGenerator(
                schema_handler, heuristics, constraints=constraints_parsed,
                iter_num=i, gt_query=groundtruth_query, cd_query=candidate_query,
                cardinality_boost=cardinality_boost,
                remove_null=remove_null, remove_one=remove_one, remove_literal=remove_literal,
            )
            data = data_generator.generate_data_main()
            relevant_tables = data_generator.relevant_tables

            coverage_saturated = (cov_accum.is_saturated() if cov_accum is not None else False) or cardinality_boost > 100
            if coverage_saturated:
                print(f"Iteration {i}: Coverage saturated — skipping coverage queries")
            status, result_data, _ = _run_multidialect_iteration(i, data, relevant_tables, skip_coverage=coverage_saturated)

            # Track boolean coverage
            if status == "not_found_with_coverage" and result_data is not None and len(result_data) == 5:
                _, _, _, _, coverage_results = result_data

                if cov_accum is not None:
                    coverage_improved = cov_accum.update(coverage_results)
                    cov_accum.print_iter_summary(i)
                    current_gt_signal, current_cd_signal = cov_accum.compute_signal()
                else:
                    coverage_improved = False
                    current_gt_signal = current_cd_signal = 0

                if coverage_enabled and coverage_available:
                    if coverage_improved:
                        iterations_without_improvement = 0
                        previous_gt_total_outcomes = current_gt_signal
                        previous_cd_total_outcomes = current_cd_signal
                        cardinality_boost += 1
                    else:
                        iterations_without_improvement += 1
                        cardinality_boost += 2
                        signal_label = "1-way"
                        print(f"  📈 {signal_label} coverage stalled, boosting cardinality (now +{cardinality_boost})")

                    if terminator is not None:
                        terminator.update(coverage_improved)

                        stats = terminator.get_stats()
                        print(f"  Terminator: risk={stats['current_risk']:.4f} (target={stats['target_risk']})")

                    if terminator is not None and terminator.should_terminate():
                        stats = terminator.get_stats()
                        print(f"\n📊 Statistical termination ({stats['estimator']})")
                        print(f"   Risk of missing coverage: {stats['current_risk']:.4f} < {stats['target_risk']}")
                        print(f"   Total iterations: {stats['total_iterations']}")
                        if 'consecutive_boring' in stats:
                            print(f"   Consecutive boring: {stats['consecutive_boring']}/{stats['patience_limit']}")
                        total_time = time.time() - start_time
                        return False, None, total_time, seq_iters

                else:
                    cardinality_boost += 1

            # When coverage is saturated, skip_coverage=True causes status="not_found".
            # Still feed no-improvement to terminator.
            if coverage_saturated and status == "not_found" and coverage_enabled and coverage_available:
                iterations_without_improvement += 1
                cardinality_boost += 2
                if terminator is not None:
                    terminator.update(False)
                    stats = terminator.get_stats()
                    print(f"  Terminator (saturated): risk={stats['current_risk']:.4f} (target={stats['target_risk']})")
                    if terminator.should_terminate():
                        print(f"\n📊 Statistical termination ({stats['estimator']}) — coverage saturated")
                        print(f"   Risk: {stats['current_risk']:.4f} < {stats['target_risk']}")
                        print(f"   Total iterations: {stats['total_iterations']}")
                        total_time = time.time() - start_time
                        return False, None, total_time, seq_iters

            if coverage_enabled and not coverage_available and terminator is not None:
                terminator.update(False)
                if terminator.should_terminate():
                    stats = terminator.get_stats()
                    print(f"\n📊 Termination - no coverage available (iterations={seq_iters})")
                    print(f"   Risk: {stats['current_risk']:.4f} < {stats['target_risk']}")
                    total_time = time.time() - start_time
                    return False, None, total_time, seq_iters

            if status == "found" and result_data is not None:
                print("\n================== Counterexample Found! ==================")
                if len(result_data) == 5:
                    data, r1, r2, metadata, cov_results = result_data
                    if cov_results and cov_accum is not None:
                        cov_accum.update(cov_results)
                        print("\n--- Boolean Coverage at Counterexample ---")
                        cov_accum.print_final_summary()
                    if metadata.get("query_errors"):
                        print(f"⚠ Warning: Counterexample involves query errors: {', '.join(metadata['query_errors'])}")
                        if isinstance(data, dict):
                            data["_query_errors"] = metadata["query_errors"]
                    category = metadata.get("category", "genuine")
                    print(f"  Category: {category}")
                    if isinstance(data, dict):
                        data["_category"] = category
                elif len(result_data) == 4:
                    data, r1, r2, metadata = result_data
                    if metadata.get("query_errors"):
                        print(f"⚠ Warning: Counterexample involves query errors: {', '.join(metadata['query_errors'])}")
                        if isinstance(data, dict):
                            data["_query_errors"] = metadata["query_errors"]
                    category = metadata.get("category", "genuine")
                    print(f"  Category: {category}")
                    if isinstance(data, dict):
                        data["_category"] = category
                else:
                    data, r1, r2 = result_data
                total_exec_time = time.time() - start_time
                print(f"Found in {total_exec_time:.2f} seconds.")
                print("Query 1:", groundtruth_query)
                print("Groundtruth result:", r1)
                print("Query 2:", candidate_query)
                print("Candidate result:", r2)
                print("Data:", data)
                print("Dialect GT:", dialect_gt, "  Dialect CD:", dialect_cd)
                return True, data, total_exec_time, seq_iters

            if status == "timeout":
                print("Iteration timed out; stopping search.")
                return False, None, total_exec_time, seq_iters
    finally:
        persistent_runner_gt.cleanup_database()
        persistent_runner_cd.cleanup_database()

    total_time = time.time() - start_time
    print(f"Search finished after {iteration} iterations. No counterexample found.")

    if coverage_enabled and cov_accum is not None:
        print("\n================== Final Boolean Coverage Summary =================")
        cov_accum.print_final_summary()
        print("===================================================================")

    return False, None, total_time, seq_iters
