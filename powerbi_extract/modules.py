"""Declarative description of one table/visual pull."""

from dataclasses import dataclass


@dataclass
class QueryModule:
    name: str
    from_entities: dict
    select_columns: list
    query_template: dict
    output_filename: str
