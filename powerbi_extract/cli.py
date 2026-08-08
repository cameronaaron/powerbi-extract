"""Generic CLI: run some or all of a caller-supplied list of query modules."""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

from powerbi_extract.client import run_paginated_query


def run_modules(modules, report_config, output_dir=".", session=None, max_workers=2):
    """Run each module, isolating failures so one bad module doesn't abort the rest.

    Returns one result per module, in module order: the DataFrame on success, or
    the raised exception on failure.
    """
    os.makedirs(output_dir, exist_ok=True)
    http = session or requests.Session()

    def run_one(module):
        try:
            return run_paginated_query(
                config=report_config,
                module_name=module.name,
                query_template=module.query_template,
                output_path=os.path.join(output_dir, module.output_filename),
                session=http,
            )
        except Exception as exc:
            print(f" -> ERROR extracting '{module.name}': {exc}", file=sys.stderr)
            return exc

    if len(modules) <= 1 or max_workers <= 1:
        results = [run_one(module) for module in modules]
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(modules))) as pool:
            results = list(pool.map(run_one, modules))

    failed = [m.name for m, r in zip(modules, results) if isinstance(r, Exception)]
    if failed:
        print(
            f"\n=== {len(failed)}/{len(modules)} module(s) failed: {', '.join(failed)} ===",
            file=sys.stderr,
        )
    return results


def main(modules, report_config, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module",
        action="append",
        dest="modules",
        help="Name of a module to run (repeatable). Defaults to all modules.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write extracted CSVs into (default: current directory).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=2,
        help="Number of modules to extract concurrently (default: 2, kept low to avoid "
        "triggering Power BI's anonymous-session throttling).",
    )
    args = parser.parse_args(argv)

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

    run_modules(selected, report_config, output_dir=args.output_dir, max_workers=args.max_workers)
