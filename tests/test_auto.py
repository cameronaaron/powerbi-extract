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


def _fake_url(key):
    import base64
    import json

    encoded = base64.b64encode(json.dumps({"k": key, "t": "x", "c": 6}).encode()).decode().rstrip("=")
    return f"https://app.powerbi.com/view?r={encoded}"


def test_run_bulk_extracts_each_url_into_own_subdir(monkeypatch, tmp_path):
    urls = [_fake_url("key-one"), _fake_url("key-two")]
    dirs_used = []

    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))
    monkeypatch.setattr(
        "powerbi_extract.auto.run_modules",
        lambda modules, config, output_dir, max_workers: dirs_used.append(output_dir),
    )

    results = auto.run_bulk(urls, output_dir=str(tmp_path))

    assert [status for _, status, _ in results] == ["ok", "ok"]
    assert dirs_used == [str(tmp_path / "key-one"), str(tmp_path / "key-two")]


def test_run_bulk_falls_back_to_index_name_for_unparseable_url(monkeypatch, tmp_path):
    dirs_used = []
    monkeypatch.setattr(auto, "discover", lambda **kwargs: (_config(), _modules()))
    monkeypatch.setattr(
        "powerbi_extract.auto.run_modules",
        lambda modules, config, output_dir, max_workers: dirs_used.append(output_dir),
    )

    auto.run_bulk(["https://app.powerbi.com/view?no-r-param"], output_dir=str(tmp_path))

    assert dirs_used == [str(tmp_path / "report_1")]


def test_run_bulk_continues_after_one_report_fails(monkeypatch, tmp_path):
    urls = [_fake_url("bad"), _fake_url("good")]

    def fake_discover(**kwargs):
        if kwargs["url"] == urls[0]:
            raise PowerBIAuthError("expired key")
        return _config(), _modules()

    monkeypatch.setattr(auto, "discover", fake_discover)
    monkeypatch.setattr("powerbi_extract.auto.run_modules", lambda *a, **k: None)

    results = auto.run_bulk(urls, output_dir=str(tmp_path))

    assert [status for _, status, _ in results] == ["failed", "ok"]
    assert "expired key" in results[0][2]


def test_main_urls_file_runs_bulk_extraction(monkeypatch, tmp_path, capsys):
    urls = [_fake_url("aaa"), _fake_url("bbb")]
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("\n".join(urls) + "\nhttps://example.com/not-a-report\n")

    calls = []

    def fake_run_bulk(discovered_urls, **kwargs):
        calls.append(list(discovered_urls))
        return [(u, "ok", 3) for u in discovered_urls]

    monkeypatch.setattr(auto, "run_bulk", fake_run_bulk)

    auto.main(["--urls-file", str(urls_file), "--output-dir", str(tmp_path)])

    assert calls == [urls]
    out = capsys.readouterr().out
    assert "Found 2 report URL(s)" in out
    assert "2 succeeded, 0 failed" in out


def test_main_urls_file_rejects_module_and_save_config(tmp_path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(_fake_url("x"))

    with pytest.raises(SystemExit):
        auto.main(["--urls-file", str(urls_file), "--module", "a"])


def test_main_urls_file_errors_when_none_found(tmp_path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("nothing here")

    with pytest.raises(SystemExit):
        auto.main(["--urls-file", str(urls_file)])


def test_main_urls_file_exits_nonzero_when_all_fail(monkeypatch, tmp_path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(_fake_url("x"))

    monkeypatch.setattr(auto, "run_bulk", lambda urls, **kwargs: [(urls[0], "failed", "boom")])

    with pytest.raises(SystemExit):
        auto.main(["--urls-file", str(urls_file), "--output-dir", str(tmp_path)])
