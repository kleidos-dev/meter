# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The client suite on every instrument the host talks to (``conformance.py``).

The simulator answers the suite as the reference. The passthrough of a
``serve --sim`` must answer it identically, values included: it is a pipe.
A stand-in bridge (the simulator on standard input and output) runs the
process path in every run, and the firmware's own bridge, when
``KLEIDOS_METER_NATIVE_BRIDGE`` names it, must give every case the
simulator's signature, and an exact case its every byte (the pin map applied,
put back and refused, every count and conversion time of the averaging
override); without it that case skips and says why, and fails with
``METER_BRIDGE_REQUIRED=1``, which ``scripts/run_tests.sh`` sets after building it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from conformance import (
    BRIDGE_ENV,
    BRIDGE_SKIP,
    CASES,
    COUNTS,
    TIMES_US,
    Outcome,
    ProcessTransport,
    Signature,
    bridge_command,
    differences,
    run_suite,
    shared_queries,
    short_header,
    value_shape,
)
from conftest import BRIDGE_REQUIRED, missing
from kleidos_meter.client import Meter, MeterTimeout, SerialTransport
from kleidos_meter.protocol import parse_fields
from kleidos_meter.sim import SimulatedMeter
from panel_harness import serving

#: The stand-in bridge: the simulator on standard input and output.
STAND_IN = [sys.executable, str(Path(__file__).with_name("sim_stdio.py"))]


@pytest.fixture(scope="module")
def reference() -> dict[str, Outcome]:
    """The suite as the deterministic simulator answers it."""
    return run_suite(Meter(SimulatedMeter()))


# -- the suite itself ---------------------------------------------------------------------


def test_the_suite_asks_every_parameterless_query_the_simulator_shares_with_the_firmware() -> None:
    queries = shared_queries()
    assert len(queries) > 50
    assert {"*IDN?", "SYST:INF?", "DIAG:HEAP?", "ROUT:STAT?", "FETC:STAT?"} <= set(queries)
    assert "SYST:LED?" not in queries, "the simulator does not implement SYSTem:LED?"
    names = [case.name for case in CASES]
    assert len(names) == len(set(names))
    assert names[: len(queries)] == list(queries)


def test_short_headers_drop_optional_nodes_and_number_a_suffix() -> None:
    assert short_header("*IDN?") == "*IDN?"
    assert short_header("SYSTem:ERRor[:NEXT]?") == "SYST:ERR?"
    assert short_header("SENSe:CURRent:SHUNt:RANGe#?") == "SENS:CURR:SHUN:RANG1?"
    assert short_header("SENSe:PROFile") == "SENS:PROF"


def test_the_simulator_answers_the_suite_as_the_firmware_documents(
    reference: dict[str, Outcome],
) -> None:
    assert reference["*IDN?"].raw == ("KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0",)
    assert reference["SYST:ERR:ALL?"].raw == ('0,"No error"',)
    assert reference["range written and read back"].raw == ("1",)
    assert reference["range put back"].raw == ("0",)
    assert reference["an undefined header"].signature.errors == (-113,)
    assert reference["a parameter where none is taken"].signature.errors == (-108,)
    assert reference["more parameters than a command takes"].signature.errors == (-108,)
    assert reference["more parameters than a unit may carry"].signature.errors == (-108,)
    # One parameter, and one no tag may hold: refused as a value, not as two.
    quoted = reference["a comma inside a quoted parameter"]
    assert (quoted.raw, quoted.signature.errors) == (('""',), (-224,))
    assert reference["a missing parameter"].signature.errors == (-109,)
    unwired = "a relay the rig does not wire"
    assert reference[unwired].signature.errors == (-241,)
    assert reference[f"{unwired}, with no state"].signature.errors == (-109,)
    assert reference[f"{unwired}, with no such state"].signature.errors == (-224,)
    assert reference["a relay the rig does not have"].signature.errors == (-114,)
    # No null and no shunt point taken: those are refused.
    for name in ("CAL:ZERO:RES?", "CAL:SHUN:RES?"):
        assert reference[name].signature.errors == (-230,), name
    # MEASure's acquisition, asked before them, is the capture they describe.
    assert reference["FETC:STAT?"].signature.shapes[0].startswith("report:samples,dropped,")
    assert reference["FETC:HEAD?"].signature.shapes[0].startswith("report:fw,chip,addr,")
    heap = reference["DIAG:HEAP?"].signature.shapes[0]
    assert heap.endswith("pre_main_allocs,pre_main_frees,allocs_since_main,frees_since_main")
    # Every other query answers one unit and queues nothing.
    for query in shared_queries():
        if query in ("CAL:ZERO:RES?", "CAL:SHUN:RES?"):
            continue
        outcome = reference[query]
        assert len(outcome.signature.shapes) == 1 and not outcome.signature.errors, query


