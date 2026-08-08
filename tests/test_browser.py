import orjson
import pytest

from powerbi_extract import browser
from powerbi_extract.client import build_payload


class _Cfg:
    dataset_id = "ds1"
    report_id = "rep1"
    visual_id = "vis1"
    model_id = 598501


def _template(from_entities, select_columns):
    from_clause = [{"Name": alias, "Entity": entity, "Type": 0} for alias, entity in from_entities.items()]
    select_clause = []
    projections = []
    for idx, (alias, prop, is_measure) in enumerate(select_columns):
        key = "Measure" if is_measure else "Column"
        select_clause.append(
            {key: {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop}, "Name": f"{alias}.{prop}"}
        )
        projections.append(idx)
    return {
        "Query": {"Version": 2, "From": from_clause, "Select": select_clause},
        "Binding": {"Primary": {"Groupings": [{"Projections": projections}]}, "DataReduction": {"DataVolume": 3, "Primary": {"Window": {}}}, "Version": 1},
        "ExecutionMetricsKind": 1,
    }


class _FakeRequest:
    def __init__(self, method, url, headers=None, post_data=None):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.post_data = post_data


class _FakeMouse:
    def __init__(self):
        self.wheel_calls = []

    def wheel(self, dx, dy):
        self.wheel_calls.append((dx, dy))


class _FakeTab:
    def __init__(self, raises=False):
        self.raises = raises
        self.clicked = False

    def click(self, timeout=None):
        if self.raises:
            raise TimeoutError("not clickable")
        self.clicked = True


class _FakePage:
    def __init__(self, requests_to_fire, tabs=None):
        self._requests_to_fire = requests_to_fire
        self._handler = None
        self.goto_calls = []
        self.waited_ms = None
        self.mouse = _FakeMouse()
        self._tabs = tabs or []

    def on(self, event, handler):
        assert event == "request"
        self._handler = handler

    def goto(self, url, wait_until=None):
        self.goto_calls.append((url, wait_until))
        for request in self._requests_to_fire:
            self._handler(request)

    def wait_for_timeout(self, ms):
        self.waited_ms = ms

    def query_selector_all(self, selector):
        return self._tabs

    def query_selector(self, selector):
        return None


class _FakeBrowser:
    def __init__(self, requests_to_fire, tabs=None):
        self._requests_to_fire = requests_to_fire
        self._tabs = tabs
        self.closed = False
        self.page = None

    def new_page(self, viewport=None):
        self.page = _FakePage(self._requests_to_fire, tabs=self._tabs)
        return self.page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, requests_to_fire, tabs=None):
        self._requests_to_fire = requests_to_fire
        self._tabs = tabs
        self.launched_headless = None
        self.browser = None

    def launch(self, headless=True):
        self.launched_headless = headless
        self.browser = _FakeBrowser(self._requests_to_fire, tabs=self._tabs)
        return self.browser


class _FakePlaywright:
    def __init__(self, requests_to_fire, tabs=None):
        self.chromium = _FakeChromium(requests_to_fire, tabs=tabs)


class _FakeSyncPlaywrightCtx:
    def __init__(self, requests_to_fire, tabs=None):
        self._requests_to_fire = requests_to_fire
        self._tabs = tabs
        self.playwright = None

    def __enter__(self):
        self.playwright = _FakePlaywright(self._requests_to_fire, tabs=self._tabs)
        return self.playwright

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_sync_playwright_factory(requests_to_fire, tabs=None):
    return lambda: _FakeSyncPlaywrightCtx(requests_to_fire, tabs=tabs)


def test_load_playwright_raises_helpful_error_when_not_installed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.sync_api" or name.startswith("playwright"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="Playwright"):
        browser._load_playwright()


def test_load_playwright_returns_sync_playwright_when_installed(monkeypatch):
    import sys
    import types

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = object()
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    assert browser._load_playwright() is fake_sync_api.sync_playwright


