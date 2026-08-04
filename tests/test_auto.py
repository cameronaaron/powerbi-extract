import pytest

from powerbi_extract import auto
from powerbi_extract.client import PowerBIAuthError, ReportConfig
from powerbi_extract.modules import QueryModule


def _config():
    return ReportConfig(dataset_id="d", report_id="r", visual_id="v", resource_key="k")


def _modules():
    return [
        QueryModule(name="a", from_entities={"u": "U"}, select_columns=[("u", "X", False)], output_filename="a.csv"),
        QueryModule(name="b", from_entities={"u": "U"}, select_columns=[("u", "Y", False)], output_filename="b.csv"),
    ]


def test_discover_requires_exactly_one_source():
    with pytest.raises(ValueError):
        auto.discover()
    with pytest.raises(ValueError):
        auto.discover(har="x.har", url="https://x")
    with pytest.raises(ValueError):
        auto.discover(har="x.har", config="c.json")


def test_discover_with_har_delegates(monkeypatch):
    monkeypatch.setattr(auto, "discover_from_har", lambda path: ("cfg", "mods"))

    assert auto.discover(har="report.har") == ("cfg", "mods")


def test_discover_with_config_delegates(monkeypatch):
    monkeypatch.setattr(auto, "load_config", lambda path: ("cfg", "mods"))

    assert auto.discover(config="saved.json") == ("cfg", "mods")


def test_discover_with_url_delegates(monkeypatch):
    import powerbi_extract.browser as browser_module

    monkeypatch.setattr(browser_module, "discover_from_url", lambda url, wait_seconds, headless: ("cfg", "mods"))

    result = auto.discover(url="https://app.powerbi.com/view?r=x", wait_seconds=3, headless=False)

    assert result == ("cfg", "mods")


def test_main_har_runs_all_discovered_modules(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))
    monkeypatch.setattr(
        "powerbi_extract.auto.run_modules",
        lambda modules, config, output_dir, max_workers: calls.append([m.name for m in modules]),
    )

    auto.main(["--har", "report.har", "--output-dir", str(tmp_path)])

    assert calls == [["a", "b"]]


def test_main_selects_specific_module(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))
    monkeypatch.setattr(
        "powerbi_extract.auto.run_modules",
        lambda modules, config, output_dir, max_workers: calls.append([m.name for m in modules]),
    )

    auto.main(["--har", "report.har", "--module", "b", "--output-dir", str(tmp_path)])

    assert calls == [["b"]]


def test_main_errors_on_unknown_module(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))

    with pytest.raises(SystemExit):
        auto.main(["--har", "report.har", "--module", "nope", "--output-dir", str(tmp_path)])

    assert "Unknown module(s): nope" in capsys.readouterr().err


def test_main_requires_har_or_url():
    with pytest.raises(SystemExit):
        auto.main([])


def test_main_rejects_both_har_and_url():
    with pytest.raises(SystemExit):
        auto.main(["--har", "x.har", "--url", "https://x"])


def test_main_saves_config_when_requested(monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))
    monkeypatch.setattr("powerbi_extract.auto.run_modules", lambda *a, **k: None)
    monkeypatch.setattr(auto, "save_config", lambda config, modules, path: saved.append(path))

    save_path = str(tmp_path / "saved.json")
    auto.main(["--har", "report.har", "--output-dir", str(tmp_path), "--save-config", save_path])

    assert saved == [save_path]


def test_main_surfaces_auth_error_as_clean_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))

    def fake_run_modules(*args, **kwargs):
        raise PowerBIAuthError("nope")

    monkeypatch.setattr("powerbi_extract.auto.run_modules", fake_run_modules)

    with pytest.raises(SystemExit):
        auto.main(["--har", "report.har", "--output-dir", str(tmp_path)])

    assert "Error: nope" in capsys.readouterr().err
