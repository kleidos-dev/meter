# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""``kleidos-meter pins``: every action, every status, on the simulator."""

from __future__ import annotations

import argparse
import runpy
import sys

import pytest
from kleidos_meter import Meter, SimulatedMeter, cli, pins_cli
from kleidos_meter.client import PortBusyError
from kleidos_meter.pins import DEFAULT_MAP, DIGEST, Wiring
from kleidos_meter.pins_cli import change, describe, main

DEFAULT_LINES = [
    f"pins: the live map, src=default verified=1 table={DIGEST} (this package's board)",
    "  SDA      GPIO4   pull up",
    "  SCL      GPIO5   pull up",
    "  ALERT    GPIO6   pull up",
    "  Relay 1  GPIO7   active low",
    "  Relay 2  GPIO15  active high",
    "  address  auto (a scan of 0x40 to 0x4F)",
    "  clock    400000 Hz at boot",
]


def test_show_prints_the_live_map(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "--sim"]) == 0
    assert capsys.readouterr().out.splitlines() == DEFAULT_LINES


def test_the_command_takes_its_own_options_before_the_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--sim", "pins", "show"]) == 0
    assert capsys.readouterr().out.splitlines() == DEFAULT_LINES


def test_show_pending_prints_the_staged_map_and_its_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["show", "--pending", "--sim"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "pins: the staged map, valid"
    assert out[1:] == DEFAULT_LINES[1:]


def test_set_stages_and_judges_and_exits_3_for_a_map_the_rules_refuse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["set", "sda=8", "pull_sda=off", "addr=0x41", "--sim"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["pins: the staged map, valid", "  SDA      GPIO8   pull off"]
    assert out[6] == "  address  0x41"
    assert main(["set", "alert=39", "--sim"]) == 3
    assert capsys.readouterr().out.splitlines()[0] == (
        "pins: the staged map, refused by the rules: too_high at alert"
    )
    assert main(["set", "relay2=none", "--sim"]) == 3
    assert "  Relay 2  none    active high" in capsys.readouterr().out.splitlines()


def test_apply_proves_the_map_and_a_wrong_sda_is_reverted_with_status_3(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["apply", "sda=18", "--sim"]) == 3
    captured = capsys.readouterr()
    assert captured.err == (
        '# 117,"Pin map reverted;the INA228 did not answer on sda=18 scl=5 addr=auto:'
        ' the last good map is back"\n'
    )
    out = captured.out.splitlines()
    assert out[0] == "pins: reverted, reason no_chip, INA228 mfg - die -, self-test 1, saved 0"
    assert out[1:] == DEFAULT_LINES


def test_apply_on_a_board_wired_so_keeps_the_map(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["apply", "sda=18", "scl=21", "scl_hz=100000", "--sim", "--sim-wiring", "sda=18,scl=21"]
    assert main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "pins: ok, reason -, INA228 mfg 0x5449 die 0x228, self-test 0, saved 1"
    assert out[1].startswith("pins: the live map, src=stored verified=1")
    assert out[2:4] == ["  SDA      GPIO18  pull up", "  SCL      GPIO21  pull up"]
    assert out[-1] == "  clock    100000 Hz at boot"


def test_apply_with_nothing_staged_applies_the_live_map_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["apply", "--sim"]) == 0
    assert capsys.readouterr().out.splitlines()[1].startswith("pins: the live map, src=default")


def test_defaults_needs_no_ina228(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["defaults", "--sim", "--sim-wiring", "sda=18"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "pins: ok, reason no_chip, INA228 mfg - die -, self-test 1, saved 1"
    assert out[1:] == DEFAULT_LINES


def test_a_refusal_the_instrument_queues_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sim = SimulatedMeter()
    sim.k2_closed = True  # the bypass is closed across a closed lead: not at rest
    monkeypatch.setattr(pins_cli, "SimulatedMeter", lambda wiring: sim)
    assert main(["defaults", "--sim"]) == 3
    captured = capsys.readouterr()
    assert "the relays are not at rest (path=both)" in captured.err
    assert captured.out.splitlines()[0].startswith("pins: refused, reason relays")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["show"], "name the port"),
        (["show", "sda=4", "--sim"], "show takes no change"),
        (["defaults", "sda=4", "--sim"], "defaults takes no change"),
        (["set", "--sim"], "set takes at least one change"),
        (["set", "sda=x", "--sim"], "sda takes a number"),
        (["set", "--sim", "--sim-wiring", "addr=0x50", "sda=4"], "straps 0x40 to 0x4F"),
    ],
)
def test_a_usage_error_exits_2_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.delenv("KLEIDOS_METER_PORT", raising=False)
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("sda=18", ("sda", 18)),
        ("SDA = 0x12", ("sda", 18)),
        ("alert=none", ("alert", None)),
        ("pull_scl=OFF", ("pull_scl", "off")),
        ("relay1_active=high", ("relay1_active", "high")),
        ("addr=auto", ("addr", "auto")),
        ("addr=0x41", ("addr", 0x41)),
        ("scl_hz=100000", ("scl_hz", 100_000)),
    ],
)
def test_a_change_reads_as_stage_takes_it(text: str, parsed: tuple[str, int | str]) -> None:
    assert change(text) == parsed


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sda", "a change is key=value"),
        ("pin=4", "a change is key=value"),
        ("pull_sda=on", "pull_sda is up or off"),
        ("relay2_active=up", "relay2_active is high or low"),
        ("scl_hz=none", "scl_hz takes a number"),
        ("addr=x41", "addr takes a number"),
    ],
)
def test_a_change_that_does_not_read_says_why(text: str, message: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=message):
        change(text)


def test_a_map_of_another_board_says_so() -> None:
    report = DEFAULT_MAP.words() | {"src": "stored", "verified": "0", "table": "0x00000000"}
    assert describe(report, staged=False)[0] == (
        f"pins: the live map, src=stored verified=0 table=0x00000000 (not this package's board,"
        f" {DIGEST})"
    )


def test_a_port_held_elsewhere_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(port: str) -> None:
        raise PortBusyError(f"{port} is held by another program")

    monkeypatch.setattr(pins_cli, "SerialTransport", busy)
    assert main(["show", "--port", "/dev/cu.meter"]) == 1
    assert capsys.readouterr().err == "# /dev/cu.meter is held by another program\n"


def test_an_instrument_that_never_answers_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            self.now += 0.5
            return self.now

    sim = SimulatedMeter(wiring=Wiring.parse(""))
    sim.boot_writes = 10**6
    monkeypatch.setattr(pins_cli, "SimulatedMeter", lambda wiring: sim)
    monkeypatch.setattr(pins_cli, "Meter", lambda transport: Meter(transport, clock=Clock()))
    assert main(["show", "--sim"]) == 1
    assert capsys.readouterr().err.startswith("# the instrument is not answering: *ESR?")


def test_run_as_a_module_the_status_is_the_process_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # runpy executes a fresh copy: the one already imported is set aside meanwhile.
    monkeypatch.delitem(sys.modules, "kleidos_meter.pins_cli")
    monkeypatch.setattr(sys, "argv", ["pins", "show", "--sim"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("kleidos_meter.pins_cli", run_name="__main__")
    assert raised.value.code == 0
    assert capsys.readouterr().out.splitlines() == DEFAULT_LINES
