from sqlalchemy import (
    Table, Column, MetaData, BigInteger, Float, String, Text, Boolean,
    ForeignKey, ForeignKeyConstraint, PrimaryKeyConstraint, CheckConstraint, UniqueConstraint, create_engine, text, insert, delete, Date, DateTime, Time
)
from sqlalchemy.schema import CreateTable, DropTable
from sqlalchemy.dialects import mysql, postgresql, sqlite, mssql
from sqlalchemy.engine import Engine, URL, make_url
from typing import List, Dict, Any, Optional, Union
import datetime
import os
from sqlwitness.online.schema_handler import SchemaHandler


class DatabaseRunner:
    
    # TODO: support bigquery and snowflake
    
    @staticmethod
    def connection_url(dialect_name: str) -> URL:
        """Read connection configuration without embedding machine-specific credentials."""
        if dialect_name == "sqlite":
            return make_url("sqlite:///test.db")
        settings = {
            "mysql": ("SQLWITNESS_MYSQL_URL", "mysql+pymysql"),
            "postgresql": ("SQLWITNESS_POSTGRES_URL", "postgresql+psycopg2"),
            "tsql": ("SQLWITNESS_MSSQL_URL", "mssql+pyodbc"),
        }
        if dialect_name not in settings:
            raise ValueError(f"Unsupported dialect: {dialect_name}")
        variable, driver = settings[dialect_name]
        value = os.environ.get(variable)
        if not value:
            raise ValueError(f"Set {variable} to a SQLAlchemy connection URL before using {dialect_name}.")
        url = make_url(value)
        if url.get_backend_name() != driver.split("+")[0]:
            raise ValueError(f"{variable} must use the {driver.split('+')[0]} backend.")
        if not url.database:
            raise ValueError(f"{variable} must include a database name.")
        return url.set(drivername=driver)

    def __init__(self, schema_handler:SchemaHandler, table_data, dialect_name: str = 'postgresql', constraints: list = [], worker_id: Optional[str] = None, relevant_tables: Optional[set] = None):
        """Initialize with target SQL dialect."""
        self.metadata = MetaData()
        self.dialect = self._get_dialect(dialect_name)
        self.dialect_name = dialect_name
        self.type_mapping = self._get_type_mapping()
        self.schema_handler = schema_handler
        self.table_data = table_data
        self.constraints = constraints
        self.relevant_tables = relevant_tables
        self.sqlalchemy_tables = self._schema_to_table()

        self.worker_id = worker_id
        if self.worker_id:
            self.db_name = f"worker_db_{self.worker_id}"
            self.schema_name = f"worker_schema_{self.worker_id}"
            self.db_file = f"worker_db_{self.worker_id}.db"
        else:
            self.db_name = "testdb"
            self.schema_name = "public"
            self.db_file = "test.db"

        self._engine: Optional[Engine] = None

    
    def _get_dialect(self, dialect_name: str):
        """Get SQLAlchemy dialect for target database."""
        dialects = {
            'mysql': mysql.dialect(),
            'postgresql': postgresql.dialect(),
            'sqlite': sqlite.dialect(),
            'tsql': mssql.dialect(),
        }
        return dialects.get(dialect_name.lower(), postgresql.dialect())
    
    def _get_admin_engine(self) -> Optional[Engine]:
        if self.dialect_name == 'mysql':
            return create_engine(self.connection_url('mysql')._replace(database=None))
        if self.dialect_name == 'tsql':
            return create_engine(
                self.connection_url('tsql').set(database='master'),
                execution_options={"isolation_level": "AUTOCOMMIT"},
            )
        return None

    def _get_worker_engine(self) -> Engine:
        if self._engine is not None:
            return self._engine
        if self.dialect_name == 'sqlite':
            url = URL.create('sqlite', database=self.db_file)
        else:
            url = self.connection_url(self.dialect_name)
            if self.worker_id and self.dialect_name in ('mysql', 'tsql'):
                url = url.set(database=self.db_name)
        self._engine = create_engine(url, pool_pre_ping=True)
        return self._engine

    def _get_type_mapping(self):
        """Map common type names to SQLAlchemy types."""
        return {
            'int': BigInteger,
            'numeric': BigInteger,
            'integer': BigInteger,
            'bigint': BigInteger,
            'smallint': BigInteger,
            'float': Float,
            'real': Float,
            'double': Float,
            'decimal': Float,
            'numeric': Float,
            'varchar': String(256),
            'char': String(256),
            'text': Text,
            'string': String(256),
            'str': String(256),
            'bool': Boolean,
            'boolean': Boolean,
            'date': Date,
            'datetime': DateTime,
            'timestamp': DateTime,
            'time': Time,
        }
    
    def _get_sqlalchemy_type(self, type_str: str):
        """Convert type string to SQLAlchemy type."""
        type_lower = type_str.lower().strip()
        
        # Strip any constraint info like varchar(100) -> varchar
        if '(' in type_lower:
            type_lower = type_lower.split('(')[0]
        
        return self.type_mapping.get(type_lower, String(256))
    
    def _parse_constraints_for_table(self, table_name: str) -> List[CheckConstraint]:
        """Parse constraints and generate CheckConstraints for a specific table."""
        table_constraints = []

        def _format_value(value):
            if isinstance(value, int) or isinstance(value, float):
                return str(value)
            else:
                return f"'{value}'"
        
        for constraint in self.constraints:
            constraint_type = list(constraint.keys())[0]
            constraint_data = constraint[constraint_type]
            
            # Helper to check if constraint applies to this table and extract column
            def get_table_column(col_name):
                if '.' in col_name:
                    table, column = col_name.split('.', 1)
                    return (table, column) if table.lower() == table_name.lower() else (None, None)
                return (None, None)
            
            
            # Handle comparison constraints
            if constraint_type in ['gte', 'lte', 'gt', 'lt']:
                col_name, val = constraint_data
                table, column = get_table_column(col_name)
                if table and column:
                    column = column.lower()
                    ops = {'gte': '>=', 'lte': '<=', 'gt': '>', 'lt': '<'}
                    
                    # Check if val is a column reference (contains '.')
                    if isinstance(val, str) and '.' in val:
                        val_table, val_column = val.split('.', 1)
                        # Skip cross-table column comparisons — they can't be expressed
                        # as a single-table CHECK constraint
                        if val_table.lower() != table_name.lower():
                            continue
                        val_column = val_column.lower()
                        constraint = CheckConstraint(
                            f"{column} {ops[constraint_type]} {val_column}",
                            name=f"ck_{table_name}_{column}_{constraint_type}_{val_column}"
                        )
                    else:
                        # It's a constant value - format it properly
                        constraint = CheckConstraint(
                            f"{column} {ops[constraint_type]} {_format_value(val)}",
                            name=f"ck_{table_name}_{column}_{constraint_type}_{_format_value(val)}"
                        )
                    table_constraints.append(constraint)
            
            # Handle equality constraints
            if constraint_type in ['equal', 'neq']:
                col_name, val = constraint_data
                table, column = get_table_column(col_name)
                if table and column:
                    column = column.lower()
                    ops = {'equal': '=', 'neq': '!='}
                    
                    # Check if val is a column reference (contains '.')
                    if isinstance(val, str) and '.' in val:
                        val_table, val_column = val.split('.', 1)
                        # Skip cross-table column comparisons
                        if val_table.lower() != table_name.lower():
                            continue
                        val_column = val_column.lower()
                        constraint = CheckConstraint(
                            f"{column} {ops[constraint_type]} {val_column}",
                            name=f"ck_{table_name}_{column}_{constraint_type}"
                        )
                    else:
                        # It's a constant value - format it properly
                        constraint = CheckConstraint(
                            f"{column} {ops[constraint_type]} {_format_value(val)}",
                            name=f"ck_{table_name}_{column}_{constraint_type}"
                        )
                    table_constraints.append(constraint)
            
            # Handle specific constraint types
            if constraint_type == 'not_null':
                table, column = get_table_column(constraint_data)
                if table and column:
                    column = column.lower()
                    constraint = CheckConstraint(
                        f"{column} IS NOT NULL",
                        name=f"ck_{table_name}_{column}_not_null"
                    )
                    table_constraints.append(constraint)
            
            if constraint_type == 'enum':
                col_name, allowed_values = constraint_data
                table, column = get_table_column(col_name)
                if table and column:
                    column = column.lower()
                    values_expr = ", ".join([_format_value(val) for val in allowed_values])
                    constraint = CheckConstraint(
                        f"{column} IN ({values_expr})",
                        name=f"ck_{table_name}_{column}_enum"
                    )
                    table_constraints.append(constraint)
            
            if constraint_type == 'domain':
                col_name, min_val, max_val = constraint_data
                table, column = get_table_column(col_name)
                if table and column:
                    column = column.lower()
                    constraint = CheckConstraint(
                        f"{column} >= {_format_value(min_val)} AND {column} <= {_format_value(max_val)}",
                        name=f"ck_{table_name}_{column}_domain"
                    )
                    table_constraints.append(constraint)
            
            if constraint_type == 'distinct':
                # Handle unique constraints on individual columns
                # print(constraint_data)
                multi_columns = []
                for col_name in constraint_data:
                    table, column = get_table_column(col_name)
                    if table and column:  # Ensure both are not None
                        multi_columns.append(column.lower())
                if multi_columns:
                    constraint = UniqueConstraint(
                        *multi_columns,
                        name=f"uq_{table_name}_{'_'.join(multi_columns)}"
                    )
                    table_constraints.append(constraint)
        
        return table_constraints
    
    def get_applied_constraints(self) -> Dict[str, List[str]]:
        applied_constraints = {}
        
        for table in self.sqlalchemy_tables:
            table_name = table.name
            constraint_descriptions = []
            
            for constraint in table.constraints:
                if isinstance(constraint, CheckConstraint):
                    constraint_descriptions.append(f"CHECK: {constraint.sqltext}")
                elif hasattr(constraint, 'name') and constraint.name:
                    constraint_descriptions.append(f"{type(constraint).__name__}: {constraint.name}")
            
            if constraint_descriptions:
                applied_constraints[table_name] = constraint_descriptions
        
        return applied_constraints

    def _schema_to_table(self) -> List[Table]:
        tables = self.schema_handler.extract_tables()
        # need to order tables based on foreign key dependencies
        fkyes = self.schema_handler.extract_foreign_keys()
        table_order = []
        visited = set()
        def visit(table):
            if table in visited:
                return
            visited.add(table)
            for child_table, child_col, parent_table, parent_col in fkyes:
                if parent_table == table:
                    visit(child_table)
            table_order.append(table)
        for table in tables:
            visit(table)
        tables = table_order[::-1]  # reverse to get correct order

        sql_alchemy_tables = []
        for table_name in tables:
            columns = []
            col_types = self.schema_handler.extract_column_types()
            pkeys = self.schema_handler.extract_primary_keys()
            fkyes = self.schema_handler.extract_foreign_keys()
            # print(fkyes)
            others = self.schema_handler.extract_other_columns()

            # Track which columns have been added to avoid duplicates
            added_columns = set()

            # Add primary key columns
            for table, col in pkeys:
                if table == table_name:
                    col_type = self._get_sqlalchemy_type(col_types.get((table, col), 'string'))
                    columns.append(Column(col, col_type, primary_key=True, autoincrement=False))
                    added_columns.add(col.lower())
            
            # Add foreign key columns with DB-level FK constraints
            # Insertion order is safe because tables are already in topo order (parents first)
            fk_constraints = []
            for child_table, child_col, parent_table, parent_col in fkyes:
                if child_table == table_name:
                    if child_col.lower() not in added_columns:
                        col_type = self._get_sqlalchemy_type(col_types.get((child_table, child_col), 'string'))
                        columns.append(Column(child_col, col_type, ForeignKey(f"{parent_table}.{parent_col}")))
                        added_columns.add(child_col.lower())
                    else:
                        # Column already added (e.g., it's also a PK) — add FK as table-level constraint
                        fk_constraints.append(
                            ForeignKeyConstraint([child_col], [f"{parent_table}.{parent_col}"])
                        )
            
            # Add other columns
            for table, col in others:
                if table == table_name:
                    # Skip if already added
                    if col.lower() not in added_columns:
                        col_type = self._get_sqlalchemy_type(col_types.get((table, col), 'string'))
                        columns.append(Column(col, col_type))
                        added_columns.add(col.lower())
            
            # Parse and add check constraints for this table
            check_constraints = self._parse_constraints_for_table(table_name)
            
            # Create table with columns, FK constraints, and check constraints
            sql_alchemy_table = Table(table_name, self.metadata, *columns, *fk_constraints, *check_constraints)
            sql_alchemy_tables.append(sql_alchemy_table)
            
        return sql_alchemy_tables
    
    def _generate_create_stmts(self) -> List[str]:
        """Generate CREATE TABLE statements for multiple schemas."""
        statements = []
        # MSSQL does not support IF NOT EXISTS on CREATE TABLE
        use_if_not_exists = self.dialect_name != 'tsql'
        for table in self.sqlalchemy_tables:
            if self.relevant_tables is not None and table.name not in self.relevant_tables:
                continue  # Unreferenced by either query — skip DDL
            create_stmt = CreateTable(table, if_not_exists=use_if_not_exists)
            statements.append(str(create_stmt.compile(dialect=self.dialect)))
        return statements
    
    def _generate_drop_stmts(self, if_exists: bool = True) -> List[str]:
        """Generate DROP TABLE statements for multiple schemas."""
        statements = []
        for table in reversed(self.sqlalchemy_tables):
            if self.relevant_tables is not None and table.name not in self.relevant_tables:
                continue  # Never created — nothing to drop
            drop_stmt = DropTable(table, if_exists=if_exists)
            statements.append(str(drop_stmt.compile(dialect=self.dialect)))
        return statements

    
    def _generate_insert_statements(self, table: Table) -> List[str]:
        if table.name not in self.table_data:
            return []

        rows = self.table_data[table.name]
        # First row is column names
        columns = rows[0]

        # If no data rows, return empty list
        if len(rows) <= 1:
            return []

        # Convert all data rows to dictionaries
        all_row_data = [dict(zip(columns, row)) for row in rows[1:]]

        # Detect self-referential FK columns (e.g. posts.parentid -> posts.id)
        fk_list = self.schema_handler.extract_foreign_keys()
        self_ref_fks = {child_col: parent_col
                        for child_table, child_col, parent_table, parent_col in fk_list
                        if child_table == table.name and parent_table == table.name}

        if not self_ref_fks:
            # Create a single INSERT statement with all rows
            stmt = insert(table).values(all_row_data)
            compiled = stmt.compile(dialect=self.dialect, compile_kwargs={"literal_binds": True})
            return [str(compiled)]

        # Self-referential FK: insert with NULL, then UPDATE to set actual values.
        # This avoids FK violations when the referenced row hasn't been inserted yet.
        statements = []

        nulled_row_data = [
            {k: (None if k in self_ref_fks else v) for k, v in row.items()}
            for row in all_row_data
        ]
        stmt = insert(table).values(nulled_row_data)
        compiled = stmt.compile(dialect=self.dialect, compile_kwargs={"literal_binds": True})
        statements.append(str(compiled))

        # Batch self-referential FK updates: one UPDATE per FK column using CASE
        pkeys = [col for t, col in self.schema_handler.extract_primary_keys() if t == table.name]
        for child_col in self_ref_fks:
            case_parts = []
            pk_vals_to_update = []
            for row in all_row_data:
                actual_val = row.get(child_col)
                if actual_val is None:
                    continue
                # Build the WHEN clause for this row's PK
                pk_conditions = []
                pk_where_vals = []
                for pk_col in pkeys:
                    pk_val = row.get(pk_col)
                    pk_where_vals.append(f"{repr(pk_val) if isinstance(pk_val, str) else pk_val}")
                    pk_conditions.append(f"{pk_col} = {repr(pk_val) if isinstance(pk_val, str) else pk_val}")

                set_val = repr(actual_val) if isinstance(actual_val, str) else actual_val
                if len(pkeys) == 1:
                    case_parts.append(f"WHEN {pk_where_vals[0]} THEN {set_val}")
                    pk_vals_to_update.append(pk_where_vals[0])
                else:
                    # Composite PK: use AND conditions in WHEN
                    case_parts.append(f"WHEN {' AND '.join(pk_conditions)} THEN {set_val}")
                    pk_vals_to_update.append(f"({' AND '.join(pk_conditions)})")

            if not case_parts:
                continue

            if len(pkeys) == 1:
                pk_col = pkeys[0]
                case_expr = f"CASE {pk_col} " + " ".join(case_parts) + " END"
                where_clause = f"{pk_col} IN ({', '.join(pk_vals_to_update)})"
            else:
                # Composite PK: use searched CASE with row-level WHERE
                case_expr = "CASE " + " ".join(
                    p.replace("WHEN ", "WHEN ") for p in case_parts
                ) + f" ELSE {child_col} END"
                where_clause = " OR ".join(
                    f"({' AND '.join(f'{pk} = {row.get(pk)}' for pk in pkeys)})"
                    for row in all_row_data if row.get(child_col) is not None
                )

            statements.append(
                f"UPDATE {table.name} SET {child_col} = {case_expr} WHERE {where_clause}"
            )

        return statements
    
    def _generate_all_inserts(self) -> List[List[str]]:
        """Generate INSERT statements for all tables in the data."""
        all_inserts = []
        for table in self.sqlalchemy_tables:
            all_inserts.append(self._generate_insert_statements(table))
        return all_inserts

    def _create_database_engine(self, connection_string: Optional[str] = None) -> Engine:
        return create_engine(connection_string or self.connection_url(self.dialect_name))

    def _get_connection_string(self, dialect_name: Optional[str] = None) -> str:
        return self.connection_url(dialect_name or self.dialect_name).render_as_string(hide_password=False)

    def execute_sql(self, engine: Engine, sql_statements: List[str]) -> None:
        """Execute a list of SQL statements on the given engine."""
        with engine.connect() as conn:
            for sql in sql_statements:
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    # print(f"{sql};")
                except Exception as e:
                    print(f"✗ Failed: {sql[:50]}... Error: {e}")
    
    def execute_query(self, query: str, connection_string: Optional[str] = None) -> Optional[List[tuple]]:
        """Execute a SELECT query and return results as list of tuples.
        
        Returns:
            List of tuples containing query results.
            Returns None if query execution failed (error occurred).
            Returns [] if query succeeded but produced no rows.
        """
        # engine = self._create_database_engine(connection_string)
        engine  = self._get_worker_engine()
        with engine.connect() as conn:
            try:
                if self.dialect_name == 'postgresql' and self.worker_id:
                    conn.execute(text(f'SET search_path TO {self.schema_name};'))
                result = conn.execute(text(query))
                rows = result.fetchall()
                return [tuple(row) for row in rows]
            except Exception as e:
                print(f"✗ Query failed: {query[:50]}... Error: {e}")
                conn.rollback()
                return None  # Signal error with None instead of empty list
    

    def setup_tables(self) -> None:
        """
        Create the worker's sandbox (if needed) and all tables.
        """
        # 1. Create the sandbox (DB or Schema) if in worker mode
        if self.worker_id:
            if self.dialect_name == 'mysql':
                admin_engine = self._get_admin_engine()
                if admin_engine:
                    self.execute_sql(admin_engine, [f"CREATE DATABASE IF NOT EXISTS {self.db_name};"])
                    admin_engine.dispose()
                
            elif self.dialect_name == 'postgresql':
                # Need to create the schema *before* creating tables
                engine = self._get_worker_engine() # Connects to 'testdb'
                with engine.connect() as conn:
                    try:
                        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {self.schema_name};"))
                        conn.commit()
                    except Exception as e:
                        print(f"✗ Failed to create schema: {e}")
                        conn.rollback()
                engine.dispose()

            elif self.dialect_name == 'tsql':
                admin_engine = self._get_admin_engine()
                if admin_engine:
                    with admin_engine.connect() as conn:
                        conn.execute(text(
                            f"IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = N'{self.db_name}') "
                            f"CREATE DATABASE [{self.db_name}];"
                        ))
                    admin_engine.dispose()

        # 2. Create tables inside the sandbox
        engine = self._get_worker_engine()
        create_statements = self._generate_create_stmts()
        
        with engine.connect() as conn:
            try:
                # Set search_path for Postgres *after* schema is created
                if self.dialect_name == 'postgresql' and self.worker_id:
                    conn.execute(text(f"SET search_path TO {self.schema_name};"))
                
                # Execute all create statements in a single transaction
                for sql in create_statements:
                    conn.execute(text(sql))
                    print(f"{sql};")
                conn.commit()
            except Exception as e:
                print(f"✗ Failed to create tables: {e}")
                conn.rollback()

    def insert_tables(self) -> None:
        """Insert data into all tables in the database."""
        engine = self._get_worker_engine()
        all_inserts = self._generate_all_inserts()

        with engine.connect() as conn:
            try:
                # Set search_path for Postgres
                if self.dialect_name == 'postgresql' and self.worker_id:
                    conn.execute(text(f"SET search_path TO {self.schema_name};"))
                
                # Execute all inserts in a single transaction
                # Each table now has only one INSERT statement with all rows
                for inserts_for_table in all_inserts:
                    for sql in inserts_for_table:
                        # print(f"{sql};")
                        conn.execute(text(sql))
                        # print(f"✓ Inserted data into table.")
                conn.commit()
            except Exception as e:
                print(f"✗ Failed to insert data: {e}")
                conn.rollback()

    def refresh_data(self, table_data: Dict[str, Any]) -> None:
        """Replace all table data without DDL. Deletes existing rows and inserts new data."""
        self.table_data = table_data
        engine = self._get_worker_engine()
        with engine.connect() as conn:
            try:
                if self.dialect_name == 'postgresql' and self.worker_id:
                    conn.execute(text(f"SET search_path TO {self.schema_name};"))
                # Delete all rows (reverse order for FK safety)
                for table in reversed(self.sqlalchemy_tables):
                    conn.execute(delete(table))
                # Insert new data
                all_inserts = self._generate_all_inserts()
                for inserts_for_table in all_inserts:
                    for sql in inserts_for_table:
                        conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                print(f"✗ Failed to refresh data: {e}")
                conn.rollback()

    def cleanup_tables(self) -> None:
        """
        Delete all rows from all tables in the database.
        Note: This is less relevant if using full cleanup_database.
        """
        engine = self._get_worker_engine()
        with engine.connect() as conn:
            try:
                # Set search_path for Postgres
                if self.dialect_name == 'postgresql' and self.worker_id:
                    conn.execute(text(f"SET search_path TO {self.schema_name};"))
                
                for table in reversed(self.sqlalchemy_tables):
                    if self.relevant_tables is not None and table.name not in self.relevant_tables:
                        continue  # Never created — nothing to delete
                    delete_stmt = delete(table)
                    conn.execute(delete_stmt)
                conn.commit()
                # print(f"✓ Deleted all rows from tables.")
            except Exception as e:
                print(f"✗ Failed to delete rows: {e}")
                conn.rollback()
    
    def cleanup_database(self) -> None:
        """
        Drop the entire worker sandbox (DB, Schema, or File).
        If not in worker mode, just drops the tables.
        """
        if self.worker_id:
            # --- Worker Mode: Drop the whole sandbox ---
            if self.dialect_name == 'mysql':
                admin_engine = self._get_admin_engine()
                if admin_engine:
                    self.execute_sql(admin_engine, [f"DROP DATABASE IF EXISTS {self.db_name};"])
                    admin_engine.dispose()
            
            elif self.dialect_name == 'postgresql':
                engine = self._get_worker_engine() # Connects to 'testdb'
                self.execute_sql(engine, [f"DROP SCHEMA IF EXISTS {self.schema_name} CASCADE;"])
                engine.dispose()

            elif self.dialect_name == 'tsql':
                # Drop only tables within the database (keep the DB alive for next iteration).
                # CREATE/DROP DATABASE is very expensive in MSSQL (physical file creation),
                # so we reuse the per-PID database across iterations.
                engine = self._get_worker_engine()
                drop_statements = self._generate_drop_stmts(if_exists=True)
                self.execute_sql(engine, drop_statements)

            elif self.dialect_name == 'sqlite':
                if self._engine is not None:
                    self._engine.dispose()
                if os.path.exists(self.db_file):
                    try:
                        os.remove(self.db_file)
                        # print(f"✓ Removed file: {self.db_file}")
                    except Exception as e:
                        print(f"✗ Failed to remove file {self.db_file}: {e}")
        
        else:
            # --- Non-Worker Mode: Just drop tables ---
            engine = self._get_worker_engine()
            drop_statements = self._generate_drop_stmts(if_exists=True)
            self.execute_sql(engine, drop_statements)
            engine.dispose()

        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # def setup_tables(self, connection_string: Optional[str] = None) -> None:
    #     """Create all tables in the database."""
    #     engine = self._create_database_engine(connection_string)
        
    #     # Generate and execute CREATE TABLE statements
    #     create_statements = self._generate_create_stmts()
    #     self.execute_sql(engine, create_statements)

    # def insert_tables(self, connection_string: Optional[str] = None) -> None:
    #     """Insert data into all tables in the database."""
    #     engine = self._create_database_engine(connection_string)
    #     all_inserts = self._generate_all_inserts()
    #     for inserts in all_inserts:
    #         self.execute_sql(engine, inserts)

    # def cleanup_tables(self, connection_string: Optional[str] = None) -> None:
    #     """Delete all rows from all tables in the database."""
    #     engine = self._create_database_engine(connection_string)
    #     with engine.connect() as conn:
    #         for table in reversed(self.sqlalchemy_tables):
    #             delete_stmt = delete(table)
    #             compiled = delete_stmt.compile(dialect=self.dialect, compile_kwargs={"literal_binds": True})
    #             self.execute_sql(engine, [str(compiled)])
    #             print(f"✓ Deleted all rows from table {table.name}.")
    
    # def cleanup_database(self, connection_string: Optional[str] = None) -> None:
    #     """Drop all tables from the database."""
    #     engine = self._create_database_engine(connection_string)
        
    #     # Generate and execute DROP TABLE statements
    #     drop_statements = self._generate_drop_stmts(if_exists=True)
    #     self.execute_sql(engine, drop_statements)
