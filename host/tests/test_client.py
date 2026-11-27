# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The client against the simulated instrument: the protocol end to end, no bench."""

from __future__ import annotations

import errno
import os
import sys
import types
from typing import Any, ClassVar

import pytest
from kleidos_meter import Meter, MeterError, MeterTimeout, SerialTransport, SimulatedMeter
from kleidos_meter.cli import main
from kleidos_meter.client import PortBusyError
from kleidos_meter.pins import DIGEST
from kleidos_meter.protocol import (
    ErrorEntry,
    Mark,
    Sample,
    block_header,
    parse_errors,
    parse_fields,
    parse_record,
    quote,
    split_units,
)


@pytest.fixture
def sim() -> SimulatedMeter:
    return SimulatedMeter(current_a=0.0886, voltage_v=4.064)


@pytest.fixture
def meter(sim: SimulatedMeter) -> Meter:
    return Meter(sim)


# --- The wire format ----------------------------------------------------------


def test_units_split_only_outside_strings() -> None:
    assert split_units('TRAC:MARK "a;b";:ABOR') == ['TRAC:MARK "a;b"', ":ABOR"]
    assert split_units('0,"Settings conflict;capture running"') == [
        '0,"Settings conflict;capture running"'
    ]
    assert split_units("1;2;3") == ["1", "2", "3"]


def test_errors_are_parsed_with_their_standard_text_and_detail() -> None:
    errors = parse_errors(
        '-221,"Settings conflict;capture running (ABORt first)",102,"Null not stored;say ""no"""'
    )
    assert errors == [
        ErrorEntry(-221, "Settings conflict;capture running (ABORt first)"),
        ErrorEntry(102, 'Null not stored;say "no"'),
    ]
    assert errors[0].standard == "Settings conflict"
    assert errors[0].detail == "capture running (ABORt first)"
    assert parse_errors('0,"No error"') == []


def test_reports_are_key_value_tokens() -> None:
    fields = parse_fields("k1=lead:open path=bypass vs_pv=1,2,3 q=cal")
    assert fields == {"k1": "lead:open", "path": "bypass", "vs_pv": "1,2,3", "q": "cal"}


@pytest.mark.parametrize(
    ("line", "record"),
    [
        ("METER t=100 ch=i_ua v=88066.019", Sample(100, {"i_ua": 88066.019})),
        ("METER_MARK mark=cut n=42 t=2500000", Mark("cut", 42, 2500000)),
        (
            "S,100,88066.019,4064.000,,25.5",
            Sample(100, {"i_ua": 88066.019, "v_mv": 4064.0, "die_c": 25.5}),
        ),
        ("K,42,2500000,cut", Mark("cut", 42, 2500000)),
        ("running=0 sink=idle", None),
        ('0,"No error"', None),
    ],
)
def test_stream_records_are_told_from_responses(line: str, record: object) -> None:
    assert parse_record(line) == record


def test_a_definite_block_header_is_read_whole_or_not_at_all() -> None:
    assert block_header(b"#") is None
    assert block_header(b"#3") is None
    assert block_header(b"#3225METER_DUMP") == (5, 225)
    with pytest.raises(ValueError):
        block_header(b"#0abc")


def test_strings_are_quoted_with_doubled_quotes() -> None:
    assert quote('say "hi"') == '"say ""hi"""'


# --- The exchange ---------------------------------------------------------------


def test_the_first_message_clears_the_queue_and_every_message_ends_in_its_errors(
    meter: Meter, sim: SimulatedMeter
) -> None:
    assert meter.idn() == "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0"
    meter.status()
    assert sim.messages[0] == "*CLS;:*IDN?;:SYST:ERR:ALL?"
    assert sim.messages[1] == "SYST:STAT?;:SYST:ERR:ALL?"


def test_the_information_report_names_the_images_elf_hash_after_the_image(meter: Meter) -> None:
    info = meter.info()
    assert info["elf"] == "5e1f0ba47c2291d3"
    # A new key is appended, never inserted: the hash follows the image, and
    # 0.18.0's pin map keys come after it.
    assert list(info)[-4:] == ["image", "elf", "pins_src", "pins_table"]