def test_a_shape_keeps_a_reports_keys_and_a_values_form_never_the_value() -> None:
    assert value_shape("a=1 b=x") == "report:a,b"
    assert value_shape("KLEIDOS,KLEIDOS-METER,KM-1,0.18.0") == "value:word|word|word|word"
    assert value_shape('0,"No error"') == "value:number|string"
    assert value_shape('"x=1"') == "value:string"
    assert value_shape("-1.5e-3") == "value:number"
    assert value_shape(".5") == "value:number"
    assert value_shape("OFF;'a;b'") == "value:word;string"


def test_differences_name_each_case_that_differs_or_is_missing() -> None:
    one = Outcome(Signature(("value:number",), False, ()), ("1",))
    other = Outcome(Signature(("value:word",), False, ()), ("x",))
    assert differences({"a": one}, {"a": one}) == []
    assert differences({"a": one, "b": one}, {"a": other}) == [
        f"a: {one.signature} != {other.signature}",
        "b: not answered",
    ]


def test_an_exact_case_is_held_to_its_bytes_and_its_errors_texts() -> None:
    refused = Signature(("value:number",), False, (-221,))
    exact = Outcome(refused, ("1",), True, ("Settings conflict;a",))
    for seen, named in (
        (Outcome(refused, ("2",), True, ("Settings conflict;a",)), "('1',) != ('2',)"),
        (
            Outcome(refused, ("1",), True, ("Settings conflict;b",)),
            "('Settings conflict;a',) != ('Settings conflict;b',)",
        ),
    ):
        assert differences({"a": exact}, {"a": seen}) == [f"a: {named}"]
        # A case that is not exact is held to its signature alone.
        loose = Outcome(refused, exact.raw, False, exact.said)
        assert differences({"a": loose}, {"a": seen}) == []


def test_the_suite_applies_puts_back_and_refuses_pin_maps(reference: dict[str, Outcome]) -> None:
    def result(name: str) -> dict[str, str]:
        raw = reference[name].raw
        assert raw is not None
        return parse_fields(next(unit for unit in raw if unit.startswith("result=")))

    assert result("no map applied yet")["result"] == "none"
    assert result("a staged map applied") == {
        "result": "ok",
        "reason": "-",
        "mfg": "0x5449",
        "die": "0x228",
        "tst": "0",
        "saved": "1",
    }
    for name, reason in (
        ("a wrong SDA applied and put back", "no_chip"),
        ("a wrong ALERT applied and put back", "alert"),
        ("an address nothing answers at applied and put back", "no_chip"),
    ):
        assert (result(name)["result"], result(name)["reason"]) == ("reverted", reason), name
        assert result(name)["saved"] == "0" and reference[name].signature.errors == (117,), name
    for name, reason in (
        ("a staged map that breaks a rule, applied", "invalid"),
        ("a map applied with the bypass closed", "relays"),
    ):
        assert (result(name)["result"], result(name)["reason"]) == ("refused", reason), name
        assert reference[name].signature.errors == (-221,), name
    # The flags refuse it before the map is looked at: the last result stands.
    under = "a map applied under a capture"
    assert reference[under].signature.errors == (-221, -221)
    assert result(under) == result("a map applied with the bypass closed")
    assert "src=stored verified=0" in str(reference["a relay moved to another pin"].raw)
    # Moved back to the rig's own pins, the relays are still to be checked.
    assert "src=default verified=0" in str(reference["the default map applied"].raw)
    assert "src=default" in str(reference["the default map staged and applied"].raw)


