# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The golden transcript asks every query of the command table.

``firmware/test/native/test_golden.cpp`` records every byte the instrument
writes to its sessions and compares it with
``firmware/test/native/golden/console.transcript`` in every run (FW-24), so
that the formatter rewrite of FW-24 to FW-26 can be shown to change no byte
of the wire. A query the sessions never ask is a response the transcript
does not hold, and its formatting could change unseen: this holds the
sessions to the table, every query header of ``kCommands`` in
``firmware/src/console.cpp`` matched by at least one unit of the sessions'
arrays, as the console matches it. The sessions also give unknown headers on
purpose (the -113 they provoke is part of the transcript), so the reverse is
not required.

Host-only: both files read as text, with the every-command session's reader
(``test_heap_session_covers_the_table.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

from test_heap_session_covers_the_table import (
    pattern,
    session_messages,
    table_headers,
    unit_headers,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "firmware" / "test" / "native" / "test_golden.cpp"
TRANSCRIPT = ROOT / "firmware" / "test" / "native" / "golden" / "console.transcript"


def golden_arrays(text: str) -> tuple[str, ...]:
    """The names of the sessions' step arrays (``kGolden...``)."""
    return tuple(re.findall(r"\bconstexpr\s+Step\s+(kGolden\w+)\[\]\s*=", text))


def golden_units(text: str) -> list[str]:
    """The header of every unit the golden sessions send."""
    return [
        header
        for message in session_messages(text, golden_arrays(text))
        for header in unit_headers(message)
    ]


def test_the_sessions_are_found() -> None:
    arrays = golden_arrays(GOLDEN.read_text(encoding="utf-8"))
    assert "kGoldenQueries" in arrays and "kGoldenErrors" in arrays, arrays


def test_every_query_of_the_table_is_asked() -> None:
    queries = [h for h in table_headers() if h.endswith("?")]
    units = golden_units(GOLDEN.read_text(encoding="utf-8"))
    assert len(queries) > 70, len(queries)
    missing = [q for q in queries if not any(pattern(q).fullmatch(u) for u in units)]
    assert not missing, "queries of kCommands the golden sessions never ask:\n" + "\n".join(missing)


def test_a_query_left_out_is_found() -> None:
    """The guard fails on a query the sessions lack."""
    units = golden_units(GOLDEN.read_text(encoding="utf-8"))
    assert not any(pattern("DIAGnostic:PLANted?").fullmatch(u) for u in units)
    planted = 'constexpr Step kGoldenX[] = {\n    { "*IDN?;:SYST:ERR?", 1 },\n};\n'
    assert golden_units(planted) == ["*IDN?", "SYST:ERR?"]


def test_the_transcript_is_tracked_and_generated() -> None:
    """The file the test compares with exists and says it is generated."""
    text = TRANSCRIPT.read_text(encoding="utf-8")
    assert "\n# The golden transcript of firmware/test/native/test_golden.cpp" in text
    assert "\n## every query\n" in text and "\n## errors\n" in text
