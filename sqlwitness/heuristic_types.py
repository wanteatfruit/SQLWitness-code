from typing import Union, List, Optional
from pydantic import BaseModel, Field


class Heuristics(BaseModel):
    cardinality_heuristics: Optional[List["CardinalityHeuristic"]] = Field(default=[], description="List of duplicate heuristics")
    value_heuristics: Optional[List["ValueHeuristic"]] = Field(default=[], description="List of value heuristics")
    relation_heuristics: Optional[List["RelationHeuristic"]] = Field(default=[], description="List of relation heuristics")
    operator_heuristics: Optional[List["OperatorHeuristic"]] = Field(default=[], description="List of operator heuristics")

class Date(BaseModel):
    year: Optional[str] = Field(default=None, description="Year component of the date")
    month: Optional[str] = Field(default=None, description="Month component of the date")
    day: Optional[str] = Field(default=None, description="Day component of the date")

class Value(BaseModel):
    int_value: Optional[int] = Field(default=None, description="Integer value")
    float_value: Optional[float] = Field(default=None, description="Floating point value")
    str_value: Optional[str] = Field(default=None, description="String value")
    date_value: Optional[Date] = Field(default=None, description="Date value")
    null_value: Optional[bool] = Field(default=None, description="Indicates if the value is NULL")

# cardinality heuristic? use cardinality as feedback => increase or decrease cardinality

class CardinalityHeuristic(BaseModel):
    """Heuristic to set the cardinality of a table"""
    table: str = Field(description="Table name, can be omitted")
    max_card: Optional[int] = Field(description="Maximum cardinality, must be a positive integer")
    min_card: Optional[int] = Field(description="Minimum cardinality, must be a non-negative integer")
    # pattern: str = Field(description="The query part that this heuristic targets, output the exact part of the query")

# class DuplicateHeuristic(BaseModel):
#     """Heuristic to have duplicate values in a table"""
#     table: str = Field(description="Table name, can be omitted")
#     column: str = Field(description="Column name, must be a specific column")
#     times: int = Field(description="Number of duplicate rows to create, must be a positive integer")
#     value: Value = Field(description="Value to duplicate, must be compatible with the column type")
#     pattern: str = Field(description="The query part that this heuristic targets, output the exact part of the query")

class ValueHeuristic(BaseModel):
    """Heuristic to set a column to a specific value or range of values"""
    table: str = Field(description="Table name, can be omitted")
    column: str = Field(description="Column name, must be a specific column")
    operator: str = Field(description="Comparison operator, one of '=', '!=', '>', '<', '>=', '<='")
    value: Value = Field(description="Value to compare against, must be compatible with the column type")
    pattern: str = Field(description="The query part that this heuristic targets, output the exact part of the query")

class RelationHeuristic(BaseModel):
    """Heuristic to set a relationship between two columns"""
    table1: str = Field(description="First table name, can be omitted")
    column1: str = Field(description="First column name, must be a specific column")
    operator: str = Field(description="Comparison operator, one of '=', '!=', '>', '<', '>=', '<='")
    table2: str = Field(description="Second table name, can be omitted")
    column2: str = Field(description="Second column name, must be a specific column")
    pattern: str = Field(description="The query part that this heuristic targets, output the exact part of the query")

class OperatorHeuristic(BaseModel):
    """Heuristic to capture arithmetic operators used in the query"""
    operator: str = Field(description="Arithmetic operator, one of '+', '-', '*', '/'")
    count: int = Field(default=1, description="Number of times this operator appears in the query")
    pattern: str = Field(description="The query part that this heuristic targets, output the exact part of the query")
