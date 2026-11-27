# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulator's clock-driven mode: samples at the profile's rate, rings that fill."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from conftest import FakeClock
from kleidos_meter import Meter
from kleidos_meter.protocol import (
    Mark,
    Sample,
    check_dump,
    parse_errors,
    parse_fields,
    split_units,
)
from kleidos_meter.protocol import parse_record as record_of
from kleidos_meter.sim import HOLD_S, MAX_MARKS, ScriptedWaveform, SimulatedMeter


def sample_of(line: str) -> Sample:
    """The sample record *line* holds."""
    record = record_of(line)
    assert isinstance(record, Sample), line
    return record


def mark_of(line: str) -> Mark:
    """The mark record *line* holds."""
    record = record_of(line)
    assert isinstance(record, Mark), line
    return record


def clocked(**knobs: Any) -> tuple[SimulatedMeter, FakeClock]:
    """A clock-driven simulator on a fake clock that a read with nothing to say leaves alone."""
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, **knobs)
    sim.read_timeout = 0.0
    return sim, clock


def ask(sim: SimulatedMeter, message: str) -> tuple[list[str], list[str]]:
    """Send *message*; return the stream lines before its response, and the response's units."""
    sim.write((message + "\n").encode())
    out = b""
    while chunk := sim.read(1 << 20):
        out += chunk
    lines = out.decode().splitlines()
    records = [line for line in lines if record_of(line) is not None]
    rest = [line for line in lines if record_of(line) is None]
    return records, (split_units(rest[-1]) if rest else [])


def report(sim: SimulatedMeter, query: str) -> dict[str, str]:
    """One report query's fields, the stream drained first."""
    _, units = ask(sim, query)
    return parse_fields(units[0])


def errors(sim: SimulatedMeter) -> list[int]:
    """The codes the queue holds, emptied."""
    _, units = ask(sim, "SYST:ERR:ALL?")
    return [entry.code for entry in parse_errors(units[0])]


def run(sim: SimulatedMeter, clock: FakeClock, seconds: float, step: float = 0.1) -> list[str]:
    """Let *seconds* pass, reading the stream every *step*: the records it carried."""
    lines: list[str] = []
    end = clock.now + seconds
    while clock.now + 1e-9 < end:
        clock.now = min(end, clock.now + step)
        while chunk := sim.read(1 << 20):
            lines += chunk.decode().splitlines()
    return lines


@pytest.mark.parametrize(
    ("profile", "seconds", "rate", "tolerance"),
    [("STATE", 10, 868, 5), ("SLEEP", 100, 14.8, 0.2)],
)
def test_a_profile_streams_at_its_period_and_the_statistics_count_what_streamed(
    profile: str, seconds: int, rate: float, tolerance: float
) -> None:
    sim, clock = clocked()
    lines, _ = ask(sim, f"SENS:PROF {profile};:INIT")  # the sample at t0 comes at once
    lines += run(sim, clock, seconds)
    last, units = ask(sim, "ABOR;:FETC:STAT?")
    lines += last
    samples = [record_of(line) for line in lines]
    currents = [s for s in samples if isinstance(s, Sample) and "i_ua" in s.values]
    stats = parse_fields(units[0])
    assert abs(len(currents) / seconds - rate) <= tolerance
    assert int(stats["samples"]) == len(currents)
    assert stats["dropped"] == "0"
    times = [s.t_us for s in currents]
    period = {"STATE": 1152, "SLEEP": 67520}[profile]
    assert all(b - a == period for a, b in zip(times, times[1:], strict=False))
    assert stats["mean_na"] == "88600000.000"  # 88.6 mA is 28352 counts of 3.125 uA


def test_the_default_channels_are_the_firmwares_and_csv_rows_stream_too() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF SLE;:INIT")
    lines = run(sim, clock, 0.2)
    channels = {next(iter(sample_of(line).values)) for line in lines}
    assert channels == {"i_ua", "v_mv", "die_c"}
    ask(sim, "ABOR;:FORM CSV;:SENS:PROF STAT;:SENS:FUNC:ON CURR,POW;:INIT")
    rows = run(sim, clock, 0.05)
    assert rows and all(row.startswith("S,") for row in rows)
    assert set(sample_of(rows[0]).values) == {"i_ua", "p_mw"}
    _, units = ask(sim, "SENS:FUNC:ON?")
    assert units == ['"CURR","POW"']


