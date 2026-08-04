import orjson
import polars as pl

from powerbi_extract.client import ReportConfig, build_payload, run_paginated_query


def _config(**overrides):
    defaults = dict(
        dataset_id="ds1", report_id="rep1", visual_id="vis1", resource_key="key1"
    )
    defaults.update(overrides)
    return ReportConfig(**defaults)


def test_report_config_headers_merge_extra_headers():
    config = _config(extra_headers={"X-Custom": "yes"})
    headers = config.headers()

    assert headers["X-PowerBI-ResourceKey"] == "key1"
    assert headers["X-Custom"] == "yes"
    assert headers["Content-Type"] == "application/json;charset=UTF-8"


def test_build_payload_shapes_column_and_measure_selects():
    config = _config()
    payload = build_payload(
        config,
        from_entities={"u": "Units Table"},
        select_columns=[("u", "Name", False), ("u", "Total", True)],
        window_config={"Count": 500},
    )

    query = payload["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    select = query["Query"]["Select"]
    assert "Column" in select[0]
    assert "Measure" in select[1]
    assert query["Binding"]["DataReduction"]["Primary"]["Window"] == {"Count": 500}
    assert payload["queries"][0]["ApplicationContext"]["DatasetId"] == "ds1"


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.content = orjson.dumps(payload)


def _dsr_payload(rows_c, restart=None, select=None):
    select = select or [
        {"Value": "G0", "Name": "u.Name"},
        {"Value": "G1", "Name": "u.Score"},
    ]
    ds = {
        "ValueDicts": {},
        "PH": [{"DM0": [{"S": [{"N": "G0"}, {"N": "G1"}], "C": c} for c in rows_c]}],
    }
    if restart:
        ds["RT"] = restart
    return {
        "results": [
            {"result": {"data": {"descriptor": {"Select": select}, "dsr": {"DS": [ds]}}}}
        ]
    }


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, headers=None, data=None):
        self.calls += 1
        return self._responses.pop(0)


def test_run_paginated_query_single_page_writes_csv(tmp_path):
    payload = _dsr_payload([["Alice", 1], ["Bob", 2]])
    session = _FakeSession([_FakeResponse(200, payload)])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        from_entities={"u": "Units"},
        select_columns=[("u", "Name", False), ("u", "Score", False)],
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert isinstance(df, pl.DataFrame)
    assert df.height == 2
    assert out.exists()


def test_run_paginated_query_paginates_and_dedupes_boundary_row(tmp_path):
    page1 = _dsr_payload([["Alice", 1], ["Bob", 2]], restart={"tok": 1})
    page2 = _dsr_payload([["Bob", 2], ["Carol", 3]])
    session = _FakeSession([_FakeResponse(200, page1), _FakeResponse(200, page2)])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        from_entities={"u": "Units"},
        select_columns=[("u", "Name", False), ("u", "Score", False)],
        output_path=str(out),
        session=session,
        sleep_between_pages=0,
        log=lambda *a, **k: None,
    )

    assert df.height == 3
    assert session.calls == 2


def test_run_paginated_query_http_error_breaks_and_returns_empty(tmp_path):
    session = _FakeSession([_FakeResponse(500, {})])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        from_entities={"u": "Units"},
        select_columns=[("u", "Name", False)],
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert df.is_empty()
    assert not out.exists()


def test_run_paginated_query_no_rows_returns_empty(tmp_path):
    empty_payload = _dsr_payload([])
    session = _FakeSession([_FakeResponse(200, empty_payload)])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        from_entities={"u": "Units"},
        select_columns=[("u", "Name", False)],
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert df.is_empty()


def test_run_paginated_query_uses_default_session_when_none_given(monkeypatch, tmp_path):
    payload = _dsr_payload([["Alice", 1]])
    fake = _FakeSession([_FakeResponse(200, payload)])
    monkeypatch.setattr("powerbi_extract.client.requests.Session", lambda: fake)
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        from_entities={"u": "Units"},
        select_columns=[("u", "Name", False)],
        output_path=str(out),
        log=lambda *a, **k: None,
    )

    assert df.height == 1
