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
pip install -e .
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
code. Transient errors (HTTP 429/500/502/503/504) are retried automatically
with exponential backoff, honoring `Retry-After` when the server sends one.

The same discovery functions are usable from Python:

```python
from powerbi_extract import discover_from_har
from powerbi_extract.browser import discover_from_url  # needs the `browser` extra

config, modules = discover_from_har("report.har")
# or: config, modules = discover_from_url("https://app.powerbi.com/view?r=...")
```

## Manual usage

If you'd rather wire up the config and columns yourself (e.g. you only want
one specific visual, or you're scripting against a report you already know),
describe the report and the columns you want, then run the extraction:

```python
from powerbi_extract import ReportConfig, QueryModule, run_paginated_query

config = ReportConfig(
    dataset_id="...",
    report_id="...",
    visual_id="...",
    resource_key="...",  # the "k" query-string value in the report's public URL
)

module = QueryModule(
    name="my_table",
    from_entities={"u": "My Table"},
    select_columns=[
        ("u", "Some Column", False),   # (source alias, property name, is_measure)
        ("u", "Some Measure", True),
    ],
    output_filename="my_table.csv",
)

df = run_paginated_query(
    config=config,
    module_name=module.name,
    from_entities=module.from_entities,
    select_columns=module.select_columns,
    output_path=module.output_filename,
)
```

Or run several modules from the CLI helper:

```python
from powerbi_extract.cli import main

main(modules=[module], report_config=config, argv=["--output-dir", "data"])
```

```bash
python your_extract_script.py --module my_table --output-dir data
```

### Finding the ids

1. Open the report's public `app.powerbi.com/view?r=...` URL in a browser
   with dev tools open.
2. Find a `POST .../public/reports/querydata` request in the Network tab.
3. `resource_key` is the `k` field decoded from the `r` query-string
   parameter (it's base64-encoded JSON: `{"k": "...", "t": "...", "c": ...}`).
4. `dataset_id`, `report_id`, and `visual_id` are in the request body's
   `ApplicationContext.Sources[0]` and top-level `DatasetId`.
5. `from_entities` and `select_columns` mirror the request body's
   `Query.From` and `Query.Select`.

## Design

- `powerbi_extract/dsr_parser.py` — decodes Power BI's compact DSR
  (data-shape-result) row format: bitmasks marking repeated/null fields,
  dictionary-encoded categorical values, and mid-stream schema changes.
- `powerbi_extract/client.py` — builds the query payload, paginates via
  `RestartTokens` until exhausted, retries transient HTTP errors with
  backoff, raises `PowerBIAuthError` on 401/403, and returns/saves a
  `polars.DataFrame`.
- `powerbi_extract/modules.py` — a plain dataclass describing one
  table/visual pull, so a project can declare a list of them.
- `powerbi_extract/cli.py` — a report-agnostic `argparse` CLI that runs a
  caller-supplied list of modules, in parallel, against a caller-supplied
  `ReportConfig`.
- `powerbi_extract/discover.py` — turns captured `querydata` requests (from a
  HAR file or a live browser capture) into a `ReportConfig` and a deduplicated
  list of `QueryModule`s, and can save/load that pair as JSON so discovery
  only has to happen once.
- `powerbi_extract/browser.py` — optional Playwright-based capture: loads a
  public report URL headlessly, scrolls and clicks through its tabs so
  interaction-triggered visuals fire too, and records the `querydata`
  traffic.
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

## Experimental: Rust extension

`src/lib.rs` is a PyO3 extension (`powerbi_extract._native`) that reimplements
`parse_powerbi_dsr`'s bitmask/dictionary-resolution loop in Rust, built with
[maturin](https://www.maturin.rs/). It is **not used by default** —
`parse_powerbi_dsr_bytes` always runs the pure Python path.

Benchmarked across 5k/30k/100k-row synthetic payloads, the Rust version was
consistently ~25-35% *slower* than pure Python + orjson, not faster. The
loop is dominated by Python dict/list attribute access, which is already
C-speed inside CPython; every such access from Rust has to cross the PyO3
FFI boundary (type checks, `Option`/`Result` wrapping, refcounting), and
that per-call overhead outweighs anything Rust gains, since there's very
little actual arithmetic to speed up. The code is kept here as a documented
experiment and a starting point if the tradeoff ever changes (e.g. a
workload with heavier per-cell computation).

To build and try it yourself:

```bash
pip install -e ".[dev]"
maturin develop --release
```

Then call `powerbi_extract.dsr_parser.parse_powerbi_dsr_native(response_json)`
directly (raises `RuntimeError` if the extension isn't built) and compare.

## License

MIT
