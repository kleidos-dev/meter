# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host tests' sweep runs every entry of the command table (FW-30).

``firmware/test/native/test_sweep.cpp`` loops over ``Console::commands()``
itself and requires as many entries as ``kSweptEntries`` and a fixture for
each, in the table's order. This is the other end: ``kSweptEntries`` and the
fixtures are what ``scripts/scpi_table.py`` reads from ``console.cpp``, so the
table the tests compiled, the one the guards read and the sweep are one, and
the sweep's file is among the host tests the Makefile builds. The reader's own
strictness is tested here too.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "firmware" / "test" / "native"
SWEEP = NATIVE / "test_sweep.cpp"
sys.path.insert(0, str(ROOT / "scripts"))

import scpi_table  # noqa: E402

TABLE = """
    static constexpr Command kCommands[] = {
        // a comment
        { "*IDN?", N, &Console::cmdIdn },
        { "A:B#", D | R | C | takes( 2U ), &Console::cmdAb },
        { "C", D | R, &Console::cmdC },
    };
"""


def fixture_patterns(text: str) -> list[str]:
    """The patterns of the sweep's fixtures, in their order."""
    found = re.search(r"\bkFixtures\[\]\s*=\s*\{(.*?)\n\};", text, re.DOTALL)
    assert found, "no kFixtures"
    return re.findall(r'^\s*\{\s*(?:\.pattern\s*=\s*)?"([^"]+)"', found.group(1), re.MULTILINE)


def test_the_sweep_counts_the_tables_entries() -> None:
    found = re.search(r"\bkSweptEntries\s*=\s*(\d+)\s*;", SWEEP.read_text(encoding="utf-8"))
    assert found, "no kSweptEntries"
    assert int(found.group(1)) == len(scpi_table.commands())


def test_the_sweep_has_a_fixture_for_every_entry_in_order() -> None:
    patterns = [entry.pattern for entry in scpi_table.commands()]
    assert fixture_patterns(SWEEP.read_text(encoding="utf-8")) == patterns


def test_the_sweep_is_built() -> None:
    assert re.search(r"\btest_sweep\.cpp\b", (NATIVE / "Makefile").read_text(encoding="utf-8"))


def test_the_reader_reads_each_kind_of_row() -> None:
    assert scpi_table.commands(TABLE) == (
        scpi_table.Entry("*IDN?", "", 0, "cmdIdn"),
        scpi_table.Entry("A:B#", "DRC", 2, "cmdAb"),
        scpi_table.Entry("C", "DR", 0, "cmdC"),
    )
    assert scpi_table.commands(TABLE)[0].query and not scpi_table.commands(TABLE)[1].query


@pytest.mark.parametrize(
    ("was", "becomes"),
    [
        ("kCommands[]", "kOthers[]"),  # no table
        ("&Console::cmdC }", "nullptr }"),  # not a row
        ("D | R, &", "D | X, &"),  # an unknown flag
        ('{ "C",', '{ "*IDN?",'),  # a header given twice
        ("takes( 2U )", "takes( two )"),  # an unreadable count
    ],
)
def test_the_reader_refuses_what_it_cannot_read_whole(was: str, becomes: str) -> None:
    planted = TABLE.replace(was, becomes)
    assert planted != TABLE
    with pytest.raises(scpi_table.TableError):
        scpi_table.commands(planted)


def test_an_empty_table_is_refused() -> None:
    with pytest.raises(scpi_table.TableError):
        scpi_table.commands("static constexpr Command kCommands[] = {\n    // none\n    };")


def test_the_census_queries_are_read_apart() -> None:
    census = scpi_table.census_commands()
    assert [entry.pattern for entry in census] == [
        "DIAGnostic:HEAP:CENSus?",
        "DIAGnostic:HEAP:CENSus:EVENt?",
    ]
    assert all(entry.census for entry in census)
    assert not any(entry.census for entry in scpi_table.commands())


def test_the_command_line_prints_the_count_and_the_entries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    total = len(scpi_table.commands())
    assert scpi_table.main(["--count"]) == 0
    assert capsys.readouterr().out == f"{total}\n"
    assert scpi_table.main(["--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == total + len(scpi_table.census_commands())
    assert scpi_table.main([]) == 0
    assert "(census build)" in capsys.readouterr().out


def test_the_command_line_fails_on_a_table_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    console = tmp_path / "console.cpp"
    console.write_text("no table here\n", encoding="utf-8")
    monkeypatch.setattr(scpi_table, "CONSOLE", console)
    assert scpi_table.main(["--count"]) == 1
    assert "no table kCommands" in capsys.readouterr().err
