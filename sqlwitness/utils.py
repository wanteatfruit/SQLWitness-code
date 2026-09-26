import re
from typing import List, Dict, Any, Optional, Tuple
from sqlglot import parse, exp

class ConstraintHelper:

    def _normalize(self, table, col):
        return (str(table).lower() if table else None, str(col).lower())


    def _parse_col(self, s):
        if not isinstance(s, str):
            return None
        parts = s.split('.', 1)
        if len(parts) == 2:
            return self._normalize(parts[0], parts[1])
        elif s.isidentifier():
            return self._normalize(None, s)
        return None

    def __init__(self, constraints):
        self.distinct_index = set()
        self.gt_index = {}
        self.col_eq_index = {}
        self.not_null_index = set()
        for c in constraints:
            for ctype, vals in c.items():
                if ctype == 'distinct':
                    for v in vals:
                        key = self._parse_col(v)
                        if key:
                            self.distinct_index.add(key)
                elif ctype == 'gt':
                    if len(vals) == 2:
                        col, val = vals
                        key = self._parse_col(col)
                        if key:
                            self.gt_index.setdefault(key, set()).add(val)
                elif ctype == 'eq':
                    if len(vals) == 2:
                        k1 = self._parse_col(vals[0])
                        k2 = self._parse_col(vals[1])
                        if k1 and k2:
                            self.col_eq_index.setdefault(k1, set()).add(k2)
                            self.col_eq_index.setdefault(k2, set()).add(k1)
                elif ctype == ' not_null':
                    col, val = vals # c != NULL
                    key = self._parse_col(col)
                    if key:
                        self.not_null_index.add(key)


    def is_distinct(self, table, col):
        return self._normalize(table, col) in self.distinct_index
    def is_not_null(self, table, col):
        return self._normalize(table, col) in self.not_null_index

    def get_gt_values(self, table, col):
        return self.gt_index.get(self._normalize(table, col), [])

    def get_eq(self, table, col):
        return self.col_eq_index.get(self._normalize(table, col), set())






class FileHelper:
    """Helper class for file operations."""
    
    @staticmethod
    def load_constraints_from_file(file_path: str) -> str:
        """Load constraints from a YAML file and convert to string format."""
        try:
            with open(file_path, 'r') as f:
                content = f.read()
            return content
        except Exception as e:
            print(f"Error loading constraints from {file_path}: {e}")
            return ""


class SQLHelper:
    """Helper class for SQL operations."""
    
    @staticmethod
    def normalize_sql_identifiers(query: str) -> str:
        """Normalize SQL query by converting all identifiers to uppercase."""
        def replace_identifier(match):
            word = match.group(0)
            sql_keywords = {'SELECT', 'FROM', 'WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'OUTER', 
                           'ON', 'GROUP', 'BY', 'HAVING', 'ORDER', 'ASC', 'DESC', 'DISTINCT',
                           'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'AND', 'OR', 'NOT', 'IN', 'EXISTS',
                           'BETWEEN', 'LIKE', 'IS', 'NULL', 'UNION', 'INTERSECT', 'EXCEPT',
                           'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'CAST', 'AS', 'LIMIT', 'OFFSET',
                           'IF', 'IFNULL', 'CONCAT', 'SUBSTRING', 'LENGTH', 'UPPER', 'LOWER',
                           'DATE', 'YEAR', 'MONTH', 'DAY', 'NOW', 'CURDATE', 'CURTIME'}
            
            if word.upper() in sql_keywords:
                return word.upper()
            elif word.replace('_', '').replace('.', '').isalnum():
                return word.upper()
            return word
        
        try:
            normalized = re.sub(r'\b[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*\b', 
                              replace_identifier, query)
            return normalized
        except Exception as e:
            print(f"Warning: Could not normalize SQL query: {e}")
            return query

def parse_string_to_number(s: str):
    """Parse a string to an integer or float if possible."""
    try:
        return int(s), 'int'
    except ValueError:
        try:
            return float(s), 'float'
        except ValueError:
            return s, 'str'
