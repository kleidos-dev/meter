# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The command lines' exit paths: ``kleidos-meter`` and ``kleidos-meter relay-timing``.

Every path a script reads is pinned here: the status (0, 1 for an instrument
that did not answer, 2 for a usage error), what goes to stdout and what goes
to stderr behind a ``#``. The instrument is the simulator throughout.
"""

from __future__ import annotations

import json
import runpy
import sys

import pytest
from kleidos_meter import Meter, SimulatedMeter, cli, pins_cli, relay_timing
from kleidos_meter.cli import main


class _Clock:
    """A monotonic clock that moves 10 ms every time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.01
        return self.now


def _sims(monkeypatch: pytest.MonkeyPatch, module: object, **knobs: int) -> list[SimulatedMeter]:
    """Make *module* build its simulator through a factory that keeps each one made.

    :param module: The module whose ``SimulatedMeter`` is replaced.
    :param knobs: Attributes set on each simulator before it is handed out.
    :returns: The simulators made, in order.
    """
    made: list[SimulatedMeter] = []

    def factory() -> SimulatedMeter:
        sim = SimulatedMeter()
        for name, value in knobs.items():
            setattr(sim, name, value)
        made.append(sim)
        return sim

    monkeypatch.setattr(module, "SimulatedMeter", factory)
    return made


# --- kleidos-meter: the exit statuses ---------------------------------------------


def test_a_message_that_times_out_exits_1_and_the_rest_still_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A command (no query) has no response (docs/scpi.md): the CLI waits its
    # timeout for one, says so behind a '#' on stderr, and goes on.
    assert main(["--sim", "--timeout", "0.05", "*CLS", "*IDN?"]) == 1
    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert err[-1] == "# '*CLS': no response within 0.05 s"
    assert all(line.startswith("# ") for line in err)
    assert captured.out.splitlines() == ["KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0"]