def test_a_reader_that_falls_behind_loses_samples_counted_in_dropped() -> None:
    sim, clock = clocked(tx_capacity=4096)
    sim.write(b"SENS:PROF STAT;:INIT\n")
    clock.now += 2.0
    held = sim.read(1 << 20).decode().splitlines()
    assert sum(len(line) + 1 for line in held) <= 4096  # what the reader left is bounded
    status = report(sim, "SYST:STAT?")
    assert status["sink"] == "stream" and int(status["dropped"]) > 0
    _, units = ask(sim, "STAT:QUES:COND?")
    assert int(units[0]) & 512  # data lost
    _, units = ask(sim, "ABOR;:FETC:STAT?")
    stats = parse_fields(units[0])
    assert int(stats["samples"]) - int(stats["dropped"]) == len(held)
    assert int(stats["samples"]) == 1737  # 2 s at 1152 us, the first at t0


def test_a_ring_fills_over_time_with_an_anchor_every_1024_samples() -> None:
    sim, clock = clocked(ring_capacity=4096)
    sim.drift_ppm = 50.0
    ask(sim, "SENS:PROF TRAC;:INIT")
    run(sim, clock, 0.5)
    _, units = ask(sim, "TRAC:POIN?;:TRAC:POIN:MAX?")
    assert units == ["1786", "4096"]
    assert report(sim, "SYST:STAT?")["running"] == "1"
    ask(sim, "*OPC")
    run(sim, clock, 1.0)
    status = report(sim, "SYST:STAT?")
    assert (status["running"], status["sink"], status["samples"]) == ("0", "ring", "4096")
    _, units = ask(sim, "*ESR?")
    assert int(units[0]) & 1  # *OPC saw the ring end by itself
    sim.write(b"TRAC:DATA? RAW\n")
    block = b""
    while chunk := sim.read(1 << 22):
        block += chunk
    body = block[block.index(b"METER_DUMP") :].rsplit(b"\n", 2)[0] + b"\n"
    assert check_dump(body) is True
    rows = body.decode().splitlines()
    anchors = [parse_fields(row) for row in rows if row.startswith("A ")]
    assert [int(a["n"]) for a in anchors] == [0, 1024, 2048, 3072]
    t0 = int(parse_fields(rows[0])["t0_us"])
    assert int(anchors[1]["t"]) == t0 + round(1024 * 280 * (1 + 50e-6))
    counts = [c for row in rows if row.startswith("R ") for c in parse_fields(row)["c"].split(",")]
    assert len(counts) == 4096 and set(counts) == {"28352"}
    # A partial dump writes the anchors below its first row first, and the
    # ring that ended by itself dumps before ABORt closes it.
    sim.write(b"TRAC:DATA? SCAL,2050,3\n")
    partial = sim.read(1 << 20).decode().splitlines()
    head = [row for row in partial if row.startswith(("A ", "METER "))]
    assert head[0].startswith("A n=0 ") and head[1].startswith("A n=1024 ")
    assert head[2].startswith("A n=2048 ") and head[3].startswith("METER t=")
    assert len([row for row in partial if row.startswith("METER t=")]) == 3
    ask(sim, "TRAC:DATA? RAW,4096;:TRAC:DATA? RAW,0,0;:TRAC:DATA? RAW,x;:TRAC:DATA? RAW,1,x")
    assert errors(sim) == [-222, -224, -120, -120]
    ask(sim, "ABOR")
    _, units = ask(sim, "TRAC:POIN?")
    assert units == ["4096"]


def test_a_level_trigger_fires_on_the_crossing_and_keeps_its_pretrigger() -> None:
    wave = ScriptedWaveform([(0.1, 0.01), (0.1, 0.2)])
    sim, clock = clocked(ring_capacity=1024, waveform=wave)
    ask(sim, "SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.1;:TRIG:PRET 100;:INIT")
    assert report(sim, "SYST:STAT?")["trig"] == "armed"
    run(sim, clock, 0.5)
    status = report(sim, "SYST:STAT?")
    assert (status["running"], status["trig"]) == ("0", "fired")
    _, units = ask(sim, "ABOR;:FETC:STAT?")
    stats = parse_fields(units[0])
    trig_n = int(stats["trig_n"])
    assert trig_n == 358  # the first sample at 100 ms of the waveform's 200 ms
    assert int(stats["samples"]) == trig_n + 1024 - 100
    sim.write(b"TRAC:DATA?\n")
    rows = sim.read(1 << 22).decode().splitlines()
    head = parse_fields(rows[0].split("METER_DUMP", 1)[1])
    assert (head["first"], head["trig_n"]) == (str(trig_n - 100), str(trig_n))
    assert f"T n={trig_n} t={stats['trig_t_us']}" in rows
    values = [float(parse_fields(row)["v"]) for row in rows if row.startswith("METER t=")]
    assert len(values) == 1024
    assert values[99] < 100_000 <= values[100]


