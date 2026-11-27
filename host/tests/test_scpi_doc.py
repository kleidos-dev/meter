# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/scpi.md's quick reference and worked session are the protocol as it stands.

The quick reference names the messages a QA session sends: each header in it
must be one the firmware's command table answers to
(``scripts/scpi_table.py``). The worked session is a bench transcript; its
messages are sent here, in its order, to the simulator, which answers as the
firmware does, and every answer the page shows must have the simulator's
form: a report the same keys in the same order (the keys left out where the
page cuts a report with ``...``), any other unit the same text, and the mark
the page shows must come back as a mark record. The values are the bench's
and are not compared.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from kleidos_meter.client import Meter
from kleidos_meter.protocol import parse_fields, split_units
from kleidos_meter.sim import SimulatedMeter

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "scpi.md"
sys.path.insert(0, str(ROOT / "scripts"))

import scpi_table  # noqa: E402

#: Where the page cuts a report short.
CUT = "..."

#: The records of the stream, which are not answers.
RECORD = ("METER ", "METER_MARK ")


def section(text: str, heading: str) -> str:
    """The body of *heading* in *text*, to the next heading of any level."""
    start = text.index(heading + "\n") + len(heading) + 1
    following = re.search(r"^#{2,3} ", text[start:], re.MULTILINE)
    return text[start : start + following.start()] if following else text[start:]


def quick_messages(text: str) -> list[str]:
    """The messages of the quick reference's Send column, in order."""
    messages = []
    for line in section(text, "## Quick reference").splitlines():
        cells = line.split(" | ")
        if line.startswith("|") and len(cells) == 3 and not cells[1].startswith("---"):
            messages += [m for m in re.findall(r"`([^`]+)`", cells[1]) if m != "\\n"]
    return messages


def exchanges(text: str) -> list[tuple[str, list[str]]]:
    """The worked session's messages, each with the answers and records the page shows."""
    body = section(text, "### A worked session")
    start = body.index("```text\n") + len("```text\n")
    block = body[start : body.index("\n```", start)]
    found: list[tuple[str, list[str]]] = []
    for line in block.splitlines():
        if line.startswith("> "):
            found.append((line[2:], []))
        elif line.startswith("< "):
            found[-1][1].append(line[2:])
        else:
            assert line == CUT, f"not a message, an answer or a cut: {line!r}"
    return found


def header_known(message: str, patterns: list[re.Pattern[str]]) -> list[str]:
    """The headers of *message* none of *patterns* answers to."""
    headers = [unit.strip().split(" ", 1)[0].lstrip(":") for unit in split_units(message)]
    return [header for header in headers if not any(p.fullmatch(header) for p in patterns)]


def same_form(shown: str, answered: str) -> list[str]:
    """What differs between a unit the page shows and the simulator's, one line each."""
    if "=" not in shown:
        return [] if shown == answered else [f"{shown!r} != {answered!r}"]
    keys, got = list(parse_fields(shown)), list(parse_fields(answered))
    if f" {CUT} " not in f" {shown} ":
        return [] if keys == got else [f"keys {keys} != {got}"]
    rest = iter(got)
    return [] if all(key in rest for key in keys) else [f"keys {keys} not among {got}"]


def page_text() -> str:
    """docs/scpi.md."""
    return PAGE.read_text(encoding="utf-8")


def test_every_header_of_the_quick_reference_is_in_the_table() -> None:
    patterns = [scpi_table.header_pattern(entry.pattern) for entry in scpi_table.commands()]
    messages = quick_messages(page_text())
    assert len(messages) > 25
    wrong = {m: unknown for m in messages if (unknown := header_known(m, patterns))}
    assert not wrong, f"docs/scpi.md's quick reference sends headers the table has not: {wrong}"


def test_the_worked_session_runs_on_the_simulator_as_the_page_shows() -> None:
    session = exchanges(page_text())
    assert len(session) >= 12
    meter = Meter(SimulatedMeter())
    meter.wait_ready()
    wrong = []
    for message, shown in session:
        if message == "*ESR?":
            continue  # wait_ready() has read it
        answers = [line for line in shown if not line.startswith(RECORD)]
        assert len(answers) == 1, f"{message}: the page shows {len(answers)} answers"
        response = meter.send(message)
        shown_units = split_units(answers[0])
        if len(shown_units) != len(response.units):
            wrong.append(f"{message}: {len(shown_units)} units, the simulator {response.units}")
            continue
        for unit, answered in zip(shown_units, response.units, strict=True):
            wrong += [f"{message}: {d}" for d in same_form(unit, answered)]
    marks = [line for _, shown in session for line in shown if line.startswith("METER_MARK ")]
    assert marks and [mark.label for mark in meter.capture.marks] == ["wake"]
    assert not wrong, "the worked session and the simulator differ:\n" + "\n".join(wrong)


@pytest.mark.parametrize(
    ("shown", "answered", "found"),
    [
        ("a=1 b=2", "a=3 b=4", []),
        ("a=1 b=2", "a=3 c=4", ["keys ['a', 'b'] != ['a', 'c']"]),
        ("a=1 ... c=2", "a=3 b=4 c=5", []),
        ("c=2 ... a=1", "a=3 b=4 c=5", ["keys ['c', 'a'] not among ['a', 'b', 'c']"]),
        ('0,"No error"', '0,"No error"', []),
        ("0", "1", ["'0' != '1'"]),
    ],
)
def test_the_comparison_reads_keys_cuts_and_values(
    shown: str, answered: str, found: list[str]
) -> None:
    assert same_form(shown, answered) == found


def test_the_readers_find_the_messages_and_a_header_not_in_the_table() -> None:
    page = "\n".join(
        [
            "## Quick reference",
            "| To | Send | Comes back |",
            "|---|---|---|",
            "| check | `\\n`, then `*ESR?` | `0` |",
            "### A worked session",
            "```text",
            "> *ESR?",
            "< 0",
            "...",
            "< METER t=1 ch=i_ua v=2",
            "```",
        ]
    )
    assert quick_messages(page) == ["*ESR?"]
    assert exchanges(page) == [("*ESR?", ["0", "METER t=1 ch=i_ua v=2"])]
    patterns = [scpi_table.header_pattern(entry.pattern) for entry in scpi_table.commands()]
    assert header_known("SENS:PROF SLE;:SENS:NOTHING 1", patterns) == ["SENS:NOTHING"]