def test_the_heap_report_says_the_steady_state_allocated_nothing(
    meter: Meter, sim: SimulatedMeter
) -> None:
    heap = meter.heap()
    assert sim.messages[-1].endswith("DIAG:HEAP?;:SYST:ERR:ALL?")
    assert list(heap) == [
        "armed",
        "allocs_after_init",
        "frees_after_init",
        "maint_allocs",
        "maint_frees",
        "free_internal",
        "min_free_internal",
        "largest_internal",
        "free_spiram",
        # Appended for the zero-allocation image (0.17.0): the counts before and from app_main.
        "pre_main_allocs",
        "pre_main_frees",
        "allocs_since_main",
        "frees_since_main",
    ]
    assert (heap["armed"], heap["allocs_after_init"], heap["frees_after_init"]) == ("1", "0", "0")
    assert (heap["allocs_since_main"], heap["frees_since_main"]) == ("0", "0")
    with pytest.raises(MeterError) as refused:
        meter.report("DIAGnostic:HEAP? 1")
    assert refused.value.codes == [-108]


def test_a_refused_unit_raises_with_the_instruments_errors_and_the_rest_still_runs(
    meter: Meter, sim: SimulatedMeter
) -> None:
    with pytest.raises(MeterError) as refused:
        meter.exchange(["SENS:CURR:RANG:IND 2", "SENS:CURR:RANG:IND?"])
    assert refused.value.codes == [-222]
    assert "use SENSe:CURRent:RANGe:INDex 0|1" in str(refused.value)
    # The query after the refused command ran, and the queue was left empty.
    units, errors, _ = meter.exchange(["SENS:CURR:RANG:IND?"])
    assert units == ["0"] and errors == []


def test_long_and_short_forms_and_relative_paths_reach_the_same_command(
    meter: Meter, sim: SimulatedMeter
) -> None:
    units, _, _ = meter.exchange(["SENSE:PROFILE STATE", "sens:prof?", "SENS:CURR:RANG:IND 1;IND?"])
    assert units == ["STATE", "1"]


def test_an_unknown_header_is_minus_113(meter: Meter) -> None:
    with pytest.raises(MeterError) as refused:
        meter.exchange(["BOGUS:CMD"])
    assert refused.value.codes == [-113]


# --- Measurements and captures --------------------------------------------------


def test_measure_returns_the_mean_in_base_units(meter: Meter) -> None:
    meter.set_profile("STATE")
    assert meter.measure("CURR") == pytest.approx(0.0886)
    assert meter.measure("VOLT") == pytest.approx(4.064)
    assert meter.measure("POW") == pytest.approx(0.0886 * 4.064)


def test_a_voltage_measured_on_a_ring_profile_selects_state(meter: Meter) -> None:
    # 0.12.0: MEASure configures first, and TRACE converts no bus voltage.
    meter.set_profile("TRACE")
    assert meter.measure("VOLT") == pytest.approx(4.064)
    assert meter.query("SENS:PROF?") == "STATE"


def test_a_streamed_capture_keeps_samples_and_marks_apart_from_responses(meter: Meter) -> None:
    meter.set_profile("STATE")
    header = meter.start(["i_ua", "v_mv"])
    assert header["range"] == "0" and header["format"] == "text"
    mark = meter.mark("cut")
    stats = meter.stop()
    assert stats["samples"] == str(len(meter.capture.rows))
    assert mark.label == "cut" and mark in meter.capture.marks
    current = meter.capture.channel("i_ua")
    assert current and all(value == pytest.approx(88600.0) for _, value in current)
    assert len(meter.capture.channel("v_mv")) == len(current)


def test_a_csv_stream_is_one_row_per_sample(meter: Meter, sim: SimulatedMeter) -> None:
    meter.set_profile("STATE")
    meter.exchange(["FORM CSV"])
    meter.start(["i_ua", "die_c"])
    meter.stop()
    rows = list(meter.capture.rows.values())
    assert rows and all(set(row) == {"i_ua", "die_c"} for row in rows)


