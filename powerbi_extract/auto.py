"""One-shot CLI: point at a HAR file, a public report URL, a saved config, or a list of URLs and it runs.

    powerbi-extract-auto --har report.har --output-dir data
    powerbi-extract-auto --url "https://app.powerbi.com/view?r=..." --output-dir data
    powerbi-extract-auto --config saved.json --output-dir data
    powerbi-extract-auto --urls-file urls.txt --output-dir data
"""

import argparse
import os
import sys

from powerbi_extract.cli import run_modules
from powerbi_extract.client import PowerBIAuthError
from powerbi_extract.discover import (
    discover_from_har,
    extract_view_urls,
    load_config,
    resource_key_from_view_url,
    save_config,
)


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


def run_bulk(urls, output_dir, wait_seconds=8, headless=True, max_workers=4):
    """Discover and extract each report URL into its own subdirectory of ``output_dir``.

    One report failing (private, expired key, unrenderable, etc.) doesn't stop the
    rest. Returns a list of (url, status, detail) tuples for the caller to summarize.
    """
    results = []
    for index, url in enumerate(urls, start=1):
        try:
            report_dir_name = resource_key_from_view_url(url)
        except ValueError:
            report_dir_name = f"report_{index}"
        report_dir = os.path.join(output_dir, report_dir_name)

        print(f"\n### [{index}/{len(urls)}] {url}")
        try:
            config, modules = discover(url=url, wait_seconds=wait_seconds, headless=headless)
            print(f"Discovered {len(modules)} module(s): {', '.join(m.name for m in modules)}")
            run_modules(modules, config, output_dir=report_dir, max_workers=max_workers)
            results.append((url, "ok", len(modules)))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            results.append((url, "failed", str(exc)))

    return results


def _print_bulk_summary(results):
    ok = [r for r in results if r[1] == "ok"]
    failed = [r for r in results if r[1] == "failed"]
    print(f"\n=== Bulk summary: {len(ok)} succeeded, {len(failed)} failed ===")
    for url, status, detail in results:
        marker = "OK" if status == "ok" else "FAIL"
        print(f"[{marker}] {url} — {detail}")


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
    source.add_argument(
        "--urls-file",
        help="Path to a text file containing one or more public report view URLs "
        "(any surrounding text, blank lines, or non-matching links are ignored). "
        "Each report is extracted into output-dir/<resource-key>/. Requires the "
        "`browser` extra (Playwright).",
    )
    parser.add_argument(
        "--save-config",
        help="Write the discovered config/modules to this path for reuse with --config. "
        "Not valid with --urls-file.",
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
        help="Name of a discovered module to run (repeatable). Defaults to all of them. "
        "Not valid with --urls-file.",
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
        help="Seconds to let each report keep loading before capture ends (--url/--urls-file mode only).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window instead of running headless (--url/--urls-file mode only).",
    )
    args = parser.parse_args(argv)

    if args.urls_file:
        if args.modules or args.save_config:
            parser.error("--module and --save-config aren't valid with --urls-file.")

        with open(args.urls_file) as f:
            urls = extract_view_urls(f.read())
        if not urls:
            parser.error(f"No public report view URLs found in '{args.urls_file}'.")

        print(f"Found {len(urls)} report URL(s) in '{args.urls_file}'.")
        results = run_bulk(
            urls,
            output_dir=args.output_dir,
            wait_seconds=args.wait_seconds,
            headless=not args.headed,
            max_workers=args.max_workers,
        )
        _print_bulk_summary(results)
        if not any(status == "ok" for _, status, _ in results):
            raise SystemExit(1)
        return

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
