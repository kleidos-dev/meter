#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The instrument's command table, read from the firmware (FW-30).

    python3 scripts/scpi_table.py            # every entry, one line each
    python3 scripts/scpi_table.py --count    # how many entries the table has
    python3 scripts/scpi_table.py --json     # the same entries, for another tool

``kCommands`` in ``firmware/src/console.cpp`` is the one definition of the
command set: no second list is written by hand. :func:`commands` reads it, in
its order, and the census build's two queries (``kCensusCommands``) after it,
marked as such; :func:`header_pattern` gives the headers an entry answers to,
as ``scpi::matches()`` reads them. What reads the table through here:
``tests/firmware/test_scpi_doc_matches_table.py`` (docs/scpi.md's command
reference), ``test_sweep_covers_the_table.py`` (the host tests' sweep),
``test_heap_session_covers_the_table.py`` and
``test_golden_session_covers_the_table.py``.

The reader is strict: a row it cannot read, a flag it does not know or a table
it does not find is a :exc:`TableError`, never a shorter table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The source that holds the table.
CONSOLE = ROOT / "firmware" / "src" / "console.cpp"

#: The command table, and the census build's queries.
TABLE = "kCommands"
CENSUS_TABLE = "kCensusCommands"

#: One row: ``{ "<pattern>", <flags>, &Console::<handler> }``.
ROW = re.compile(r'\{\s*"([^"]+)"\s*,\s*([^,{}]+?)\s*,\s*&Console::(\w+)\s*\}')

#: The refusals' letters, as docs/scpi.md's Flags column writes them.
REFUSALS = "DRC"

#: The flags that say how many parameters a command takes.
NONE = frozenset({"N", "kNoParameters"})
TAKES = re.compile(r"takes\(\s*([0-9]+)U\s*\)")


class TableError(ValueError):
    """The source does not hold a table this reader can read whole."""


@dataclass(frozen=True)
class Entry:
    """One row of the command table.

    :ivar pattern: the header, short form in upper case, optional nodes in
        square brackets, ``#`` for a numeric suffix.
    :ivar flags: the refusals' letters among :data:`REFUSALS`, in that order.
    :ivar most: the most parameters it takes, 0 for none.
    :ivar handler: the member of ``Console`` that runs it.
    :ivar census: whether only a census build answers it.
    """

    pattern: str
    flags: str
    most: int
    handler: str
    census: bool = False

    @property
    def query(self) -> bool:
        """Whether the header is a query's."""
        return self.pattern.endswith("?")


def without_comments(source: str) -> str:
    """*source* with its ``//`` comments removed; the table holds no ``//`` in a string."""
    return re.sub(r"//[^\n]*", "", source)


def table_body(source: str, name: str) -> str:
    """The initializer of the array *name* in *source*, its comments removed.

    :raises TableError: *source* declares no such table.
    """
    found = re.search(
        rf"\bstatic constexpr Command {name}\[\]\s*=\s*\{{(.*?)\n\s*\}};",
        without_comments(source),
        re.DOTALL,
    )
    if found is None:
        raise TableError(f"no table {name}[]")
    return found.group(1)


def read_flags(text: str, pattern: str) -> tuple[str, int]:
    """The refusals' letters and the parameter count of one row's flags.

    :raises TableError: a flag this reader does not know.
    """
    letters: set[str] = set()
    most = 0
    for part in (piece.strip() for piece in text.split("|")):
        takes = TAKES.fullmatch(part)
        if takes:
            most = int(takes.group(1))
        elif part in REFUSALS and len(part) == 1:
            letters.add(part)
        elif part not in NONE:
            raise TableError(f"{pattern}: an unknown flag {part!r}")
    return "".join(letter for letter in REFUSALS if letter in letters), most


def read_table(source: str, name: str = TABLE) -> tuple[Entry, ...]:
    """The rows of the table *name* in *source*, in its order.

    :raises TableError: no such table, a row of it this reader cannot read, or
        a header given twice.
    """
    body = table_body(source, name)
    entries = []
    for pattern, flags, handler in ROW.findall(body):
        letters, most = read_flags(flags, pattern)
        entries.append(Entry(pattern, letters, most, handler, census=name == CENSUS_TABLE))
    left = ROW.sub("", body).replace(",", "").split()
    if left:
        raise TableError(f"{name}: not a row: {' '.join(left)[:120]}")
    patterns = [entry.pattern for entry in entries]
    twice = sorted({p for p in patterns if patterns.count(p) > 1})
    if twice:
        raise TableError(f"{name}: given twice: {', '.join(twice)}")
    if not entries:
        raise TableError(f"{name} is empty")
    return tuple(entries)


def commands(source: str | None = None) -> tuple[Entry, ...]:
    """Every entry of the command table, which every image answers.

    :param source: console.cpp's text; the file itself when ``None``.
    """
    return read_table(CONSOLE.read_text(encoding="utf-8") if source is None else source)


def census_commands(source: str | None = None) -> tuple[Entry, ...]:
    """The census build's queries, which no other image answers.

    :param source: console.cpp's text; the file itself when ``None``.
    """
    text = CONSOLE.read_text(encoding="utf-8") if source is None else source
    return read_table(text, CENSUS_TABLE)


def header_pattern(pattern: str) -> re.Pattern[str]:
    """The headers *pattern* answers to, as a regular expression to ``fullmatch``.

    Each node in its long or its short form, the optional ones left out or
    not, a numeric suffix where the pattern takes one, in any case.
    """
    query = pattern.endswith("?")
    body = pattern[:-1] if query else pattern
    if body.startswith("*"):
        return re.compile(re.escape(body) + (r"\?" if query else ""), re.IGNORECASE)
    parts = []
    for optional, node in re.findall(r"(\[)?:?([A-Za-z0-9]+#?)\]?", body):
        name = node.rstrip("#")
        short = "".join(c for c in name if c.isupper() or c.isdigit())
        word = f"(?:{re.escape(name)}|{re.escape(short)})" + (r"\d*" if node.endswith("#") else "")
        parts.append(f"(?::{word})?" if optional else f":{word}")
    return re.compile("".join(parts)[1:] + (r"\?" if query else ""), re.IGNORECASE)


def main(argv: list[str] | None = None) -> int:
    """Print the table; the exit status."""
    parser = argparse.ArgumentParser(description="the instrument's command table")
    form = parser.add_mutually_exclusive_group()
    form.add_argument("--count", action="store_true", help="how many entries the table has")
    form.add_argument("--json", action="store_true", help="the entries as JSON, the census's too")
    options = parser.parse_args(argv)
    try:
        table, census = commands(), census_commands()
    except (OSError, TableError) as error:
        print(f"scpi_table: {error}", file=sys.stderr)
        return 1
    if options.count:
        print(len(table))
    elif options.json:
        print(json.dumps([entry.__dict__ for entry in (*table, *census)], indent=2))
    else:
        for entry in (*table, *census):
            note = "  (census build)" if entry.census else ""
            print(f"{entry.pattern:44} {entry.flags:3} {entry.most} {entry.handler}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
