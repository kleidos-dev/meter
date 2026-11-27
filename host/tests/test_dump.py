# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The ring dump decoded on the host: ``protocol.decode_dump`` against the instrument's own scaling.

The golden tests dump one simulated ring twice, ``RAW`` and ``SCALed``, and
hold the decoder's currents from the raw counts and the capture's
``FETCh:HEADer?`` to the thousandths the ``SCALed`` rows print, on every
sample, with a measured shunt and a null in force so that each term of
``scale.h`` is exercised.
"""

from __future__ import annotations

import zlib
from typing import Any

import pytest
from conftest import FakeClock
from kleidos_meter import Meter, MeterError
from kleidos_meter.protocol import (
    SHUNT_LSB_PV,
    Anchor,
    DumpMark,
    DumpScale,
    Mark,
    RawRow,
    ScaledRow,
    Trigger,
    current_nanoamps,
    decode_dump,
    parse_dump_row,
    parse_fields,
    parse_thousandths,
    split_units,
)
from kleidos_meter.sim import ScriptedWaveform, SimulatedMeter

#: A DUT that steps through a busy current, a small reverse one, a few
#: hundred nanoamps and a large one: negative quotients, sub-count values and
#: both ends of range 1.
WAVE = ScriptedWaveform([(0.02, 0.0886), (0.02, -0.000123), (0.02, 0.0000005), (0.02, 0.0301)])


def clocked(**knobs: Any) -> tuple[SimulatedMeter, FakeClock]:
    """A clock-driven simulator on a fake clock."""
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, **knobs)
    sim.read_timeout = 0.0
    return sim, clock


def ask(sim: SimulatedMeter, message: str) -> bytes:
    """Send *message* and return every byte it was answered with."""
    sim.write((message + "\n").encode())
    out = b""
    while chunk := sim.read(1 << 22):
        out += chunk
    return out


def units(sim: SimulatedMeter, message: str) -> list[str]:
    """The response units of *message*, no block in it."""
    return split_units(ask(sim, message).decode().strip().splitlines()[-1])


def block(sim: SimulatedMeter, query: str) -> bytes:
    """The payload of the definite-length block *query* is answered with."""
    out = ask(sim, query)
    width = int(out[1:2])
    length = int(out[2 : 2 + width])
    return out[2 + width : 2 + width + length]


def advance(sim: SimulatedMeter, clock: FakeClock, seconds: float) -> None:
    """Let *seconds* pass, reading the line every 0.1 s."""
    end = clock.now + seconds
    while clock.now + 1e-9 < end:
        clock.now = min(end, clock.now + 0.1)
        while sim.read(1 << 20):
            pass


def captured(range_index: int) -> tuple[SimulatedMeter, FakeClock]:
    """A full 4096-sample ring on *range_index*, with a measured shunt, a null and drift."""
    sim, clock = clocked(ring_capacity=4096, waveform=WAVE)
    sim.drift_ppm = 50.0
    assert units(sim, f"SENS:CURR:RANG:IND {range_index};:SYST:ERR:ALL?") == ['0,"No error"']
    units(sim, f"SENS:CURR:SHUN:RANG{range_index} 0.100123;:CAL:ZERO 1;:SYST:ERR:ALL?")
    advance(sim, clock, 1.1)
    units(sim, "ABOR;:SENS:PROF TRAC;:INIT;:SYST:ERR:ALL?")
    advance(sim, clock, 0.3)
    units(sim, "TRAC:MARK 'near';:SYST:ERR:ALL?")
    advance(sim, clock, 0.4)
    units(sim, "TRAC:MARK 'far';:SYST:ERR:ALL?")
    advance(sim, clock, 1.0)  # the ring ends by itself when full
    assert units(sim, "ABOR;:TRAC:POIN?;:SYST:ERR:ALL?")[0] == "4096"
    return sim, clock


@pytest.mark.parametrize("range_index", [0, 1])
def test_the_raw_dump_decodes_to_the_scaled_dumps_thousandths_on_every_sample(
    range_index: int,
) -> None:
    sim, _ = captured(range_index)
    header = parse_fields(units(sim, "FETC:HEAD?")[0])
    # Each term of scale.h is in force: the range's LSB, a measured shunt, a null.
    assert header["shunt_lsb_pv"] == str(SHUNT_LSB_PV[range_index])
    assert header["shunt_uohm"] == "100123"
    assert int(header["offset_pv"]) == -449_746
    raw = decode_dump(block(sim, "TRAC:DATA? RAW"), header)
    scaled_block = block(sim, "TRAC:DATA? SCAL")
    scaled = decode_dump(scaled_block)
    assert raw.raw and not scaled.raw and raw.crc_checked and scaled.crc_checked
    assert raw.count == scaled.count == 4096 and list(raw.indices) == list(scaled.indices)
    assert raw.nanoamps is not None and scaled.nanoamps is not None
    # The device's thousandths, read from each SCALed row as it is printed.
    printed = [
        (parse_fields(row)["t"], parse_fields(row)["v"])
        for row in scaled_block.decode().splitlines()
        if row.startswith("METER t=")
    ]
    assert len(printed) == 4096
    for offset, index in enumerate(raw.indices):
        t_text, v_text = printed[offset]
        assert raw.nanoamps[offset] == parse_thousandths(v_text), index
        assert raw.nominal_t_us(index) == int(t_text), index
    assert list(raw.nanoamps) == list(scaled.nanoamps)
    # The waveform's levels survive the round trip: a reverse current stays
    # negative (the quotient truncated toward zero), and so on.
    assert min(raw.nanoamps) < 0 < max(raw.nanoamps)
    assert raw.counts is not None and len(set(raw.counts)) == 4
    assert [(m.label, m.index) for m in raw.marks] == [(m.label, m.index) for m in scaled.marks]
    assert [m.label for m in raw.marks] == ["near", "far"]
    assert raw.anchors == scaled.anchors and [a.index for a in raw.anchors] == [0, 1024, 2048, 3072]


def test_timestamps_follow_the_drift_anchors() -> None:
    """Interpolated between the anchors, a timestamp is the drifting clock's to the microsecond."""
    sim, _ = captured(0)
    dump = decode_dump(block(sim, "TRAC:DATA? RAW"), units(sim, "FETC:HEAD?")[0])
    stats = sim.last_stats
    assert stats is not None
    for index in dump.indices:
        error = dump.t_us(index) - sim._sample_time(stats, index)
        # Past the last anchor the nominal period runs on: 50 ppm of 1023 periods.
        assert abs(error) <= (1 if index <= 3072 else 15), index
    nominal = abs(dump.nominal_t_us(3000) - sim._sample_time(stats, 3000))
    assert nominal > 30  # what the anchors correct
    assert list(dump.timestamps()) == [dump.t_us(index) for index in dump.indices]
    assert [m.t_us for m in dump.marks] == [dump.t_us(m.index) for m in dump.marks]
    samples = list(dump.samples())
    assert len(samples) == 4096 and samples[5].index == 5
    assert dump.counts is not None
    assert samples[5].t_us == dump.t_us(5) and samples[5].counts == dump.counts[5]