def test_a_level_trigger_needs_a_ring_and_a_slow_bus_refuses_burst() -> None:
    sim, _ = clocked()
    ask(sim, "TRIG:SOUR LEV;:SENS:PROF STAT;:INIT")
    assert errors(sim) == [-221]
    ask(sim, "SENS:PROF TRAC;:SENS:CAPT STAT;:INIT")
    assert errors(sim) == [-221]
    ask(sim, "*RST;:SENS:PROF BURS;:INIT")
    _, units = ask(sim, "SYST:ERR?")
    assert "BURST needs a faster bus" in units[0] and "95 us at 400000 Hz" in units[0]
    ask(sim, "SYST:COMM:I2C:FREQ 1000000;:INIT")
    assert errors(sim) == []
    ask(sim, "ABOR;:SENS:PROF TRAC;:SENS:FUNC:ON CURR;:INIT")
    assert errors(sim) == [-221]  # a ring profile takes no channels


def test_a_limited_capture_ends_by_itself_after_sample_count() -> None:
    sim, clock = clocked()
    lines, _ = ask(sim, "SENS:PROF STAT;:SAMP:COUN 50;:SENS:CAPT:LIM ON;:INIT;:*OPC")
    lines += run(sim, clock, 0.5)
    assert len(lines) == 50
    assert report(sim, "SYST:STAT?")["running"] == "0"
    _, units = ask(sim, "*ESR?")
    assert int(units[0]) & 1
    ask(sim, "SENS:PROF SLE")
    assert errors(sim) == [-221]  # flag C: ended, not closed
    _, units = ask(sim, "ABOR;:FETC:STAT?")
    assert parse_fields(units[0])["samples"] == "50"


def test_statistics_only_counts_without_streaming_and_batches_double() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF STAT;:SENS:CAPT STAT;:INIT")
    assert report(sim, "SYST:STAT?")["sink"] == "stats"
    assert run(sim, clock, 1.0) == []
    _, units = ask(sim, "ABOR;:FETC:STAT?;:FETC:STAT:BATC?")
    stats, batches = parse_fields(units[0]), parse_fields(units[1])
    assert stats["samples"] == "869"
    # 869 samples: batches of 16 once 64 of 8 were complete, 54 complete ones.
    assert (batches["batches"], batches["batch_samples"]) == ("54", "16")
    assert stats["batches"] == "54" and len(batches["means_na"].split(",")) == 54
    assert stats["sd_na"] == "0.000" and stats["min_na"] == stats["max_na"]


def test_threshold_marks_follow_a_square_wave() -> None:
    wave = ScriptedWaveform([(0.01, 0.02), (0.01, 0.08)])
    sim, clock = clocked(waveform=wave)
    ask(sim, "SENS:PROF STAT;:SENS:CURR:THR 0.05;:SENS:CURR:THR:HYST 0.001;")
    ask(sim, "SENS:CURR:THR:MARK ON;:INIT")
    lines = run(sim, clock, 0.1)
    marks = [r for r in map(record_of, lines) if isinstance(r, Mark)]
    assert [m.label for m in marks[:4]] == ["thr_rise", "thr_fall", "thr_rise", "thr_fall"]
    _, units = ask(sim, "ABOR;:FETC:STAT?")
    stats = parse_fields(units[0])
    assert int(stats["thr_marks"]) == len(marks) and stats["rises"] == "5"
    assert 0 < int(stats["above_n"]) < int(stats["samples"])
    assert float(stats["min_na"]) < 50e6 < float(stats["max_na"])
    assert float(stats["sd_na"]) > 0


def test_the_hold_recloses_the_lead_in_real_time_marked_under_a_capture() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF SLE;:INIT;:ROUT:LEAD OPEN,FORC,LIVE")
    lines = run(sim, clock, HOLD_S + 1, step=1.0)
    marks = [r for r in map(record_of, lines) if isinstance(r, Mark)]
    assert [m.label for m in marks] == ["hold:k1:closed"]
    assert marks[0].index == HOLD_S * 1_000_000 // 67520 + 1  # the sample the hold fell at
    assert report(sim, "ROUT:STAT?")["path"] == "shunt"
    assert errors(sim) == [114]
    _, units = ask(sim, "STAT:QUES:COND?")
    assert int(units[0]) & 1024  # hold


