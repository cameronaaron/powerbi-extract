# powerbi-extract

Pull tabular data out of a **public** Power BI report by replaying the
`querydata` calls its embedded visuals already make in your browser. No
Power BI API access, service principal, or workspace login required — just
the report's public view URL.

This is the same technique you'd use to scrape a table off any
`app.powerbi.com/view?r=...` embed: open the report, watch the network tab
for `querydata` requests, and note the dataset/report/visual ids and the
columns each visual selects. This library turns that into a reusable,
paginated extraction.

## Install

```bash
pip install git+https://github.com/cameronaaron/powerbi-extract
```

Requires Python 3.9+. Runtime dependencies: `requests`, `polars`, `orjson`.

## Quick start: auto-run from a HAR file or a URL

You don't have to hand-decode ids and column lists yourself. `powerbi-extract-auto`
discovers the `ReportConfig` and the table/visual list for you and runs the
extraction in one shot.

**From a HAR file** (open the report, let it fully load, then in dev tools'
Network tab choose "Save all as HAR" — no extra install needed):

```bash
powerbi-extract-auto --har report.har --output-dir data
```

**From the report's URL directly** (spins up a headless Chromium via
Playwright, loads the report, captures the `querydata` traffic itself):

```bash
pip install "powerbi-extract[browser]"
playwright install chromium

powerbi-extract-auto --url "https://app.powerbi.com/view?r=..." --output-dir data
```

Both modes print the discovered module names and pull all of them, in
parallel (`--max-workers`, default 4), by default; pass `--module <name>`
(repeatable) to limit the run. Useful flags for `--url` mode:
`--wait-seconds N` to give a slow report more time to finish loading before
capture ends, and `--headed` to watch the browser instead of running headless
(handy for debugging).

Discovery (a HAR export or a browser capture) only has to happen once. Save
it and reuse it on later runs without re-exporting anything:

```bash
powerbi-extract-auto --har report.har --save-config report.json --output-dir data
powerbi-extract-auto --config report.json --output-dir data
```

If the report turns out to be private or its resource key has expired, the
extraction fails fast with a clear message instead of a bare HTTP status
code. A table is only written when every page of it came back: if a page
fails after retries, that module fails and no partial CSV is left behind.
The command exits non-zero if any module (or, in bulk mode, any report)
failed, so it's safe to use in scripts and cron jobs. Transient errors (HTTP 429/500/502/503/504) are retried automatically
with exponential backoff, honoring `Retry-After` when the server sends one.

**Bulk mode**: point `--urls-file` at a text file containing one or more
public report URLs — blank lines and unrelated text/links in the file are
ignored, so you can drop URLs straight out of a bookmarks export or a notes
file. Each report is extracted into its own `output-dir/<resource-key>/`
subfolder; one report failing (dead resource key, private report, etc.)
doesn't stop the rest, and a pass/fail summary prints at the end:

```bash
powerbi-extract-auto --urls-file urls.txt --output-dir data --wait-seconds 14
```

`--module` and `--save-config` aren't supported in this mode since they're
per-report concepts.

The same discovery functions are usable from Python:

```python
from powerbi_extract import discover_from_har
from powerbi_extract.browser import discover_from_url  # needs the `browser` extra

config, modules = discover_from_har("report.har")
# or: config, modules = discover_from_url("https://app.powerbi.com/view?r=...")
```

## Manual usage

If you'd rather wire up the config and query yourself (e.g. you only want
one specific visual, or you're scripting against a report you already know),
copy the captured request's `Query`/`Binding` object verbatim as
`query_template` — do not hand-reconstruct it from `From`/`Select` alone,
since that drops information (filters, aggregated measures) the report may
require:

```python
from powerbi_extract import ReportConfig, QueryModule, run_paginated_query

config = ReportConfig(
    dataset_id="...",
    report_id="...",
    visual_id="...",
    resource_key="...",  # the "k" query-string value in the report's public URL
    model_id=...,  # top-level "modelId" from the captured request body — varies per report
)

module = QueryModule(
    name="my_table",
    from_entities={"u": "My Table"},  # introspection/naming only, not sent to the API
    select_columns=[
        ("u", "Some Column", False),  # ditto — (source alias, property name, is_measure)
        ("u", "Some Measure", True),
    ],
    # The captured request body's
    # queries[0].Query.Commands[0].SemanticQueryDataShapeCommand, verbatim.
    query_template={"Query": {...}, "Binding": {...}, "ExecutionMetricsKind": 1},
    output_filename="my_table.csv",
)

df = run_paginated_query(
    config=config,
    module_name=module.name,
    query_template=module.query_template,
    output_path=module.output_filename,
)
```

