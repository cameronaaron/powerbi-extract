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

## Usage

Describe the report and the columns you want, then run the extraction:

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
  `RestartTokens` until exhausted, and returns/saves a `polars.DataFrame`.
- `powerbi_extract/modules.py` — a plain dataclass describing one
  table/visual pull, so a project can declare a list of them.
- `powerbi_extract/cli.py` — a report-agnostic `argparse` CLI that runs a
  caller-supplied list of modules against a caller-supplied `ReportConfig`.

Nothing in this package knows about any specific report, dataset, or field
list — that's supplied by the caller.

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
