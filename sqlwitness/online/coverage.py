"""Optional SQLFpc service client for query coverage."""

import json
import argparse
import requests
from pathlib import Path
from typing import Dict, List, Any, Optional, Union


SQLFPC_API_URL = "https://in2test.lsi.uniovi.es/tdrules/api/v4/rules"


def parse_datatype(type_str: str) -> tuple[str, Optional[str]]:
    """
    Parse SQLWitness type string to SQLFpc datatype.
    
    Args:
        type_str: Type string like 'int', 'varchar', 'enum,Y,N'
        
    Returns:
        Tuple of (datatype, size) where size is optional
    """
    if ',' in type_str:
        # Handle enum types: 'enum,Y,N' -> 'varchar'
        parts = type_str.split(',')
        if parts[0].lower() == 'enum':
            # Estimate size based on longest enum value
            max_len = max(len(v) for v in parts[1:]) if len(parts) > 1 else 10
            return 'varchar', str(max_len)
        return parts[0], parts[1] if len(parts) > 1 else None
    
    # Handle types with size: 'varchar(100)' -> ('varchar', '100')
    if '(' in type_str:
        base_type = type_str[:type_str.index('(')]
        size = type_str[type_str.index('(')+1:type_str.index(')')]
        return base_type, size
    
    return type_str, None


