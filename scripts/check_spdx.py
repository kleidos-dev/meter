#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every tracked file names its copyright holder and its license.

A file carries both in its own header -- an ``SPDX-FileCopyrightText`` line
directly above its ``SPDX-License-Identifier`` line, in its own comment syntax
-- unless it cannot hold a comment (a license text, a generated schematic, a
render, a calibration record), in which case ``REUSE.toml`` names it
explicitly. ``reuse lint`` checks that every file is covered; this checks the
stricter rule the repository keeps: no file inherits its notice from a
catch-all, the holder is spelled one way, and ``hardware/`` is
CERN-OHL-S-2.0 while everything else is GPL-3.0-or-later.

    python3 scripts/check_spdx.py          # check, exit 1 on any finding
    python3 scripts/check_spdx.py --fix    # add the missing copyright lines
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Spelled in two pieces so that REUSE does not read this file's own literals
# as tags.
TAG_LICENSE = "SPDX-" + "License-Identifier:"
TAG_COPYRIGHT = "SPDX-" + "FileCopyrightText:"
HOLDER = "2026 José M. Requena Plens"

HARDWARE_LICENSE = "CERN-OHL-S-2.0"
SOFTWARE_LICENSE = "GPL-3.0-or-later"

# How far into a file the header may sit (a shebang, a front matter).
HEADER_LINES = 15

# Directories REUSE itself treats as license texts.
SKIPPED_PREFIXES = ("LICENSES/",)


def tracked_files() -> list[str]:
    """The files in the index: everything tracked plus anything staged, so a new
    file is checked before it lands, and an untracked work in progress is not."""
    out = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({line for line in out.splitlines() if line and (ROOT / line).is_file()})


def annotated_patterns() -> list[str]:
    """The paths REUSE.toml annotates explicitly; a bare ``**`` is refused."""
    data = tomllib.loads((ROOT / "REUSE.toml").read_text(encoding="utf-8"))
    patterns: list[str] = []
    for annotation in data.get("annotations", []):
        paths = annotation.get("path", [])
        patterns.extend([paths] if isinstance(paths, str) else paths)
    return patterns


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def expected_license(path: str) -> str:
    return HARDWARE_LICENSE if path.startswith("hardware/") else SOFTWARE_LICENSE


def check_file(path: str, text: str) -> list[str]:
    """The findings for one file that must carry its own header."""
    lines = text.splitlines()[:HEADER_LINES]
    license_index = next((i for i, line in enumerate(lines) if TAG_LICENSE in line), None)
    if license_index is None:
        return [f"{path}: no {TAG_LICENSE} line in its first {HEADER_LINES} lines"]
    findings: list[str] = []
    license = lines[license_index].split(TAG_LICENSE, 1)[1].strip().rstrip("->").strip()
    if license != expected_license(path):
        findings.append(f"{path}: licensed {license}, expected {expected_license(path)}")
    if license_index == 0 or TAG_COPYRIGHT not in lines[license_index - 1]:
        findings.append(f"{path}: no {TAG_COPYRIGHT} line directly above {TAG_LICENSE}")
    elif HOLDER not in lines[license_index - 1]:
        findings.append(f"{path}: copyright line does not read '{HOLDER}'")
    return findings


def fix_file(path: Path) -> bool:
    """Inserts the copyright line above the license line, in the same syntax."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines[:HEADER_LINES]):
        if TAG_LICENSE not in line:
            continue
        if i > 0 and TAG_COPYRIGHT in lines[i - 1]:
            return False
        prefix, _, rest = line.partition(TAG_LICENSE)
        suffix = " -->" if rest.rstrip().endswith("-->") else ""
        lines.insert(i, f"{prefix}{TAG_COPYRIGHT} {HOLDER}{suffix}\n")
        path.write_text("".join(lines), encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fix", action="store_true", help="add missing copyright lines")
    args = parser.parse_args(argv)

    patterns = annotated_patterns()
    findings: list[str] = []
    if "**" in patterns:
        findings.append("REUSE.toml: a catch-all '**' annotation hides files with no notice")

    for path in tracked_files():
        if path.startswith(SKIPPED_PREFIXES):
            continue
        if any(matches(path, pattern) for pattern in patterns):
            continue
        file_path = ROOT / path
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"{path}: binary file not annotated in REUSE.toml")
            continue
        if args.fix and fix_file(file_path):
            text = file_path.read_text(encoding="utf-8")
            print(f"fixed {path}")
        findings.extend(check_file(path, text))

    for finding in findings:
        print(finding, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
