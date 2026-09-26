from sqlglot import parse, exp
from typing import List, Dict, Any, Tuple
from collections import defaultdict, deque
class SchemaHandler:
    """Handles primary and foreign key extraction and column dependency sorting."""

    def __init__(self, schema = None) -> None:
        self.schema = schema or []
        self._normalize_table_column_to_lower()
    
    @classmethod
    def from_ast(cls, asts):
        """Create SchemaHandler from list of SQLGlot ASTs. Might not be accurate as DDL has many variants."""
        instance = cls()
        schema = instance._ast_to_polygon_schema(asts)
        return cls(schema)
    
    @classmethod
    def from_schema(cls, schema: List[Dict[str, Any]]):
        """Create SchemaHandler from existing polygon schema representation."""
        return cls(schema)

    def _ast_to_polygon_schema(self,asts):
        def ast_to_schema(ast):
            if not isinstance(ast, exp.Create) or ast.kind != "TABLE":
                raise ValueError("Expected a CREATE TABLE AST")
            
            # Extract table name
            schema = ast.this
            if not isinstance(schema, exp.Schema):
                raise ValueError("Expected Schema in CREATE TABLE")
            
            # Get table name - handle both quoted and unquoted names
            table_obj = schema.this
            if hasattr(table_obj, 'this'):
                table_name = str(table_obj.this)
            else:
                table_name = str(table_obj)
            
            # Remove quotes if present
            table_name = table_name.strip('"\'')
            
            # Initialize result structure
            result = {
                "TableName": table_name,
                "PKeys": [],
                "FKeys": [],
                "Others": []
            }
            
            # Process column definitions
            for expr in schema.expressions:
                if isinstance(expr, exp.ColumnDef):
                    col_name = str(expr.this)
                    
                    # Handle different ways the type might be represented
                    if hasattr(expr.kind, 'this'):
                        if hasattr(expr.kind.this, 'name'):  # Type enum
                            col_type = expr.kind.this.name
                        else:
                            col_type = str(expr.kind.this).replace('Type.', '')
                    else:
                        col_type = str(expr.kind)
                    
                    # Check constraints for this column
                    is_primary_key = False
                    is_foreign_key = False
                    fkey_info = None
                    
                    if expr.constraints:
                        for constraint in expr.constraints:
                            if isinstance(constraint, exp.ColumnConstraint):
                                constraint_kind = constraint.kind
                                
                                # Check for primary key constraint
                                if isinstance(constraint_kind, exp.PrimaryKeyColumnConstraint):
                                    is_primary_key = True
                                
                                # Check for foreign key constraint (REFERENCES)
                                elif isinstance(constraint_kind, exp.Reference):
                                    is_foreign_key = True
                                    # Extract referenced table
                                    ref_table = str(constraint_kind.this.this)
                                    # Assume foreign key references primary key of referenced table
                                    # In practice, you might need to extract the specific column from the REFERENCES clause
                                    fkey_info = {
                                        "FName": col_name,
                                        "PName": col_name,  # Assuming same column name, adjust if needed
                                        "PTable": ref_table
                                    }
                    
                    # Categorize the column
                    if is_primary_key:
                        result["PKeys"].append({"Name": col_name, "Type": col_type.lower()})
                    elif is_foreign_key and fkey_info:
                        result["FKeys"].append(fkey_info)
                        # Also add to Others since FK columns are regular columns
                        result["Others"].append({"Name": col_name, "Type": col_type.lower()})
                    else:
                        result["Others"].append({"Name": col_name, "Type": col_type.lower()})
                
                # Handle table-level primary key definitions
                elif isinstance(expr, exp.PrimaryKey):
                    for pk_expr in expr.expressions:
                        if isinstance(pk_expr, exp.Ordered):
                            # Extract column name from Ordered expression
                            pk_col = pk_expr.this
                            if isinstance(pk_col, exp.Column):
                                pk_name = str(pk_col.this)
                                # Find the column type from Others and move to PKeys
                                for i, other_col in enumerate(result["Others"]):
                                    if other_col["Name"] == pk_name:
                                        result["PKeys"].append(other_col)
                                        result["Others"].pop(i)
                                        break
            
            return result

        db_schemas = []
        table_names = []
        for i, ast in enumerate(asts):
            schema = ast_to_schema(ast)
            table_names.append(schema["TableName"])
            db_schemas.append(schema)

        # normalize PTable to index
        for schema in db_schemas:
            for fkey in schema["FKeys"]:
                if fkey["PTable"] in table_names:
                    fkey["PTable"] = table_names.index(fkey["PTable"])
                else:
                    fkey["PTable"] = None  # Handle case where PTable is not found
        return db_schemas


    def _normalize_table_column_to_lower(self):
        """Normalize all table and column names to lowercase for consistent access."""
        for table in self.schema:
            table["TableName"] = table["TableName"].lower()
            for col in table.get("PKeys", []):
                col["Name"] = col["Name"].lower()
            for col in table.get("Others", []):
                col["Name"] = col["Name"].lower()
            for fkey in table.get("FKeys", []):
                fkey["FName"] = fkey["FName"].lower()
                fkey["PName"] = fkey["PName"].lower()
    
    def extract_tables(self) -> List[str]:
        """Extract table names from the current schema."""
        return [table["TableName"] for table in self.schema]

    def extract_primary_keys(self) -> List[Tuple[str,str]]:
        """Extract primary keys from the current schema."""
        pkeys = []
        for table in self.schema:
            table_name = table["TableName"]
            for pk in table.get("PKeys", []):
                col_name = pk["Name"]
                pkeys.append((table_name, col_name))
        return pkeys

    def extract_foreign_keys(self) -> List[Tuple[str, str, str, str]]:
        """Extract foreign key relationships from the current schema."""
        foreign_keys = []
        
        for table in self.schema:
            table_name = table["TableName"]
            
            for fkey in table.get("FKeys", []):
                child_column = fkey["FName"]
                parent_column = fkey["PName"] 
                parent_table_index = int(fkey["PTable"])
                
                if 0 <= parent_table_index < len(self.schema):
                    parent_table = self.schema[parent_table_index]["TableName"]
                    foreign_keys.append((table_name, child_column, parent_table, parent_column))
        
        return foreign_keys
    
    def extract_all_columns(self) -> List[Tuple[str, str]]:
        """Extract all columns from the current schema."""
        all_columns = []
        for table in self.schema:
            table_name = table["TableName"]
            for col in table.get("PKeys", []) + table.get("Others", []):
                col_name = col["Name"]
                all_columns.append((table_name, col_name))
            for fkey in table.get("FKeys", []):
                col_name = fkey["FName"]
                all_columns.append((table_name, col_name))
        return all_columns
    
    def extract_all_columns_in_table(self, table_name: str) -> List[str]:
        """Extract all columns from a specific table in the current schema."""
        for table in self.schema:
            if table["TableName"] == table_name:
                columns = []
                column_names = set()  # Track unique column names
                
                # Add PKeys
                for col in table.get("PKeys", []):
                    col_name = col["Name"]
                    if col_name not in column_names:
                        columns.append(col_name)
                        column_names.add(col_name)
                
                # Add FKeys (only if not already added as PKey)
                for fkey in table.get("FKeys", []):
                    col_name = fkey["FName"]
                    if col_name not in column_names:
                        columns.append(col_name)
                        column_names.add(col_name)
                
                # Add Others
                for col in table.get("Others", []):
                    col_name = col["Name"]
                    if col_name not in column_names:
                        columns.append(col_name)
                        column_names.add(col_name)
                
                return columns
        return []
    

    def extract_other_columns(self) -> List[Tuple[str, str]]:
        """Extract non-primary key columns from the current schema."""
        other_columns = []
        for table in self.schema:
            table_name = table["TableName"]
            for col in table.get("Others", []):
                col_name = col["Name"]
                other_columns.append((table_name, col_name))
        return other_columns

    def extract_column_types(self) -> Dict[Tuple[str, str], str]:
        """Extract column types from the current schema."""
        col_types = {}
        for table in self.schema:
            table_name = table["TableName"]
            for col in table.get("PKeys", []) + table.get("Others", []):
                col_name = col["Name"]
                col_type = col["Type"]
                col_types[(table_name, col_name)] = col_type
            # for fkey in table.get("FKeys", []):
            #     p_name = fkey["PName"]
            #     col_name = fkey["FName"]
            #     col_types[(table_name, col_name)] = col_types.get((self.schema[int(fkey["PTable"])]["TableName"], p_name), "str")
        # add forigen key after the loop to avoid missing type
        for table in self.schema:
            table_name = table["TableName"]
            for fkey in table.get("FKeys", []):
                p_name = fkey["PName"]
                col_name = fkey["FName"]
                col_types[(table_name, col_name)] = col_types.get((self.schema[int(fkey["PTable"])]["TableName"], p_name), "str")
        # print("Column types:", col_types)
        return col_types