Or run several modules in parallel, with each module's failure isolated
(the result list holds a DataFrame or the exception, in module order):

```python
from powerbi_extract.runner import run_modules

results = run_modules([module], config, output_dir="data")
```

### Finding the ids

1. Open the report's public `app.powerbi.com/view?r=...` URL in a browser
   with dev tools open.
2. Find a `POST .../public/reports/querydata` request in the Network tab.
3. `resource_key` is the `k` field decoded from the `r` query-string
   parameter (it's base64-encoded JSON: `{"k": "...", "t": "...", "c": ...}`).
4. `dataset_id`, `report_id`, and `visual_id` are in the request body's
   `ApplicationContext.Sources[0]` and top-level `DatasetId`.
5. `model_id` is the request body's top-level `modelId`.
6. `query_template` is the request body's
   `queries[0].Query.Commands[0].SemanticQueryDataShapeCommand`, copied as-is.
   `from_entities`/`select_columns` (mirroring `Query.From`/`Query.Select`)
   are only used for naming and dedup — they are not sent to the API.

## Design

- `powerbi_extract/dsr_parser.py` — decodes Power BI's compact DSR
  (data-shape-result) row format: bitmasks marking repeated/null fields,
  dictionary-encoded categorical values, and mid-stream schema changes. A
  malformed row raises `ValueError` instead of truncating the page.
- `powerbi_extract/client.py` — replays a captured `query_template` verbatim,
  overriding only the pagination window for `Window`-shaped queries, retries
  transient HTTP errors and 401/403 with backoff before raising
  `PowerBIAuthError`, paginates via `RestartTokens` until exhausted, and
  returns/saves a `polars.DataFrame`. A page that still fails raises
  `PowerBIQueryError` and nothing is written.
- `powerbi_extract/modules.py` — a plain dataclass describing one
  table/visual pull, so a project can declare a list of them.
- `powerbi_extract/runner.py` — runs a list of modules in parallel against
  one `ReportConfig`, isolating each module's errors.
- `powerbi_extract/discover.py` — turns captured `querydata` requests (from a
  HAR file or a live browser capture) into a `ReportConfig` (including the
  per-report `modelId`, read from the capture, not hardcoded) and a
  deduplicated list of `QueryModule`s carrying the verbatim `query_template`,
  can save/load that pair as JSON so discovery only has to happen once, and
  skips (rather than crashes on) query shapes it can't generically handle,
  like DAX subquery sources.
- `powerbi_extract/browser.py` — optional Playwright-based capture: loads a
  public report URL headlessly, scrolls, clicks through both page-tab bars
  and next-page arrow controls (reports use either navigation style) so
  interaction-triggered visuals across every page fire, and records the
  `querydata` traffic.
- `powerbi_extract/auto.py` — the `powerbi-extract-auto` CLI entry point that
  wires discovery straight into the paginated extraction.

Nothing in this package knows about any specific report, dataset, or field
list — that's supplied by the caller (or discovered automatically).

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Coverage is enforced at 100% via `pyproject.toml`.

## Tried and rejected: a Rust parser

A PyO3 port of the DSR parser was benchmarked on 5k/30k/100k-row payloads
and came out 25-35% *slower* than pure Python + orjson. The loop is
dominated by dict/list access, which is already C-speed in CPython, and
every access from Rust pays the FFI crossing. It was removed; revisit only
for a workload with heavy per-cell computation.

## Use responsibly

This only works on reports their owners published to the web with
"Publish to web", and it sends the same requests the report's own visuals
send. Keep request rates modest (the default concurrency is deliberately
low) and respect the publisher's terms for the data.

## License

[AGPL-3.0-or-later](LICENSE). You can use, modify and redistribute this,
including commercially; if you distribute it or run a modified version as
a network service, you must publish your source under the same license.
