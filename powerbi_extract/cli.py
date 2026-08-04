"""Generic CLI: run some or all of a caller-supplied list of query modules."""

import argparse
import os

import requests

from powerbi_extract.client import run_paginated_query


def run_modules(modules, report_config, output_dir=".", session=None):
    os.makedirs(output_dir, exist_ok=True)
    http = session or requests.Session()
    for module in modules:
        run_paginated_query(
            config=report_config,
            module_name=module.name,
            from_entities=module.from_entities,
            select_columns=module.select_columns,
            output_path=os.path.join(output_dir, module.output_filename),
            session=http,
        )


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

    run_modules(selected, report_config, output_dir=args.output_dir)