def test_capture_from_url_filters_and_captures_requests(monkeypatch):
    good_body = build_payload(
        _Cfg(), query_template=_template({"u": "Units"}, [("u", "Name", False)]), window_config={"Count": 1}
    )
    requests_to_fire = [
        _FakeRequest("GET", "https://x/public/reports/querydata"),
        _FakeRequest("POST", "https://x/other"),
        _FakeRequest("POST", "https://x/public/reports/querydata", post_data=None),
        _FakeRequest("POST", "https://x/public/reports/querydata", post_data="not json"),
        _FakeRequest(
            "POST",
            "https://x/public/reports/querydata",
            headers={"x-powerbi-resourcekey": "abc"},
            post_data=orjson.dumps(good_body).decode(),
        ),
    ]
    tabs = [_FakeTab(), _FakeTab(raises=True)]
    monkeypatch.setattr(
        browser, "_load_playwright", lambda: _fake_sync_playwright_factory(requests_to_fire, tabs=tabs)
    )

    captured = browser.capture_from_url("https://app.powerbi.com/view?r=x", wait_seconds=1, headless=True)

    assert len(captured) == 1
    assert captured[0].headers["x-powerbi-resourcekey"] == "abc"
    assert tabs[0].clicked
    assert not tabs[1].clicked


class _FakeNextPageButton:
    def __init__(self, raises=False):
        self.raises = raises
        self.click_count = 0

    def click(self, timeout=None):
        self.click_count += 1
        if self.raises:
            raise TimeoutError("disabled")


class _NextPagePage:
    def __init__(self, num_pages, raises_on_last=False):
        self._remaining = num_pages
        self._raises_on_last = raises_on_last
        self.buttons = []

    def query_selector(self, selector):
        if self._remaining <= 0:
            return None
        self._remaining -= 1
        raises = self._raises_on_last and self._remaining == 0
        button = _FakeNextPageButton(raises=raises)
        self.buttons.append(button)
        return button

    def wait_for_timeout(self, ms):
        pass


def test_click_through_pages_stops_when_no_next_button():
    page = _NextPagePage(num_pages=3)

    browser._click_through_pages(page, wait_seconds=1)

    assert len(page.buttons) == 3
    assert all(b.click_count == 1 for b in page.buttons)


def test_click_through_pages_respects_max_click_cap():
    page = _NextPagePage(num_pages=browser.MAX_PAGE_CLICKS + 10)

    browser._click_through_pages(page, wait_seconds=1)

    assert len(page.buttons) == browser.MAX_PAGE_CLICKS


def test_click_through_pages_stops_when_click_fails():
    page = _NextPagePage(num_pages=5, raises_on_last=True)

    browser._click_through_pages(page, wait_seconds=1)

    assert page.buttons[-1].raises


class _ShrinkingTabsPage:
    def __init__(self, tabs):
        self._remaining = list(tabs)
        self._first_call = True

    def query_selector_all(self, selector):
        if self._first_call:
            self._first_call = False
            return list(self._remaining)
        if self._remaining:
            self._remaining.pop()
        return list(self._remaining)

    def wait_for_timeout(self, ms):
        pass


def test_visit_all_tabs_skips_index_once_fewer_tabs_remain():
    tabs = [_FakeTab(), _FakeTab(), _FakeTab()]
    page = _ShrinkingTabsPage(tabs)

    browser._visit_all_tabs(page, wait_seconds=1)

    assert tabs[0].clicked


def test_discover_from_url_builds_config_and_modules(monkeypatch):
    good_body = build_payload(
        _Cfg(), query_template=_template({"u": "Units"}, [("u", "Name", False)]), window_config={"Count": 1}
    )
    from powerbi_extract.discover import CapturedRequest

    fake_captured = [CapturedRequest(url="https://x", headers={"X-PowerBI-ResourceKey": "k"}, body=good_body)]
    monkeypatch.setattr(browser, "capture_from_url", lambda *a, **k: fake_captured)

    config, modules = browser.discover_from_url("https://app.powerbi.com/view?r=x")

    assert config.resource_key == "k"
    assert modules[0].name == "units_name"