def test_a_capture_refuses_configuration_until_abort(meter: Meter) -> None:
    meter.set_profile("STATE")
    meter.start()
    with pytest.raises(MeterError) as refused:
        meter.set_profile("SLEEP")
    assert refused.value.codes == [-221]
    meter.stop()
    with pytest.raises(MeterError) as idle:
        meter.exchange(["ABOR"])
    assert idle.value.codes == [111]


def test_the_ring_dump_is_a_definite_block(meter: Meter) -> None:
    meter.set_profile("TRACE")
    meter.start()
    meter.mark("rstr")
    meter.stop()
    rows = meter.dump(raw=True)
    assert rows[0].startswith("METER_DUMP count=64 ")
    assert rows[-1].startswith("METER_DUMP_END crc32=")
    counts = [
        int(c) for row in rows if row.startswith("R ") for c in parse_fields(row)["c"].split(",")
    ]
    assert len(counts) == 64
    # One count of range 0 is 3.125 uA: 88.6 mA is 28352 counts.
    assert set(counts) == {28352}
    assert "K n=64 label=rstr" in rows


def test_a_null_window_reports_running_then_its_result(meter: Meter) -> None:
    started = meter.zero(60)
    assert started["running"] == "1"
    result, errors = meter.close_zero()
    assert result["running"] == "0" and result["stored"] == "1" and errors == []
    assert result["offset_pv"] == "-449746"


def test_a_lead_order_that_disconnects_the_dut_needs_force(meter: Meter) -> None:
    with pytest.raises(MeterError) as refused:
        meter.exchange(["ROUT:LEAD OPEN"])
    assert refused.value.codes == [109]
    units, _, _ = meter.exchange(["ROUT:LEAD OPEN,FORC", "ROUT:RES?"])
    assert parse_fields(units[0])["path"] == "none"
    # A DUT with no battery draws nothing, which the shunt reads: a capture
    # there is a reading (relay.h shuntCarriesAll); one on the null path is not.
    units, _, _ = meter.exchange(["ROUT:LEAD CLOS", "ROUT:NULL ON", "ROUT:RES?"])
    assert parse_fields(units[0])["path"] == "bypass"
    with pytest.raises(MeterError) as bypassed:
        meter.start()
    # INITiate refused, so FETCh:HEADer? had no capture either.
    assert bypassed.value.codes == [101, -230]


def test_an_acquisition_over_its_window_is_refused_and_the_count_is_not(meter: Meter) -> None:
    meter.set_profile("SLEEP")
    meter.exchange(["SAMP:COUN 1000000"])  # the count alone is accepted
    with pytest.raises(MeterError) as refused:
        meter.measure("CURR")
    assert refused.value.codes == [-222]
    assert "exceeds 600 s" in str(refused.value)
    meter.exchange(["SAMP:COUN 600"])  # the guided calibration's 40.5 s
    assert meter.measure("CURR") == pytest.approx(0.0886)


def test_a_byte_outside_printable_ascii_discards_its_message(
    meter: Meter, sim: SimulatedMeter
) -> None:
    meter.idn()  # the session's first message clears the queue (*CLS)
    sim.write(b"\x01*IDN?\n")
    assert sim.read(4096) == b""
    units, errors, _ = meter.exchange([], raise_errors=False)
    assert [e.code for e in errors] == [-101]
    assert errors[0].detail == "byte 0x01 is not printable ASCII (program message discarded)"


def test_the_command_line_talks_to_the_simulator(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sim", "*IDN?", "SYST:ERR:ALL?"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0", '0,"No error"']


# --- The line: open order and readiness ------------------------------------------


class _RecordingSerial:
    """A ``serial.Serial`` that records what is done to the control lines, in order."""

    calls: ClassVar[list[tuple[str, object]]] = []

    def __init__(self) -> None:
        object.__setattr__(self, "is_open", False)

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("rts", "dtr"):
            _RecordingSerial.calls.append((name, value))
        object.__setattr__(self, name, value)

    def open(self) -> None:
        _RecordingSerial.calls.append(("open", None))
        object.__setattr__(self, "is_open", True)


