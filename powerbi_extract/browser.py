"""Discover a ReportConfig + QueryModules by loading a public report URL live."""

import orjson

from powerbi_extract.discover import QUERYDATA_PATH, CapturedRequest, build_config_and_modules

TAB_SELECTOR = "[aria-label='Page navigation'] button, .tabList .tab"


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "URL-based discovery requires Playwright. Install with "
            "`pip install \"powerbi-extract[browser]\"` and run `playwright install chromium`."
        ) from exc
    return sync_playwright


def _visit_all_tabs(page, wait_seconds):
    tabs = page.query_selector_all(TAB_SELECTOR)
    for tab in tabs:
        try:
            tab.click(timeout=2000)
            page.wait_for_timeout(min(wait_seconds, 3) * 1000)
        except Exception:
            continue


def _scroll_report(page, wait_seconds):
    for _ in range(4):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(min(wait_seconds, 2) * 500)


def capture_from_url(view_url, wait_seconds=8, headless=True):
    sync_playwright = _load_playwright()
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()

            def on_request(request):
                if request.method != "POST" or QUERYDATA_PATH not in request.url:
                    return
                post_data = request.post_data
                if not post_data:
                    return
                try:
                    body = orjson.loads(post_data)
                except orjson.JSONDecodeError:
                    return
                captured.append(
                    CapturedRequest(url=request.url, headers=dict(request.headers), body=body)
                )

            page.on("request", on_request)
            page.goto(view_url, wait_until="networkidle")
            page.wait_for_timeout(wait_seconds * 1000)
            _scroll_report(page, wait_seconds)
            _visit_all_tabs(page, wait_seconds)
        finally:
            browser.close()

    return captured


def discover_from_url(view_url, wait_seconds=8, headless=True):
    captured = capture_from_url(view_url, wait_seconds=wait_seconds, headless=headless)
    return build_config_and_modules(captured)
