# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The relay refusals docs/scpi.md lists are the ones the firmware composes.

``docs/scpi.md``, "ROUTe: the relay module", lists the detail of every error a
relay order can queue, one a line of a ``text`` block: the code, then the
detail, a value the firmware fills in written ``<so>``. This guard holds each
detail's fixed text to the string literals of ``console.cpp`` and
``console_commands.cpp``, so that a reworded refusal, or one that left, fails
here, and the code to a constant of ``scpi.h``.

Host-only: the page and the sources read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "scpi.md"
SOURCES = [ROOT / "firmware" / "src" / name for name in ("console.cpp", "console_commands.cpp")]
CODES = ROOT / "firmware" / "src" / "scpi.h"

#: The paragraph the list follows.
HEADING = "**The refusals' details.**"

#: A line of the list: the code, right-aligned, and the detail.
ROW = re.compile(r"^ ?(-?\d{3}) (.+)$")

#: A value the firmware fills in, outside an order's own usage (``<ms>``).
FILLED = re.compile(r"<(?!ms>)[^>]+>")

#: A C++ string literal, and the white space between two adjacent ones.
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"(?:\s*\n\s*(?="))?')


def literals(source: str) -> list[str]:
    """The string literals of *source*, adjacent ones joined as the compiler joins them."""
    joined = re.sub(r'"\s*\n\s*"', "", source)
    return [match.group(1) for match in LITERAL.finditer(joined)]


def rows(page: str) -> list[tuple[int, str]]:
    """The code and the detail of each line of the list in *page*."""
    start = page.index("```text\n", page.index(HEADING)) + len("```text\n")
    lines = page[start : page.index("\n```", start)].splitlines()
    parsed = [ROW.match(line) for line in lines]
    assert all(parsed), f"not a code and a detail: {lines[parsed.index(None)]!r}"
    return [(int(match.group(1)), match.group(2)) for match in parsed if match]


def unknown(detail: str, known: list[str]) -> list[str]:
    """The fixed parts of *detail* that no literal of *known* holds."""
    parts = [part for part in FILLED.split(detail) if len(part) > 1]
    return [part for part in parts if not any(part in literal for literal in known)]


def _known() -> list[str]:
    return [text for path in SOURCES for text in literals(path.read_text(encoding="utf-8"))]


def test_every_detail_is_the_firmware_s() -> None:
    known = _known()
    listed = rows(PAGE.read_text(encoding="utf-8"))
    assert len(listed) > 20
    wrong = {detail: parts for _, detail in listed if (parts := unknown(detail, known))}
    assert not wrong, f"docs/scpi.md lists refusals the firmware does not compose: {wrong}"


def test_every_code_is_the_firmware_s() -> None:
    codes = {int(code) for code in re.findall(r"= (-?\d{3});", CODES.read_text(encoding="utf-8"))}
    wrong = sorted({code for code, _ in rows(PAGE.read_text(encoding="utf-8"))} - codes)
    assert not wrong, f"docs/scpi.md lists codes scpi.h does not define: {wrong}"


def test_the_guard_sees_a_reworded_refusal() -> None:
    known = literals('f( "null window running (ABORt first)" );\ng( "a "\n   "b" );')
    assert known == ["null window running (ABORt first)", "a b"]
    assert unknown("null window running (ABORt first)", known) == []
    assert unknown("null window open (ABORt first)", known) == ["null window open (ABORt first)"]
    assert unknown("<k3|k4>a b<path>", known) == []
    page = f"{HEADING}\n\n```text\n-224 use X <ms>\n 110 order <path>\n```\n"
    assert rows(page) == [(-224, "use X <ms>"), (110, "order <path>")]
    assert unknown("use X <ms>", ["use X <ms>"]) == []
