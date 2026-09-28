"""Boolean coverage queries and cumulative 1-way predicate outcome tracking."""

from typing import Dict, List, Optional, Tuple
from sqlglot import parse_one, exp


def _extract_case_conditions(ast: exp.Expression) -> List[exp.Expression]:
    """
    Extract all WHEN conditions from CASE expressions in the query.
    E.g., CASE WHEN x > 5 THEN 'a' WHEN y < 10 THEN 'b' END
          -> [x > 5, y < 10]
    """
    conditions = []
    
    # Find all Case expressions in the AST
    for case_expr in ast.find_all(exp.Case):
        # Case expressions have 'ifs' which are the WHEN branches
        ifs = case_expr.args.get('ifs', [])
        for if_branch in ifs:
            # Each If has 'this' (the condition) and 'true' (the result)
            condition = if_branch.args.get('this')
            if condition:
                conditions.append(condition.copy())
    
    return conditions


def _flatten_predicates(condition: exp.Expression) -> List[exp.Expression]:
    """
    Flatten AND-connected predicates into a list of individual expressions.
    E.g., (a > 5 AND b < 10) -> [a > 5, b < 10]
    """
    if condition is None:
        return []
    
    # Unwrap parentheses
    while isinstance(condition, exp.Paren):
        condition = condition.this
    
    if isinstance(condition, exp.And):
        left = _flatten_predicates(condition.left)
        right = _flatten_predicates(condition.right)
        return left + right
    else:
        return [condition]


def _predicate_to_case(predicate: exp.Expression, alias_name: str) -> exp.Alias:
    """
    Wrap a predicate into a 3-valued logic CASE expression:
    
    CASE 
        WHEN P THEN 1          -- TRUE
        WHEN NOT (P) THEN 0    -- FALSE
        ELSE -1                -- NULL/UNKNOWN
    END AS alias_name
    
    This distinguishes TRUE, FALSE, and NULL which is critical for SQL fuzzing
    since NULL propagation is a common source of bugs.
    """
    pred_copy = predicate.copy()
    
    # WHEN P THEN 1 (TRUE case)
    true_branch = exp.If(this=pred_copy, true=exp.Literal.number(1))
    
    # WHEN NOT (P) THEN 0 (FALSE case)
    # Wrap in Paren for safety, then negate
    negated_pred = exp.Not(this=exp.Paren(this=predicate.copy()))
    false_branch = exp.If(this=negated_pred, true=exp.Literal.number(0))
    
    # ELSE -1 (NULL/UNKNOWN case)
    case_expr = exp.Case(
        ifs=[true_branch, false_branch],
        default=exp.Neg(this=exp.Literal.number(1))
    )
    return exp.alias_(case_expr, alias_name)


def _extract_tables_with_aliases(ast: exp.Expression) -> List[exp.Expression]:
    """
    Extract all table sources from FROM and JOIN clauses, preserving aliases.
    For subqueries, keeps them as opaque units (derived tables).
    Returns a list of Table or Subquery expressions.
    
    NOTE: Only extracts from the outermost SELECT, not from CTEs or subqueries.
    """
    tables = []
    
    # Find the outermost SELECT (skip WITH clause if present)
    select_node = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    
    if select_node:
        # Get the main FROM source from this SELECT
        from_node = select_node.args.get('from')
        if from_node and from_node.this:
            source = from_node.this
            tables.append(source.copy())
        
        # Get sources from JOINs
        if 'joins' in select_node.args:
            for join in select_node.args['joins']:
                source = join.this
                if source:
                    tables.append(source.copy())
    
    return tables


def _get_main_select(ast: exp.Expression) -> Optional[exp.Select]:
    """
    Return the main (outermost) SELECT node, skipping CTE definitions.
    For a With node, ast.this is the main query body, not a CTE body.
    Falls back to find() only for unusual AST shapes.
    """
    if isinstance(ast, exp.Select):
        return ast
    if isinstance(ast, exp.With):
        main = ast.this
        return main if isinstance(main, exp.Select) else main.find(exp.Select) if main else None
    return ast.find(exp.Select)


