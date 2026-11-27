# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host tests compile every decision the image holds.

The coverage report counts what LLVM instruments, and a region the host build
preprocesses out is not instrumented: it is absent from the report, not shown
as an untaken side, and a file reads 100 % with decisions no test took
(``docs/testing.md``, "What the report cannot see"). So ``firmware/src``
compiles the same on both builds: no ``#if`` or ``#ifdef`` chooses code by
platform. Each conditional left is listed below with what it chooses and why
no decision hides behind it; an include guard is not a conditional here.

Host-only: the sources read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "firmware" / "src"

#: Each conditional of ``firmware/src`` (its file and its condition, as written)
#: and why the host's coverage misses no decision behind it.
ALLOWED = {
    (
        "relay.cpp",
        "#if defined( CONFIG_RTC_CLK_SRC_EXT_CRYS ) || defined( CONFIG_RTC_CLK_SRC_EXT_OSC )",
    ): "an #error that stops a build whose RTC clock takes the relays' pins: no code",
    ("heap_monitor.h", "#ifdef METER_HEAP_CENSUS"): (
        "the census ring's size, a constant; the census's code is the same in both builds"
        " and the host tests attach a ring of their own (heap::attachCensus)"
    ),
}

_CONDITIONAL = re.compile(r"^\s*#\s*(if|ifdef|ifndef)\b.*$", re.MULTILINE)


def conditionals() -> dict[tuple[str, str], int]:
    """Each conditional of ``firmware/src`` but the include guards, and how many times."""
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SOURCES.iterdir()):
        if path.suffix not in (".h", ".cpp"):
            continue
        guard = f"KLEIDOS_METER_{path.stem.upper()}_H"
        for match in _CONDITIONAL.finditer(path.read_text(encoding="utf-8")):
            line = re.sub(r"\s+", " ", match.group(0).strip()).replace("# ", "#")
            if path.suffix == ".h" and line == f"#ifndef {guard}":
                continue
            key = (path.name, line)
            found[key] = found.get(key, 0) + 1
    return found


def test_no_decision_is_compiled_for_the_device_alone() -> None:
    found = conditionals()
    assert set(found) == set(ALLOWED), sorted(set(found) ^ set(ALLOWED))
    assert all(count == 1 for count in found.values()), found


def test_a_platform_branch_would_fail() -> None:
    planted = "#ifdef ESP_PLATFORM\nint x;\n#endif\n"
    assert _CONDITIONAL.search(planted)
