"""One-shot CLI: point at a HAR file, a public report URL, or a saved config and it runs.

    powerbi-extract-auto --har report.har --output-dir data
    powerbi-extract-auto --url "https://app.powerbi.com/view?r=..." --output-dir data
    powerbi-extract-auto --config saved.json --output-dir data
"""

import argparse
import sys

from powerbi_extract.cli import run_modules
from powerbi_extract.client import PowerBIAuthError
from powerbi_extract.discover import discover_from_har, load_config, save_config


def discover(har=None, url=None, config=None, wait_seconds=8, headless=True):
    sources = [har, url, config]
    if sum(bool(s) for s in sources) != 1:
        raise ValueError("Pass exactly one of `har`, `url`, or `config`.")

    if config:
        return load_config(config)
    if har:
        return discover_from_har(har)

    from powerbi_extract.browser import discover_from_url

    return discover_from_url(url, wait_seconds=wait_seconds, headless=headless)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--har", help="Path to a HAR file exported from the browser's Network tab."
    )
    source.add_argument(
        "--url",
        help="Public report view URL (app.powerbi.com/view?r=...). "
        "Requires the `browser` extra (Playwright).",
    )
    source.add_argument(
        "--config", help="Path to a config previously written with --save-config."
    )
    parser.add_argument(
        "--save-config",
        help="Write the discovered config/modules to this path for reuse with --config.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write extracted CSVs into (default: current directory).",
    )
    parser.add_argument(
        "--module",
        action="append",
        dest="modules",
        help="Name of a discovered module to run (repeatable). Defaults to all of them.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Number of modules to extract concurrently (default: 4).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=8,
        help="Seconds to let the report keep loading before capture ends (--url mode only).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless (--url mode only).",
    )
    args = parser.parse_args(argv)

    config, modules = discover(
        har=args.har,
        url=args.url,
        config=args.config,
        wait_seconds=args.wait_seconds,
        headless=not args.headed,
    )
    print(f"Discovered {len(modules)} module(s): {', '.join(m.name for m in modules)}")

    if args.save_config:
        save_config(config, modules, args.save_config)
        print(f"Saved config to '{args.save_config}' (reuse with --config).")

    all_modules = {m.name: m for m in modules}
    if args.modules:
        unknown = set(args.modules) - set(all_modules)
        if unknown:
            parser.error(
                f"Unknown module(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(all_modules))}"
            )
        selected = [all_modules[name] for name in args.modules]
    else:
        selected = modules

    try:
        run_modules(selected, config, output_dir=args.output_dir, max_workers=args.max_workers)
    except PowerBIAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