def _extract_cte_prefix(ast: exp.Expression) -> Optional[exp.With]:
    """
    Extract the WITH clause (CTEs) from the AST if present.
    Returns a copy of the With node, or None if no CTEs.
    """
    with_node = ast.find(exp.With)
    if with_node:
        return with_node.copy()
    return None


def build_boolean_coverage(sql_query: str, dialect: str = 'mysql') -> Optional[Dict[str, Optional[str]]]:
    """
    Build boolean coverage queries for Grey-Box Fuzzing.
    
    This function generates two coverage queries that use 3-valued logic:
    - 1 = TRUE
    - 0 = FALSE  
    - -1 = NULL/UNKNOWN
    
    Args:
        sql_query: The SQL query to analyze
        dialect: The SQL dialect to use for parsing (default: 'mysql')
    
    Returns:
        A dictionary with two keys:
        - 'row_coverage': Query testing WHERE and JOIN predicates on all row combinations
                         Uses CROSS JOIN to test all possible row pairs
        - 'agg_coverage': Query testing HAVING predicates on groups (None if no GROUP BY)
                         Removes WHERE clause to test all groups
        
        Returns None if parsing fails.
    """
    if dialect == "postgresql":
        dialect = "postgres"
    try:
        ast = parse_one(sql_query, read=dialect)
    except Exception as e:
        print(f"❌ Failed to parse SQL query: {e}")
        return None
    
    result: Dict[str, Optional[str]] = {
        'row_coverage': None,
        'agg_coverage': None
    }
    
    # ========== 1. Row-Level Coverage Query ==========
    # Goal: Test every WHERE and JOIN predicate on every combination of rows
    row_select_columns = []
    sig_idx = 0
    
    # Get the outermost SELECT
    main_select = _get_main_select(ast)
    
    # Determine the source for predicates:
    # If main SELECT has a WHERE clause, use it. Otherwise, look at subqueries.
    predicate_source_select = main_select
    main_where = main_select.args.get('where') if main_select else None
    
    # If main SELECT has no WHERE, check if there are subqueries with WHERE
    if not main_where:
        # Look for subqueries that have WHERE clauses
        for subquery in ast.find_all(exp.Subquery):
            sub_select = subquery.find(exp.Select)
            if sub_select and sub_select.args.get('where'):
                predicate_source_select = sub_select
                main_where = sub_select.args.get('where')
                break
    
    # Extract WHERE predicates from the identified source
    if main_where and main_where.this:
        where_predicates = _flatten_predicates(main_where.this)
        for pred in where_predicates:
            case_col = _predicate_to_case(pred, f"Sig_Where_{sig_idx}")
            row_select_columns.append(case_col)
            sig_idx += 1
    
    # Extract JOIN ON predicates — only from the predicate source SELECT's own joins,
    # not from nested subqueries (their tables aren't in our CROSS JOIN).
    join_sig_idx = 0
    join_source = predicate_source_select or main_select
    for join in (join_source.args.get('joins', []) if join_source else []):
        on_clause = join.args.get('on')
        if on_clause:
            join_predicates = _flatten_predicates(on_clause)
            for pred in join_predicates:
                case_col = _predicate_to_case(pred, f"Sig_Join_{join_sig_idx}")
                row_select_columns.append(case_col)
                join_sig_idx += 1
    
    # Extract CASE expression conditions from SELECT clause
    case_sig_idx = 0
    select_node = _get_main_select(ast)
    if select_node:
        case_conditions = _extract_case_conditions(select_node)
        for cond in case_conditions:
            case_col = _predicate_to_case(cond, f"Sig_Case_{case_sig_idx}")
            row_select_columns.append(case_col)
            case_sig_idx += 1
    
    # Build row coverage query if we have any predicates
    if row_select_columns:
        # Use the predicate source SELECT for table extraction if different from main
        tables = []
        if predicate_source_select is not None:
            tables = _extract_tables_with_aliases(predicate_source_select)
        
        # If no tables from predicate source, fall back to main AST
        if not tables:
            tables = _extract_tables_with_aliases(ast)
        
        if tables:
            # Build CROSS JOIN chain for testing all row combinations
            row_query = exp.Select()
            
            # Add all CASE columns
            for col in row_select_columns:
                row_query = row_query.select(col)
            
            # Set FROM to first table
            row_query = row_query.from_(tables[0])
            
            # Add remaining tables as CROSS JOINs
            for tbl in tables[1:]:
                cross_join = exp.Join(this=tbl, kind="CROSS")
                row_query = row_query.join(cross_join)
            
            # Add DISTINCT modifier to reduce redundant signal combinations
            row_query = row_query.distinct()
            
            # Check if original query has CTEs - if so, wrap the coverage query
            cte_prefix = _extract_cte_prefix(ast)
            if cte_prefix:
                # Create a new query with CTEs prefix
                final_query = exp.Select()
                final_query.set("with", cte_prefix)
                # Copy expressions from row_query
                for expr in row_query.expressions:
                    final_query = final_query.select(expr)
                # Copy FROM
                final_query.set("from", row_query.args.get("from"))
                # Copy JOINs
                if "joins" in row_query.args:
                    final_query.set("joins", row_query.args["joins"])
                # Copy DISTINCT
                final_query = final_query.distinct()
                result['row_coverage'] = final_query.sql(dialect=dialect)
            else:
                result['row_coverage'] = row_query.sql(dialect=dialect)
    
    # ========== 2. Aggregate-Level Coverage Query ==========
    # Goal: Test HAVING predicates on groups, ignoring row-level filters
    # Only look at the top-level SELECT's GROUP BY and HAVING, not subqueries
    select_node = _get_main_select(ast)
    group_by = select_node.args.get('group') if select_node else None
    
    if group_by:
        # Clone the original AST for modification
        agg_ast = ast.copy()
        
        # Get the top-level SELECT node from the clone
        agg_select_node = _get_main_select(agg_ast)
        
        # Extract GROUP BY keys for SELECT
        agg_select_columns = []
        group_node = agg_select_node.args.get('group') if agg_select_node else None
        if group_node:
            for group_expr in group_node.expressions:
                agg_select_columns.append(group_expr.copy())
        
        # Extract HAVING predicates and convert to CASE signals
        having_node = agg_select_node.args.get('having') if agg_select_node else None
        having_sig_idx = 0
        if having_node and having_node.this:
            having_predicates = _flatten_predicates(having_node.this)
            for pred in having_predicates:
                case_col = _predicate_to_case(pred, f"Sig_Having_{having_sig_idx}")
                agg_select_columns.append(case_col)
                having_sig_idx += 1
        
        if agg_select_columns and having_sig_idx > 0:
            if agg_select_node:
                # Set the new expressions directly
                agg_select_node.set("expressions", agg_select_columns)
                
                # Remove WHERE clause from top-level SELECT (Relaxed Mode)
                where_node = agg_select_node.args.get('where')
                if where_node:
                    agg_select_node.set("where", None)
                
                # Remove HAVING clause from top-level SELECT after extracting signals
                if having_node:
                    agg_select_node.set("having", None)
                
                # Remove ORDER BY clause - not relevant for agg coverage
                if agg_select_node.args.get('order'):
                    agg_select_node.set("order", None)
                
                result['agg_coverage'] = agg_ast.sql(dialect=dialect)
    
    return result


