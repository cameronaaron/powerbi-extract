# SPDX-FileCopyrightText: 2026 Cameron Aaron
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Discover a ReportConfig + QueryModules by loading a public report URL live."""

import orjson

from powerbi_extract.discover import QUERYDATA_PATH, CapturedRequest, build_config_and_modules

TAB_SELECTOR = "div.content.text.ui-role-button-text, button.sectionItem, .tabList .tab"
NEXT_PAGE_SELECTOR = "button[aria-label='Next Page']"
MAX_PAGE_CLICKS = 20


def _load_playwright():
    """-> (sync_playwright, playwright's Error class)."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "URL-based discovery requires Playwright. Install with "
            '`pip install "powerbi-extract[browser]"` and run `playwright install chromium`.'
        ) from exc
    return sync_playwright, Error


# Clicks are best-effort: a tab or button that won't take a click (`errors`,
# Playwright's own failure type) is skipped; anything else is a real bug.
def _visit_all_tabs(page, wait_seconds, errors):
    tab_count = len(page.query_selector_all(TAB_SELECTOR))
    for index in range(tab_count):
        tabs = page.query_selector_all(TAB_SELECTOR)
        if index >= len(tabs):
            continue
        try:
            tabs[index].click(timeout=3000)
            page.wait_for_timeout(min(wait_seconds, 4) * 1000)
        except errors:
            continue


def _click_through_pages(page, wait_seconds, errors):
    for _ in range(MAX_PAGE_CLICKS):
        button = page.query_selector(NEXT_PAGE_SELECTOR)
        if not button:
            break
        try:
            button.click(timeout=3000)
        except errors:
            break
        page.wait_for_timeout(min(wait_seconds, 4) * 1000)


def _scroll_report(page, wait_seconds):
    for _ in range(4):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(min(wait_seconds, 2) * 500)


def capture_from_url(view_url, wait_seconds=8, headless=True):
    sync_playwright, errors = _load_playwright()
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})

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
                captured.append(CapturedRequest(url=request.url, headers=dict(request.headers), body=body))

            page.on("request", on_request)
            page.goto(view_url, wait_until="networkidle")
            page.wait_for_timeout(wait_seconds * 1000)
            _scroll_report(page, wait_seconds)
            _visit_all_tabs(page, wait_seconds, errors)
            _click_through_pages(page, wait_seconds, errors)
        finally:
            browser.close()

    return captured


def discover_from_url(view_url, wait_seconds=8, headless=True):
    captured = capture_from_url(view_url, wait_seconds=wait_seconds, headless=headless)
    return build_config_and_modules(captured)
