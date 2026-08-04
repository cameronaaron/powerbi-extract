"""Paginated Power BI public-report query-data client."""

import time
from dataclasses import dataclass, field

import orjson
import polars as pl
import requests

from powerbi_extract.dsr_parser import parse_powerbi_dsr_bytes

DEFAULT_URL = "https://wabi-west-us-c-primary-api.analysis.windows.net/public/reports/querydata?synchronous=true"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
AUTH_STATUS_CODES = {401, 403}


class PowerBIAuthError(RuntimeError):
    """Raised when the report is private, unpublished, or its resource key has expired."""


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


def _retry_delay(response, attempt, base_delay):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return base_delay * (2**attempt)


def _post_with_retries(http, config, payload, max_retries, base_delay, log):
    for attempt in range(max_retries + 1):
        response = http.post(
            config.url,
            headers=config.headers(),
            data=orjson.dumps(payload),
        )

        if response.status_code == 200:
            return response

        if response.status_code in AUTH_STATUS_CODES:
            raise PowerBIAuthError(
                f"HTTP {response.status_code} from Power BI. This report is either not "
                "public, or its resource key has expired — re-export a fresh HAR/URL "
                "capture from the browser."
            )

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            delay = _retry_delay(response, attempt, base_delay)
            log(f" -> HTTP {response.status_code}, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
            continue

        return response


def run_paginated_query(
    config,
    module_name,
    from_entities,
    select_columns,
    output_path,
    page_size=30000,
    sleep_between_pages=0,
    max_retries=4,
    retry_base_delay=1.0,
    session=None,
    log=print,
):
    """Fetch all pages for one query module and write them to ``output_path`` as CSV."""
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
        response = _post_with_retries(http, config, payload, max_retries, retry_base_delay, log)

        if response.status_code != 200:
            log(f" -> ERROR (HTTP {response.status_code}) on page {page}, giving up on this module.")
            break

        rows, restart_tokens = parse_powerbi_dsr_bytes(response.content)
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
        if sleep_between_pages:
            time.sleep(sleep_between_pages)

    if all_rows:
        df = pl.DataFrame(all_rows, infer_schema_length=None)
        df.write_csv(output_path)
        log(f"==> SUCCESS: Saved {df.height} total rows to '{output_path}'\n")
        log(df.head(3))
        return df

    log("==> No data captured.")
    return pl.DataFrame()
