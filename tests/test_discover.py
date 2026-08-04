import base64
import json

import orjson
import pytest

from powerbi_extract.client import build_payload
from powerbi_extract.discover import (
    CapturedRequest,
    build_config_and_modules,
    captured_requests_from_har,
    discover_from_har,
    load_config,
    resource_key_from_view_url,
    save_config,
)


class _Cfg:
    dataset_id = "ds1"
    report_id = "rep1"
    visual_id = "vis1"
    model_id = 598501


def _body(from_entities, select_columns):
    return build_payload(
        _Cfg(), from_entities=from_entities, select_columns=select_columns, window_config={"Count": 30000}
    )


def _har_entry(method="POST", url="https://x/public/reports/querydata", headers=None, post_text=None):
    entry = {
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()],
        }
    }
    if post_text is not None:
        entry["request"]["postData"] = {"text": post_text}
    return entry


def test_resource_key_from_view_url_decodes_r_param():
    r = base64.b64encode(json.dumps({"k": "the-key", "t": 1, "c": 2}).encode()).decode().rstrip("=")
    url = f"https://app.powerbi.com/view?r={r}"

    assert resource_key_from_view_url(url) == "the-key"


def test_resource_key_from_view_url_missing_param_raises():
    with pytest.raises(ValueError):
        resource_key_from_view_url("https://app.powerbi.com/view")


def test_captured_requests_from_har_filters_and_parses(tmp_path):
    good_body = _body({"u": "Units"}, [("u", "Name", False)])
    har = {
        "log": {
            "entries": [
                _har_entry(method="GET"),
                _har_entry(url="https://x/other/endpoint", post_text="{}"),
                _har_entry(post_text=None),
                _har_entry(post_text="not json"),
                _har_entry(headers={"X-PowerBI-ResourceKey": "key1"}, post_text=orjson.dumps(good_body).decode()),
            ]
        }
    }
    har_path = tmp_path / "report.har"
    har_path.write_bytes(orjson.dumps(har))

    captured = captured_requests_from_har(str(har_path))

    assert len(captured) == 1
    assert captured[0].headers["X-PowerBI-ResourceKey"] == "key1"
    assert captured[0].body["queries"][0]["ApplicationContext"]["DatasetId"] == "ds1"


def test_build_config_and_modules_dedupes_and_reads_headers():
    body_a = _body({"u": "Units"}, [("u", "Name", False), ("u", "Total", True)])
    body_a_dup = _body({"u": "Units"}, [("u", "Name", False), ("u", "Total", True)])
    body_b = _body({"p": "People"}, [("p", "Email", False)])

    captured = [
        CapturedRequest(url="https://x/querydata", headers={"x-powerbi-resourcekey": "abc"}, body=body_a),
        CapturedRequest(url="https://x/querydata", headers={}, body=body_a_dup),
        CapturedRequest(url="https://x/querydata", headers={}, body=body_b),
    ]

    config, modules = build_config_and_modules(captured)

    assert config.dataset_id == "ds1"
    assert config.report_id == "rep1"
    assert config.visual_id == "vis1"
    assert config.resource_key == "abc"
    assert len(modules) == 2
    names = {m.name for m in modules}
    assert names == {"units", "people"}

    units = next(m for m in modules if m.name == "units")
    assert units.select_columns == [("u", "Name", False), ("u", "Total", True)]


def test_build_config_and_modules_empty_raises():
    with pytest.raises(ValueError):
        build_config_and_modules([])


def test_build_config_and_modules_no_usable_modules_raises():
    body = _body({}, [])
    captured = [CapturedRequest(url="https://x", headers={}, body=body)]

    with pytest.raises(ValueError):
        build_config_and_modules(captured)


def test_module_from_query_falls_back_to_index_name_when_no_entities():
    body = _body({}, [])
    query = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]["Query"]
    query["Select"] = [
        {
            "Column": {"Expression": {"SourceRef": {"Source": "u"}}, "Property": "Name"},
            "Name": "u.Name",
        }
    ]
    body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]["Query"] = query
    captured = [CapturedRequest(url="https://x", headers={}, body=body)]

    config, modules = build_config_and_modules(captured)

    assert modules[0].name == "query_0"


def test_module_from_query_skips_unrecognized_select_items():
    body = _body({"u": "Units"}, [("u", "Name", False)])
    query = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]["Query"]
    query["Select"].append({"Aggregation": {}, "Name": "u.Weird"})
    captured = [CapturedRequest(url="https://x", headers={}, body=body)]

    config, modules = build_config_and_modules(captured)

    assert modules[0].select_columns == [("u", "Name", False)]


def test_discover_from_har_end_to_end(tmp_path):
    body = _body({"u": "Units"}, [("u", "Name", False)])
    har = {
        "log": {
            "entries": [
                _har_entry(headers={"X-PowerBI-ResourceKey": "key1"}, post_text=orjson.dumps(body).decode())
            ]
        }
    }
    har_path = tmp_path / "report.har"
    har_path.write_bytes(orjson.dumps(har))

    config, modules = discover_from_har(str(har_path))

    assert config.resource_key == "key1"
    assert modules[0].name == "units"


def test_save_and_load_config_round_trips(tmp_path):
    body = _body({"u": "Units"}, [("u", "Name", False), ("u", "Total", True)])
    captured = [CapturedRequest(url="https://x", headers={"X-PowerBI-ResourceKey": "key1"}, body=body)]
    config, modules = build_config_and_modules(captured)
    path = tmp_path / "saved.json"

    save_config(config, modules, str(path))
    loaded_config, loaded_modules = load_config(str(path))

    assert loaded_config == config
    assert len(loaded_modules) == len(modules)
    assert loaded_modules[0].name == modules[0].name
    assert [tuple(c) for c in loaded_modules[0].select_columns] == modules[0].select_columns