def sqlwitness_to_sqlfpc_schema(sqlwitness_schema: Union[List[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """
    Transform SQLWitness schema format to SQLFpc schema format.
    
    Args:
        sqlwitness_schema: Schema in SQLWitness format - either:
                          - List of table dicts: [{'TableName': 'Employee', 'PKeys': [...], 'FKeys': [...], 'Others': [...]}]
                          - Dict with 'Tables' key: {'Tables': [{'TableName': ...}]}
        
    Returns:
        Schema in SQLFpc format
    """
    entities = []
    
    # Handle both list format and dict with 'Tables' key for backward compatibility
    tables: List[Dict[str, Any]] = []
    if isinstance(sqlwitness_schema, list):
        tables = sqlwitness_schema
    elif isinstance(sqlwitness_schema, dict) and 'Tables' in sqlwitness_schema:
        tables = sqlwitness_schema['Tables']
    else:
        raise ValueError("Schema must be either a list of tables or a dict with 'Tables' key")
    
    for table in tables:
        table_name = table.get('TableName', '')
        attributes = []
        
        # Process primary keys
        for pkey in table.get('PKeys', []):
            datatype, size = parse_datatype(pkey.get('Type', 'varchar'))
            attr = {
                'name': pkey.get('Name', ''),
                'datatype': datatype,
                'uid': 'true',
                'notnull': 'false'
            }
            if size:
                attr['size'] = size
            attributes.append(attr)
        
        # Process foreign keys
        for fkey in table.get('FKeys', []):
            # FKeys can use either 'Name' or 'FName' for the column name
            fkey_name = fkey.get('Name') or fkey.get('FName', '')
            
            # Try to get type, but if not present, infer from referenced primary key
            # Foreign keys should match the type of the primary key they reference
            fkey_type = fkey.get('Type')
            if not fkey_type:
                # If no type specified, assume 'int' (most common for foreign keys)
                # The SQLFpc service should handle this
                fkey_type = 'int'
            
            datatype, size = parse_datatype(fkey_type)
            attr = {
                'name': fkey_name,
                'datatype': datatype,
                'notnull': 'false'
            }
            if size:
                attr['size'] = size
            # Add foreign key reference info if available
            if 'References' in fkey:
                attr['rid'] = 'true'
                if isinstance(fkey['References'], str):
                    attr['ridname'] = fkey['References']
            # Handle PName/PTable format (references to primary key)
            elif 'PName' in fkey or 'PTable' in fkey:
                attr['rid'] = 'true'
                # PName is the referenced column name, PTable is the table index
                if 'PName' in fkey:
                    attr['ridname'] = fkey['PName']
            attributes.append(attr)
        
        # Process other columns
        for col in table.get('Others', []):
            datatype, size = parse_datatype(col.get('Type', 'varchar'))
            attr = {
                'name': col.get('Name', ''),
                'datatype': datatype,
                'notnull': 'false'
            }
            if size:
                attr['size'] = size
            attributes.append(attr)
        
        entity = {
            'name': table_name,
            'attributes': attributes
        }
        
        entities.append(entity)
    
    return {
        'storetype': '',
        'entities': entities
    }


def get_sqlfpc_queries(query: str, schema: Dict[str, Any], options: str = "") -> Dict[str, Any]:
    """
    Make REST API call to SQLFpc service to get equivalent queries.
    
    Args:
        query: SQL query string
        schema: Schema in SQLFpc format
        options: Optional configuration string
        
    Returns:
        Response from SQLFpc service containing equivalent queries
    """
    request_body = {
        'query': query,
        'schema': schema,
        'options': options
    }
    
    try:
        response = requests.post(
            SQLFPC_API_URL,
            json=request_body,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {
            'error': str(e),
            'rules': []
        }


def extract_queries_from_response(response: Dict[str, Any]) -> List[str]:
    """
    Extract queries from SQLFpc response.
    
    Args:
        response: Response from SQLFpc service
        
    Returns:
        List of query strings
    """
    if 'error' in response and response['error']:
        print(f"Error in SQLFpc response: {response['error']}")
        return []
    
    queries = []
    for rule in response.get('rules', []):
        query = rule.get('query', '')
        if query:
            queries.append(query)
    
    return queries


def get_equivalent_queries(query: str, sqlwitness_schema: Union[List[Dict[str, Any]], Dict[str, Any]], options: str = "") -> List[str]:
    """
    Get equivalent queries from SQLFpc service.
    
    This is the main endpoint function to retrieve equivalent queries.
    
    Args:
        query: SQL query string
        sqlwitness_schema: Schema in SQLWitness format - either:
                          - List of table dicts: [{'TableName': 'Employee', 'PKeys': [...], ...}]
                          - Dict with 'Tables' key: {'Tables': [{'TableName': ...}]}
        options: Optional SQLFpc options
        
    Returns:
        List of equivalent query strings
        
    Example:
        # List format (preferred for __init__.py compatibility)
        schema = [
            {
                'TableName': 'Employee',
                'PKeys': [{'Name': 'id', 'Type': 'int'}],
                'FKeys': [],
                'Others': [{'Name': 'name', 'Type': 'varchar'}]
            }
        ]
        queries = get_equivalent_queries("SELECT * FROM Employee WHERE id = 1", schema)
        
        # Or dict format (for JSON files)
        schema = {
            "Tables": [{
                "TableName": "Employee",
                "PKeys": [{"Name": "id", "Type": "int"}],
                "FKeys": [],
                "Others": [{"Name": "name", "Type": "varchar"}]
            }]
        }
        queries = get_equivalent_queries("SELECT * FROM Employee WHERE id = 1", schema)
    """
    sqlfpc_schema = sqlwitness_to_sqlfpc_schema(sqlwitness_schema)
    response = get_sqlfpc_queries(query, sqlfpc_schema, options)
    
    if 'error' in response and response['error']:
        print(f"Error: {response['error']}")
        return []
    
    return extract_queries_from_response(response)


def load_sqlwitness_schema(schema_path: Path) -> Dict[str, Any]:
    """
    Load SQLWitness schema from JSON file.
    
    Args:
        schema_path: Path to schema JSON file
        
    Returns:
        Parsed schema dictionary
    """
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description='Test query coverage using SQLFpc service'
    )
    parser.add_argument(
        'query',
        help='SQL query to analyze'
    )
    parser.add_argument(
        'schema',
        type=Path,
        help='Path to SQLWitness schema JSON file'
    )
    parser.add_argument(
        '--options',
        default='',
        help='SQLFpc options string'
    )
    parser.add_argument(
        '--output',
        type=Path,
        help='Output file to save queries as JSON'
    )
    
    args = parser.parse_args()
    
    # Load schema and get equivalent queries
    print(f"Loading schema from {args.schema}...")
    sqlwitness_schema = load_sqlwitness_schema(args.schema)
    
    print(f"Query: {args.query}")
    queries = get_equivalent_queries(args.query, sqlwitness_schema, args.options)
    
    # Display results
    print(f"\n{'='*70}")
    print(f"Found {len(queries)} equivalent queries")
    print(f"{'='*70}\n")
    
    if queries:
        for i, query in enumerate(queries, 1):
            print(f"[{i}] {query}")
        
        # Save to file if requested
        if args.output:
            output_data = {
                'original_query': args.query,
                'total_queries': len(queries),
                'queries': queries
            }
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2)
            print(f"\n✓ Saved {len(queries)} queries to {args.output}")
    else:
        print("No equivalent queries found.")


if __name__ == '__main__':
    main()
