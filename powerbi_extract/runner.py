# SPDX-FileCopyrightText: 2026 Cameron Aaron
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run a list of query modules, isolating failures."""

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
        except Exception as exc:  # noqa: BLE001 - isolate each module; reported below
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