def count_predicates(coverage_result: Optional[Dict[str, Optional[str]]]) -> Dict[str, int]:
    """
    Count the number of predicates in coverage queries.
    
    Args:
        coverage_result: The result from build_boolean_coverage()
    
    Returns:
        Dictionary with 'row_predicates' and 'agg_predicates' counts
    """
    import re
    
    counts = {
        'row_predicates': 0,
        'agg_predicates': 0
    }
    
    if coverage_result is None:
        return counts
    
    row_cov = coverage_result.get('row_coverage')
    if row_cov:
        # Count Sig_Where_X, Sig_Join_X, and Sig_Case_X patterns
        where_count = len(re.findall(r'Sig_Where_\d+', row_cov))
        join_count = len(re.findall(r'Sig_Join_\d+', row_cov))
        case_count = len(re.findall(r'Sig_Case_\d+', row_cov))
        counts['row_predicates'] = where_count + join_count + case_count
    
    agg_cov = coverage_result.get('agg_coverage')
    if agg_cov:
        # Count Sig_Having_X patterns
        having_count = len(re.findall(r'Sig_Having_\d+', agg_cov))
        counts['agg_predicates'] = having_count
    
    return counts


# ---------------------------------------------------------------------------
# T-way interaction coverage utilities
# ---------------------------------------------------------------------------