def test_one_timeout_among_answered_messages_still_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The status is the run's, not the last message's: a later answer does not clear it.
    assert main(["--sim", "--timeout", "0.05", "*IDN?", "*CLS", "*ESR?"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "0"


def test_every_message_answered_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sim", "*ESR?", "SYST:ERR:ALL?"]) == 0
    assert capsys.readouterr().out.splitlines() == ["0", '0,"No error"']


def test_a_running_instrument_gets_no_reboot_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _sims(monkeypatch, cli, esr=0)  # its power-on bit already read by an earlier host
    assert main(["--sim", "*IDN?"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("KLEIDOS,KLEIDOS-METER,")


def test_no_port_and_no_sim_is_a_usage_error_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KLEIDOS_METER_PORT", raising=False)
    with pytest.raises(SystemExit) as raised:
        main(["*IDN?"])
    assert raised.value.code == 2
    assert "name the port with --port or KLEIDOS_METER_PORT, or use --sim" in (
        capsys.readouterr().err
    )


def test_no_message_is_a_usage_error_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--sim"])
    assert raised.value.code == 2
    assert "messages" in capsys.readouterr().err


def test_an_instrument_that_never_answers_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sims = _sims(monkeypatch, cli, boot_writes=10**6)
    monkeypatch.setattr(cli, "Meter", lambda transport: Meter(transport, clock=_Clock()))
    assert main(["--sim", "*IDN?"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "# the instrument is not answering: *ESR?: no answer within 15 s\n"
    assert captured.out == ""
    assert sims[0].messages == []  # nothing reached it: every write was lost to the boot


def test_the_port_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []

    def transport(port: str) -> SimulatedMeter:
        opened.append(port)
        return SimulatedMeter()

    monkeypatch.setenv("KLEIDOS_METER_PORT", "/dev/null-meter-from-env")
    monkeypatch.setattr(cli, "SerialTransport", transport)
    assert main(["*IDN?"]) == 0
    assert opened == ["/dev/null-meter-from-env"]
    assert capsys.readouterr().out.startswith("KLEIDOS,KLEIDOS-METER,")


# --- kleidos-meter: what is printed ---------------------------------------------


def test_stream_rows_are_printed_before_the_response_that_follows_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A STATE capture streams: the simulator takes four samples as each message
    # arrives and two more as ABORt closes the capture.
    messages = ["SENS:PROF STAT;:INIT;:SYST:ERR:ALL?", "*IDN?", "ABOR;:SYST:ERR:ALL?"]
    assert main(["--sim", *messages]) == 0
    out = capsys.readouterr().out.splitlines()
    period_us = 1152
    rows = [f"# {1_000_000 + n * period_us} {{'i_ua': 88600.0, 'v_mv': 4064.0}}" for n in range(10)]
    # Each row once, in time order, before the answer to the message it arrived under.
    assert out == [
        '0,"No error"',
        *rows[:4],
        "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0",
        *rows[4:],
        '0,"No error"',
    ]


def test_a_block_is_printed_before_the_units_of_its_response(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--sim", "SENS:PROF TRAC;:INIT;:SYST:ERR:ALL?", "ABOR;:TRAC:DATA? SCAL"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == '0,"No error"'
    assert out[1].startswith("METER_DUMP count=64 ")
    assert out[-2].startswith("METER_DUMP_END crc32=")
    assert out[-1] == ""  # the dump's unit is the block; nothing follows it on the line
    assert sum(line.startswith("METER t=") for line in out) == 64


# --- kleidos-meter: the subcommands -----------------------------------------------


@pytest.mark.parametrize(
    ("command", "module"),
    [
        ("calibrate", "kleidos_meter.calibrate"),
        ("relay-timing", "kleidos_meter.relay_timing"),
        ("calibration", "kleidos_meter.calibration_cli"),
        ("pins", "kleidos_meter.pins_cli"),
    ],
)
def test_each_subcommand_is_handed_the_rest_of_the_line_and_its_status(
    monkeypatch: pytest.MonkeyPatch, command: str, module: str
) -> None:
    __import__(module)
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> int:
        seen.append(argv)
        return 7

    monkeypatch.setattr(sys.modules[module], "main", fake)
    assert main([command, "--sim", "verify"]) == 7
    assert seen == [["--sim", "verify"]]


@pytest.mark.parametrize(
    ("argv", "at"),
    [
        (["pins"], 0),
        (["--sim", "pins", "show"], 1),
        (["--port", "/dev/cu.meter", "pins"], 2),
        (["--port=/dev/cu.meter", "--timeout", "2", "capture"], 3),
        (["--timeout=2", "--sim", "calibrate"], 2),
        (["--sim", "*IDN?", "pins"], None),
        (["--sim"], None),
        (["--port"], None),
        ([], None),
    ],
)
def test_a_subcommand_is_found_past_the_commands_own_options(argv: list[str], at: int) -> None:
    assert cli.subcommand_at(argv) == at


def test_the_options_before_a_subcommand_are_handed_to_it_after_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(pins_cli, "main", seen.append)
    main(["--port", "/dev/cu.meter", "--sim", "pins", "show", "--pending"])
    assert seen == [["show", "--pending", "--port", "/dev/cu.meter", "--sim"]]


def test_without_a_subcommand_the_words_are_messages(capsys: pytest.CaptureFixture[str]) -> None:
    # A subcommand's name after a message is a message like any other.
    assert main(["--sim", "*IDN?", "calibrate;:SYST:ERR:ALL?"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("-113,")


def test_the_arguments_default_to_the_process_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["kleidos-meter", "--sim", "*IDN?"])
    assert main() == 0
    assert capsys.readouterr().out.startswith("KLEIDOS,KLEIDOS-METER,")


@pytest.mark.parametrize(
    ("module", "argv", "status"),
    [
        ("kleidos_meter.cli", ["--sim", "*IDN?"], 0),
        ("kleidos_meter.cli", ["--sim", "--timeout", "0.05", "*CLS"], 1),
        (
            "kleidos_meter.relay_timing",
            ["--sim", "--relay", "k1", "--repeats", "1", "--hold", "1"],
            0,
        ),
        ("kleidos_meter.calibration_cli", ["--sim", "--registry", "no-such-registry", "verify"], 3),
        ("kleidos_meter.calibrate", ["--sim", "--yes", "--registry", "no-such-registry"], 1),
    ],
)
def test_run_as_a_module_the_status_is_the_process_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: str,
    argv: list[str],
    status: int,
) -> None:
    # runpy executes a fresh copy: the one already imported is set aside meanwhile.
    monkeypatch.delitem(sys.modules, module)
    monkeypatch.setattr(sys, "argv", [module, *argv])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module(module, run_name="__main__")
    assert raised.value.code == status
    capsys.readouterr()


# --- kleidos-meter relay-timing ---------------------------------------------------


def test_relay_timing_on_the_simulator_prints_json_and_leaves_the_path_at_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sims = _sims(monkeypatch, relay_timing)
    argv = ["--sim", "--relay", "k1", "--relay", "k2", "--repeats", "2", "--hold", "1", "--json"]
    assert relay_timing.main(argv) == 0
    document = json.loads(capsys.readouterr().out)
    assert set(document) == {"edges", "summary"}
    # Each LIVE move is marked in the ring, so each is an edge (the simulated DUT draws
    # the same either way, so the edges' figures mean nothing here).
    assert [edge["order"] for edge in document["edges"]] == [
        *["k1:open", "k1:closed"] * 2,
        *["k2:closed", "k2:open"] * 2,
    ]
    assert set(document["summary"]) == {"k1:open", "k1:closed", "k2:closed", "k2:open"}
    sim = sims[0]
    for relay in ("k1", "k2"):
        first, second = relay_timing.MOVES[relay]
        assert sim.messages.count(f"{first};:SYST:ERR:ALL?") == 2
        assert sim.messages.count(f"{second};:SYST:ERR:ALL?") == 2
    assert sim.messages.count("ABOR;:SYST:ERR:ALL?") == 2
    assert sim.messages[-1] == "ROUT:STAT?;:SYST:ERR:ALL?"
    assert sim.path == "shunt" and sim.capture is None  # back at rest, the capture closed


def test_relay_timing_prints_one_summary_line_per_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    edges = [
        relay_timing.Edge("k1", "k1:open", 100, 108, 2240, 12950.0, 6.0, 75.0),
        relay_timing.Edge("k1", "k1:open", 300, 307, 1960, 12950.0, 5.0, 383.0),
        relay_timing.Edge("k1", "k1:closed", 200, 203, 840, 6.0, 12950.0, 90.0),
    ]
    monkeypatch.setattr(relay_timing, "measure", lambda meter, relay, repeats, hold: edges)
    assert relay_timing.main(["--sim", "--relay", "k1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "k1:closed: n=1 median=840 us range=840..840 us min_contrast=90.0",
        "k1:open: n=2 median=2100.0 us range=1960..2240 us min_contrast=75.0",
    ]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--sim", "--relay", "k1", "--repeats", "9"], "--repeats is 1 to 8"),
        (["--sim", "--relay", "k1", "--hold", "0"], "--hold is a positive number of ms"),
        (["--relay", "k1"], "name the port with --port or KLEIDOS_METER_PORT, or use --sim"),
        (["--sim"], "--relay"),
    ],
)
def test_relay_timing_refuses_a_run_it_cannot_measure_with_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.delenv("KLEIDOS_METER_PORT", raising=False)
    sims = _sims(monkeypatch, relay_timing)
    with pytest.raises(SystemExit) as raised:
        relay_timing.main(argv)
    assert raised.value.code == 2
    assert message in capsys.readouterr().err
    assert sims == []  # refused before anything was opened
