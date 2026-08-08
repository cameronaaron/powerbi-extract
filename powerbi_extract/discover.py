"""Auto-discover a ReportConfig + QueryModules from captured querydata requests."""

import base64
import copy
import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

import orjson

from powerbi_extract.client import ReportConfig
from powerbi_extract.modules import QueryModule

QUERYDATA_PATH = "public/reports/querydata"
VIEW_URL_RE = re.compile(r"https://app\.powerbi\.com/view\?r=[^\s\"'<>]+")


def extract_view_urls(text):
    """Pull out deduplicated public report view URLs from arbitrary text (e.g. a URL list file)."""
    seen = set()
    urls = []
    for url in VIEW_URL_RE.findall(text):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


@dataclass
class CapturedRequest:
    """One captured querydata POST: its URL, headers, and decoded JSON body."""

    url: str
    headers: dict
    body: dict


def _header(headers, name):
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def resource_key_from_view_url(view_url):
    """Decode the base64-encoded 'r' query-string param of a public report view URL."""
    parsed = urlparse(view_url)
    r_values = parse_qs(parsed.query).get("r")
    if not r_values:
        raise ValueError(f"No 'r' query-string parameter found in {view_url!r}")
    padded = r_values[0] + "=" * (-len(r_values[0]) % 4)
    decoded = base64.b64decode(padded)
    return json.loads(decoded)["k"]


def captured_requests_from_har(har_path):
    """Read a browser-exported .har file and pull out its querydata POST calls."""
    with open(har_path, "rb") as f:
        har = orjson.loads(f.read())

    captured = []
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = request.get("url", "")
        if request.get("method") != "POST" or QUERYDATA_PATH not in url:
            continue

        post_data = request.get("postData", {}).get("text")
        if not post_data:
            continue
        try:
            body = orjson.loads(post_data)
        except orjson.JSONDecodeError:
            continue

        headers = {h["name"]: h["value"] for h in request.get("headers", [])}
        captured.append(CapturedRequest(url=url, headers=headers, body=body))

    return captured


def _module_from_query(query_container, index):
    command = query_container["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    query = command["Query"]
    from_clause = query.get("From", [])
    if any("Entity" not in item for item in from_clause):
        return None

    from_entities = {item["Name"]: item["Entity"] for item in from_clause}

    select_columns = []
    for item in query.get("Select", []):
        try:
            if "Column" in item:
                expr = item["Column"]
                is_measure = False
            elif "Measure" in item:
                expr = item["Measure"]
                is_measure = True
            elif "Aggregation" in item:
                # An aggregated measure, e.g. CountNonNull(...) — wraps a Column expression.
                inner = item["Aggregation"]["Expression"]
                expr = inner.get("Column", inner)
                is_measure = True
            else:
                continue
            alias = expr["Expression"]["SourceRef"]["Source"]
            prop = expr["Property"]
        except KeyError:
            continue
        select_columns.append((alias, prop, is_measure))

    if not select_columns:
        return None

    entity_part = "_".join(sorted(set(from_entities.values()))) or f"query_{index}"
    column_part = "_".join(prop for _, prop, _ in select_columns)
    base_name = f"{entity_part}_{column_part}" if column_part else entity_part
    name = base_name.lower().replace(" ", "_")
    return QueryModule(
        name=name,
        from_entities=from_entities,
        select_columns=select_columns,
        query_template=copy.deepcopy(command),
        output_filename=f"{name}.csv",
    )


def _dedupe_names(modules):
    seen_counts = {}
    for module in modules:
        seen_counts[module.name] = seen_counts.get(module.name, 0) + 1

    seen_so_far = {}
    for module in modules:
        if seen_counts[module.name] == 1:
            continue
        seen_so_far[module.name] = seen_so_far.get(module.name, 0) + 1
        suffix = seen_so_far[module.name]
        module.name = f"{module.name}_{suffix}"
        module.output_filename = f"{module.name}.csv"
    return modules


def build_config_and_modules(captured_requests):
    """Turn captured querydata requests into a (ReportConfig, [QueryModule]) pair."""
    if not captured_requests:
        raise ValueError("No querydata requests were captured.")

    first = captured_requests[0]
    app_context = first.body["queries"][0]["ApplicationContext"]
    config_kwargs = dict(
        dataset_id=app_context["DatasetId"],
        report_id=app_context["Sources"][0]["ReportId"],
        visual_id=app_context["Sources"][0]["VisualId"],
        resource_key=_header(first.headers, "X-PowerBI-ResourceKey") or "",
        url=first.url,
    )
    # The model ID varies per report/dataset — a hardcoded default gets rejected
    # with 401 PowerBINotAuthorizedException even with a perfectly valid key and
    # query, since it addresses the wrong model entirely.
    if "modelId" in first.body:
        config_kwargs["model_id"] = first.body["modelId"]
    config = ReportConfig(**config_kwargs)

    seen = set()
    modules = []
    for index, captured in enumerate(captured_requests):
        module = _module_from_query(captured.body["queries"][0], index)
        if module is None:
            continue
        # Dedupe on the full query shape (including Where/Aggregation), not just
        # projected columns — two queries can share select columns but differ in
        # their filter, and collapsing them would silently drop one.
        shape_key = orjson.dumps(module.query_template, option=orjson.OPT_SORT_KEYS)
        if shape_key in seen:
            continue
        seen.add(shape_key)
        modules.append(module)

    if not modules:
        raise ValueError("Captured requests didn't yield any usable query modules.")

    return config, _dedupe_names(modules)


def discover_from_har(har_path):
    """Discover a (ReportConfig, [QueryModule]) pair from a browser-exported HAR file."""
    return build_config_and_modules(captured_requests_from_har(har_path))


def save_config(config, modules, path):
    payload = {
        "config": asdict(config),
        "modules": [asdict(module) for module in modules],
    }
    with open(path, "wb") as f:
        f.write(orjson.dumps(payload))


def load_config(path):
    with open(path, "rb") as f:
        payload = orjson.loads(f.read())

    config = ReportConfig(**payload["config"])
    modules = [QueryModule(**module) for module in payload["modules"]]
    return config, modules