def test_the_port_is_opened_then_rts_then_dtr_released(monkeypatch: pytest.MonkeyPatch) -> None:
    # pyserial applies DTR then RTS inside open(): lines set before it pass
    # through "DTR released, RTS asserted", the state that resets the board.
    _RecordingSerial.calls = []
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=_RecordingSerial))
    SerialTransport("/dev/null-meter")
    assert _RecordingSerial.calls == [("open", None), ("rts", False), ("dtr", False)]


class _Clock:
    """A monotonic clock that moves 10 ms every time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


def test_wait_ready_rides_out_a_boot_and_reports_the_reboot(sim: SimulatedMeter) -> None:
    sim.reboot(writes=3)  # the newline and two queries are lost while it boots
    meter = Meter(sim, clock=_Clock())
    assert meter.wait_ready(timeout=5.0) is True
    assert sim.messages == ["*ESR?"]  # the one query the booted instrument read
    # Its register was read and cleared: a second check finds it running.
    assert meter.wait_ready() is False
    assert meter.idn().startswith("KLEIDOS,KLEIDOS-METER,")


def test_wait_ready_refuses_an_answer_that_is_no_register() -> None:
    """Another instrument on the line (or a shell) answers *ESR? with words: say so."""
    script = _Script(b"", b"login:\n")
    with pytest.raises(MeterError, match=r"^\*ESR\? answered 'login:'$"):
        Meter(script, clock=_Clock()).wait_ready()
    assert script.sent == [b"\n", b"*ESR?\n"]


def test_wait_ready_on_a_running_instrument_answers_at_once(sim: SimulatedMeter) -> None:
    sim.esr = 0
    meter = Meter(sim, clock=_Clock())
    assert meter.wait_ready() is False
    assert sim.messages == ["*ESR?"]


def test_wait_ready_discards_a_partial_line_and_buffered_stream(sim: SimulatedMeter) -> None:
    sim.esr = 0
    sim._out += b"METER t=1 ch=i_ua v=1.000\nhalf a li"
    meter = Meter(sim, clock=_Clock())
    assert meter.wait_ready() is False
    assert meter.capture.rows == {}


def test_wait_ready_gives_up_on_an_instrument_that_never_answers(sim: SimulatedMeter) -> None:
    sim.boot_writes = 10**6
    with pytest.raises(MeterTimeout):
        Meter(sim, clock=_Clock()).wait_ready(timeout=1.0)


def test_the_command_line_says_when_the_instrument_rebooted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--sim", "*ESR?"]) == 0
    captured = capsys.readouterr()
    assert "rebooted" in captured.err
    # The handshake consumed the power-on bit; the message reads a clear register.
    assert captured.out.splitlines() == ["0"]


# --- Statistics (0.10.0) -------------------------------------------------------


def test_a_statistics_only_capture_streams_nothing_and_reports_its_spread(meter: Meter) -> None:
    meter.set_profile("STATE")
    header = meter.start_statistics()
    assert header["fw"] == meter.idn().split(",")[3]
    stats = meter.stop()
    assert meter.capture.rows == {}
    assert stats["sd_na"] == "0.000"
    assert stats["vmin_uv"] == stats["vmax_uv"] == "4064000"
    assert float(stats["mean_charge_na"]) == pytest.approx(88_600_000, rel=1e-6)
    assert stats["thr_na"] == "-"
    assert stats["batches"] == "32"
    batches = meter.batches()
    assert batches["batches"] == "32"
    assert len(batches["means_na"].split(",")) == 32
    meter.set_capture(False)
    assert meter.query("SENS:CAPT?") == "DATA"


def test_the_threshold_counts_a_dut_above_it(meter: Meter, sim: SimulatedMeter) -> None:
    meter.set_threshold(0.05, 0.001)
    assert meter.query("SENS:CURR:THR?") == "0.05"
    assert meter.query("SENS:CURR:THR:HYST?") == "0.001"
    meter.set_profile("TRACE")
    meter.start_statistics()
    stats = meter.stop()
    assert stats["thr_na"] == "50000000"
    assert stats["rises"] == "1"
    assert stats["vmin_uv"] == "-"
    meter.set_threshold(0.5)
    meter.start_statistics()
    assert meter.stop()["above_n"] == "0"
    meter.set_threshold(None)
    assert meter.query("SENS:CURR:THR?") == "OFF"


def test_the_simulator_refuses_a_capture_mode_it_does_not_have(meter: Meter) -> None:
    with pytest.raises(MeterError) as raised:
        meter.exchange(["SENS:CAPT RING"])
    assert raised.value.codes == [-224]
    meter.exchange(["*RST"])
    assert meter.query("SENS:CAPT?") == "DATA"


def test_batches_before_any_capture_are_minus_230(meter: Meter) -> None:
    with pytest.raises(MeterError) as raised:
        meter.batches()
    assert raised.value.codes == [-230]


# --- Triggers, limits and the dump's CRC (0.11.0) ------------------------------


def test_a_ring_dump_is_held_to_its_crc(meter: Meter, sim: SimulatedMeter) -> None:
    meter.set_profile("TRACE")
    meter.start()
    meter.stop()
    rows = meter.dump()
    assert rows[-1].startswith("METER_DUMP_END crc32=")
    sim.corrupt_dump = True
    with pytest.raises(MeterError, match="arrived damaged: CRC-32"):
        meter.dump()


def test_check_dump_tells_a_good_block_an_old_one_and_a_damaged_one() -> None:
    import zlib

    from kleidos_meter.protocol import check_dump

    body = b"METER_DUMP count=1 t0_us=0 period_us=280 fmt=raw20\nR n=0 c=5\n"
    good = body + f"METER_DUMP_END crc32={zlib.crc32(body):08x} bytes={len(body)}\n".encode()
    assert check_dump(good) is True
    assert check_dump(body + b"METER_DUMP_END\n") is None
    with pytest.raises(ValueError, match="covers"):
        check_dump(body + b"METER_DUMP_END crc32=00000000 bytes=3\n")
    with pytest.raises(ValueError, match="no METER_DUMP_END"):
        check_dump(body)


def test_the_trigger_and_the_limit_are_settings_the_simulator_keeps(meter: Meter) -> None:
    meter.exchange(["TRIG:SOUR LEV", "TRIG:LEV 0.05", "SENS:CAPT:LIM ON"])
    assert meter.query("TRIG:SOUR?") == "LEV"
    assert meter.query("TRIG:LEV?") == "0.05"
    assert meter.query("SENS:CAPT:LIM?") == "1"
    with pytest.raises(MeterError) as raised:
        meter.exchange(["TRIG:SOUR BUS"])
    assert raised.value.codes == [-224]
    meter.exchange(["*RST"])
    assert meter.query("TRIG:SOUR?") == "IMM"


# --- Self-test and health (0.12.0) ---------------------------------------------


def test_the_self_test_and_the_health_report(meter: Meter, sim: SimulatedMeter) -> None:
    assert meter.self_test() == 0
    sim.self_test = 32
    assert meter.self_test() == 32
    sim.reset_reason = "task_wdt"
    health = meter.health()
    assert health["reset"] == "task_wdt"
    assert health["elf"] == sim.elf


def test_watchdog_reports_the_subscriptions() -> None:
    from kleidos_meter.client import Meter
    from kleidos_meter.sim import SimulatedMeter

    meter = Meter(SimulatedMeter())
    assert meter.watchdog() == {
        "console": "1",
        "sampler": "1",
        "timeout_s": "5",
        "panic": "1",
        "stalls": "0",
    }


# --- The exclusive open and the URL path ----------------------------------------


class _Opener:
    """A pyserial stand-in that records how a port was made and opened."""

    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.handles: list[types.SimpleNamespace] = []
        self.urls: list[tuple[str, dict[str, Any]]] = []

    def _handle(self) -> types.SimpleNamespace:
        handle = types.SimpleNamespace(port=None, exclusive=None, opened_exclusive=None)

        def open_() -> None:
            if self.busy:
                raise OSError(errno.EAGAIN, f"Could not exclusively lock port {handle.port}")
            handle.opened_exclusive = handle.exclusive

        handle.open = open_
        self.handles.append(handle)
        return handle

    def Serial(self) -> types.SimpleNamespace:
        return self._handle()

    def serial_for_url(self, url: str, **kwargs: object) -> types.SimpleNamespace:
        self.urls.append((url, kwargs))
        handle = self._handle()
        handle.port = url
        return handle


def test_the_port_is_locked_against_every_other_opener_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _Opener()
    monkeypatch.setitem(sys.modules, "serial", opener)
    SerialTransport("/dev/null-meter")
    SerialTransport("/dev/null-meter", exclusive=False)
    assert [h.opened_exclusive for h in opener.handles] == [True, False]
    assert opener.handles[0].port == "/dev/null-meter" and opener.urls == []
    assert (opener.handles[0].rts, opener.handles[0].dtr) == (False, False)


def test_a_url_is_opened_through_serial_for_url_and_locked_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _Opener()
    monkeypatch.setitem(sys.modules, "serial", opener)
    SerialTransport("socket://127.0.0.1:5025")
    assert opener.urls == [("socket://127.0.0.1:5025", {"do_not_open": True})]
    handle = opener.handles[0]
    assert handle.opened_exclusive is True and handle.baudrate == 6_000_000
    assert (handle.rts, handle.dtr) == (False, False)


def test_a_loop_url_reaches_a_real_pyserial_handler() -> None:
    transport = SerialTransport("loop://")
    try:
        assert transport.write(b"*IDN?\n") == 6
        assert transport.read(64) == b"*IDN?\n"
    finally:
        transport.close()


class _Line:
    """A ``serial.Serial`` stand-in that records the reads, writes and flushes made on it.

    :ivar written: What ``write()`` returns: a count, or None as pyserial
        returns for a write it could not count.
    """

    written: int | None = None
    #: What SerialTransport sets on the line it opens.
    timeout: float
    dsrdtr: bool
    rtscts: bool
    #: Every line made, in order.
    made: ClassVar[list[_Line]] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.inbound = b"*IDN? answer\n"
        _Line.made.append(self)

    def open(self) -> None:
        """Open as a free port does."""

    def write(self, data: bytes) -> int | None:
        self.calls.append(("write", data))
        return _Line.written

    def flush(self) -> None:
        self.calls.append(("flush", None))

    def read(self, size: int) -> bytes:
        self.calls.append(("read", size))
        chunk, self.inbound = self.inbound[:size], self.inbound[size:]
        return chunk

    def close(self) -> None:
        self.calls.append(("close", None))


@pytest.mark.parametrize(("written", "reported"), [(6, 6), (None, 0)])
def test_a_write_waits_until_the_bytes_have_left_and_reports_their_count(
    monkeypatch: pytest.MonkeyPatch, written: int | None, reported: int
) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=_Line))
    monkeypatch.setattr(_Line, "written", written)
    monkeypatch.setattr(_Line, "made", [])
    transport = SerialTransport("/dev/null-meter")
    assert transport.write(b"*IDN?\n") == reported
    assert _Line.made[0].calls == [("write", b"*IDN?\n"), ("flush", None)]


def test_a_read_asks_for_at_most_the_size_and_a_close_releases_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=_Line))
    monkeypatch.setattr(_Line, "made", [])
    transport = SerialTransport("/dev/null-meter")
    assert transport.read(5) == b"*IDN?"
    assert transport.read(4096) == b" answer\n"
    assert transport.read(4096) == b""  # the port's timeout ran out with nothing
    transport.close()
    line = _Line.made[0]
    assert line.calls == [("read", 5), ("read", 4096), ("read", 4096), ("close", None)]
    assert line.timeout == 0.05 and (line.dsrdtr, line.rtscts) == (False, False)


def test_a_port_held_elsewhere_is_reported_as_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "serial", _Opener(busy=True))
    with pytest.raises(PortBusyError, match="held by another opener"):
        SerialTransport("/dev/null-meter")


def test_any_other_open_failure_is_left_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _Opener()
    monkeypatch.setitem(sys.modules, "serial", opener)

    def missing() -> None:
        raise OSError(errno.ENOENT, "no such port")

    def handle() -> types.SimpleNamespace:
        made = opener._handle()
        made.open = missing
        return made

    monkeypatch.setattr(opener, "Serial", handle)
    with pytest.raises(OSError) as raised:
        SerialTransport("/dev/null-meter")
    assert not isinstance(raised.value, PortBusyError) and raised.value.errno == errno.ENOENT


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a POSIX pseudo-terminal")
def test_a_second_real_open_of_a_locked_port_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import serial

    # A pseudo-terminal has no modem lines and no custom rates: the lock is the point.
    monkeypatch.setattr(serial.Serial, "_update_rts_state", lambda self: None)
    monkeypatch.setattr(serial.Serial, "_update_dtr_state", lambda self: None)
    controller, follower = os.openpty()
    path = os.ttyname(follower)
    try:
        first = SerialTransport(path, 115_200)
        with pytest.raises(PortBusyError):
            SerialTransport(path, 115_200)
        first.close()
        SerialTransport(path, 115_200).close()  # the close released the lock
    finally:
        os.close(controller)
        os.close(follower)


def test_the_command_line_reports_a_held_port_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "serial", _Opener(busy=True))
    assert main(["--port", "/dev/null-meter", "*IDN?"]) == 1
    assert capsys.readouterr().err.startswith("# /dev/null-meter is held by another opener")


# --- The exchange on the router -------------------------------------------------


class _Script:
    """A transport that answers every message with the next canned bytes, one byte a read."""

    def __init__(self, *answers: bytes) -> None:
        self._answers = list(answers)
        self._out = b""
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.sent.append(data)
        self._out += self._answers.pop(0)
        return len(data)

    def read(self, size: int) -> bytes:
        chunk, self._out = self._out[:1], self._out[1:]
        return chunk

    def close(self) -> None:
        """Nothing to release."""


def _block(payload: bytes) -> bytes:
    digits = str(len(payload)).encode("ascii")
    return b"#" + str(len(digits)).encode("ascii") + digits + payload


def test_a_block_between_units_arriving_a_byte_at_a_time_is_routed(sim: SimulatedMeter) -> None:
    body = b"METER_DUMP count=1 t0_us=0 period_us=280 fmt=raw20\nR n=0 c=7\nMETER_DUMP_END\n"
    script = _Script(
        b"METER t=1 ch=i_ua v=2.000\nMETER_HDR fw=0.18.0;"
        + _block(body)
        + b';0,"No error"\nK,3,840,late\n'
    )
    meter = Meter(script)
    units, errors, block = meter.exchange(["FETC:HEAD?", "TRAC:DATA? RAW"])
    assert (units, errors, block) == (["METER_HDR fw=0.18.0"], [], body)
    assert meter.capture.rows == {1: {"i_ua": 2.0}}
    assert meter.capture.marks == []  # routed after the response: kept for the next call


def test_a_dump_from_before_the_crc_is_accepted_as_it_is() -> None:
    body = b"METER_DUMP count=1 t0_us=0 period_us=280 fmt=raw20\nR n=0 c=7\nMETER_DUMP_END\n"
    meter = Meter(_Script(_block(body) + b';0,"No error"\n'))
    assert meter.dump() == [
        "METER_DUMP count=1 t0_us=0 period_us=280 fmt=raw20",
        "R n=0 c=7",
        "METER_DUMP_END",
    ]


def test_a_dump_that_answers_no_block_is_refused() -> None:
    meter = Meter(_Script(b'0,"No error"\n'))
    with pytest.raises(MeterError, match="answered no block"):
        meter.dump()


def test_a_mark_is_found_among_the_marks_that_came_with_it() -> None:
    """A threshold mark after it is not it, and an order that emitted none says so."""
    meter = Meter(
        _Script(
            b'METER_MARK mark=m1 n=3 t=840\nMETER_MARK mark=above n=4 t=1120\n0,"No error"\n',
            b'METER_MARK mark=above n=5 t=1400\n0,"No error"\n',
        )
    )
    assert meter.mark("m1") == Mark("m1", 3, 840)
    with pytest.raises(MeterError, match="accepted but no mark record came"):
        meter.mark("m2")
    assert [mark.label for mark in meter.capture.marks] == ["m1", "above", "above"]


def test_a_drain_forgets_a_block_half_read() -> None:
    meter = Meter(_Script(b"#15AB", b"1\n"), clock=_Clock())
    with pytest.raises(MeterTimeout):
        meter.send("TRAC:DATA?", timeout=0.1)
    assert meter.drain() == 2
    assert meter.send("*OPC?").units == ["1"]


def test_set_shunt_stores_the_value_and_its_tag_and_reads_them_back(
    meter: Meter, sim: SimulatedMeter
) -> None:
    data = meter.set_shunt(0.1002, "km-2026-11-22")
    assert sim.messages[-1] == (
        '*CLS;:SENS:CURR:SHUN 0.100200;:CAL:TAG "km-2026-11-22";:CAL:DATA?;:SYST:ERR:ALL?'
    )
    assert (data["shunt_uohm"], data["shunt_measured"], data["cal_tag"]) == (
        "100200",
        "1",
        "km-2026-11-22",
    )
    with pytest.raises(MeterError) as refused:
        meter.set_shunt(0.3, "km-out-of-range")
    assert refused.value.codes == [-222]


# --- The pin map (0.18.0) -------------------------------------------------------


def test_the_pin_map_is_read_live_or_staged(meter: Meter) -> None:
    live = meter.pins()
    assert (live["sda"], live["relay2"], live["src"], live["verified"]) == (
        "4",
        "15",
        "default",
        "1",
    )
    assert live["table"] == DIGEST
    staged = meter.pins(pending=True)
    assert (staged["valid"], staged["problem"]) == ("1", "-")


def test_stage_sends_a_unit_per_role_it_changes_and_returns_the_verdict(
    meter: Meter, sim: SimulatedMeter
) -> None:
    staged = meter.stage(sda=18, relay1_active="high", addr=0x41, scl_hz=100_000)
    assert sim.messages[-1] == (
        "SYST:PINS:STAG SDA,18,UP;:SYST:PINS:STAG REL1,7,HIGH;:SYST:PINS:ADDR 65"
        ";:SYST:PINS:CLOC 100000;:SYST:PINS:PEND?;:SYST:ERR:ALL?"
    )
    assert (staged["sda"], staged["relay1_active"], staged["addr"], staged["scl_hz"]) == (
        "18",
        "high",
        "0x41",
        "100000",
    )
    assert staged["valid"] == "1"
    assert meter.stage(alert=None, addr="auto")["problem"] == "missing"
    assert sim.messages[-1].startswith("SYST:PINS:STAG ALER,NONE,UP;:SYST:PINS:ADDR AUTO;")
    assert meter.stage(pull_scl="off")["pull_scl"] == "off"
    assert meter.stage(addr=0)["addr"] == "auto"


def test_stage_refuses_a_field_the_map_has_not_and_raises_the_instruments_refusal(
    meter: Meter,
) -> None:
    with pytest.raises(ValueError, match="no such field of a pin map: pin, sdb"):
        meter.stage(sdb=4, pin=5)
    with pytest.raises(MeterError) as refused:
        meter.stage(sda=64)
    assert refused.value.codes == [-222]


def test_apply_returns_the_result_and_the_errors_a_revert_queued(meter: Meter) -> None:
    meter.stage(sda=18)
    result, errors = meter.apply()
    assert (result["result"], result["reason"], result["saved"]) == ("reverted", "no_chip", "0")
    assert [entry.code for entry in errors] == [117]
    meter.stage(sda=4, relay2=16)
    result, errors = meter.apply()
    assert (result["result"], errors) == ("ok", [])
    assert (meter.pins()["relay2"], meter.pins()["src"]) == ("16", "stored")
    result, errors = meter.pins_defaults()
    assert (result["result"], errors) == ("ok", [])
    assert meter.pins()["src"] == "default"
