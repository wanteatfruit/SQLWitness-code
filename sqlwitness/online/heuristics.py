from sqlglot import parse, exp
from sqlwitness.online.schema_handler import SchemaHandler
from sqlwitness.utils import parse_string_to_number
import sqlwitness.heuristic_types as at
import sqlite3
import re

class HeuristicsHandler:
    """for each heuristic, append possible values for a column"""

    def __init__(self, gt_query:str, c_query:str, schema: SchemaHandler, dialect, dialect_gt, dialect_cd):
        self.gt_query = gt_query
        self.c_query = c_query
        try:
            if dialect != None:
                if dialect=="postgresql":
                    dialect = "postgres"
                self.gt_ast = parse(gt_query, dialect=dialect)[0]
                self.c_ast = parse(c_query, dialect=dialect)[0]
            if dialect == None and dialect_cd != None and dialect_gt != None:
                if dialect_gt=="postgresql":
                    dialect_gt = "postgres"
                if dialect_cd=="postgresql":
                    dialect_cd = "postgres"
                self.gt_ast = parse(gt_query, dialect=dialect_gt)[0]
                self.c_ast = parse(c_query, dialect=dialect_cd)[0]
        except Exception as e:
            print("Error parsing SQL queries:", e)
            self.gt_ast = None
            self.c_ast = None
        self.schema = schema
        self.all_columns = schema.extract_all_columns()
        self.column_types = schema.extract_column_types()
        self.str_types = {'text', 'char', 'varchar', 'string', 'str'}
        self.int_types = {'int', 'integer', 'smallint', 'bigint', 'number'}
        self.float_types = {'real', 'float', 'double', 'decimal', 'numeric'}


        self.applicable_vh = [] # heuristics that can be applied to current query
        self.applicable_ch = []
        self.applicable_rh = []
        self.applicable_oh = [] # operator heuristics


    def get_applicable_heuristics(self):
        self.h_null_random()
        self.h_constant_value_boundary()
        self.h_count_cardinality()
        self.h_arithmetic_operators_regex()
        
        return (self.applicable_vh, self.applicable_ch, self.applicable_oh)


    def h_and_cardinality(self):
        max_and_in_where = 0
        max_and_in_join = 0
        for ast in [self.gt_ast, self.c_ast]:
            if ast is not None:
                and_in_where = 0
                for where_node in ast.find_all(exp.Where):
                    condition = where_node.this
                    for ands in condition.find_all(exp.And):
                        and_in_where += 1
                max_and_in_where = max(max_and_in_where, and_in_where)
                and_in_join = 0
                for join_node in ast.find_all(exp.Join):
                    for ands in join_node.find_all(exp.And):
                        and_in_join += 1
                max_and_in_join = max(max_and_in_join, and_in_join)
        total_ands = max(max_and_in_where, max_and_in_join)
        if total_ands > 0:
            ch = at.CardinalityHeuristic(table="", min_card=pow(4, total_ands), max_card=None)
            self.applicable_ch.append(ch)
                        

    def h_count_cardinality(self):
        max_card = 0
        min_card = 2147483647
        for ast in [self.gt_ast, self.c_ast]:
            if ast is not None:
                for node in ast.find_all(exp.Count):
                    parent = node.parent
                    if parent is not None:
                        if isinstance(parent, exp.GT) or isinstance(parent, exp.GTE):
                            right = parent.expression
                            if isinstance(right, exp.Literal):
                                card = parse_string_to_number(right.this)[0]
                                if isinstance(card, int) and card > max_card:
                                    min_card = card
                        elif isinstance(parent, exp.LT) or isinstance(parent, exp.LTE):
                            right = parent.expression
                            if isinstance(right, exp.Literal):
                                card = parse_string_to_number(right.this)[0]
                                if isinstance(card, int) and card < min_card:
                                    max_card = card

        if min_card < 2147483647:
            ch = at.CardinalityHeuristic(table="", min_card=min_card*2, max_card=None)
            self.applicable_ch.append(ch)
        if max_card > 0:
            ch = at.CardinalityHeuristic(table="", min_card=0, max_card=max_card*4)
            self.applicable_ch.append(ch)

                        

    def h_null_random(self):
        pass

    def h_constant_value_only(self):
        literals = {'int': [], 'float': [], 'str': []}
        for ast in [self.gt_ast, self.c_ast]:
            if ast is not None:
                for node in ast.find_all(exp.Literal):
                    if node.is_string:
                        literals['str'].append(node.this.strip("'").strip('"'))
                    else:
                        literal, lit_type = parse_string_to_number(node.this)
                        if lit_type == 'int':
                            literals['int'].append(literal)
                            literals['float'].append(float(literal))
                        elif lit_type == 'float':
                            literals['float'].append(literal)
        for val in set(literals['int']):
            vh_eq = at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(int_value=val), pattern="")
            self.applicable_vh.append(vh_eq)
        for val in set(literals['float']):
            vh_eq = at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(float_value=val), pattern="")
            self.applicable_vh.append(vh_eq)
        for val in set(literals['str']):
            vh_eq = at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(str_value=val), pattern="")
            self.applicable_vh.append(vh_eq)

    _DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

    def h_constant_value_boundary(self):
        literals = {'int': [], 'float': [], 'str': [], 'date': []}
        for ast in [self.gt_ast, self.c_ast]:
            if ast is not None:
                for node in ast.find_all(exp.Literal):
                    if node.is_string:
                        s = node.this.strip("'").strip('"')
                        m = self._DATE_RE.match(s)
                        if m:
                            literals['date'].append((m.group(1), m.group(2), m.group(3)))
                        
                        literals['str'].append(s)
                    else:
                        literal, lit_type = parse_string_to_number(node.this)
                        if lit_type == 'int':
                            literals['int'].append(literal)
                            literals['float'].append(float(literal))
                        elif lit_type == 'float':
                            literals['float'].append(literal)
        for val in set(literals['int']):
            self.applicable_vh.append(
                at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(int_value=val), pattern="")
            )
        for val in set(literals['float']):
            self.applicable_vh.append(
                at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(float_value=val), pattern="")
            )
        for val in set(literals['str']):
            self.applicable_vh.append(
                at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(str_value=val), pattern="")
            )
        for val in set(literals['date']):
            year, month, day = val
            self.applicable_vh.append(
                at.ValueHeuristic(table="", column="*", operator='=',
                                  value=at.Value(date_value=at.Date(year=year, month=month, day=day)),
                                  pattern="")
            )

    def h_arithmetic_operators_regex(self):
        """Extract arithmetic operators (+, -, *, /) from queries using regex and count occurrences"""
        operator_pattern = r'[\+\-\*/]'
        
        operator_max_counts = {}  # operator -> max count
        operator_patterns = {}  # operator -> pattern example
        
        for query in [self.gt_query, self.c_query]:
            if query:
                query_operator_counts = {}
                matches = re.finditer(operator_pattern, query)
                for match in matches:
                    operator = match.group(0)
                    query_operator_counts[operator] = query_operator_counts.get(operator, 0) + 1
                    
                    if operator not in operator_patterns:
                        start = max(0, match.start() - 20)
                        end = min(len(query), match.end() + 20)
                        operator_patterns[operator] = query[start:end].strip()
                
                for operator, count in query_operator_counts.items():
                    operator_max_counts[operator] = max(operator_max_counts.get(operator, 0), count)
        
        for operator, count in operator_max_counts.items():
            pattern = operator_patterns[operator]
            oh = at.OperatorHeuristic(operator=operator, count=count, pattern=pattern)
            self.applicable_oh.append(oh)

    def h_constant_value_regex(self):
        """Extract constant values using regex patterns"""
        literals = {'int': set(), 'float': set(), 'str': set()}
        
        for query in [self.gt_query, self.c_query]:
            if query:
                int_pattern = r'\b(\d+)\b(?!\.\d)'
                for match in re.finditer(int_pattern, query):
                    try:
                        val = int(match.group(1))
                        literals['int'].add(val)
                    except ValueError:
                        pass
                
                float_pattern = r'\b(\d+\.\d+)\b'
                for match in re.finditer(float_pattern, query):
                    try:
                        val = float(match.group(1))
                        literals['float'].add(val)
                    except ValueError:
                        pass
                
                str_pattern = r"'([^']*)'\s*|\"([^\"]*)\""
                for match in re.finditer(str_pattern, query):
                    val = match.group(1) if match.group(1) is not None else match.group(2)
                    if val:  # Only add non-empty strings
                        literals['str'].add(val)
        
        for val in literals['int']:
            vh_eq = at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(int_value=val), pattern="")
            vh_ge = at.ValueHeuristic(table="", column="*", operator='>=', value=at.Value(int_value=val), pattern="")
            vh_gt = at.ValueHeuristic(table="", column="*", operator='>', value=at.Value(int_value=val), pattern="")
            self.applicable_vh.extend([vh_eq, vh_ge, vh_gt])
        
        for val in literals['float']:
            vh_eq = at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(float_value=val), pattern="")
            vh_ge = at.ValueHeuristic(table="", column="*", operator='>=', value=at.Value(float_value=val), pattern="")
            vh_gt = at.ValueHeuristic(table="", column="*", operator='>', value=at.Value(float_value=val), pattern="")
            self.applicable_vh.extend([vh_eq, vh_ge, vh_gt])
        
        for val in literals['str']:
            vh_eq = at.ValueHeuristic(table="", column="*", operator='=', value=at.Value(str_value=val), pattern="")
            self.applicable_vh.append(vh_eq)

    def h_count_cardinality_regex(self):
        """Extract count cardinality constraints using regex patterns and set all as min_card"""
        cardinalities = set()
        
        for query in [self.gt_query, self.c_query]:
            if query:
                count_patterns = [
                    r'COUNT\s*\([^)]*\)\s*(?:>|>=|<|<=|=|!=)\s*(\d+)',  # COUNT(...) op number
                    r'(\d+)\s*(?:>|>=|<|<=|=|!=)\s*COUNT\s*\([^)]*\)'   # number op COUNT(...)
                ]
                
                for pattern in count_patterns:
                    for match in re.finditer(pattern, query, re.IGNORECASE):
                        try:
                            card = int(match.group(1))
                            cardinalities.add(card)
                        except ValueError:
                            pass
        
        for card in cardinalities:
            ch = at.CardinalityHeuristic(table="", min_card=card, max_card=None)
            self.applicable_ch.append(ch)
