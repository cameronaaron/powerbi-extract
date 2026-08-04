import pytest

from powerbi_extract.cli import main, run_modules
from powerbi_extract.client import ReportConfig
from powerbi_extract.modules import QueryModule


def _config():
    return ReportConfig(dataset_id="d", report_id="r", visual_id="v", resource_key="k")


def _modules():
    return [
        QueryModule(name="a", from_entities={"u": "U"}, select_columns=[("u", "X", False)], output_filename="a.csv"),
        QueryModule(name="b", from_entities={"u": "U"}, select_columns=[("u", "Y", False)], output_filename="b.csv"),
    ]


def test_run_modules_calls_run_paginated_query_per_module(monkeypatch, tmp_path):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs["module_name"])

    monkeypatch.setattr("powerbi_extract.cli.run_paginated_query", fake_run)

    run_modules(_modules(), _config(), output_dir=str(tmp_path))

    assert calls == ["a", "b"]
    assert tmp_path.exists()


def test_main_runs_all_modules_by_default(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "powerbi_extract.cli.run_paginated_query",
        lambda **kwargs: calls.append(kwargs["module_name"]),
    )

    main(_modules(), _config(), argv=["--output-dir", str(tmp_path)])

    assert calls == ["a", "b"]


def test_main_runs_only_selected_module(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "powerbi_extract.cli.run_paginated_query",
        lambda **kwargs: calls.append(kwargs["module_name"]),
    )

    main(_modules(), _config(), argv=["--module", "b", "--output-dir", str(tmp_path)])

    assert calls == ["b"]


def test_main_errors_on_unknown_module(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(_modules(), _config(), argv=["--module", "nope", "--output-dir", str(tmp_path)])

    err = capsys.readouterr().err
    assert "Unknown module(s): nope" in err
