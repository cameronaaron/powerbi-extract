import orjson
import polars as pl
import pytest

from powerbi_extract.client import (
    PowerBIAuthError,
    ReportConfig,
    build_payload,
    is_paginated_template,
    run_paginated_query,
)


def _config(**overrides):
    defaults = dict(
        dataset_id="ds1", report_id="rep1", visual_id="vis1", resource_key="key1"
    )
    defaults.update(overrides)
    return ReportConfig(**defaults)


def _template(from_entities, select_columns):
    from_clause = [{"Name": alias, "Entity": entity, "Type": 0} for alias, entity in from_entities.items()]
    select_clause = []
    projections = []
    for idx, (alias, prop, is_measure) in enumerate(select_columns):
        key = "Measure" if is_measure else "Column"
        select_clause.append(
            {"Column" if key == "Column" else "Measure": {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop}, "Name": f"{alias}.{prop}"}
        )
        projections.append(idx)
    return {
        "Query": {"Version": 2, "From": from_clause, "Select": select_clause},
        "Binding": {"Primary": {"Groupings": [{"Projections": projections}]}, "DataReduction": {"DataVolume": 3, "Primary": {"Window": {}}}, "Version": 1},
        "ExecutionMetricsKind": 1,
    }


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
        query_template=_template({"u": "Units Table"}, [("u", "Name", False), ("u", "Total", True)]),
        window_config={"Count": 500},
    )

    query = payload["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    select = query["Query"]["Select"]
    assert "Column" in select[0]
    assert "Measure" in select[1]
    assert query["Binding"]["DataReduction"]["Primary"]["Window"] == {"Count": 500}
    assert payload["queries"][0]["ApplicationContext"]["DatasetId"] == "ds1"


def test_build_payload_preserves_where_clause_from_captured_template():
    # Some reports scope their resource key to a mandatory filter (RLS-style):
    # replaying a query without it gets rejected as unauthorized even though the
    # key itself is valid, so the template's Where clause must survive untouched.
    config = _config()
    template = _template({"u": "Units"}, [("u", "Name", False)])
    template["Query"]["Where"] = [{"Condition": {"In": {"Expressions": [], "Values": []}}}]

    payload = build_payload(config, query_template=template, window_config={"Count": 500})

    query = payload["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    assert query["Query"]["Where"] == template["Query"]["Where"]


def test_build_payload_does_not_mutate_the_template():
    config = _config()
    template = _template({"u": "Units"}, [("u", "Name", False)])
    original = orjson.dumps(template, option=orjson.OPT_SORT_KEYS)

    build_payload(config, query_template=template, window_config={"Count": 500})

    assert orjson.dumps(template, option=orjson.OPT_SORT_KEYS) == original


def test_build_payload_without_window_config_replays_original_reduction():
    # Single-value/card measures are captured with a Top-shaped reduction, not
    # Window — forcing a Window onto them gets rejected as unauthorized by Power
    # BI even with a valid key, so replaying them must leave Primary untouched.
    config = _config()
    template = _template({"u": "Units"}, [("u", "Total", True)])
    template["Binding"]["DataReduction"] = {"DataVolume": 3, "Primary": {"Top": {}}}

    payload = build_payload(config, query_template=template)

    query = payload["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    assert query["Binding"]["DataReduction"]["Primary"] == {"Top": {}}


def test_is_paginated_template_detects_window_vs_top():
    windowed = _template({"u": "Units"}, [("u", "Name", False)])
    assert is_paginated_template(windowed) is True

    top_shaped = _template({"u": "Units"}, [("u", "Total", True)])
    top_shaped["Binding"]["DataReduction"] = {"DataVolume": 3, "Primary": {"Top": {}}}
    assert is_paginated_template(top_shaped) is False


def test_is_paginated_template_tolerates_explicit_none_values():
    # Some captured queries carry `"DataReduction": null` (or no Binding at all)
    # rather than omitting the key — `.get(key, {})` doesn't fall back for a
    # present-but-None value, so this must not raise.
    assert is_paginated_template({}) is False
    assert is_paginated_template({"Binding": None}) is False
    assert is_paginated_template({"Binding": {"DataReduction": None}}) is False
    assert is_paginated_template({"Binding": {"DataReduction": {"Primary": None}}}) is False


def test_build_payload_tolerates_explicit_none_binding():
    config = _config()
    template = {"Query": {"Version": 2, "From": [], "Select": []}, "Binding": None}

    payload = build_payload(config, query_template=template, window_config={"Count": 500})

    query = payload["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    assert query["Binding"]["DataReduction"]["Primary"] == {"Window": {"Count": 500}}


class _FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.content = orjson.dumps(payload)
        self.headers = headers or {}


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
        query_template=_template({"u": "Units"}, [("u", "Name", False), ("u", "Score", False)]),
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert isinstance(df, pl.DataFrame)
    assert df.height == 2
    assert out.exists()


def test_run_paginated_query_top_shaped_module_sends_one_request_and_ignores_restart(tmp_path):
    # Even if a (malformed) response carried a restart token, a Top-shaped query
    # has no pagination concept and must not loop or force a Window reduction.
    template = _template({"u": "Units"}, [("u", "Total", True)])
    template["Binding"]["DataReduction"] = {"DataVolume": 3, "Primary": {"Top": {}}}
    payload = _dsr_payload([["Alice", 1]], restart={"tok": 1})
    session = _FakeSession([_FakeResponse(200, payload)])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=template,
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert df.height == 1
    assert session.calls == 1


def test_run_paginated_query_paginates_and_dedupes_boundary_row(tmp_path):
    page1 = _dsr_payload([["Alice", 1], ["Bob", 2]], restart={"tok": 1})
    page2 = _dsr_payload([["Bob", 2], ["Carol", 3]])
    session = _FakeSession([_FakeResponse(200, page1), _FakeResponse(200, page2)])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False), ("u", "Score", False)]),
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert df.height == 3
    assert session.calls == 2


def test_run_paginated_query_http_error_breaks_and_returns_empty(tmp_path):
    session = _FakeSession([_FakeResponse(500, {})] * 5)
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
        output_path=str(out),
        session=session,
        max_retries=0,
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
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
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
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
        output_path=str(out),
        log=lambda *a, **k: None,
    )

    assert df.height == 1