def test_a_lead_cycle_and_a_null_window_end_on_the_clock() -> None:
    sim, clock = clocked()
    ask(sim, "ROUT:LEAD:CYCL 200,FORC")
    run(sim, clock, 0.25)
    assert report(sim, "ROUT:STAT?")["path"] == "shunt"
    ask(sim, "CAL:ZERO 2")
    _, units = ask(sim, "STAT:OPER:COND?")
    assert units == ["1"]
    run(sim, clock, 1.0)
    taking = report(sim, "CAL:ZERO:RES?")
    assert (taking["n_expect"], taking["path"]) == ("7142", "shunt")  # TRACE, 280 us
    assert 3500 < int(taking["samples"]) < 3700
    run(sim, clock, 1.1)
    ended = report(sim, "CAL:ZERO:RES?")  # open until ABORt, all its samples taken
    assert (ended["running"], ended["samples"]) == ("1", "7142")
    ask(sim, "INIT")
    assert errors(sim) == [112]  # ended, not closed
    ask(sim, "ABOR")
    closed = report(sim, "CAL:ZERO:RES?")
    assert (closed["running"], closed["n"], closed["stored"]) == ("0", "7142", "1")


def test_the_null_reports_carry_the_circuit_the_window_was_opened_in() -> None:
    """The keys of the firmware's two reports (``cmdZeroResult``), in their order."""
    circuit = ["k1", "k2", "k3", "k4", "path", "check"]
    sim = SimulatedMeter()
    ask(sim, "SENS:PROF SLE;:ROUT:NULL ON;:CAL:ZERO 60")
    running = report(sim, "CAL:ZERO:RES?")
    assert list(running) == ["running", "profile", "over_s", "n_expect", "samples", *circuit]
    assert (running["profile"], running["n_expect"]) == ("SLEEP", "888")
    ask(sim, "ABOR;:ROUT:NULL OFF")
    closed = report(sim, "CAL:ZERO:RES?")
    head = ["running", "range", "offset_pv", "sd_pv", "over_s", "die_mc", "n"]
    assert list(closed) == [*head, *circuit, "stored"]
    assert (closed["n"], closed["path"], closed["k1"]) == ("888", "bypass", "lead:open")


def test_the_watchdog_test_reboots_after_the_timeout_and_reports_the_released_coil() -> None:
    sim, clock = clocked()
    ask(sim, "ROUT:LEAD OPEN,FORC")
    sim.write(b"DIAG:WATC:TEST\n")
    sim.write(b"*IDN?\n")
    clock.now += 4.0
    assert sim.read(1 << 16) == b""  # nothing answers while the watchdog starves
    clock.now += 1.5
    health = report(sim, "SYST:HEAL?")
    assert (health["reset"], health["reset_released"]) == ("task_wdt", "k1")
    assert int(health["uptime_us"]) < 1_100_000  # the boot started the clock again
    _, units = ask(sim, "*ESR?")
    assert int(units[0]) & 0x80
    assert errors(sim) == [116]
    assert report(sim, "ROUT:STAT?")["path"] == "shunt"


def test_an_injected_reboot_loses_the_capture_and_keeps_the_calibration() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:CURR:SHUN 0.1002;:CAL:TAG 'bench';:SENS:PROF STAT;:INIT")
    run(sim, clock, 0.1)
    sim.inject_reboot("panic", writes=1)
    sim.write(b"*IDN?\n")  # discarded while it boots
    assert sim.read(1 << 16) == b""
    health = report(sim, "SYST:HEAL?")
    assert (health["reset"], health["reset_released"]) == ("panic", "-")
    _, units = ask(sim, "FETC:STAT?;:SENS:CURR:SHUN?;:CAL:TAG?")
    assert units[0:2] == ["0.1002", '"bench"']
    assert errors(sim) == [-230]
    assert report(sim, "SYST:STAT?")["sink"] == "idle"


def test_the_status_registers_latch_edges_through_their_filters() -> None:
    sim, clock = clocked()
    ask(sim, "STAT:OPER:ENAB 16;:*SRE 128;:SENS:PROF STAT;:INIT")
    _, units = ask(sim, "*STB?;:STAT:OPER?;:STAT:OPER?")
    assert units == [str(0x80 | 0x40), "16", "0"]
    ask(sim, "STAT:OPER:NTR 16;:STAT:OPER:PTR 0;:ABOR")
    _, units = ask(sim, "STAT:OPER?;:STAT:OPER:NTR?;:STAT:OPER:PTR?;:STAT:OPER:ENAB?")
    assert units == ["16", "16", "0", "16"]
    ask(sim, "STAT:PRES")
    _, units = ask(sim, "STAT:OPER:ENAB?;:STAT:OPER:PTR?;:STAT:QUES:NTR?;:STAT:QUES:ENAB?")
    assert units == ["0", "32767", "0", "0"]
    ask(sim, "STAT:QUES:PTR 40000")
    ask(sim, "STAT:QUES:ENAB 40000")
    ask(sim, "STAT:QUES:ENAB")
    assert errors(sim) == [-222, -224, -109]
    ask(sim, "*CLS")
    sim.quality = "uncal"
    ask(sim, "STAT:QUES:ENAB 256;:*ESE 32")
    _, units = ask(sim, "*STB?;:STAT:QUES?;:STAT:QUES:COND?;:*ESE?;:*SRE?")
    assert units == ["8", "256", "256", "32", "128"]
    ask(sim, "BOGUS")
    _, units = ask(sim, "*STB?;:*ESR?")
    assert units == [str(4 | 32), "32"]  # an error queued; the command error reached the ESR
    ask(sim, "*CLS;:*OPC")
    _, units = ask(sim, "*ESR?;:*OPC?;:*WAI;:*STB?")
    assert units == ["1", "1", "0"]
    sim.diag_flags |= 0x0200  # MATHOF
    _, units = ask(sim, "STAT:QUES:COND?")
    assert int(units[0]) & 2


