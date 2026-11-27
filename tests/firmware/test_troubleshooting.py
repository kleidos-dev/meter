# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/troubleshooting.md names only the errors docs/scpi.md defines.

The troubleshooting page sends a reader to an error number's meaning, so an
error it names that the protocol's table does not hold (renumbered, retired or
mistyped) sends them nowhere. An error is written in a code span, alone
(`` `-221` ``) or as the instrument answers it (`` `-221,"Settings
conflict;..."` ``); every such number must be a row of the table in
docs/scpi.md, "The error queue", and a standard text quoted with it must be
that row's. The page's table keeps its five columns, and each row names
where its behavior is defined with a link.

Host-only: both pages read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "troubleshooting.md"
SCPI = ROOT / "docs" / "scpi.md"

#: The page's columns, in order.
COLUMNS = ["Symptom", "Likely cause", "Check", "Fix", "Defined in"]

#: An error in a code span: the number alone, or the number and the quoted standard text.
ERROR = re.compile(r'^(-?\d{3})(?:$|,"([^;"]*))')


def errors_table(text: str) -> dict[int, str]:
    """docs/scpi.md's error codes and their standard texts."""
    section = text[text.index("### The error queue") :]
    section = section[: section.index("\n### ", 1)]
    rows = re.findall(r"^\| (-?\d+) \| ([^|]*) \|", section, re.MULTILINE)
    return {int(code): name.strip() for code, name in rows}


def named(text: str) -> list[tuple[int, str | None]]:
    """The errors *text* names in its code spans, each with its quoted text if any."""
    found = []
    for span in re.findall(r"`([^`\n]+)`", text):
        match = ERROR.match(span)
        if match:
            found.append((int(match.group(1)), match.group(2)))
    return found


def unknown(text: str, table: dict[int, str]) -> list[str]:
    """What *text* names that *table* does not hold, one line each."""
    wrong = []
    for code, quoted in named(text):
        if code not in table:
            wrong.append(f"{code}: not in docs/scpi.md's error table")
        elif quoted is not None and quoted != table[code]:
            wrong.append(f"{code}: quoted as {quoted!r}, the table says {table[code]!r}")
    return sorted(set(wrong))


def rows(text: str) -> list[list[str]]:
    """The cells of the page's table, its header row first."""
    section = text[text.index("## The table") :]
    section = section[: section.index("\n## ", 1)]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in line.strip("|").split(" | ")] for line in lines]


def test_every_error_the_page_names_is_the_protocol_s() -> None:
    table = errors_table(SCPI.read_text(encoding="utf-8"))
    text = PAGE.read_text(encoding="utf-8")
    assert len(named(text)) > 20
    wrong = unknown(text, table)
    assert not wrong, "docs/troubleshooting.md names errors docs/scpi.md does not:\n" + "\n".join(
        wrong
    )


def test_the_table_has_its_columns_and_every_row_its_reference() -> None:
    table = rows(PAGE.read_text(encoding="utf-8"))
    assert table[0] == COLUMNS
    body = list(table[2:])
    assert len(body) > 20
    for row in body:
        assert len(row) == len(COLUMNS), row[0]
        assert "](" in row[-1], f"{row[0]}: names no section where it is defined"


def test_the_guard_sees_an_unknown_code_and_a_wrong_text() -> None:
    table = errors_table(SCPI.read_text(encoding="utf-8"))
    assert table[-221] == "Settings conflict" and table[116] == "Relays released by reset"
    text = 'a `-221,"Settings conflict;x"`, a `102` and `0x40`, `1a86:55d3`, `4096`'
    assert named(text) == [(-221, "Settings conflict"), (102, None)]
    assert unknown(text, table) == []
    planted = 'a `-299` and `-221,"Setting conflict;x"`'
    assert unknown(planted, table) == [
        "-221: quoted as 'Setting conflict', the table says 'Settings conflict'",
        "-299: not in docs/scpi.md's error table",
    ]


def test_a_pin_change_that_leaves_no_answer_has_its_row() -> None:
    """The way back from a pin map with no INA228 needs only the console, and the row says so."""
    table = rows(PAGE.read_text(encoding="utf-8"))
    found = [row for row in table[2:] if "after a pin change" in row[0]]
    assert len(found) == 1, "no row for an instrument that does not answer after a pin change"
    symptom, cause, check, fix, defined = found[0]
    assert "does not answer" in symptom
    assert "`SYST:PINS?`" in check and "`src=`" in check
    assert "`SYST:PINS:DEF`" in fix and "GPIO43" in fix and "GPIO44" in fix
    for link in ("scpi.md#the-pin-map", "panel.md#the-pin-view", "carrier-board.md#22-"):
        assert link in defined, link