def test_a_partial_dump_starts_at_its_index_and_keeps_the_anchors_below_it() -> None:
    sim, _ = captured(0)
    header = units(sim, "FETC:HEAD?")[0]
    whole = decode_dump(block(sim, "TRAC:DATA? RAW"), header)
    part = decode_dump(block(sim, "TRAC:DATA? RAW,2050,40"), header)
    assert (part.start, part.count, list(part.indices)[-1]) == (2050, 40, 2089)
    assert [a.index for a in part.anchors] == [0, 1024, 2048]
    assert part.nanoamps is not None and whole.nanoamps is not None
    assert list(part.nanoamps) == list(whole.nanoamps[2050:2090])
    assert part.t_us(2060) == whole.t_us(2060)
    assert whole.counts is not None
    assert part.counts_between(2000, 2052) == list(whole.counts[2050:2052])
    assert part.counts_between(0, 10) == []


def test_a_triggered_ring_carries_its_first_index_and_its_trigger() -> None:
    sim, clock = clocked(ring_capacity=1024, waveform=ScriptedWaveform([(0.1, 0.01), (0.1, 0.2)]))
    units(sim, "SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.1;:TRIG:PRET 100;:INIT;:SYST:ERR:ALL?")
    advance(sim, clock, 0.5)
    stats = parse_fields(units(sim, "ABOR;:FETC:STAT?")[0])
    header = units(sim, "FETC:HEAD?")[0]
    dump = decode_dump(block(sim, "TRAC:DATA? RAW"), header)
    trig_n = int(stats["trig_n"])
    assert (dump.first, dump.start, dump.trig_n) == (trig_n - 100, trig_n - 100, trig_n)
    assert dump.trigger == Trigger(trig_n, int(stats["trig_t_us"]))
    assert dump.count == 1024
    # A partial dump past the trigger keeps its index, not its T row.
    later = decode_dump(block(sim, "TRAC:DATA? RAW,200,8"), header)
    assert (later.trig_n, later.trigger) == (trig_n, None)


