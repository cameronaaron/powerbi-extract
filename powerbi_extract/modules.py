# SPDX-FileCopyrightText: 2026 Cameron Aaron
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Declarative description of one table/visual pull."""

from dataclasses import dataclass


@dataclass
class QueryModule:
    name: str
    from_entities: dict
    select_columns: list
    query_template: dict
    output_filename: str