def test_the_simulators_entry_points_share_one_lock() -> None:
    sim = SimulatedMeter(clocked=True)
    got: list[bytes] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            got.append(sim.read(1 << 16))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        sim.write(b"SENS:PROF STAT;:INIT\n")
        for _ in range(50):
            sim.write(b"*IDN?\n")
            time.sleep(0.002)
        sim.write(b"ABOR;:FETC:STAT?\n")
        time.sleep(0.1)
    finally:
        stop.set()
        thread.join()
    lines = b"".join(got).decode().splitlines()
    assert sum(line.startswith("KLEIDOS,") for line in lines) == 50
    stats = parse_fields(next(line for line in lines if line.startswith("samples=")))
    streamed = [line for line in lines if line.startswith("METER t=")]
    assert all(record_of(line) is not None for line in streamed)
    assert int(stats["samples"]) == len(streamed) and stats["dropped"] == "0"


def test_a_meter_on_the_clock_driven_simulator_waits_for_its_samples() -> None:
    sim, clock = clocked()
    sim.read_timeout = 0.05  # a port's: the meter's own clock moves while it waits
    meter = Meter(sim, clock=clock)
    meter.exchange(["SENS:PROF STAT", "INIT"])
    clock.now += 0.1
    stats = meter.stop()
    assert int(stats["samples"]) == len(meter.capture.channel("i_ua")) == 87


def test_the_heap_report_carries_the_four_counts_from_app_main() -> None:
    sim, _ = clocked()
    heap = report(sim, "DIAG:HEAP?")
    assert list(heap)[-4:] == [
        "pre_main_allocs",
        "pre_main_frees",
        "allocs_since_main",
        "frees_since_main",
    ]
    assert (heap["allocs_since_main"], heap["frees_since_main"]) == ("0", "0")


def test_the_scan_the_bus_speed_and_the_trigger_settings() -> None:
    sim, _ = clocked()
    assert report(sim, "DIAG:I2C:SCAN?") == {"expected": "0x40", "found": "0x40", "n": "1"}
    sim.scan_found = []
    assert report(sim, "DIAG:I2C:SCAN?")["found"] == "-"
    ask(sim, "SYST:COMM:I2C:FREQ 40000;:SYST:COMM:I2C:FREQ x;:SYST:COMM:I2C:FREQ 800000")
    assert errors(sim) == [-222, -120]
    _, units = ask(sim, "SYST:COMM:I2C:FREQ?;:SYST:STAT?")
    assert units[0] == "800000" and "scl_hz=800000" in units[1]
    ask(sim, "TRIG:SLOP NEG;:TRIG:PRET 5;:TRIG:LEV -0.25;:SAMP:TIM?")
    _, units = ask(sim, "TRIG:SLOP?;:TRIG:PRET?;:TRIG:LEV?;:SAMP:TIM?;:FORM?")
    assert units == ["NEG", "5", "-0.25", "0.00028", "TEXT"]
    ask(sim, "TRIG:PRET 1048576;:TRIG:LEV 2;:TRIG:LEV x;:TRIG:SLOP UP;:FORM XML;:SAMP:COUN 0")
    assert errors(sim) == [-222, -222, -104, -224, -224, -222]
    ask(sim, "*RST")
    _, units = ask(sim, "TRIG:SLOP?;:TRIG:PRET?;:TRIG:LEV?;:SYST:COMM:I2C:FREQ?;:CAL:VBUS?")
    assert units == ["POS", "0", "0.0", "400000", "UNKNOWN"]
    ask(sim, "CAL:VBUS SOUR;:SENS:CURR:THR:MARK ON")
    _, units = ask(sim, "CAL:VBUS?;:SENS:CURR:THR:MARK?;:SENS:CURR:THR:HYST?")
    assert units == ["SOURCE", "1", "0.0"]