def test_a_damaged_dump_is_refused() -> None:
    sim, _ = captured(0)
    sim.corrupt_dump = True
    with pytest.raises(ValueError, match="CRC-32"):
        decode_dump(block(sim, "TRAC:DATA? RAW"))
    meter = Meter(sim)
    with pytest.raises(MeterError, match="arrived damaged: CRC-32"):
        meter.trace()


def test_meter_trace_reads_the_header_and_the_dump_in_one_message() -> None:
    sim = SimulatedMeter()
    with Meter(sim) as meter:
        meter.set_profile("TRACE")
        meter.start()
        meter.mark("rstr")
        meter.stop()
        dump = meter.trace()
        rows = meter.dump(raw=False)
    assert dump.count == 64 and dump.counts is not None and set(dump.counts) == {28352}
    assert dump.nanoamps is not None and set(dump.nanoamps) == {88_600_000}
    assert {parse_fields(row)["v"] for row in rows if row.startswith("METER t=")} == {"88600.000"}
    assert dump.marks == (Mark("rstr", 64, dump.t_us(64)),)


def test_meter_trace_without_a_block_is_an_error() -> None:
    class Silent:
        """Answers the message with its error queue and no block."""

        def __init__(self) -> None:
            self.out = b""

        def write(self, data: bytes) -> int:
            self.out += b'0,"No error"\n'
            return len(data)

        def read(self, size: int) -> bytes:
            chunk, self.out = self.out[:size], self.out[size:]
            return chunk

        def close(self) -> None:
            pass

    with pytest.raises(MeterError, match="no header and block"):
        Meter(Silent()).trace()


# --- The arithmetic and the rows -------------------------------------------------


def test_current_nanoamps_is_scale_h_to_the_integer() -> None:
    # scale.h's own check: one range-0 count at the nominal shunt is CURRENT_LSB.
    assert current_nanoamps(1, 312_500, 0, 100_000) == 3125
    # The quotient truncates toward zero, as C++ does, not toward minus infinity.
    assert current_nanoamps(-1, 312_500, 0, 100_123) == -3121
    assert current_nanoamps(1, 312_500, 0, 100_123) == 3121
    # The null is subtracted before the division.
    assert current_nanoamps(0, 312_500, -449_746, 100_000) == 4497
    assert current_nanoamps(7, 78_125, 1_000, 0) == 0  # the firmware's guard
    assert current_nanoamps(5, 78_125, 0, -100_000) == -3906


def test_the_scale_comes_from_the_header() -> None:
    header = "range=1 shunt_lsb_pv=78125 shunt_uohm=99876 offset_pv=-12"
    assert DumpScale.from_header(header) == DumpScale(78_125, -12, 99_876)
    assert DumpScale.from_header({"range": "0", "shunt_uohm": "1", "offset_pv": "0"}) == DumpScale(
        312_500, 0, 1
    )
    assert DumpScale(312_500, 0, 100_000).nanoamps(2) == 6250
    for bad in ("shunt_uohm=1 offset_pv=0", "range=2 shunt_uohm=1 offset_pv=0", "range=0"):
        with pytest.raises(ValueError, match="lacks the scale"):
            DumpScale.from_header(bad)


def test_thousandths_read_back_exactly() -> None:
    assert parse_thousandths("88600.000") == 88_600_000
    assert parse_thousandths("-0.005") == -5
    assert parse_thousandths("0.123") == 123
    for bad in ("1.5", "1.2345", "x", "+1.000", "1e3"):
        with pytest.raises(ValueError, match="three-decimal"):
            parse_thousandths(bad)


def test_each_body_row_parses_and_the_others_are_not_rows() -> None:
    assert parse_dump_row("R n=16 c=1,-2,3") == RawRow(16, (1, -2, 3))
    assert parse_dump_row("A n=1024 t=1286720") == Anchor(1024, 1_286_720)
    assert parse_dump_row("T n=358 t=1100240") == Trigger(358, 1_100_240)
    marked = parse_dump_row("K n=7 label=k1:open")
    assert isinstance(marked, DumpMark) and marked.label == "k1:open"
    assert parse_dump_row("METER t=1000280 ch=i_ua v=-1.250") == ScaledRow(1_000_280, -1250)
    for other in ("METER_DUMP count=1", "METER_DUMP_END crc32=0 bytes=0", "", "S,1,2"):
        assert parse_dump_row(other) is None
    for bad in ("R n=x c=1", "R n=0", "A n=1", "METER t=5 ch=v_mv v=1.000", "METER t=5 ch=i_ua"):
        with pytest.raises(ValueError, match="malformed dump row"):
            parse_dump_row(bad)


