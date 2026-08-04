"""Auto-discover a ReportConfig + QueryModules from captured querydata requests."""

import base64
import json
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

import orjson

from powerbi_extract.client import ReportConfig
from powerbi_extract.modules import QueryModule

QUERYDATA_PATH = "public/reports/querydata"


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
    query = query_container["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]["Query"]
    from_entities = {item["Name"]: item["Entity"] for item in query.get("From", [])}

    select_columns = []
    for item in query.get("Select", []):
        if "Column" in item:
            key, is_measure = "Column", False
        elif "Measure" in item:
            key, is_measure = "Measure", True
        else:
            continue
        alias = item[key]["Expression"]["SourceRef"]["Source"]
        prop = item[key]["Property"]
        select_columns.append((alias, prop, is_measure))

    entity_names = "_".join(sorted(set(from_entities.values()))) or f"query_{index}"
    name = entity_names.lower().replace(" ", "_")
    return QueryModule(
        name=name,
        from_entities=from_entities,
        select_columns=select_columns,
        output_filename=f"{name}.csv",
    )


def build_config_and_modules(captured_requests):
    """Turn captured querydata requests into a (ReportConfig, [QueryModule]) pair."""
    if not captured_requests:
        raise ValueError("No querydata requests were captured.")

    first = captured_requests[0]
    app_context = first.body["queries"][0]["ApplicationContext"]
    config = ReportConfig(
        dataset_id=app_context["DatasetId"],
        report_id=app_context["Sources"][0]["ReportId"],
        visual_id=app_context["Sources"][0]["VisualId"],
        resource_key=_header(first.headers, "X-PowerBI-ResourceKey") or "",
        url=first.url,
    )

    seen = set()
    modules = []
    for index, captured in enumerate(captured_requests):
        module = _module_from_query(captured.body["queries"][0], index)
        shape_key = (tuple(sorted(module.from_entities.items())), tuple(module.select_columns))
        if shape_key in seen or not module.select_columns:
            continue
        seen.add(shape_key)
        modules.append(module)

    if not modules:
        raise ValueError("Captured requests didn't yield any usable query modules.")

    return config, modules


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