def test_a_scripted_waveform_repeats_or_holds_its_last_step() -> None:
    wave = ScriptedWaveform([(1, 0.1), (1, 0.2)])
    assert (wave(0), wave(999_999), wave(1_000_000), wave(2_000_000)) == (0.1, 0.1, 0.2, 0.1)
    held = ScriptedWaveform([(1, 0.1), (1, 0.2)], repeat=False)
    assert held(5_000_000) == 0.2 and held.period_us == 2_000_000
    with pytest.raises(ValueError):
        ScriptedWaveform([])
    with pytest.raises(ValueError):
        ScriptedWaveform([(0, 0.1)])


def test_a_level_trigger_aborted_while_armed_dumps_the_ring_it_wrapped() -> None:
    wave = ScriptedWaveform([(1.0, 0.2)])
    sim, clock = clocked(ring_capacity=1024, waveform=wave)
    ask(sim, "SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:SLOP NEG;:TRIG:LEV 0.1;:INIT")
    run(sim, clock, 0.5)  # never below the level: armed, the ring running circular
    ask(sim, "TRAC:MARK 'late'")
    _, units = ask(sim, "SYST:STAT?;:TRAC:POIN?")
    assert "trig=armed" in units[0] and units[1] == "1024"
    ask(sim, "ABOR")
    sim.write(b"TRAC:DATA? RAW\n")
    rows = sim.read(1 << 22).decode().splitlines()
    head = parse_fields(rows[0].split("METER_DUMP", 1)[1])
    assert head["first"] == str(1786 - 1024) and "trig_n" not in head
    assert "K n=1786 label=late" not in rows  # a mark past the last sample is not dumped
    ask(sim, "*RST;:TRIG:SOUR LEV;:TRIG:SLOP NEG;:TRIG:LEV 0.3;:TRIG:PRET 500")
    ask(sim, "SENS:PROF TRAC;:INIT")
    run(sim, clock, 0.01)
    ask(sim, "TRAC:MARK 'in'")
    run(sim, clock, 0.01)
    sim.waveform = ScriptedWaveform([(1.0, 0.5)])
    run(sim, clock, 0.01)  # above the level now: armed for a fall
    sim.waveform = wave
    run(sim, clock, 0.4)
    _, units = ask(sim, "ABOR;:FETC:STAT?")
    assert parse_fields(units[0])["trig_n"] != "-"
    sim.write(b"TRAC:DATA? RAW\n")
    rows = sim.read(1 << 22).decode().splitlines()
    assert any(row.startswith("K n=") and row.endswith("label=in") for row in rows)


def test_marks_are_bounded_checked_and_written_in_the_streams_format() -> None:
    sim, clock = clocked()
    ask(sim, "TRAC:MARK 'x'")
    ask(sim, "FORM CSV;:SENS:PROF STAT;:INIT")
    for n in range(16):
        records, _ = ask(sim, f"TRAC:MARK 'm{n}'")
    assert records[-1].startswith("K,") and records[-1].endswith(",m15")
    ask(sim, "TRAC:MARK 'm16';:TRAC:MARK 'a b';:TRAC:MARK 'a=b'")
    assert errors(sim) == [111, 105, -224, -224]
    _, units = ask(sim, "TRAC:POIN?")
    assert units == ["0"]  # a stream holds no ring


def test_relay_orders_under_windows_and_their_refusals() -> None:
    sim, clock = clocked()
    ask(sim, "CAL:ZERO 1;:ROUT:LEAD OPEN,FORC")
    assert errors(sim) == [112]
    _, units = ask(sim, "SYST:STAT?;:*OPC")
    assert "sink=null" in units[0]
    run(sim, clock, 1.1)
    _, units = ask(sim, "*ESR?")
    assert int(units[0]) & 1  # *OPC waited for the null window
    ask(sim, "ABOR;:SENS:PROF STAT;:INIT;:ROUT:LEAD OPEN,FORC;:ROUT:LEAD:CYCL 100,FORC")
    assert errors(sim) == [-221, -221]
    ask(sim, "ABOR;:ROUT:LEAD OPEN,FORC;:ROUT:LEAD:CYCL 100,FORC;:ROUT:REL1 ON;:ROUT:REL9 ON")
    assert errors(sim) == [-221, -114, -114]
    ask(sim, "ROUT:LEAD CLOS;:ROUT:NULL ON;:MEAS:CURR?")
    assert errors(sim) == [101]  # the null path: the shunt carries nothing of the DUT


