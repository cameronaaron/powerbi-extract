"""Report-agnostic Power BI public-report data extraction."""

from powerbi_extract.client import PowerBIAuthError, ReportConfig, build_payload, run_paginated_query
from powerbi_extract.discover import (
    build_config_and_modules,
    discover_from_har,
    load_config,
    resource_key_from_view_url,
    save_config,
)
from powerbi_extract.dsr_parser import parse_powerbi_dsr, parse_powerbi_dsr_bytes
from powerbi_extract.modules import QueryModule

__all__ = [
    "ReportConfig",
    "QueryModule",
    "PowerBIAuthError",
    "build_payload",
    "run_paginated_query",
    "parse_powerbi_dsr",
    "parse_powerbi_dsr_bytes",
    "discover_from_har",
    "build_config_and_modules",
    "resource_key_from_view_url",
    "save_config",
    "load_config",
]
