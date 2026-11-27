# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/scpi.md's command reference is the command table, header for header (FW-30).

``docs/scpi.md`` is normative, and until FW-30 nothing held its command
reference to the firmware: a command added, renamed or removed, or a flag
changed, could leave the page behind. This reads the reference by the notation
the page itself defines ("How the **Command** column is written"): a header
per code span, ` / ` between a setting and its query, ` · ` between headers
that share a row and its flags, a leading ``:`` continuing the header before,
``<function>`` for each function the page lists and ``<n>`` for a numeric
suffix. The set of headers must equal the command table's
(``scripts/scpi_table.py``), each with the table's D, R and C and with as many
parameters as the table lets it take; the census build's queries must say that
they are.

Host-only: both files read as text.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "scpi.md"
sys.path.insert(0, str(ROOT / "scripts"))

import scpi_table  # noqa: E402

#: The section that holds the reference, and the one after it.
SECTION = "\n## Command reference\n"

#: The separators the notation defines, and whether the headers joined share the flags.
PAIR, SHARED = " / ", " · "

#: The functions ``<function>`` stands for, as the page lists them.
FUNCTIONS = re.compile(r"^- `<function>` in a header is (.*)\.$", re.MULTILINE)


class NotationError(ValueError):
    """A row of the reference the notation does not read."""


@dataclass(frozen=True)
class Row:
    """One header of the reference.

    :ivar flags: its refusals' letters, in the table's order.
    :ivar most: the most parameters it takes.
    :ivar census: whether the page says a census build alone answers it.
    """

    flags: str
    most: int
    census: bool = False


def reference(text: str) -> str:
    """The command reference's section of the page *text*."""
    _, found, rest = text.partition(SECTION)
    assert found, "the page has no command reference"
    return rest.partition("\n## ")[0]


def cells(line: str) -> list[str]:
    """The cells of one table row; a pipe inside a code span is written ``\\|``."""
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def nodes(header: str) -> list[str]:
    """The nodes of a header, each with its colon and its brackets."""
    return re.findall(r"\[:[A-Za-z0-9<>]+\]|:?[A-Za-z0-9<>]+", header)


def continued(header: str, before: str) -> str:
    """*header*, which starts with a colon, after *before*: from its last required node."""
    kept = nodes(before.rstrip("?"))
    while kept and kept[-1].startswith("["):
        kept.pop()
    if not kept:
        raise NotationError(f"{header} continues {before!r}, which has no required node")
    return "".join(kept[:-1]) + header


def count(parameters: str) -> int:
    """The most parameters the syntax *parameters* allows."""
    return parameters.count(",") + 1 if parameters else 0


def expand(first: str, flags: str, functions: list[str]) -> dict[str, tuple[str, int]]:
    """The headers of one row's first cell: each with its flags and its parameters.

    :raises NotationError: the cell is not what the notation allows.
    """
    spans = re.findall(r"`([^`]+)`", first)
    between = re.split(r"`[^`]+`", first)[1:-1]
    if not spans or any(gap not in (PAIR, SHARED) for gap in between):
        raise NotationError(f"not a list of headers: {first}")
    if not re.fullmatch(r"(?:`[^`]+`(?: / | · )?)+", first):
        raise NotationError(f"text outside the headers: {first}")
    out: dict[str, tuple[str, int]] = {}
    before = ""
    for index, span in enumerate(spans):
        written, _, parameters = span.replace("\\|", "|").partition(" ")
        header = continued(written, before) if written.startswith(":") else written
        own = flags
        if index > 0 and between[index - 1] == PAIR:
            if header != before + "?":
                raise NotationError(f"{header} is joined to {before} and is not its query")
            own = ""
        before = header
        for function in functions if "<function>" in header else [""]:
            name = header.replace("<function>", function).replace("<n>", "#")
            if name in out:
                raise NotationError(f"{name} is given twice in one row")
            out[name] = (own, count(parameters))
    return out


def documented(text: str) -> dict[str, Row]:
    """Every header of the page's command reference.

    :raises NotationError: a row the notation does not read, a flag it does not
        define, or a header given twice.
    """
    section = reference(text)
    listed = FUNCTIONS.search(section)
    functions = re.findall(r"`([^`]+)`", listed.group(1)) if listed else []
    rows: dict[str, Row] = {}
    columns: list[str] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            columns = []
            continue
        row = cells(line)
        if not columns:
            columns = row
            continue
        if columns[0] != "Command" or set("".join(row)) <= {"-", ":"}:
            continue
        letters = row[columns.index("Flags")].split() if "Flags" in columns else []
        if not set(letters) <= set(scpi_table.REFUSALS):
            raise NotationError(f"an undefined flag: {line[:100]}")
        flags = "".join(letter for letter in scpi_table.REFUSALS if letter in letters)
        for header, (own, most) in expand(row[0], flags, functions).items():
            if header in rows:
                raise NotationError(f"{header} is documented twice")
            rows[header] = Row(own, most, "a census build's alone" in row[-1])
    return rows


