# SPDX-FileCopyrightText: 2026 Cameron Aaron
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Paginated Power BI public-report query-data client."""

import copy
import time
from dataclasses import dataclass, field
from typing import Optional

import orjson
import polars as pl
import requests

from powerbi_extract.dsr_parser import parse_powerbi_dsr_bytes

DEFAULT_URL = (
    "https://wabi-west-us-c-primary-api.analysis.windows.net/public/reports/querydata?synchronous=true"
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
AUTH_STATUS_CODES = {401, 403}


class PowerBIAuthError(RuntimeError):
    """The report is private or unpublished, or its resource key has expired."""


class PowerBIQueryError(RuntimeError):
    """A query failed partway; nothing was written, since a partial table looks complete."""


@dataclass
class ReportConfig:
    """Everything needed to address one Power BI public report's query endpoint."""

    dataset_id: str
    report_id: str
    visual_id: str
    resource_key: str
    url: str = DEFAULT_URL
    # Per-report. A wrong id is rejected with 401, so leave it unset rather
    # than guess; discovery fills it in from the captured request.
    model_id: Optional[int] = None
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


def is_paginated_template(query_template):
    """True for a Window-shaped (paginated table) query, False for Top-shaped (card/KPI).

    The backend rejects a query whose reduction shape differs from what the
    visual asked for (401, even with a valid key), so the shape must be kept.
    """
    # `or {}` rather than `.get(k, {})`: captures can carry explicit nulls.
    binding = query_template.get("Binding") or {}
    data_reduction = binding.get("DataReduction") or {}
    primary = data_reduction.get("Primary") or {}
    return "Window" in primary


def build_payload(config, query_template, window_config=None):
    """Build a querydata payload by replaying a captured query verbatim.

    Reconstructing the query from scratch gets 401s on reports that scope the
    key with a mandatory filter or select aggregated measures, so the captured
    Query/Select/Where is replayed exactly. ``window_config`` applies only to
    Window-shaped queries; None replays the template's own DataReduction.
    """
    command = copy.deepcopy(query_template)
    if window_config is not None:
        if not command.get("Binding"):
            command["Binding"] = {}
        if not command["Binding"].get("DataReduction"):
            command["Binding"]["DataReduction"] = {"DataVolume": 3}
        command["Binding"]["DataReduction"]["Primary"] = {"Window": window_config}

    payload = {
        "version": "1.0.0",
        "queries": [
            {
                "Query": {"Commands": [{"SemanticQueryDataShapeCommand": command}]},
                "ApplicationContext": {
                    "DatasetId": config.dataset_id,
                    "Sources": [{"ReportId": config.report_id, "VisualId": config.visual_id}],
                },
            }
        ],
        "cancelQueries": [],
    }
    if config.model_id is not None:
        payload["modelId"] = config.model_id
    return payload


def _retry_delay(response, attempt, base_delay):
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return base_delay * (2**attempt)


def _post_with_retries(http, config, payload, max_retries, base_delay, log):
    for attempt in range(max_retries + 1):
        response = http.post(config.url, headers=config.headers(), data=orjson.dumps(payload))
        status = response.status_code
        if status == 200:
            return response

        # Anonymous sessions also answer 401/403 under request bursts, which
        # only a retry can tell apart from a dead key.
        retryable = status in AUTH_STATUS_CODES or status in RETRYABLE_STATUS_CODES
        if retryable and attempt < max_retries:
            delay = _retry_delay(response, attempt, base_delay)
            log(f" -> HTTP {status}, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
            continue

        if status in AUTH_STATUS_CODES:
            raise PowerBIAuthError(
                f"HTTP {status} from Power BI. This report is either not public, or its "
                "resource key has expired. Re-export a fresh HAR/URL capture from the browser."
            )
        return response


def run_paginated_query(
    config,
    module_name,
    query_template,
    output_path,
    page_size=30000,
    sleep_between_pages=0,
    max_retries=4,
    retry_base_delay=1.0,
    session=None,
    log=print,
):
    """Fetch every page of one query module and write it to ``output_path`` as CSV.

    Raises PowerBIQueryError if any page fails; no file is written in that case.
    """
    log(f"\n=== Extracting module: {module_name} ===")

    http = session or requests.Session()
    all_rows = []
    restart_tokens = None
    page = 1
    paginated = is_paginated_template(query_template)

    while True:
        if paginated:
            window_config = {"Count": page_size}
            if restart_tokens:
                window_config["RestartTokens"] = restart_tokens
            payload = build_payload(config, query_template, window_config)
        else:
            payload = build_payload(config, query_template)  # one shot, no RestartTokens

        response = _post_with_retries(http, config, payload, max_retries, retry_base_delay, log)
        if response.status_code != 200:
            raise PowerBIQueryError(
                f"'{module_name}': HTTP {response.status_code} on page {page} "
                f"({len(all_rows)} rows fetched before it were discarded)"
            )

        rows, restart_tokens = parse_powerbi_dsr_bytes(response.content)
        if not rows:
            log(" -> No additional rows returned.")
            break

        # Consecutive pages overlap by one row.
        if all_rows and rows[0] == all_rows[-1]:
            rows = rows[1:]

        all_rows.extend(rows)
        log(f" -> Page {page}: fetched {len(rows)} rows (total so far: {len(all_rows)})")

        if not paginated or not restart_tokens:
            break

        page += 1
        if sleep_between_pages:
            time.sleep(sleep_between_pages)

    if not all_rows:
        log("==> No data captured.")
        return pl.DataFrame()

    df = pl.DataFrame(all_rows, infer_schema_length=None)
    df.write_csv(output_path)
    log(f"==> Saved {df.height} rows to '{output_path}'\n")
    log(df.head(3))
    return df