def test_the_sweep_turns_every_count_and_time_on_and_some_are_refused(
    reference: dict[str, Outcome],
) -> None:
    swept = [
        (outcome, report)
        for name, outcome in reference.items()
        if name.startswith("override sweep: ") and ", " in name
        for report in outcome.raw or ()
    ]
    assert len(swept) == 3 * 3 * len(COUNTS) * len(TIMES_US)
    on = [report for _, report in swept if "avg_state=on" in report]
    refused = [text for outcome, _ in swept[::2] for text in outcome.said]
    assert len(on) + len(refused) == len(swept)
    # Both limits are met: a period under what the reads need, and one over 200 ms.
    assert any("us its reads need at 50000 Hz" in text for text in refused)
    assert any("under the 100 us its reads need at 1000000 Hz" in text for text in refused)
    assert any("over the 200000 us its pacing allows" in text for text in refused)
    assert any("avg=1024" in report for report in on)


# -- the passthrough ----------------------------------------------------------------------


def test_the_passthrough_answers_the_suite_byte_for_byte_as_the_simulator(
    reference: dict[str, Outcome], tmp_path: Path
) -> None:
    with serving(["--sim", "--data-dir", str(tmp_path / "runs"), "--scpi-passthrough", "0"]) as s:
        assert s.passthrough is not None
        meter = Meter(SerialTransport(s.passthrough))
        try:
            seen = run_suite(meter)
        finally:
            meter.close()
    assert differences(reference, seen) == []
    for name, outcome in reference.items():
        assert seen[name].raw == outcome.raw, name


# -- a process on standard input and output -----------------------------------------------


def test_a_stand_in_bridge_answers_the_suite_as_the_simulator(
    reference: dict[str, Outcome],
) -> None:
    meter = Meter(ProcessTransport(STAND_IN))
    try:
        seen = run_suite(meter)
    finally:
        meter.close()
    assert differences(reference, seen) == []
    for name, outcome in reference.items():
        assert seen[name].raw == outcome.raw, name


def test_the_firmwares_bridge_gives_every_case_the_simulators_signature(
    reference: dict[str, Outcome],
) -> None:
    command = bridge_command(os.environ.get(BRIDGE_ENV))
    if command is None:
        missing(BRIDGE_SKIP, BRIDGE_REQUIRED)
    meter = Meter(ProcessTransport(command))
    try:
        seen = run_suite(meter)
    finally:
        meter.close()
    assert differences(reference, seen) == []


def test_the_bridge_is_named_by_a_file_or_not_at_all(tmp_path: Path) -> None:
    assert bridge_command(None) is None
    assert bridge_command("") is None
    with pytest.raises(FileNotFoundError, match=BRIDGE_ENV):
        bridge_command(str(tmp_path / "missing"))
    binary = tmp_path / "bridge"
    binary.write_bytes(b"")
    assert bridge_command(str(binary)) == [str(binary)]
    assert "make -C firmware/test/native bridge" in BRIDGE_SKIP


def test_a_read_waits_at_most_its_timeout_and_an_ended_process_takes_nothing() -> None:
    transport = ProcessTransport([sys.executable, "-c", "pass"], timeout=0.05)
    started = time.monotonic()
    while transport.read(16) == b"" and time.monotonic() - started < 5.0:
        pass
    assert transport.read(16) == b""
    with pytest.raises(MeterTimeout, match="ended"):
        for _ in range(100):
            transport.write(b"*IDN?\n")
            time.sleep(0.01)
    transport.close()


def test_a_process_that_ignores_its_closed_input_is_terminated() -> None:
    transport = ProcessTransport([sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    transport.close()
    assert time.monotonic() - started < 10.0


def test_a_bridge_that_is_required_and_not_named_fails_the_case(
    reference: dict[str, Outcome], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BRIDGE_ENV, raising=False)
    monkeypatch.setenv(BRIDGE_REQUIRED, "1")
    with pytest.raises(pytest.fail.Exception, match=BRIDGE_REQUIRED):
        test_the_firmwares_bridge_gives_every_case_the_simulators_signature(reference)
    monkeypatch.setenv(BRIDGE_REQUIRED, "0")
    with pytest.raises(pytest.skip.Exception, match=BRIDGE_ENV):
        test_the_firmwares_bridge_gives_every_case_the_simulators_signature(reference)


def test_the_bridge_case_runs_whatever_binary_the_variable_names(
    reference: dict[str, Outcome], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "bridge"
    binary.write_text(f"#!/bin/sh\nexec {' '.join(STAND_IN)}\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv(BRIDGE_ENV, str(binary))
    test_the_firmwares_bridge_gives_every_case_the_simulators_signature(reference)