def seal(rows: list[str]) -> bytes:
    """A block of *rows* closed by its CRC row."""
    body = ("\n".join(rows) + "\n").encode()
    return body + f"METER_DUMP_END crc32={zlib.crc32(body):08x} bytes={len(body)}\n".encode()


RAW_OPEN = "METER_DUMP count=3 t0_us=1000 period_us=280 fmt=raw20"


def test_a_dump_from_before_the_crc_decodes_unchecked() -> None:
    legacy = f"{RAW_OPEN}\nR n=0 c=1,2,3\nMETER_DUMP_END\n".encode()
    dump = decode_dump(legacy)
    assert not dump.crc_checked and dump.nanoamps is None and dump.counts is not None
    assert list(dump.counts) == [1, 2, 3]
    assert dump.t_us(2) == 1560  # no anchor: the nominal period
    with pytest.raises(ValueError, match="SCALed dump holds no raw counts"):
        decode_dump(
            seal(["METER_DUMP count=1 t0_us=0 period_us=280 ring=8", "METER t=0 ch=i_ua v=1.000"])
        ).counts_between(0, 1)


@pytest.mark.parametrize(
    ("rows", "match"),
    [
        (["R n=0 c=1,2,3"], "does not open with METER_DUMP"),
        (["METER_DUMP count=3 period_us=280 fmt=raw20", "R n=0 c=1,2,3"], "malformed opening"),
        ([RAW_OPEN, "R n=0 c=1", "R n=2 c=2,3"], "follows index 0"),
        ([RAW_OPEN, "R n=0 c=1,2"], "holds 2 samples"),
        ([RAW_OPEN, "R n=0 c=1,2,3", "METER t=1000 ch=i_ua v=1.000"], "RAW dump holds"),
        (
            ["METER_DUMP count=1 t0_us=1000 period_us=280 ring=8", "METER t=1100 ch=i_ua v=1.000"],
            "not on the grid",
        ),
        (
            ["METER_DUMP count=1 t0_us=1000 period_us=0 ring=8", "METER t=1000 ch=i_ua v=1.000"],
            "not on the grid",
        ),
    ],
)
def test_a_malformed_dump_is_refused(rows: list[str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        decode_dump(seal(rows))


def test_a_header_from_another_capture_or_without_the_scale_is_refused() -> None:
    good = seal([RAW_OPEN, "R n=0 c=1,2,3"])
    with pytest.raises(ValueError, match="another capture's: t0_us=5"):
        decode_dump(good, "t0_us=5 period_us=280 range=0 shunt_uohm=1 offset_pv=0")
    with pytest.raises(ValueError, match="another capture's: period_us=1152"):
        decode_dump(good, {"period_us": "1152"})
    with pytest.raises(ValueError, match="lacks the scale"):
        decode_dump(good, "t0_us=1000")
    # A SCALed dump needs no scale: its currents are printed.
    scaled = seal(
        ["METER_DUMP count=1 t0_us=1000 period_us=280 ring=8", "METER t=1280 ch=i_ua v=2.000"]
    )
    dump = decode_dump(scaled, "t0_us=1000")
    assert dump.nanoamps is not None
    assert (dump.start, list(dump.nanoamps), dump.counts) == (1, [2000], None)


def test_the_anchors_correct_inside_their_span_and_run_on_outside_it() -> None:
    rows = [
        "METER_DUMP count=8 t0_us=0 period_us=100 fmt=raw20 first=2",
        "A n=4 t=410",
        "R n=2 c=0,0,0,0,0,0,0,0",
        "A n=8 t=830",
        "K n=6 label=x",
    ]
    dump = decode_dump(seal(rows))
    assert dump.first == 2 and dump.start == 2
    assert [dump.t_us(n) for n in (2, 3, 4, 5, 6, 7, 8, 9)] == [
        210,
        310,
        410,
        515,
        620,
        725,
        830,
        930,
    ]
    assert dump.marks == (Mark("x", 6, 620),)
    assert dump.anchors == (Anchor(4, 410), Anchor(8, 830))