class CoverageAccumulator:
    """Track each predicate's outcomes independently across iterations.

    Row outcomes contain predicate signals in order. Aggregate outcomes have
    GROUP BY keys followed by HAVING predicate signals.
    """

    def __init__(self, num_gt_row_predicates: int, num_gt_agg_predicates: int,
                 num_cd_row_predicates: int, num_cd_agg_predicates: int) -> None:
        self.num_gt_row = num_gt_row_predicates
        self.num_gt_agg = num_gt_agg_predicates
        self.num_cd_row = num_cd_row_predicates
        self.num_cd_agg = num_cd_agg_predicates
        self.num_gt = num_gt_row_predicates + num_gt_agg_predicates
        self.num_cd = num_cd_row_predicates + num_cd_agg_predicates
        self.cumulative_gt = [set() for _ in range(self.num_gt)]
        self.cumulative_cd = [set() for _ in range(self.num_cd)]

    def compute_signal(self) -> Tuple[int, int]:
        """Return total distinct per-predicate outcomes for each query."""
        return (sum(map(len, self.cumulative_gt)), sum(map(len, self.cumulative_cd)))

    def is_saturated(self) -> bool:
        predicates = self.cumulative_gt + self.cumulative_cd
        return bool(predicates) and all(len(outcomes) == 3 for outcomes in predicates)

    def update(self, coverage_results: Dict) -> bool:
        previous = self.compute_signal()
        for key, row_count, agg_count, cumulative in (
            ('gt_outcomes', self.num_gt_row, self.num_gt_agg, self.cumulative_gt),
            ('cd_outcomes', self.num_cd_row, self.num_cd_agg, self.cumulative_cd),
        ):
            for kind, row in coverage_results.get(key, []):
                if kind == 'row':
                    for index, value in enumerate(row[:row_count]):
                        cumulative[index].add(value)
                elif kind == 'agg' and agg_count:
                    for index, value in enumerate(row[-agg_count:]):
                        cumulative[row_count + index].add(value)
        return self.compute_signal() != previous

    def print_iter_summary(self, i: int) -> None:
        for label, cumulative in (('GT', self.cumulative_gt), ('CD', self.cumulative_cd)):
            if cumulative:
                counts = [len(outcomes) for outcomes in cumulative]
                total, maximum = sum(counts), len(counts) * 3
                print(f"Iteration {i}: {label} 1-way Coverage: {total}/{maximum} outcomes ({total / maximum:.2%})")
                print(f"  Per-predicate coverage: {counts}")

    def print_final_summary(self) -> None:
        for label, cumulative in (('GT', self.cumulative_gt), ('CD', self.cumulative_cd)):
            if cumulative:
                total, maximum = sum(map(len, cumulative)), len(cumulative) * 3
                print(f"{label} 1-way Coverage: {total}/{maximum} outcomes ({total / maximum:.2%})")
                for index, outcomes in enumerate(cumulative):
                    missing = {-1, 0, 1} - outcomes
                    if missing:
                        print(f"  Sig_{index}: covered {sorted(outcomes)}, missing {sorted(missing)}")
