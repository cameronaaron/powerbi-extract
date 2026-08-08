"""Paginated Power BI public-report query-data client."""

import copy
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


def is_paginated_template(query_template):
    """Whether a captured query's DataReduction is Window-shaped (a table visual)
    rather than Top-shaped (a single-value card/KPI measure).

    Forcing a Window reduction onto a Top-shaped query gets rejected with 401
    PowerBINotAuthorizedException even with a perfectly valid resource key — the
    backend checks that the reduction shape matches what the visual actually
    asked for, not just the key. Top-shaped queries return their (bounded)
    result in one shot and have no RestartTokens/pagination concept.
    """
    # `.get(key, {})` only falls back when the key is absent — some captured
    # queries carry `"DataReduction": null` explicitly, and chaining `.get` on
    # that None crashes, so fall back on falsy values too, not just missing keys.
    binding = query_template.get("Binding") or {}
    data_reduction = binding.get("DataReduction") or {}
    primary = data_reduction.get("Primary") or {}
    return "Window" in primary


def build_payload(config, query_template, window_config=None):
    """Build a querydata payload by replaying a captured query verbatim.

    Reports that scope their resource key with a mandatory filter (Where clause)
    or that select aggregated measures reject a from-scratch reconstruction of
    the query with 401 PowerBINotAuthorizedException, even though the resource
    key itself is valid — the backend checks the query shape, not just the key.
    Replaying the exact captured Query/Select/Where keeps every report's queries
    authorized. ``window_config`` is only applied for Window-shaped (paginated
    table) queries; pass None to replay the template's own DataReduction as-is
    (required for Top-shaped single-value queries — see ``is_paginated_template``).
    """
    command = copy.deepcopy(query_template)
    if window_config is not None:
        # setdefault only fills in a missing key — a captured query with an
        # explicit `"Binding": null` or `"DataReduction": null` needs the same
        # None-vs-missing handling as is_paginated_template above.
        if not command.get("Binding"):
            command["Binding"] = {}
        if not command["Binding"].get("DataReduction"):
            command["Binding"]["DataReduction"] = {"DataVolume": 3}
        command["Binding"]["DataReduction"]["Primary"] = {"Window": window_config}

    return {
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
            # A resource key that's genuinely dead fails on the very first request.
            # Anonymous public-report sessions also return 401/403 under request
            # bursts (concurrent modules, fast pagination) — indistinguishable from
            # a dead key except by retrying, so treat it like the other transient
            # codes before giving up for good.
            if attempt < max_retries:
                delay = _retry_delay(response, attempt, base_delay)
                log(
                    f" -> HTTP {response.status_code} (possible throttling), retrying in "
                    f"{delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                continue
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
    query_template,
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
    paginated = is_paginated_template(query_template)

    while True:
        if paginated:
            window_config = {"Count": page_size}
            if restart_tokens:
                window_config["RestartTokens"] = restart_tokens
            payload = build_payload(config, query_template, window_config)
        else:
            # Top-shaped (single-value) query — replay its own DataReduction as
            # captured; it has no RestartTokens concept, so this is always one shot.
            payload = build_payload(config, query_template)

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

        if not paginated or not restart_tokens:
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
