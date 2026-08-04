"""Paginated Power BI public-report query-data client."""

import time
from dataclasses import dataclass, field

import orjson
import polars as pl
import requests

from powerbi_extract.dsr_parser import parse_powerbi_dsr

DEFAULT_URL = "https://wabi-west-us-c-primary-api.analysis.windows.net/public/reports/querydata?synchronous=true"


@dataclass
class ReportConfig:
    """Everything needed to address one Power BI public report's query endpoint."""

    dataset_id: str
    report_id: str
    visual_id: str
    resource_key: str
    url: str = DEFAULT_URL
    model_id: int = 598501
    extra_headers: dict = field(default_factory=dict)

    def headers(self):
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://app.powerbi.com",
            "Referer": "https://app.powerbi.com/",
            "X-PowerBI-ResourceKey": self.resource_key,
        }
        headers.update(self.extra_headers)
        return headers


def build_payload(config, from_entities, select_columns, window_config):
    from_clause = [
        {"Name": alias, "Entity": entity_name, "Type": 0}
        for alias, entity_name in from_entities.items()
    ]

    select_clause = []
    projections = []

    for idx, (alias, prop, is_measure) in enumerate(select_columns):
        key = "Measure" if is_measure else "Column"
        select_clause.append(
            {
                key: {
                    "Expression": {"SourceRef": {"Source": alias}},
                    "Property": prop,
                },
                "Name": f"{alias}.{prop}",
            }
        )
        projections.append(idx)

    return {
        "version": "1.0.0",
        "queries": [
            {
                "Query": {
                    "Commands": [
                        {
                            "SemanticQueryDataShapeCommand": {
                                "Query": {
                                    "Version": 2,
                                    "From": from_clause,
                                    "Select": select_clause,
                                },
                                "Binding": {
                                    "Primary": {"Groupings": [{"Projections": projections}]},
                                    "DataReduction": {
                                        "DataVolume": 3,
                                        "Primary": {"Window": window_config},
                                    },
                                    "Version": 1,
                                },
                                "ExecutionMetricsKind": 1,
                            }
                        }
                    ]
                },
                "ApplicationContext": {
                    "DatasetId": config.dataset_id,
                    "Sources": [{"ReportId": config.report_id, "VisualId": config.visual_id}],
                },
            }
        ],
        "cancelQueries": [],
        "modelId": config.model_id,
    }


def run_paginated_query(
    config,
    module_name,
    from_entities,
    select_columns,
    output_path,
    page_size=30000,
    sleep_between_pages=0.3,
    session=None,
    log=print,
):
    """Fetch all pages for one query module and write them to ``output_path`` as CSV.

    Returns the resulting :class:`polars.DataFrame` (empty if nothing was captured).
    """
    log(f"\n==========================================")
    log(f"Extracting Module: {module_name}")
    log(f"==========================================")

    http = session or requests.Session()
    all_rows = []
    restart_tokens = None
    page = 1

    while True:
        window_config = {"Count": page_size}
        if restart_tokens:
            window_config["RestartTokens"] = restart_tokens

        payload = build_payload(config, from_entities, select_columns, window_config)
        response = http.post(
            config.url,
            headers=config.headers(),
            data=orjson.dumps(payload),
        )

        if response.status_code != 200:
            log(f" -> ERROR (HTTP {response.status_code}) on page {page}")
            break

        rows, restart_tokens = parse_powerbi_dsr(orjson.loads(response.content))
        if not rows:
            log(" -> No additional rows returned.")
            break

        if all_rows and rows[0] == all_rows[-1]:
            rows = rows[1:]

        all_rows.extend(rows)
        log(f" -> Page {page}: Fetched {len(rows)} rows (Total so far: {len(all_rows)})")

        if not restart_tokens:
            break

        page += 1
        time.sleep(sleep_between_pages)

    if all_rows:
        df = pl.DataFrame(all_rows, infer_schema_length=None)
        df.write_csv(output_path)
        log(f"==> SUCCESS: Saved {df.height} total rows to '{output_path}'\n")
        log(df.head(3))
        return df

    log("==> No data captured.")
    return pl.DataFrame()