def test_a_new_window_waits_for_the_one_that_ended_to_be_closed() -> None:
    sim, clock = clocked(ring_capacity=1024)
    ask(sim, "SENS:PROF TRAC;:INIT")
    run(sim, clock, 0.5)
    ask(sim, "CAL:ZERO 1;:INIT")
    assert errors(sim) == [-221, -221]


def test_parameters_the_simulator_refuses_as_the_firmware_does() -> None:
    sim, _ = clocked()
    ask(sim, "*ESE 300;:SENS:CURR:SHUN abc;:SENS:CURR:SHUN;:SENS:CURR:THR 5")
    assert errors(sim) == [-224, -104, -109, -222]
    ask(sim, "CAL:SHUN")
    assert errors(sim) == [-221]  # range 0 has no null yet
    ask(sim, "CAL:ZERO 1;:ABOR;:CAL:SHUN")
    assert errors(sim) == [-230]  # TRACE converts no bus voltage
    ask(sim, "SENS:PROF SLE;:SAMP:COUN 9000;:CAL:SHUN")
    assert errors(sim) == [-222]  # 9000 x 67.52 ms is over 600 s


def test_the_holds_seconds_are_counted_in_whole_microseconds() -> None:
    """The firmware rounds whole microseconds up; a float's last bit is not a second more."""
    sim, clock = clocked()
    clock.now = 100.05
    assert (clock.now + 120) - clock.now > 120  # what the floats alone would make 121
    ask(sim, "ROUT:BYP ON")
    assert report(sim, "ROUT:STAT?")["hold_s"] == "120"
    clock.now += 0.000001
    assert report(sim, "ROUT:STAT?")["hold_s"] == "120"
    clock.now = 101.05
    assert report(sim, "ROUT:STAT?")["hold_s"] == "119"
    clock.now = 101.0500015
    assert report(sim, "ROUT:STAT?")["hold_s"] == "119"


def test_samples_taken_are_counted_in_whole_microseconds() -> None:
    """The sample whose period ends now is taken, whatever a float makes of the instant."""
    sim, clock = clocked()
    clock.now = 100.05
    ask(sim, "CAL:ZERO 1")
    clock.now = 100.05 + 10 * 280e-6  # ten TRACE periods
    assert report(sim, "CAL:ZERO:RES?")["samples"] == "10"
    ask(sim, "ABOR;:SENS:PROF STAT;:SAMP:COUN 1000")
    clock.now = 100.05
    ask(sim, "MEAS:CURR?")
    clock.now = 100.05 + 2 * 1152e-6  # two STATE periods
    sim.write(b"ABOR\n")
    sim.read(1 << 16)
    _, units = ask(sim, "SYST:ERR:ALL?")
    assert "the acquisition was aborted after 2 of 1000 samples" in units[0]


def test_the_bypass_and_the_null_path_make_the_firmwares_four_paths() -> None:
    sim, _ = clocked()
    assert report(sim, "ROUT:RES?")["moved"] == "-"  # no order since boot
    ask(sim, "ROUT:BYP ON")
    state = report(sim, "ROUT:STAT?")
    assert (state["k2"], state["path"], state["hold_s"]) == ("bypass:closed", "both", "120")
    ask(sim, "INIT;:CAL:ZERO 1;:MEAS:CURR?")
    assert errors(sim) == [101, 101, 101]  # the shunt carries a fraction
    ask(sim, "ROUT:BYP OFF;:ROUT:NULL ON")
    result = report(sim, "ROUT:RES?")
    assert (result["k1"], result["k2"], result["path"]) == ("lead:open", "bypass:closed", "bypass")
    assert result["moved"] == "k2,k1"  # make-before-break: the bypass closes first
    ask(sim, "INIT;:ROUT:LEAD:CYCL 100,FORC")
    assert errors(sim) == [101, -221]  # nothing of the DUT on the shunt; a cycle starts at shunt
    ask(sim, "ROUT:BYP OFF")
    assert errors(sim) == [109]  # the DUT would be left with no supply
    ask(sim, "ROUT:NULL OFF")
    result = report(sim, "ROUT:RES?")
    assert (result["path"], result["moved"]) == ("shunt", "k1,k2")
    assert report(sim, "ROUT:STAT?")["hold_s"] == "-"
    ask(sim, "ROUT:NULL OFF;:ROUT:NULL ON,FORC;:ROUT:LEAD SHUT")
    assert errors(sim) == [-224, -224]  # FORCe only after LIVE; a word the order does not take
    assert report(sim, "ROUT:RES?")["moved"] == "-"  # a refused order leaves no result