def test_run_paginated_query_retries_transient_errors_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("powerbi_extract.client.time.sleep", lambda *a, **k: None)
    payload = _dsr_payload([["Alice", 1]])
    session = _FakeSession(
        [
            _FakeResponse(503, {}),
            _FakeResponse(429, {}, headers={"Retry-After": "0.1"}),
            _FakeResponse(200, payload),
        ]
    )
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
        output_path=str(out),
        session=session,
        log=lambda *a, **k: None,
    )

    assert df.height == 1
    assert session.calls == 3


def test_run_paginated_query_retries_use_exponential_backoff_without_retry_after(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr("powerbi_extract.client.time.sleep", lambda d: delays.append(d))
    payload = _dsr_payload([["Alice", 1]])
    session = _FakeSession([_FakeResponse(500, {}), _FakeResponse(200, payload)])
    out = tmp_path / "out.csv"

    run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
        output_path=str(out),
        session=session,
        retry_base_delay=1.0,
        log=lambda *a, **k: None,
    )

    assert delays == [1.0]


def test_run_paginated_query_falls_back_to_backoff_on_unparseable_retry_after(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr("powerbi_extract.client.time.sleep", lambda d: delays.append(d))
    payload = _dsr_payload([["Alice", 1]])
    session = _FakeSession(
        [_FakeResponse(429, {}, headers={"Retry-After": "not-a-number"}), _FakeResponse(200, payload)]
    )
    out = tmp_path / "out.csv"

    run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
        output_path=str(out),
        session=session,
        retry_base_delay=2.0,
        log=lambda *a, **k: None,
    )

    assert delays == [2.0]


def test_run_paginated_query_sleeps_between_pages_when_configured(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("powerbi_extract.client.time.sleep", lambda d: sleeps.append(d))
    page1 = _dsr_payload([["Alice", 1]], restart={"tok": 1})
    page2 = _dsr_payload([["Bob", 2]])
    session = _FakeSession([_FakeResponse(200, page1), _FakeResponse(200, page2)])
    out = tmp_path / "out.csv"

    run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False)]),
        output_path=str(out),
        session=session,
        sleep_between_pages=0.5,
        log=lambda *a, **k: None,
    )

    assert sleeps == [0.5]


def test_run_paginated_query_raises_auth_error_on_401():
    session = _FakeSession([_FakeResponse(401, {})])

    with pytest.raises(PowerBIAuthError):
        run_paginated_query(
            config=_config(),
            module_name="mod",
            query_template=_template({"u": "Units"}, [("u", "Name", False)]),
            output_path="unused.csv",
            session=session,
            max_retries=0,
            log=lambda *a, **k: None,
        )


def test_run_paginated_query_retries_401_before_raising_auth_error():
    session = _FakeSession([_FakeResponse(401, {}), _FakeResponse(401, {})])

    with pytest.raises(PowerBIAuthError):
        run_paginated_query(
            config=_config(),
            module_name="mod",
            query_template=_template({"u": "Units"}, [("u", "Name", False)]),
            output_path="unused.csv",
            session=session,
            max_retries=1,
            retry_base_delay=0,
            log=lambda *a, **k: None,
        )

    assert session.calls == 2


def test_run_paginated_query_recovers_from_401_throttle(tmp_path):
    payload = _dsr_payload([["Alice", 1]])
    session = _FakeSession([_FakeResponse(401, {}), _FakeResponse(200, payload)])
    out = tmp_path / "out.csv"

    df = run_paginated_query(
        config=_config(),
        module_name="mod",
        query_template=_template({"u": "Units"}, [("u", "Name", False), ("u", "Score", False)]),
        output_path=str(out),
        session=session,
        max_retries=1,
        retry_base_delay=0,
        log=lambda *a, **k: None,
    )

    assert df.height == 1


def test_run_paginated_query_raises_auth_error_on_403():
    session = _FakeSession([_FakeResponse(403, {})])

    with pytest.raises(PowerBIAuthError):
        run_paginated_query(
            config=_config(),
            module_name="mod",
            query_template=_template({"u": "Units"}, [("u", "Name", False)]),
            output_path="unused.csv",
            session=session,
            max_retries=0,
            log=lambda *a, **k: None,
        )
