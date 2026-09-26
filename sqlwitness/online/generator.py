from sqlwitness.online.schema_handler import SchemaHandler
from itertools import product
from collections import defaultdict, deque
import random
from datetime import datetime, timedelta, date, time
from dateutil import parser
from typing import Optional, Any
import sqlwitness.heuristic_types as at
class DataGenerator:
    def __init__(self, schema: SchemaHandler, heuristics: tuple[list[at.ValueHeuristic], list[at.CardinalityHeuristic], list[at.OperatorHeuristic]] = ([], [], []), constraints: list = [], iter_num: int = 0, gt_query: Optional[str] = None, cd_query: Optional[str] = None, cardinality_boost: int = 0, remove_null: bool = False, remove_one: bool = False, remove_literal: bool = False):
        self.schema = schema
        self.constraints = constraints
        self.all_columns = schema.extract_all_columns() # list of (table, column)
        self.column_types = schema.extract_column_types() # dict of (table, column) ->
        # normaliz type mapping
        self.str_types = {'text', 'char', 'varchar', 'str'}
        self.int_types = {'int', 'integer', 'numeric'}
        self.float_types = {'real', 'float', 'double'}
        self.date_types = {'date'}
        self.time_types = {'time'}
        self.datetime_types = {'datetime', 'timestamp'}
        self.bool_types = {'bool', 'boolean'}

        # ablation options
        self.remove_null = remove_null
        self.remove_one = remove_one
        self.remove_literal = remove_literal

        # Default candidate values per type (override these to change generation behaviour)
        _base_date = datetime(2023, 1, 1).date()
        _base_time = datetime(2023, 1, 1, 0, 0, 0)
        _base_datetime = datetime(2023, 1, 1, 0, 0, 0)

        # remove one
        if self.remove_one:
            self.default_int_candidates: list = [0, None]
            self.default_float_candidates: list = [0.0, None]
            self.default_str_candidates: list = ['default1', None]
            self.default_date_candidates: list = [_base_date, None]
            self.default_time_candidates: list = [_base_time.time(), None]
            self.default_datetime_candidates: list = [_base_datetime, None]
            self.default_bool_candidates: list = [True, None]

        # remove null
        elif self.remove_null:
            self.default_int_candidates: list = [0, 1]
            self.default_float_candidates: list = [0.0, 1.0]
            self.default_str_candidates: list = ['default1', 'default2']
            self.default_date_candidates: list = [_base_date, _base_date + timedelta(days=1)]
            self.default_time_candidates: list = [_base_time.time(), _base_date + timedelta(hours=1)]
            self.default_datetime_candidates: list = [_base_datetime, _base_datetime + timedelta(hours=1)]
            self.default_bool_candidates: list = [True, False]
        
        # default
        else:
            self.default_int_candidates: list = [0, 1, None]
            self.default_float_candidates: list = [0.0, 1.0, None]
            self.default_str_candidates: list = ['default1', 'default2', None]
            self.default_date_candidates: list = [_base_date, _base_date + timedelta(days=1), None]
            self.default_time_candidates: list = [_base_time.time(), (_base_time + timedelta(hours=1)).time(), None]
            self.default_datetime_candidates: list = [_base_datetime, _base_datetime + timedelta(hours=1), None]
            self.default_bool_candidates: list = [True, False, None]

        self.gt_query = gt_query
        self.cd_query = cd_query

        # generation parameters
        self.default_row_counts = lambda lower, upper : random.randint(lower, upper) # per table
        self.default_lower = 0
        self.high_prob = 0.0 # chance to switch to higher row count
        self.high_lower = 1000
        self.clipping_max = 100

        # Coverage-guided cardinality: base upper limit + boost from coverage saturation
        # self.default_upper = iter_num * 2
        # self.default_upper = cardinality_boost
        # self.default_upper = 1000
        self.default_upper = cardinality_boost
        

        self.heuristics = heuristics
        value_heurs, card_heurs, op_heurs = self.heuristics
        self.value_heurs = value_heurs
        if self.remove_literal: 
            self.value_heurs = [] # ablation
        self.card_heurs = card_heurs
        self.op_heurs = []


        # Foreign key dependency graph (parent column -> children columns)
        self.fk_parent_map = defaultdict(list)  # child column -> [parent columns]
        self.fk_children_map = defaultdict(list)  # parent column -> [child columns]
        
        self.table_data = {}
        self.table_row_count = {}
        self.candidate_values = {col: [] for col in self.all_columns} # dict of (table, column) -> possible values
        self.value_constraints = []
        self.relational_constraints = []  # NEW: column-column constraints
        value_constraint_types = {'gte', 'lte', 'gt', 'lt', 'equal', 'neq', 'not_null', 'enum', 'domain'}
        for constraint in self.constraints:
            constraint_type = list(constraint.keys())[0]
            if constraint_type in value_constraint_types:
                # Check if it's a relational constraint (column vs column)
                if constraint_type in ['neq', 'gt', 'gte', 'lt', 'lte', 'equal']:
                    operand1, operand2 = constraint[constraint_type]
                    # If operand2 is a column reference (contains '.'), it's relational
                    if isinstance(operand2, str) and '.' in operand2:
                        self.relational_constraints.append({
                            'type': constraint_type,
                            'col1': operand1,
                            'col2': operand2
                        })
                        # print("Added relational constraint:", constraint)
                    else:
                        # Column-constant constraint
                        self.value_constraints.append(constraint)
                        # print("Added value constraint:", constraint)
                else:
                    self.value_constraints.append(constraint)
                    # print("Added value constraint:", constraint)

        self._build_fk_graph()
        # print("\033[96mForeign Key Graph Built:\nParents:", dict(self.fk_parent_map), "\nChildren:", dict(self.fk_children_map), "\033[0m")

    def apply_value_heuristic(self, vh: at.ValueHeuristic):
        target_cols = self._get_applicable_columns(vh)
        value = vh.value
        op = vh.operator
        for tb, col in target_cols:
            if value.int_value is not None:
                v = value.int_value
                self.candidate_values[(tb, col)].extend([v, v - 1, v + 1])
            if value.float_value is not None:
                v = value.float_value
                self.candidate_values[(tb, col)].extend([v, v - 1.0, v + 1.0])
            if value.str_value is not None:
                v = value.str_value
                self.candidate_values[(tb, col)].extend(
                    [v]
                )
            if value.date_value:
                year  = int(value.date_value.year)  if value.date_value.year  else 2023
                month = int(value.date_value.month) if value.date_value.month else 1
                day   = int(value.date_value.day)   if value.date_value.day   else 1
                date_val = datetime(year, month, day).date()
                self.candidate_values[(tb, col)].extend(
                    [date_val, date_val - timedelta(days=1), date_val + timedelta(days=1)]
                )

    def apply_operator_heuristic(self, oh: at.OperatorHeuristic):
        """Apply operator mutations to existing candidate values
        
        Randomly select values and apply operators based on count.
        +/- are treated the same (addition/subtraction), *// are treated the same (multiplication/division).
        For example, if count=2 with '+', we might generate: v1+v2, v1+v3, etc.
        """
        operator = oh.operator
        count = oh.count
        
        # Map operators to their group (+ and - are the same, * and / are the same)
        # Randomly choose which operation within the group to use
        use_addition = False
        use_multiplication = False
        if operator in ('+', '-'):
            use_addition = random.choice([True, False])  # True=+, False=-
        elif operator in ('*', '/'):
            use_multiplication = random.choice([True, False])  # True=*, False=/
        
        # Apply operator mutations to all integer columns
        for col in self.all_columns:
            col_type = self.column_types[col]
            
            # Only apply to integer types for now (case-insensitive comparison)
            if col_type.lower() in self.int_types:
                current_candidates = [v for v in self.candidate_values[col] if v is not None and isinstance(v, int)]
                
                if len(current_candidates) == 0:
                    continue
                
                # Generate new values by applying operators 'count' times
                for _ in range(count):
                    # Randomly select values to combine
                    if len(current_candidates) >= 2:
                        v1, v2 = random.sample(current_candidates, 2)
                    else:
                        v1 = random.choice(current_candidates)
                        v2 = random.choice(current_candidates)
                    
                    # Apply the operator
                    if operator in ('+', '-'):
                        if use_addition or operator == '+':
                            new_val = v1 + v2
                        else:
                            new_val = v1 - v2
                        self.candidate_values[col].append(new_val)
                    elif operator in ('*', '/'):
                        if use_multiplication or operator == '*':
                            new_val = v1 * v2
                        else:
                            if v2 != 0:
                                new_val = v1 // v2
                            else:
                                new_val = v1
                        self.candidate_values[col].append(new_val)

    def typecheck_candidates(self):
        """Type check candidate values and remove incompatible ones"""
        for col, candidates in self.candidate_values.items():
            table, column = col
            col_type = self.column_types[col]
            if col_type in self.int_types:
                self.candidate_values[col] = [c for c in candidates if isinstance(c, int) or c is None]
            elif col_type in self.float_types:
                self.candidate_values[col] = [c for c in candidates if isinstance(c, float) or c is None]
            elif col_type in self.str_types:
                self.candidate_values[col] = [c for c in candidates if isinstance(c, str) or c is None]
            elif col_type in self.date_types:
                valid_dates = []
                for c in candidates:
                    if isinstance(c, date):
                        valid_dates.append(c)
                    elif isinstance(c, str):
                        try:
                            parsed_date = parser.parse(c).date()
                            valid_dates.append(parsed_date)
                        except (ValueError, OverflowError):
                            continue
                    elif c is None:
                        valid_dates.append(c)
                self.candidate_values[col] = [c for c in valid_dates]
            elif col_type in self.time_types:
                valid_times = []
                for c in candidates:
                    if isinstance(c, time):
                        valid_times.append(c)
                    elif isinstance(c, str):
                        try:
                            parsed_time = parser.parse(c).time()
                            valid_times.append(parsed_time)
                        except (ValueError, OverflowError):
                            continue
                    elif c is None:
                        valid_times.append(c)
                self.candidate_values[col] = [c for c in valid_times]
            elif col_type in self.datetime_types:
                valid_datetimes = []
                for c in candidates:
                    if isinstance(c, datetime):
                        valid_datetimes.append(c)
                    elif isinstance(c, str):
                        try:
                            parsed_datetime = parser.parse(c)
                            valid_datetimes.append(parsed_datetime)
                        except (ValueError, OverflowError):
                            continue
                    elif c is None:
                        valid_datetimes.append(c)
                self.candidate_values[col] = [c for c in valid_datetimes]
            elif col_type in self.bool_types:
                self.candidate_values[col] = [c for c in candidates if isinstance(c, bool) or c is None]
            else:
                # default to string
                self.candidate_values[col] = [c for c in candidates if isinstance(c, str) or c is None]


    def value_constraints_check(self):
        # inclusion, not null, comparison
        # enforce on candidate values
        # {'gte': ['Activity.games_played', 0]}
        for constraint in self.value_constraints:
            if 'gte' in constraint:
                # Greater than or equal: {'gte': ['Activity.games_played', 0]}
                col_name, min_val = constraint['gte']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or v >= min_val]
                    if min_val not in self.candidate_values[key]:
                        self.candidate_values[key].append(min_val)
            
            if 'lte' in constraint:
                # Less than or equal: {'lte': ['Activity.games_played', 1000]}
                col_name, max_val = constraint['lte']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or v <= max_val]
                    if max_val not in self.candidate_values[key]:
                        self.candidate_values[key].append(max_val)
            
            if 'gt' in constraint:
                # Greater than: {'gt': ['Activity.games_played', 0]}
                col_name, min_val = constraint['gt']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or v > min_val]
                    if (min_val + 1) not in self.candidate_values[key]:
                        self.candidate_values[key].append(min_val + 1)
            
            if 'lt' in constraint:
                # Less than: {'lt': ['Activity.games_played', 1000]}
                col_name, max_val = constraint['lt']
                print("Applying lt constraint:", col_name, max_val)
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    print("Before applying lt, candidates:", self.candidate_values[key])
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or v < max_val]
                    if (max_val - 1) not in self.candidate_values[key]:
                        self.candidate_values[key].append(max_val - 1)
            
            if 'equal' in constraint:
                # Equal: {'equal': ['Activity.games_played', 100]}
                col_name, val = constraint['equal']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or v == val]
                    # Ensure the equal value is included
                    if val not in self.candidate_values[key]:
                        self.candidate_values[key].append(val)
            
            if 'neq' in constraint:
                # Not equal: {'neq': ['Activity.games_played', 0]}
                col_name, val = constraint['neq']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or v != val]
            
            if 'not_null' in constraint:
                # Not null: {'not_null': 'Activity.player_id'}
                col_name = constraint['not_null']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is not None]
            
            if 'enum' in constraint:
                # Enum values: {'enum': ['Activity.action', ['show', 'answer', 'skip']]}
                col_name, allowed_values = constraint['enum']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v in allowed_values]
                    # Ensure allowed values are included
                    for val in allowed_values:
                        if val not in self.candidate_values[key]:
                            self.candidate_values[key].append(val)
            
            if 'domain' in constraint:
                # Domain range: {'domain': ['Activity.games_played', 0, 1000]}
                col_name, min_val, max_val = constraint['domain']
                table, column = col_name.split('.')
                key = (table.lower(), column.lower())
                if key in self.candidate_values:
                    self.candidate_values[key] = [v for v in self.candidate_values[key] if v is None or (min_val <= v <= max_val)]
                    # Ensure boundary values are included
                    if min_val not in self.candidate_values[key]:
                        self.candidate_values[key].append(min_val)
                    if max_val not in self.candidate_values[key]:
                        self.candidate_values[key].append(max_val)

    def get_not_null_values(self, candidates: list) -> list:
        return [c for c in candidates if c is not None]


    def generate_pk_values(self, pk_cols: list[tuple[str, str]], generated_values = None) -> list[list]:
        """Pk columns could be composite. Generate unique combinations, not individual unique columns."""
        if not pk_cols:
            return []
        
        if generated_values is None:
            generated_values = {}
        
        # All PK columns are from the same table, so use the first table's row count
        table_name = pk_cols[0][0]
        row_count = self.table_row_count.get(table_name, self.default_row_counts)
        
        if len(pk_cols) == 1:
            tc = pk_cols[0]
            self.value_constraints.append({'not_null': f"{tc[0]}.{tc[1]}"})
            
            # Check for membership constraint
            ref_values = self._get_referenced_values(tc, generated_values)
            if ref_values:
                # print(f"PK column {tc[0]}.{tc[1]} has membership constraint with {len(ref_values)} referenced values")
                self.candidate_values[tc] = ref_values
            
            initial_vals = self.generate_values(tc)
            unique_values = self.generate_unique_values([tc], initial_vals, self.value_constraints)
            column_values = unique_values[0][:row_count] if unique_values else []
            return [column_values]
        
        else:
            # Composite PK - generate unique tuples using generate_unique_values
            # Collect non-null candidates for each column
            initial_composite_values = []
            for table, col in pk_cols:
                tc = (table, col)
                self.value_constraints.append({'not_null': f"{tc[0]}.{tc[1]}"})
                
                # Check if this PK column has a membership constraint
                ref_values = self._get_referenced_values(tc, generated_values)
                if ref_values:
                    # print(f"PK column {table}.{col} has membership constraint with {len(ref_values)} referenced values")
                    self.candidate_values[tc] = ref_values
                    # For now, just mark it in candidates
                    # The referenced values will be populated later
                
                initial_vals = self.generate_values(tc)
                # print(f"Initial values for {table}.{col}: {initial_vals}")
                initial_composite_values.append(initial_vals)
                        
            # Use generate_unique_values to handle composite unique constraint
            unique_values = self.generate_unique_values(pk_cols, initial_composite_values, self.value_constraints)
            
            return unique_values

    def _get_relational_constrained_columns(self) -> set[tuple[str, str]]:
        """
        Identify columns that are targets of relational constraints.
        These should be generated WITH constraints, not filtered after.
        
        Returns:
            Set of (table, column) tuples that appear as the second operand
            in relational constraints (e.g., 'supervisor' in 'empId != supervisor')
        """
        constrained_cols = set()
        for constraint in self.relational_constraints:
            col2_name = constraint['col2']
            table, column = col2_name.split('.')
            constrained_cols.add((table.lower(), column.lower()))
        return constrained_cols
    
    def _apply_relational_constraints(self, generated_values: dict) -> dict:
        """
        Apply column-column relational constraints (e.g., empId != supervisor).
        Now primarily generates constrained columns with awareness, rather than filtering.
        """
        if not self.relational_constraints:
            return generated_values
        
        print(f"Applying {len(self.relational_constraints)} relational constraints...")
        
        for constraint in self.relational_constraints:
            op = constraint['type']
            col1_name = constraint['col1']
            col2_name = constraint['col2']
            
            table1, column1 = col1_name.split('.')
            table2, column2 = col2_name.split('.')
            
            if table1.lower() != table2.lower():
                print(f"  Warning: Cross-table relational constraint not yet supported: {col1_name} {op} {col2_name}")
                continue
            
            table = table1.lower()
            key1 = (table, column1.lower())
            key2 = (table, column2.lower())
            
            # Check if col1 (source) is generated
            if key1 not in generated_values:
                print(f"  Warning: Column {col1_name} not generated yet, skipping constraint")
                continue
            
            # print(f"  Applying: {col1_name} {op} {col2_name}")
            
            # Strategy: Always generate col2 with constraint awareness
            # This avoids the filtering problem that can produce 0 rows
            if key2 not in generated_values:
                # print(f"    Generating {col2_name} WITH constraint awareness (preferred)")
                generated_values = self._generate_with_relational_constraint(
                    generated_values, table, key1, key2, op
                )
            else:
                # Both already generated - this shouldn't happen with new ordering
                # but keep as fallback
                print(f"    Warning: {col2_name} already generated, falling back to filtering")
                print(f"    This may reduce row count. Consider reordering generation.")
                generated_values = self._filter_by_relational_constraint(
                    generated_values, table, key1, key2, op
                )
        
        return generated_values
    
    def _generate_with_relational_constraint(self, generated_values: dict, table: str,
                                            key1: tuple, key2: tuple, op: str) -> dict:
        """
        Generate col2 values that satisfy the relational constraint with col1.
        Used for constraints like Employee.empId != Employee.supervisor.
        """
        col1_values = generated_values[key1]
        col2_values = []
        
        # Build candidate pool for col2
        # For supervisor, it should reference other empIds + allow NULL
        if op == 'neq':
            # Use col1 values as candidates (can reference other employees)
            base_candidates = col1_values[:] if col1_values else []
        else:
            # Use regular candidates from candidate_values
            base_candidates = self.candidate_values.get(key2, col1_values[:] if col1_values else [])
        
        # Ensure base_candidates is always a list
        if base_candidates is None:
            base_candidates = []
        
        # For each row, generate col2 value satisfying constraint
        for idx, col1_val in enumerate(col1_values):
            if op == 'neq':
                # Exclude the value from col1 in same row
                valid_candidates = [c for c in base_candidates if c != col1_val]
                # Always allow NULL (no supervisor case)
                valid_candidates.append(None)
            elif col1_val is None:
                # If col1 is NULL, can't apply comparison constraint - use all base candidates
                valid_candidates = base_candidates[:]
                if None not in valid_candidates:
                    valid_candidates.append(None)
            elif op == 'gt':
                # col1 > col2 means we need col2 < col1
                valid_candidates = [c for c in base_candidates if c is not None and c < col1_val]
                valid_candidates.append(None)
            elif op == 'gte':
                # col1 >= col2 means we need col2 <= col1
                valid_candidates = [c for c in base_candidates if c is not None and c <= col1_val]
                valid_candidates.append(None)
            elif op == 'lt':
                # col1 < col2 means we need col2 > col1
                valid_candidates = [c for c in base_candidates if c is not None and c > col1_val]
                valid_candidates.append(None)
            elif op == 'lte':
                # col1 <= col2 means we need col2 >= col1
                valid_candidates = [c for c in base_candidates if c is not None and c >= col1_val]
                valid_candidates.append(None)
            elif op == 'equal':
                valid_candidates = [col1_val]  # Must equal
            else:
                valid_candidates = base_candidates[:]
            
            if valid_candidates:
                col2_values.append(random.choice(valid_candidates))
            else:
                # If no valid candidates, use NULL
                col2_values.append(None)
        
        generated_values[key2] = col2_values
        return generated_values
    
    def _filter_by_relational_constraint(self, generated_values: dict, table: str,
                                        key1: tuple, key2: tuple, op: str) -> dict:
        """
        Filter rows to keep only those satisfying the relational constraint.
        """
        col1_vals = generated_values[key1]
        col2_vals = generated_values[key2]
        row_count = len(col1_vals)
        
        valid_indices = []
        for idx in range(row_count):
            val1 = col1_vals[idx]
            val2 = col2_vals[idx]
            
            if self._check_constraint(val1, val2, op):
                valid_indices.append(idx)
        
        print(f"  Filtered {row_count} -> {len(valid_indices)} rows for {table}")
        
        # Keep only valid rows for all columns in this table
        for key in list(generated_values.keys()):
            if key[0] == table:
                generated_values[key] = [generated_values[key][i] for i in valid_indices]
        
        # Update row count
        self.table_row_count[table] = len(valid_indices)
        
        return generated_values
    
    def _check_constraint(self, val1, val2, op: str) -> bool:
        """Check if two values satisfy a relational constraint."""
        # Handle NULL values (SQL three-valued logic)
        if val1 is None or val2 is None:
            return True  # NULL comparisons are typically allowed
        
        if op == 'neq':
            return val1 != val2
        elif op == 'gt':
            return val1 > val2
        elif op == 'gte':
            return val1 >= val2
        elif op == 'lt':
            return val1 < val2
        elif op == 'lte':
            return val1 <= val2
        elif op == 'equal':
            return val1 == val2
        else:
            return True
    
    def _get_referenced_values(self, column: tuple[str, str], generated_values: dict) -> Optional[list]:
        parents = self.fk_parent_map.get(column)
        if not parents:
            return None

        for parent_col in parents:
            if parent_col in generated_values:
                return generated_values[parent_col]

        return None

    def _extract_foreign_key_relationships(self) -> list[tuple[tuple[str, str], tuple[str, str]]]:
        """Extract all foreign key relationships from schema, constraints, and heuristics.
        
        Returns:
            List of tuples: (referenced_column, referencing_column)
            Each column is represented as (table, column) tuple
        """
        relationships = []
        seen_edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()

        def _add_relationship(parent_node: tuple[str, str], child_node: tuple[str, str]) -> None:
            edge = (parent_node, child_node)
            if edge in seen_edges:
                # print(f"\033[91m[FK] Duplicate relationship skipped: {edge}\033[0m")
                return
            seen_edges.add(edge)
            relationships.append(edge)

        fk_list = self.schema.extract_foreign_keys()
        for fk in fk_list:
            child_table, child_col, parent_table, parent_col = fk
            parent_node = (parent_table.lower(), parent_col.lower())
            child_node = (child_table.lower(), child_col.lower())
            _add_relationship(parent_node, child_node)
        
        # Extract constraint-based membership relationships
        for constraint in self.constraints:
            if 'membership' in constraint:
                fk_col, ref_col = constraint['membership']
                fk_table, fk_column = fk_col.split('.')
                ref_table, ref_column = ref_col.split('.')
                fk_node = (fk_table.lower(), fk_column.lower())
                ref_node = (ref_table.lower(), ref_column.lower())
                _add_relationship(ref_node, fk_node)
        
        
        return relationships

    def _build_fk_graph(self) -> None:
        """Construct adjacency maps for foreign key dependencies."""
        self.fk_parent_map.clear()
        self.fk_children_map.clear()

        relationships = self._extract_foreign_key_relationships()
        for parent_col, child_col in relationships:
            self.fk_children_map[parent_col].append(child_col)
            self.fk_parent_map[child_col].append(parent_col)

    def _generate_fk_column(self, child_col: tuple[str, str], generated_values: dict) -> None:
        """Populate a foreign key column based on previously generated parent values."""
        child_table, child_column = child_col
        if child_col in generated_values:
            # print(f"\033[91m[FK] Skipping {child_table}.{child_column} - already populated\033[0m")
            return

        parents = self.fk_parent_map.get(child_col, [])
        if not parents:
            # print(f"\033[91m[FK] No parent mapping for {child_table}.{child_column}; nothing to generate\033[0m")
            return

        parent_col = parents[0]
        parent_values = generated_values.get(parent_col, [])
        if parent_values is None:
            parent_values = []

        row_count = self.table_row_count.get(child_table, self.default_row_counts)
        if row_count == 0:
            generated_values[child_col] = []
            # print(f"\033[91m[FK] {child_table}.{child_column} has 0 target rows after parent checks\033[0m")
            return

        has_not_null = any(
            constraint.get('not_null') == f"{child_table}.{child_column}"
            for constraint in self.constraints
            if isinstance(constraint, dict)
        )

        if len(parent_values) == 0:
            if has_not_null:
                # print(f"\033[91m[FK] {child_table}.{child_column} has NOT NULL but no referenced values; forcing table row count to 0\033[0m")
                self.table_row_count[child_table] = 0
                generated_values[child_col] = []
            else:
                generated_values[child_col] = [None] * row_count
                # print(f"\033[91m[FK] {child_table}.{child_column} filled with NULLs (no parent values)\033[0m")
            return

        is_unique_fk = self._is_column_unique(child_table, child_column)

        if is_unique_fk:
            available_refs = [v for v in parent_values if v is not None] if has_not_null else parent_values[:]

            # Deduplicate while preserving order
            seen = set()
            unique_available = []
            for value in available_refs:
                if value not in seen:
                    seen.add(value)
                    unique_available.append(value)

            if not unique_available:
                if has_not_null:
                    # print(f"\033[91m[FK] No non-null values available for unique FK {child_table}.{child_column}; reducing row count to 0\033[0m")
                    self.table_row_count[child_table] = 0
                    generated_values[child_col] = []
                else:
                    generated_values[child_col] = [None] * row_count
                    # print(f"\033[91m[FK] {child_table}.{child_column} unique FK filled with NULLs\033[0m")
                return

            if len(unique_available) < row_count:
                # print(f"\033[91m[FK] Only {len(unique_available)} unique parent values available for {child_table}.{child_column}; reducing row count from {row_count}\033[0m")
                row_count = len(unique_available)
                self.table_row_count[child_table] = row_count

            generated_values[child_col] = random.sample(unique_available, row_count)
            # print(f"\033[91m[FK] Generated unique mapping for {child_table}.{child_column} with {row_count} rows\033[0m")
            return

        # Non-unique FK: allow duplicates and optional NULLs
        candidates = parent_values[:]
        if not self.remove_null and not has_not_null and None not in candidates:
            candidates.append(None)

        if not candidates:
            generated_values[child_col] = [None] * row_count
            # print(f"\033[91m[FK] {child_table}.{child_column} candidates empty; defaulting to NULLs\033[0m")
            return

        self.candidate_values[child_col] = candidates
        generated_values[child_col] = self.generate_values(child_col)
        # print(f"\033[91m[FK] Generated non-unique mapping for {child_table}.{child_column} with {len(generated_values[child_col])} rows\033[0m")
    
    def add_default_candidates(self):
        for col, candidates in self.candidate_values.items():
            col_type = self.column_types.get(col, 'str')
            if col_type in self.int_types:
                candidates.extend(self.default_int_candidates)
            elif col_type in self.float_types:
                candidates.extend(self.default_float_candidates)
            elif col_type in self.str_types:
                candidates.extend(self.default_str_candidates)
            elif col_type in self.date_types:
                candidates.extend(self.default_date_candidates)
            elif col_type in self.time_types:
                candidates.extend(self.default_time_candidates)
            elif col_type in self.datetime_types:
                candidates.extend(self.default_datetime_candidates)
            elif col_type.startswith('enum'):
                candidates.extend(col_type.split(',')[1:])
            elif col_type in self.bool_types:
                candidates.extend(self.default_bool_candidates)
            else:
                raise ValueError("Unsupported candidate type for generating values")

    def _prune_defaults_for_column(self, tc: tuple[str, str], candidates: list, min_heuristic_values: int = 4) -> list:
        """Remove default values from a column's candidates if heuristics provided enough."""
        col_type = self.column_types.get(tc, 'str')
        _default_map = {
            frozenset(self.int_types): self.default_int_candidates,
            frozenset(self.float_types): self.default_float_candidates,
            frozenset(self.str_types): self.default_str_candidates,
            frozenset(self.date_types): self.default_date_candidates,
            frozenset(self.time_types): self.default_time_candidates,
            frozenset(self.datetime_types): self.default_datetime_candidates,
            frozenset(self.bool_types): self.default_bool_candidates,
        }
        defaults = None
        for type_group, default_list in _default_map.items():
            if col_type in type_group:
                defaults = default_list
                break
        if defaults is None:
            return candidates
        non_default = [v for v in candidates if v not in defaults]
        if len(non_default) >= min_heuristic_values:
            return non_default
        return candidates

    def _compute_constraint_aware_row_counts(self) -> dict:
        """
        Determine row count for each table based on constraints.
        
        Key rules:
        1. FK + Unique: child_rows ≤ parent_rows
        2. Enum/domain constraints: rows ≤ domain_size
        3. Heuristics: ensure enough rows for duplicates
        4. Default: use base_count for unconstrained tables
        """
        table_counts = {}
        # base_count = self.default_upper  # Default upper bound
        
        # Step 1: Initialize with base counts
        for table in self.schema.schema:
            table_name = table['TableName']
            table_counts[table_name] = self.default_row_counts(self.default_lower, self.default_upper)
        
        # Step 2: Apply cardinality heuristics
        # print(f"\033[94mApplying cardinality heuristics to adjust table row counts...\033[0m")
        # print(self.card_heurs)
        # for dh in self.card_heurs:
        #     if random.random() < self.high_prob:
        #         if dh.min_card is not None and dh.min_card > self.high_lower:
        #             min_card = dh.min_card
        #         else:
        #             min_card = self.high_lower
        #         all_tables = [table['TableName'] for table in self.schema.schema]
        #         # chosen_table = random.choice(all_tables)
        #         for chosen_table in all_tables:
        #             # table_counts[chosen_table] = self.default_upper

        #             table_counts[chosen_table] = self.default_row_counts(0, base_count+min_card)

        # Step 3: Analyze unique constraints and cardinality limits
        cardinality_limits = self._compute_cardinality_limits()
        
        # Step 4: Build FK dependency graph with uniqueness info
        fk_graph = self._build_fk_dependency_graph()
        
        # Step 5: Topological ordering (parent before child)
        table_order = self._topological_sort_tables()
        
        # Step 6: Propagate constraints in topological order
        for table in table_order:
            # Check FK constraints where this table is child
            if table in fk_graph:
                for parent_table, fk_info in fk_graph[table].items():
                    if fk_info['is_unique']:
                        # Unique FK: child rows ≤ parent rows
                        table_counts[table] = min(
                            table_counts[table],
                            table_counts[parent_table]
                        )
            
            # Check cardinality limits from constraints
            if table in cardinality_limits:
                table_counts[table] = min(
                    table_counts[table],
                    cardinality_limits[table]
                )
        
        # Step 7: Allow empty tables (no minimum constraint)
        # Empty tables can be valid test cases for finding counterexamples
        for table in table_counts:
            table_counts[table] = max(0, table_counts[table])
        
        # Step 8: clipping extreme values
        # for table in table_counts:
        #     table_counts[table] = min(table_counts[table], self.clipping_max)
        
        return table_counts
    
    def _compute_cardinality_limits(self) -> dict:
        """
        Compute maximum possible rows per table based on value constraints.
        Returns dict: {table_name: max_rows}
        """
        limits = {}
        
        for table in self.schema.schema:
            table_name = table['TableName']
            min_limit = float('inf')
            
            # Check each column for cardinality-limiting constraints
            # Get all columns from this table using schema handler
            table_cols = self.schema.extract_all_columns_in_table(table_name)
            
            for col_name in table_cols:
                col_key = (table_name.lower(), col_name.lower())
                
                # Check if column has unique constraint
                is_unique = self._is_column_unique(table_name, col_name)
                
                if is_unique:
                    # Calculate domain size for this column
                    domain_size = self._calculate_domain_size(table_name, col_name)
                    min_limit = min(min_limit, domain_size)
            
            if min_limit != float('inf'):
                limits[table_name] = min_limit
        
        return limits
    
    def _calculate_domain_size(self, table: str, column: str) -> int:
        """Calculate how many distinct values a column can have."""
        col_key = (table.lower(), column.lower())
        
        # Check for enum constraints
        for constraint in self.value_constraints:
            if 'enum' in constraint:
                col_name, allowed_values = constraint['enum']
                if col_name == f"{table}.{column}":
                    return len(allowed_values)
            
            if 'domain' in constraint:
                col_name, min_val, max_val = constraint['domain']
                if col_name == f"{table}.{column}":
                    col_type = self.column_types.get(col_key, 'int')
                    if col_type in self.int_types:
                        return int(max_val - min_val + 1)
                    elif col_type in self.float_types:
                        return 1000  # Approximate for floats
        
        # Default: practically unlimited
        return 100000000
    
    def _is_column_unique(self, table: str, column: str) -> bool:
        """Check if a column has a unique constraint."""
        col_key = (table.lower(), column.lower())
        col_type = self.column_types.get(col_key, '')

        # Heuristic: enum columns usually carry categorical labels, so treating them as unique
        # artificially caps row counts. Skip uniqueness for enums to avoid underestimation.
        if isinstance(col_type, str) and col_type.startswith('enum'):
            return False

        # Check primary keys
        pk_cols = self.schema.extract_primary_keys()
        if col_key in pk_cols:
            return True
        
        # Check unique constraints
        for constraint in self.constraints:
            if 'distinct' in constraint:
                unique_cols = constraint['distinct']
                if f"{table}.{column}" in unique_cols:
                    # Simple unique (not composite)
                    if len(unique_cols) == 1:
                        return True
        
        return False
    
    def _build_fk_dependency_graph(self) -> dict:
        """
        Build FK dependency graph with uniqueness information.
        Returns: {child_table: {parent_table: {'is_unique': bool, 'columns': [...]}}}
        """
        graph = defaultdict(dict)
        
        # Get all FK relationships
        fk_relationships = self._extract_foreign_key_relationships()
        
        for parent_col, child_col in fk_relationships:
            parent_table, parent_col_name = parent_col
            child_table, child_col_name = child_col
            
            # Check if child FK column is unique
            is_unique = self._is_column_unique(child_table, child_col_name)
            
            if parent_table not in graph[child_table]:
                graph[child_table][parent_table] = {
                    'is_unique': is_unique,
                    'columns': []
                }
            else:
                # Update uniqueness if any FK column is unique
                graph[child_table][parent_table]['is_unique'] = (
                    graph[child_table][parent_table]['is_unique'] or is_unique
                )
            
            graph[child_table][parent_table]['columns'].append({
                'parent_col': parent_col_name,
                'child_col': child_col_name
            })
        
        return dict(graph)
    
    def _topological_sort_tables(self) -> list:
        """Return tables in topological order (parents before children)."""
        tables = [t['TableName'] for t in self.schema.schema]
        fk_list = self.schema.extract_foreign_keys()
        
        # Build adjacency list (parent -> children)
        adj = defaultdict(list)
        in_degree = defaultdict(int)
        
        for child_table, child_col, parent_table, parent_col in fk_list:
            if child_table != parent_table:  # Skip self-references
                adj[parent_table].append(child_table)
                in_degree[child_table] += 1
                if parent_table not in in_degree:
                    in_degree[parent_table] = 0
        
        # Initialize in_degree for all tables
        for table in tables:
            if table not in in_degree:
                in_degree[table] = 0
        
        # Kahn's algorithm
        queue = deque([t for t in tables if in_degree[t] == 0])
        result = []
        
        while queue:
            table = queue.popleft()
            result.append(table)
            
            for child in adj[table]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
        
        # Handle cycles (shouldn't happen in well-formed schemas)
        remaining = [t for t in tables if t not in result]
        result.extend(remaining)
        
        return result


    def generate_values(self, tc: tuple[str, str]) -> list:
        """Generate values for a column. Allow duplicates."""
        table, column = tc
        count = self.table_row_count.get(table, self.default_row_counts)
        result = []
        
        # Handle empty tables
        if count == 0:
            return []
        
        # nullable columns can have None, so need to use manual type check
        self.typecheck_candidates()
        self.value_constraints_check() # added boundary values from constraints, guarantee at least min/max if any
        
        # Deduplicate candidates to ensure diversity
        candidates = self.candidate_values.get(tc, [])
        if candidates:
            # Convert to list(set(...)) to remove duplicates while preserving hashable types
            # Need to handle unhashable types like lists/dicts separately if they exist
            try:
                candidates = list(set(candidates))
            except TypeError:
                # If unhashable types present, do manual deduplication
                seen = []
                unique_candidates = []
                for c in candidates:
                    if c not in seen:
                        seen.append(c)
                        unique_candidates.append(c)
                candidates = unique_candidates
        
        # print(f"Generating values for {table}.{column} with candidates: {candidates}")
        
        if self.remove_null:
            candidates = [c for c in candidates if c is not None]

        if not candidates:
            # No candidates available - shouldn't happen but handle gracefully
            return [None] * count
        # print(candidates, tc)
        while len(result) < count:
            result.append(random.choice(candidates))
        return result
    
    def generate_unique_values(self, unique_tc: list[tuple[str, str]], initial_values: list, value_constraints: list = []) -> list:
        """Generate unique combinations for composite unique constraints.
        
        Args:
            unique_tc: List of columns that together form a unique constraint
            initial_values: List of initial values for each column (parallel to unique_tc)
            value_constraints: List of value constraints to respect
            
        Returns:
            List of lists, where each inner list contains values for one column
        """
        if not unique_tc:
            return []
        # All columns should be from the same table for unique constraints
        table_name = unique_tc[0][0].lower()
        row_count = self.table_row_count.get(table_name, self.default_row_counts)
        
        # Handle empty tables
        if row_count == 0:
            return [[]] * len(unique_tc) if len(unique_tc) > 1 else [[]]
        
        if len(unique_tc) == 1:
            # Single column unique constraint - generate unique values for that column
            tc = unique_tc[0]
            # print(f"Generating unique values for {table_name}.{tc[1]} with initial values: {initial_values}")
            generated_list: list[Any] = []
            generated_set: set[Any] = set()

            if initial_values:
                for val in initial_values:
                    if val not in generated_set:
                        generated_set.add(val)
                        generated_list.append(val)
                    if len(generated_list) >= row_count:
                        break

            candidates = self.candidate_values.get(tc, [])
            col_type = self.column_types.get(tc, 'str')

            if len(generated_list) < row_count:
                for val in candidates:
                    if val not in generated_set:
                        generated_set.add(val)
                        generated_list.append(val)
                    if len(generated_list) >= row_count:
                        break

            if len(generated_list) >= row_count:
                return [generated_list[:row_count]]

            # Generate new unique values if needed
            min_val, max_val = None, None
            for constraint in value_constraints:
                if 'gte' in constraint:
                    col_name, c_min = constraint['gte']
                    c_table, c_column = col_name.split('.')
                    if (c_table.lower(), c_column.lower()) == tc:
                        min_val = max(min_val, c_min) if min_val is not None else c_min
                if 'gt' in constraint:
                    col_name, c_min = constraint['gt']
                    c_table, c_column = col_name.split('.')
                    if (c_table.lower(), c_column.lower()) == tc:
                        min_val = max(min_val, c_min + 1) if min_val is not None else c_min + 1
                if 'lte' in constraint:
                    col_name, c_max = constraint['lte']
                    c_table, c_column = col_name.split('.')
                    if (c_table.lower(), c_column.lower()) == tc:
                        max_val = min(max_val, c_max) if max_val is not None else c_max
                if 'lt' in constraint:
                    print("==lt constraint found")
                    col_name, c_max = constraint['lt']
                    c_table, c_column = col_name.split('.')
                    if (c_table.lower(), c_column.lower()) == tc:
                        max_val = min(max_val, c_max - 1) if max_val is not None else c_max - 1
                if 'domain' in constraint:
                    col_name, c_min, c_max = constraint['domain']
                    c_table, c_column = col_name.split('.')
                    if (c_table.lower(), c_column.lower()) == tc:
                        min_val = max(min_val, c_min) if min_val is not None else c_min
                        max_val = min(max_val, c_max) if max_val is not None else c_max

            remaining = row_count - len(generated_list)
            if remaining > 0:
                if col_type in self.int_types:
                    if min_val is None:
                        min_val = 0
                    if max_val is None:
                        max_val = min_val + 100000
                    min_val = int(min_val)
                    max_val = int(max_val)
                    candidate = min_val
                    while len(generated_list) < row_count and candidate <= max_val:
                        if candidate not in generated_set:
                            generated_set.add(candidate)
                            generated_list.append(candidate)
                        candidate += 1
                elif col_type in self.float_types:
                    min_val = float(min_val) if min_val is not None else 0.0
                    max_val = float(max_val) if max_val is not None else min_val + 100000.0
                    if max_val < min_val:
                        min_val, max_val = max_val, min_val
                    remaining_slots = row_count - len(generated_list)
                    step = (max_val - min_val) / max(remaining_slots, 1)
                    if step <= 0:
                        step = 1.0
                    idx = 0
                    while len(generated_list) < row_count:
                        value = min_val + idx * step
                        idx += 1
                        if value > max_val and max_val is not None:
                            break
                        value = round(value, 6)
                        if value not in generated_set:
                            generated_set.add(value)
                            generated_list.append(value)
                elif col_type in self.str_types:
                    next_index = len(generated_list)
                    while len(generated_list) < row_count:
                        base = f"unique_{next_index}"
                        suffix = 0
                        candidate = base
                        while candidate in generated_set:
                            suffix += 1
                            candidate = f"{base}_{suffix}"
                        generated_set.add(candidate)
                        generated_list.append(candidate)
                        next_index += 1
                elif col_type in self.date_types:
                    base_date = datetime(2024, 1, 1).date()
                    offset = len(generated_list)
                    while len(generated_list) < row_count:
                        candidate = base_date + timedelta(days=offset)
                        if candidate not in generated_set:
                            generated_set.add(candidate)
                            generated_list.append(candidate)
                        offset += 1
                elif col_type in self.time_types:
                    base_time = datetime(2024, 1, 1, 0, 0, 0)
                    offset = len(generated_list)
                    while len(generated_list) < row_count:
                        candidate = (base_time + timedelta(seconds=offset)).time()
                        if candidate not in generated_set:
                            generated_set.add(candidate)
                            generated_list.append(candidate)
                        offset += 1
                elif col_type in self.datetime_types:
                    base_datetime = datetime(2024, 1, 1, 0, 0, 0)
                    offset = len(generated_list)
                    while len(generated_list) < row_count:
                        candidate = base_datetime + timedelta(seconds=offset)
                        if candidate not in generated_set:
                            generated_set.add(candidate)
                            generated_list.append(candidate)
                        offset += 1

            final_values = generated_list
            if len(final_values) < row_count:
                print(
                    f"  Warning: only {len(final_values)} unique values available for {table_name}, "
                    f"reducing row count from {row_count}"
                )
                self.table_row_count[table_name] = len(final_values)
                row_count = len(final_values)
            if row_count == 0:
                return [[]]
            return [final_values[:row_count]]
        
        else:
            # check if satisfiable
            generated_tuples = set()
            for idx, tc in enumerate(iterable=initial_values[0]):
                current_tuple = tuple(initial_values[col_idx][idx] for col_idx in range(len(unique_tc)))
                generated_tuples.add(current_tuple)
            # print(f"Initial unique tuples for {table_name}: {generated_tuples}")
            if len(generated_tuples) >= row_count:
                return initial_values
            
            # append new combination from candidates first
            candidates_list = []
            for tc in unique_tc:
                candidates = self.candidate_values.get(tc, [])
                candidates_list.append(candidates)
            for prod in product(*candidates_list):
                if len(generated_tuples) >= row_count:
                    break
                if prod not in generated_tuples:
                    generated_tuples.add(prod)
            
            # print("Generating new unique combinations if needed...")
            # generate new combinations if needed
            col_types = [self.column_types.get(tc, 'str') for tc in unique_tc]
            min_max_vals = [(None, None) for _ in unique_tc]
            new_vals = [[] for _ in unique_tc]
            for i, tc in enumerate(unique_tc):
                for constraint in value_constraints:
                    if 'gte' in constraint:
                        col_name, c_min = constraint['gte']
                        c_table, c_column = col_name.split('.')
                        if (c_table.lower(), c_column.lower()) == tc:
                            min_val, max_val = min_max_vals[i]
                            min_max_vals[i] = (max(min_val, c_min) if min_val is not None else c_min, max_val)
                    if 'gt' in constraint:
                        col_name, c_min = constraint['gt']
                        c_table, c_column = col_name.split('.')
                        if (c_table.lower(), c_column.lower()) == tc:
                            min_val, max_val = min_max_vals[i]
                            min_max_vals[i] = (max(min_val, c_min + 1) if min_val is not None else c_min + 1, max_val)
                    if 'lte' in constraint:
                        col_name, c_max = constraint['lte']
                        c_table, c_column = col_name.split('.')
                        if (c_table.lower(), c_column.lower()) == tc:
                            min_val, max_val = min_max_vals[i]
                            min_max_vals[i] = (min_val, min(max_val, c_max) if max_val is not None else c_max)
                    if 'lt' in constraint:
                        col_name, c_max = constraint['lt']
                        c_table, c_column = col_name.split('.')
                        if (c_table.lower(), c_column.lower()) == tc:
                            min_val, max_val = min_max_vals[i]
                            min_max_vals[i] = (min_val, min(max_val, c_max - 1) if max_val is not None else c_max - 1)
                    if 'domain' in constraint:
                        col_name, c_min, c_max = constraint['domain']
                        c_table, c_column = col_name.split('.')
                        if (c_table.lower(), c_column.lower()) == tc:
                            min_val, max_val = min_max_vals[i]
                            min_max_vals[i] = (max(min_val, c_min) if min_val is not None else c_min,
                                               min(max_val, c_max) if max_val is not None else c_max)

            for i, tc in enumerate(unique_tc):
                col_type = col_types[i]
                min_val, max_val = min_max_vals[i]
                if col_type in self.int_types:
                    if min_val is None:
                        min_val = -1000
                    if max_val is None:
                        max_val = min_val + 1000
                    min_val = int(min_val)
                    max_val = int(max_val)
                    new_vals[i] = list(set(list(range(min_val, max_val + 1))) - set(initial_values[i]))
                elif col_type in self.float_types:
                    min_val = float(min_val) if min_val is not None else None
                    max_val = float(max_val) if max_val is not None else None
                    if min_val is None:
                        min_val = -1000.0
                    if max_val is None:
                        max_val = min_val + 1000.0
                    step = (max_val - min_val) / 1000.0
                    float_candidates = [min_val + i * step for i in range(1001)]
                    new_vals[i] = list(set(float_candidates) - set(initial_values[i]))
                elif col_type in self.str_types:
                    base_str = "unique_str_"
                    str_candidates = [f"{base_str}{j}" for j in range(10000)]
                    new_vals[i] = list(set(str_candidates) - set(initial_values[i]))
                elif col_type in self.date_types:
                    base_date = datetime(2024, 1, 1).date()
                    date_candidates = [base_date + timedelta(days=j) for j in range(1000)]
                    new_vals[i] = list(set(date_candidates) - set(initial_values[i]))
                elif col_type in self.time_types:
                    base_time = datetime(2024, 1, 1, 0, 0, 0)
                    time_candidates = [(base_time + timedelta(seconds=j)).time() for j in range(86400)]
                    new_vals[i] = list(set(time_candidates) - set(initial_values[i]))
                elif col_type in self.datetime_types:
                    base_datetime = datetime(2024, 1, 1, 0, 0, 0)
                    datetime_candidates = [base_datetime + timedelta(seconds=j) for j in range(86400)]
                    new_vals[i] = list(set(datetime_candidates) - set(initial_values[i]))

            # print(f"New candidate values for unique constraint on {table_name}: {new_vals}")
            for prod in product(*new_vals):
                if len(generated_tuples) >= row_count:
                    break
                if prod not in generated_tuples:
                    generated_tuples.add(prod)

            # print(f"Total unique tuples generated for {table_name}: {len(generated_tuples)}")
            # Transpose to get list of columns
            result = [[] for _ in unique_tc]
            for tup in generated_tuples:
                for i in range(len(unique_tc)):
                    result[i].append(tup[i])
            final_count = len(generated_tuples)
            if final_count < row_count:
                print(
                    f"  Warning: only {final_count} unique tuples available for {table_name}, "
                    f"reducing row count from {row_count}"
                )
                self.table_row_count[table_name] = final_count
                row_count = final_count
            if row_count == 0:
                return [[] for _ in unique_tc]

            trimmed_result = [col_values[:row_count] for col_values in result]
            # print(f"Generated unique values for {[f'{t}.{c}' for t,c in unique_tc]}: {trimmed_result}")
            return trimmed_result
    
    def generate_data_main(self):
        """each table is a list of tuples"""
        # 1. prepare table data structures
        relevant_tables = self._get_relevant_tables() if (self.gt_query or self.cd_query) else {t['TableName'] for t in self.schema.schema}
        self.relevant_tables = relevant_tables
        all_schema_tables = {t['TableName'] for t in self.schema.schema}
        skipped = all_schema_tables - relevant_tables
        if skipped:
            print(f"Skipping unreferenced tables: {skipped}")

        for table in self.schema.schema:
            table_name = table['TableName']
            column_order = self.schema.extract_all_columns_in_table(table_name=table_name)
            self.table_data[table_name] = [column_order]  # first row is column names
            if table_name not in relevant_tables:
                self.table_row_count[table_name] = 0  # empty — schema exists, no rows
        # print("Constraint-aware row counts:", self.table_row_count)
        # 2. apply heuristics
        # print("Applying value heuristics...")
        # print(self.value_heurs)
        for vh in self.value_heurs:
            self.apply_value_heuristic(vh)
        
        # 2.5 apply operator heuristics (mutate existing candidate values)
        # print("Applying operator heuristics...")
        # print(self.op_heurs)
        for oh in self.op_heurs:
            self.apply_operator_heuristic(oh)

        # print("Candidate values after applying heuristics:")
        # print(self.candidate_values)

        # 3. add default values to candidates
        self.add_default_candidates()

        self.table_row_count = self._compute_constraint_aware_row_counts()
        # skipping tables that are not relevant to the query
        for t in skipped:
            self.table_row_count[t] = 0

        # 4. convert schema to constraints and apply
        all_cols = self.schema.extract_all_columns()
        pk_cols = self.schema.extract_primary_keys()
        
        # NEW: Identify columns involved in relational constraints
        # These should be generated WITH constraint awareness, not filtered after
        relational_constrained_cols = self._get_relational_constrained_columns()
        print("Relational constrained columns:", relational_constrained_cols)
        
        # Columns that participate as foreign keys must be generated after parent columns
        dependent_cols = set(self.fk_parent_map.keys())
        
        # Independent columns EXCLUDE relational constrained columns
        independent_cols = [
            col for col in all_cols 
            if col not in dependent_cols 
            and col not in pk_cols
            and col not in relational_constrained_cols  # NEW: Don't generate these yet
        ]
        # print("Independent columns:", independent_cols)
        # print("Foreign key dependent columns:", dependent_cols)
        # print("Primary key columns:", pk_cols)
        generated_values = {}

        # NEW: Determine table generation order based on dependencies
        table_order = self._get_table_generation_order()
        # print("Table generation order:", table_order)

        # Generate columns table by table in dependency order
        for table_name in table_order:
            # Generate PK columns for this table
            table_pk_cols = [col for col in pk_cols if col[0] == table_name]
            if table_pk_cols:
                pk_values = self.generate_pk_values(table_pk_cols, generated_values)
                for idx, col in enumerate(table_pk_cols):
                    generated_values[col] = pk_values[idx]
            
            # Generate independent columns for this table with unique constraints
            for constraint in self.constraints:
                if 'distinct' in constraint.keys():
                    # print(constraint)
                    unique_col_names = constraint['distinct']
                    # Collect ALL columns in the constraint for this table
                    all_unique_cols = []
                    cols_to_generate = []
                    for col_name in unique_col_names:
                        t, column = col_name.split('.')
                        if t.lower() != table_name.lower():
                            continue
                        key = (t.lower(), column.lower())
                        all_unique_cols.append(key)
                        # Only generate values for columns not already generated (e.g., not PKs)
                        if key not in generated_values:
                            cols_to_generate.append(key)
                    
                    # Process the unique constraint if any columns belong to this table
                    if all_unique_cols:
                        # If some columns are already generated (e.g., PKs), use those values
                        # Otherwise generate new values
                        initial_values = []
                        for uc in all_unique_cols:
                            if uc in generated_values:
                                # Use existing values (e.g., from PK generation)
                                initial_vals = generated_values[uc]
                            else:
                                # Generate new values
                                initial_vals = self.generate_values(uc)
                            initial_values.append(initial_vals)
                        
                        # For single column case, flatten the initial_values
                        if len(all_unique_cols) == 1:
                            initial_values = initial_values[0]
                        
                        # Skip this constraint if ALL columns are already generated as PKs
                        # (composite PK already ensures uniqueness)
                        if not cols_to_generate:
                            print(f"Skipping distinct constraint for {all_unique_cols} - already generated as PK")
                            continue
                        
                        # Generate unique combinations
                        unique_values = self.generate_unique_values(all_unique_cols, initial_values, self.value_constraints)
                        # print("Generated for unique columns", all_unique_cols, "values:", unique_values)
                        
                        # Update generated_values only for columns that weren't already generated
                        for idx, uc in enumerate(all_unique_cols):
                            if uc not in generated_values or uc in cols_to_generate:
                                generated_values[uc] = unique_values[idx]
                            if uc in independent_cols:
                                independent_cols.remove(uc)
            
            # Generate remaining independent columns for this table
            table_independent_cols = [col for col in independent_cols if col[0] == table_name]
            for tc in table_independent_cols:
                if tc not in generated_values:
                    generated_values[tc] = self.generate_values(tc)
                    independent_cols.remove(tc)
            
            # Generate dependent columns for this table
            table_dependent_cols = [col for col in dependent_cols if col[0] == table_name]
            for child_col in table_dependent_cols:
                self._generate_fk_column(child_col, generated_values)

        # NEW: Apply relational constraints (e.g., empId != supervisor)
        if self.relational_constraints:
            generated_values = self._apply_relational_constraints(generated_values)

        # print("Generated values for all columns:")
        # print(generated_values)

        # print("self.table_row_count:", self.table_row_count)

        # transpose column-wise to row-wise
        for table_name in self.table_data.keys():
            column_order = self.table_data[table_name][0]
            for row_idx in range(self.table_row_count[table_name]):
                row = []
                for col_name in column_order:
                    key = (table_name, col_name)
                    if key in generated_values:
                        row.append(generated_values[key][row_idx])
                    else:
                        row.append(None) # default
                self.table_data[table_name].append(row)
            
        # print("Generated table data:")
        # print(self.table_data)
        # print(self.candidate_values)
        return self.table_data

    
    def _get_applicable_columns(self, heuristic: at.ValueHeuristic | at.RelationHeuristic):
        """Given a heuristic, return the list of (table, column) it applies to"""
        if isinstance(heuristic, at.RelationHeuristic):
            tb1 = heuristic.table1.lower() if heuristic.table1 else None
            col1 = heuristic.column1.lower()
            tb2 = heuristic.table2.lower() if heuristic.table2 else None
            col2 = heuristic.column2.lower()
            target_col1, target_col2 = [], []
            if tb1 and col1:
                target_col1 = [(tb1, col1)] if (tb1, col1) in self.all_columns else []
            elif col1 and not tb1:
                target_col1 = [(t, c) for (t, c) in self.all_columns if c == col1]
            if tb2 and col2:
                target_col2 = [(tb2, col2)] if (tb2, col2) in self.all_columns else []
            elif col2 and not tb2:
                target_col2 = [(t, c) for (t, c) in self.all_columns if c == col2]
            return target_col1[0], target_col2[0]

        else:
            tb = heuristic.table.lower() if heuristic.table else None
            col = heuristic.column.lower()
            if col and tb:
                if col == '*':
                    target_cols = [c for c in self.all_columns if c[0] == tb]
                else:
                    target_cols = [(tb, col)] if (tb, col) in self.all_columns else []
            elif col and not tb:
                # search for the table that contains this column
                if col == '*':
                    target_cols = self.all_columns[:]
                else:
                    target_cols = [(t, c) for (t, c) in self.all_columns if c == col]
            else:
                target_cols = []
            return target_cols


    def _get_relevant_tables(self) -> set[str]:
        """Return tables referenced in either query, plus their FK ancestors."""
        import re
        queries = ' '.join(filter(None, [self.gt_query, self.cd_query]))
        all_table_names = [t['TableName'] for t in self.schema.schema]

        queried: set[str] = set()
        for tname in all_table_names:
            if re.search(r'\b' + re.escape(tname) + r'\b', queries, re.IGNORECASE):
                queried.add(tname.lower())

        # Transitively include FK parent tables (required for referential integrity)
        fk_list = self.schema.extract_foreign_keys()  # (child_table, child_col, parent_table, parent_col)
        changed = True
        while changed:
            changed = False
            for child_table, _, parent_table, _ in fk_list:
                if child_table.lower() in queried and parent_table.lower() not in queried:
                    queried.add(parent_table.lower())
                    changed = True

        return {t['TableName'] for t in self.schema.schema if t['TableName'].lower() in queried}

    def _get_table_generation_order(self) -> list[str]:
        """
        Determine table generation order based on foreign key dependencies.
        Returns tables in topological order: parent tables before child tables.
        """
        # Get all tables
        tables = set(self.table_data.keys())
        
        # Build table dependency graph from FK relationships
        fk_relationships = self._extract_foreign_key_relationships()
        table_edges = set()
        for ref_col, fk_col in fk_relationships:
            ref_table, _ = ref_col
            fk_table, _ = fk_col
            if ref_table != fk_table:  # Skip self-references
                # Edge from parent to child (parent must be generated first)
                table_edges.add((ref_table, fk_table))
        
        # Topological sort
        from collections import defaultdict, deque
        
        in_degree = defaultdict(int)
        adj = defaultdict(list)
        
        for table in tables:
            in_degree[table] = 0
        
        for parent, child in table_edges:
            adj[parent].append(child)
            in_degree[child] += 1
        
        queue = deque([t for t in tables if in_degree[t] == 0])
        result = []
        
        while queue:
            table = queue.popleft()
            result.append(table)
            for neighbor in adj[table]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        # Handle cycles or remaining tables
        remaining = tables - set(result)
        result.extend(remaining)
        
        return result

    def _column_dependency_toposort(self):
        """Return topologically sorted equivalence classes for column dependencies.
        Returns a list of sets, where each set contains columns that form an equivalence class (cycle).
        Generation should follow the list order, and within each equivalence class, pick one column first randomly."""
        dep_edges = []
        node_set = set()

        # Get all FK relationships and convert to dependency edges
        fk_relationships = self._extract_foreign_key_relationships()
        for ref_col, fk_col in fk_relationships:
            dep_edges.append((ref_col, fk_col))
            node_set.add(ref_col)
            node_set.add(fk_col)
                
                

        # Build adjacency list and in-degree count
        adj = defaultdict(list)
        in_deg = defaultdict(int)
        for parent, child in dep_edges:
            adj[parent].append(child)
            in_deg[child] += 1
            if parent not in in_deg:
                in_deg[parent] = 0

        # Kahn's algorithm with cycle detection
        queue = deque([node for node in node_set if in_deg[node] == 0])
        result = []
        processed_nodes = set()
        
        while queue:
            node = queue.popleft()
            result.append({node})  # Each independent node forms its own equivalence class
            processed_nodes.add(node)
            
            for neighbor in adj[node]:
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    queue.append(neighbor)

        # Handle remaining nodes (those in cycles)
        remaining_nodes = node_set - processed_nodes
        if remaining_nodes:
            # Find strongly connected components (equivalence classes) using Tarjan's algorithm
            equivalence_classes = self._find_strongly_connected_components(remaining_nodes, adj, dep_edges)
            result.extend(equivalence_classes)
        
        return result

    def _find_strongly_connected_components(self, nodes, adj, edges):
        """Find strongly connected components using Tarjan's algorithm.
        Returns a list of sets, where each set is an equivalence class (SCC)."""
        # Build reverse adjacency list for the remaining nodes
        rev_adj = defaultdict(list)
        for parent, child in edges:
            if parent in nodes and child in nodes:
                rev_adj[child].append(parent)
        
        visited = set()
        finish_order = []
        
        # First DFS to get finish order
        def dfs1(node):
            if node in visited:
                return
            visited.add(node)
            for neighbor in adj[node]:
                if neighbor in nodes:
                    dfs1(neighbor)
            finish_order.append(node)
        
        for node in nodes:
            dfs1(node)
        
        # Second DFS on reversed graph to find SCCs
        visited.clear()
        sccs = []
        
        def dfs2(node, current_scc):
            if node in visited:
                return
            visited.add(node)
            current_scc.add(node)
            for neighbor in rev_adj[node]:
                if neighbor in nodes:
                    dfs2(neighbor, current_scc)
        
        # Process nodes in reverse finish order
        for node in reversed(finish_order):
            if node not in visited:
                current_scc = set()
                dfs2(node, current_scc)
                if current_scc:
                    sccs.append(current_scc)
        
        return sccs