def differences(page: dict[str, Row], table: tuple[scpi_table.Entry, ...]) -> list[str]:
    """What the page and the table do not say alike, one line each."""
    entries = {entry.pattern: entry for entry in table}
    lines = [f"{header}: documented, not in the table" for header in page.keys() - entries.keys()]
    lines += [f"{header}: in the table, not documented" for header in entries.keys() - page.keys()]
    for header in page.keys() & entries.keys():
        row, entry = page[header], entries[header]
        if row.flags != entry.flags:
            lines.append(f"{header}: flags {row.flags!r} on the page, {entry.flags!r} in the table")
        if row.most != entry.most:
            lines.append(f"{header}: {row.most} parameters on the page, {entry.most} in the table")
        if row.census != entry.census:
            lines.append(f"{header}: a census build's on one side alone")
    return sorted(lines)


def whole_table() -> tuple[scpi_table.Entry, ...]:
    """The command table and the census build's queries."""
    return (*scpi_table.commands(), *scpi_table.census_commands())


def page_text() -> str:
    """docs/scpi.md."""
    return PAGE.read_text(encoding="utf-8")


def test_the_reference_is_the_table() -> None:
    found = differences(documented(page_text()), whole_table())
    assert not found, "docs/scpi.md and kCommands differ:\n" + "\n".join(found)


def test_an_extra_header_on_the_page_is_found() -> None:
    planted = page_text().replace(
        "| `*STB?` | | the Status Byte |",
        "| `*STB?` | | the Status Byte |\n| `DIAGnostic:PLANted?` | D | a planted command |",
    )
    assert planted != page_text()
    assert differences(documented(planted), whole_table()) == [
        "DIAGnostic:PLANted?: documented, not in the table"
    ]


def test_a_header_missing_from_the_page_is_found() -> None:
    planted = page_text().replace("| `*STB?` | | the Status Byte |\n", "")
    assert planted != page_text()
    assert differences(documented(planted), whole_table()) == [
        "*STB?: in the table, not documented"
    ]


def test_a_header_the_table_gains_is_found() -> None:
    extra = scpi_table.Entry("DIAGnostic:PLANted?", "", 0, "cmdPlanted")
    assert differences(documented(page_text()), (*whole_table(), extra)) == [
        "DIAGnostic:PLANted?: in the table, not documented"
    ]


@pytest.mark.parametrize(
    ("was", "becomes", "line"),
    [
        (
            "| `ABORt` | D |",
            "| `ABORt` | D R |",
            "ABORt: flags 'DR' on the page, 'D' in the table",
        ),
        (
            "| `SENSe:CHARge:RESet` | D R C |",
            "| `SENSe:CHARge:RESet` | D R |",
            "SENSe:CHARge:RESet: flags 'DR' on the page, 'DRC' in the table",
        ),
        (
            "`CALibration:DATE <year>,<month>,<day>` /",
            "`CALibration:DATE <year>,<month>` /",
            "CALibration:DATE: 2 parameters on the page, 3 in the table",
        ),
        (
            "| `*WAI` |",
            "| `*WAI <n>` |",
            "*WAI: 1 parameters on the page, 0 in the table",
        ),
        (
            "| `DIAGnostic:HEAP:CENSus?` | | a census build's alone",
            "| `DIAGnostic:HEAP:CENSus?` | | an image's",
            "DIAGnostic:HEAP:CENSus?: a census build's on one side alone",
        ),
    ],
)
def test_a_flag_a_count_and_a_census_mark_are_held(was: str, becomes: str, line: str) -> None:
    planted = page_text().replace(was, becomes)
    assert planted != page_text(), was
    assert differences(documented(planted), whole_table()) == [line]


def test_the_notation_is_expanded() -> None:
    assert expand("`*ESE <n>` / `*ESE?`", "D", []) == {"*ESE": ("D", 1), "*ESE?": ("", 0)}
    assert expand("`A[:B]:VOLTage[:DC]? [<range>]` · `:POWer[:DC]?` · `:ENERgy?`", "DR", []) == {
        "A[:B]:VOLTage[:DC]?": ("DR", 1),
        "A[:B]:POWer[:DC]?": ("DR", 0),
        "A[:B]:ENERgy?": ("DR", 0),
    }
    assert expand("`A:THReshold:MARK ON\\|OFF` / `:MARK?`", "R", []) == {
        "A:THReshold:MARK": ("R", 1),
        "A:THReshold:MARK?": ("", 0),
    }
    assert expand("`READ[:SCALar]:<function>?`", "", ["CURRent[:DC]", "ENERgy"]) == {
        "READ[:SCALar]:CURRent[:DC]?": ("", 0),
        "READ[:SCALar]:ENERgy?": ("", 0),
    }
    assert expand("`SHUNt:RANGe<n> <ohms>`", "R", []) == {"SHUNt:RANGe#": ("R", 1)}
    assert expand("`D? [SCALed\\|RAW][,<from>[,<count>]]`", "R", []) == {"D?": ("R", 3)}


@pytest.mark.parametrize(
    "cell",
    ["`A` and `B`", "A", "`A` / `B?`", "`:B?`", "`A` (all of them)", "`A` · `A`"],
)
def test_a_cell_the_notation_does_not_read_is_an_error(cell: str) -> None:
    with pytest.raises(NotationError):
        expand(cell, "", [])


@pytest.mark.parametrize(
    ("was", "becomes"),
    [
        ("| `ABORt` | D |", "| `ABORt` | D X |"),
        ("| `*WAI` | |", "| `*STB?` | |"),
    ],
)
def test_an_undefined_flag_and_a_header_given_twice_are_errors(was: str, becomes: str) -> None:
    planted = page_text().replace(was, becomes)
    assert planted != page_text(), was
    with pytest.raises(NotationError):
        documented(planted)