def test_relay_interlocks_under_a_capture_follow_the_firmware() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF STAT;:INIT;:ROUT:LEAD CLOS")
    assert errors(sim) == []  # an order that moves nothing is answered whatever runs
    ask(sim, "ROUT:BYP ON,LIVE;:ROUT:NULL ON,LIVE")
    assert errors(sim) == [110, 110]  # it would take the capture off the DUT's current
    records, _ = ask(sim, "ROUT:NULL ON,LIVE,FORC;:ROUT:BYP ON,LIVE")
    assert [mark_of(r).label for r in records] == ["k2:closed", "k1:open"]  # LIVE moves
    assert report(sim, "ROUT:STAT?")["path"] == "bypass"
    ask(sim, "ROUT:LEAD CLOS,LIVE;:ROUT:LEAD OPEN,LIVE")
    assert errors(sim) == []  # the capture is bypassed already: no FORCe needed again
    lines = run(sim, clock, HOLD_S + 1, step=1.0)
    marks = [r for r in map(record_of, lines) if isinstance(r, Mark)]
    assert [m.label for m in marks] == ["hold:k1:closed", "hold:k2:open"]  # K1 first
    assert errors(sim) == [113]
    assert report(sim, "ROUT:STAT?")["path"] == "shunt"
    ask(sim, "ABOR;:ROUT:BYP ON")
    run(sim, clock, HOLD_S + 1, step=1.0)
    assert errors(sim) == [115]  # the hold opened the bypass across a closed lead
    assert report(sim, "SYST:HEAL?")["hold_released"] == "2"


def test_moves_past_the_marks_are_driven_unmarked_and_a_cycle_marks_its_reclose() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF STAT;:INIT")
    for n in range(MAX_MARKS):
        ask(sim, f"TRAC:MARK 'm{n}'")
    records, _ = ask(sim, "ROUT:LEAD OPEN,FORC,LIVE")
    assert records == [] and report(sim, "ROUT:STAT?")["path"] == "none"
    ask(sim, "ABOR;:ROUT:LEAD CLOS;:INIT;:ROUT:LEAD:CYCL 100,FORC,LIVE")
    lines = run(sim, clock, 0.2)
    marks = [r for r in map(record_of, lines) if isinstance(r, Mark)]
    assert [m.label for m in marks] == ["k1:closed"]  # the reclose, on the console's loop


def test_a_lead_cycle_beside_a_closed_bypass_keeps_the_hold() -> None:
    sim, clock = clocked()
    ask(sim, "ROUT:LEAD:CYCL 200,FORC;:ROUT:BYP ON;:ROUT:LEAD CLOS")
    assert report(sim, "ROUT:STAT?")["path"] == "both"  # the explicit order cancelled the cycle
    ask(sim, "ROUT:BYP OFF;:ROUT:LEAD:CYCL 200,FORC")
    run(sim, clock, 0.1)
    ask(sim, "ROUT:BYP ON")
    assert report(sim, "ROUT:STAT?")["path"] == "bypass"
    run(sim, clock, 0.2)
    state = report(sim, "ROUT:STAT?")
    assert (state["path"], state["hold_s"]) == ("both", "120")  # recloses into path=both
    sim.inject_reboot("panic")
    health = report(sim, "SYST:HEAL?")
    assert health["reset_released"] == "k2"
    _, units = ask(sim, "SYST:ERR:ALL?")
    assert "released k2 (path=both)" in units[0]


def test_a_measure_holds_the_console_for_its_samples_and_what_came_meanwhile_waits() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF STAT;:SAMP:COUN 1000")  # 1.152 s of samples
    _, units = ask(sim, "MEAS:CURR?;:SYST:ERR:COUN?")
    assert units == []  # nothing yet: the acquisition holds the console
    _, units = ask(sim, "*IDN?")
    assert units == []  # waits in the inbox, not an ABORt
    clock.now += 1.2
    out = b""
    while chunk := sim.read(1 << 16):
        out += chunk
    first, second = out.decode().splitlines()
    assert first == "0.088600000;0"  # the acquisition's message resumed where it stopped
    assert second.startswith("KLEIDOS,")


def test_an_abort_ends_a_measure_and_the_rest_of_its_message_runs_in_its_turn() -> None:
    sim, clock = clocked()
    ask(sim, "SENS:PROF STAT;:SAMP:COUN 1000")
    ask(sim, "MEAS:CURR?;:SYST:ERR:ALL?")
    clock.now += 0.5
    sim.write(b"ABOR;:SYST:ERR:COUN?\n")
    out = b""
    while chunk := sim.read(1 << 16):
        out += chunk
    first, second = out.decode().splitlines()
    assert first.startswith('-230,"Data corrupt or stale;the acquisition was aborted after 434 of')
    assert second == "0"
