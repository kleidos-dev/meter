#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the calibration registry, and keep its index in step with it.

    python3 scripts/check_calibration_registry.py            # check; fails on a problem
    python3 scripts/check_calibration_registry.py --write    # check, then regenerate the index

What it checks (``calibration/README.md``, "The registry"): every file against
its schema; each accepted calibration's arithmetic (the range means and their
scatter, the split, every budget term recomputed from its limit and
distribution, u_c as their root sum of squares, U = k × u_c); each against its
configuration's limits; the hashes of the records and raw logs an event filed;
two successive calibrations of one instrument by their normalized error; the
instruments of one sense core against each other; and that ``index.json`` is
what the files give. A flag (an instrument apart from its group) is printed
and does not fail.

No third-party module: the registry code is the host package's
(``host/kleidos_meter/registry.py``), imported from the tree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from kleidos_meter.registry import (  # noqa: E402
    Registry,
    build_index,
    check,
    dump_index,
    iter_problems,
    validate,
)


def main(argv: list[str] | None = None) -> int:
    """Run the check.

    :returns: 0 when the registry and its index hold, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT / "calibration")
    parser.add_argument("--write", action="store_true", help="regenerate index.json")
    args = parser.parse_args(argv)

    registry = Registry.from_directory(args.root)
    problems, flags = check(registry)
    for flag in flags:
        print(f"flag: {flag}")
    if problems:
        print("calibration registry: problems\n" + iter_problems(problems), file=sys.stderr)
        return 1
    index = build_index(registry)
    index_problems = validate(index, registry.schemas["index"])
    if index_problems:
        print("index: " + iter_problems(index_problems), file=sys.stderr)
        return 1
    text = dump_index(index)
    path = args.root / "index.json"
    if args.write:
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(args.root.parent)}")
    elif not path.is_file() or path.read_text(encoding="utf-8") != text:
        print(
            "calibration/index.json is not what the registry gives: "
            "run python3 scripts/check_calibration_registry.py --write",
            file=sys.stderr,
        )
        return 1
    instruments = len(index["instruments"])
    calibrations = sum(len(i["calibrations"]) for i in index["instruments"].values())
    print(
        f"calibration registry: {len(registry.configurations)} configurations, "
        f"{instruments} instruments, {calibrations} accepted calibrations; ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
