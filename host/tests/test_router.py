# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The line router: the same responses and records however the line's bytes are cut."""

from __future__ import annotations

import zlib

import pytest
from kleidos_meter.protocol import (
    BlockProgress,
    LineRouter,
    Mark,
    Overflow,
    Response,
    Sample,
    split_units,
)


def _block(payload: bytes) -> bytes:
    """*payload* as a definite-length block: ``#``, the width, the count, the bytes."""
    digits = str(len(payload)).encode("ascii")
    return b"#" + str(len(digits)).encode("ascii") + digits + payload


#: A dump of 0.11.0 and later: its rows hold every byte the router acts on
#: outside a block (newlines, both quotes, ';' and '#' with a header after it).
_BODY = (
    b"METER_DUMP count=2 t0_us=0 period_us=280 fmt=raw20\n"
    b"A n=0 t=0\n"
    b"R n=0 c=5,5\n"
    b"K n=1 label=x;#3225 \"'\n"
)
DUMP = _BODY + f"METER_DUMP_END crc32={zlib.crc32(_BODY):08x} bytes={len(_BODY)}\n".encode()

#: A dump before 0.11.0: a bare closing row, no CRC.
LEGACY_DUMP = b"METER_DUMP count=1 t0_us=0 period_us=280 ring=1048576\nR n=0 c=7\nMETER_DUMP_END\n"

#: A line holding a little of everything, and the events it must route to.
STREAM_AND_EVENTS: list[tuple[bytes, list[object]]] = [
    (b"METER t=1 ch=i_ua v=1.000\n", [Sample(1, {"i_ua": 1.0})]),
    (b"K,7,1960,cut\n", [Mark("cut", 7, 1960)]),
    # A block after a unit and before more units: the header, the dump, the queue.
    (
        b"METER_HDR fw=0.18.0;" + _block(DUMP) + b';0,"No error"\n',
        [Response(["METER_HDR fw=0.18.0", '0,"No error"'], [DUMP])],
    ),
    (b"S,2,3.0,4.0,,25.5\n", [Sample(2, {"i_ua": 3.0, "v_mv": 4.0, "die_c": 25.5})]),
    # A block that opens its line and ends it.
    (_block(LEGACY_DUMP) + b"\n", [Response([], [LEGACY_DUMP])]),
    # Quoted ';' and '#' neither split nor open a block, in either quote.
    (
        b"0,\"a;b#3x\";'it''s; #15ABCDE';1\n",
        [Response(['0,"a;b#3x"', "'it''s; #15ABCDE'", "1"])],
    ),
    (b"\n", []),
    # Two blocks in one line, the second empty.
    (
        _block(b"ABCDE") + b";" + _block(b"") + b';-113,"Undefined header"\n',
        [Response(['-113,"Undefined header"'], [b"ABCDE", b""])],
    ),
    # A closed quote leaves the next unit free to open a block.
    (b'"q";#15ABCDE\n', [Response(['"q"'], [b"ABCDE"])]),
    # A '#' inside a unit is text, even with a header after it, and so is a
    # '#' at a unit's start with no header after it, and the byte after that.
    (b"x#15ABCDE\n", [Response(["x#15ABCDE"])]),
    (b"#0abc;#\n", [Response(["#0abc", "#"])]),
    (b"##15ABCDE\n", [Response(["##15ABCDE"])]),
    (b"1\r\n", [Response(["1"])]),
    (b"METER_MARK mark=rstr n=64 t=17920\n", [Mark("rstr", 64, 17920)]),
]

STREAM = b"".join(chunk for chunk, _ in STREAM_AND_EVENTS)
EXPECTED = [event for _, events in STREAM_AND_EVENTS for event in events]


def _routed(*chunks: bytes) -> list[object]:
    router = LineRouter()
    events: list[object] = []
    for chunk in chunks:
        events += router.feed(chunk)
    assert router.held == 0
    return events


def _without_progress(events: list[object]) -> list[object]:
    return [event for event in events if not isinstance(event, BlockProgress)]


def test_the_whole_line_at_once_routes_every_unit_record_and_block() -> None:
    events = _routed(STREAM)
    assert _without_progress(events) == EXPECTED
    progress = [event for event in events if isinstance(event, BlockProgress)]
    assert progress == [
        BlockProgress(0, len(DUMP)),
        BlockProgress(len(DUMP), len(DUMP)),
        BlockProgress(0, len(LEGACY_DUMP)),
        BlockProgress(len(LEGACY_DUMP), len(LEGACY_DUMP)),
        BlockProgress(0, 5),
        BlockProgress(5, 5),
        BlockProgress(0, 0),
        BlockProgress(0, 5),
        BlockProgress(5, 5),
    ]


@pytest.mark.parametrize("cut", range(len(STREAM) + 1))
def test_a_cut_at_any_byte_offset_routes_the_same_events(cut: int) -> None:
    assert _without_progress(_routed(STREAM[:cut], STREAM[cut:])) == EXPECTED


def test_one_byte_at_a_time_routes_the_same_events_and_counts_each_block_byte() -> None:
    events = _routed(*(STREAM[i : i + 1] for i in range(len(STREAM))))
    assert _without_progress(events) == EXPECTED
    progress = [event for event in events if isinstance(event, BlockProgress)]
    dump = [event.received for event in progress if event.total == len(DUMP)]
    assert dump == list(range(len(DUMP) + 1))


def test_a_response_carries_its_first_block_as_block() -> None:
    assert Response(["1"]).block is None
    assert Response([], [b"A", b"B"]).block == b"A"


def test_a_block_still_arriving_is_held_and_a_reset_forgets_it() -> None:
    router = LineRouter()
    assert router.feed(b"#15AB") == [BlockProgress(0, 5), BlockProgress(2, 5)]
    assert router.held == 2
    router.reset()
    assert router.held == 0
    # The rest of the old block is now plain text.
    assert router.feed(b"CDE;1\n") == [Response(["CDE", "1"])]


def test_a_header_cut_before_its_count_waits_for_the_count() -> None:
    router = LineRouter()
    assert router.feed(b"#3") == []
    assert router.feed(b"00") == []
    assert router.feed(b"3abc") == [BlockProgress(0, 3), BlockProgress(3, 3)]
    assert router.feed(b"\n") == [Response([], [b"abc"])]


def test_a_line_over_the_limit_is_given_up_whole_and_the_next_one_is_routed() -> None:
    router = LineRouter(max_line=16)
    assert router.feed(b"x" * 10) == []
    assert router.feed(b"x;" * 15) == []
    assert router.held == 0
    assert router.feed(b"x" * 5) == []
    assert router.feed(b"\n1\n") == [Overflow(10 + 30 + 5 + 1), Response(["1"])]


def test_units_split_at_semicolons_outside_both_kinds_of_quote() -> None:
    assert split_units("TRAC:MARK 'a;b';:ABOR") == ["TRAC:MARK 'a;b'", ":ABOR"]
    assert split_units("CAL:TAG 'it''s;x';*IDN?") == ["CAL:TAG 'it''s;x'", "*IDN?"]
    # One kind of quote is a plain character inside the other.
    assert split_units('"it\'s;x";\'say "hi;"\';2') == ['"it\'s;x"', "'say \"hi;\"'", "2"]
    assert split_units("") == [""]
