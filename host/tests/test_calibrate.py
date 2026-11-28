# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The guided shunt calibration against the simulated instrument, filing registry events."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import SIM_SERIAL
from kleidos_meter import Meter, SimulatedMeter
from kleidos_meter.calibrate import (
    REFERENCES,
    Block,
    Point,
    Refused,
    Session,
    TeeTransport,
    judge,
    main,
    ppm_apart,
    sample_sd,
)
from kleidos_meter.cli import main as cli_main
from kleidos_meter.registry import Registry, check
from test_registry import file_under

NOW = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))


def session(sim: SimulatedMeter, root: Path, answer: str = "y", **kwargs: Any) -> Session:
    transcript = TeeTransport(sim)
    return Session(
        Meter(transcript),
        root=root,
        ask=lambda prompt: answer,
        say=lambda text: None,
        sleep=lambda seconds: None,
        null_seconds=1,
        samples=10,
        now=lambda: NOW,
        transcript=transcript,
        **kwargs,
    )


def test_an_accepted_session_writes_the_working_copy_and_files_the_event(
    registry_root: Path,
) -> None:
    sim = SimulatedMeter()
    s = session(sim, registry_root)
    result = s.run()
    assert result.accepted, result.failed()
    assert [b.range for b in s.blocks] == [0, 1, 1, 0]
    assert all(len(b.points) == 4 for b in s.blocks)
    assert sim.shunt_measured
    assert sim.shunt_ohm == pytest.approx(0.100123, abs=2e-6)
    assert sim.cal_date == (2026, 11, 21)
    assert sim.tag == "R100_20261121"
    assert sim.vbus_side == "source"
    assert sim.k1_open is False  # every null closed K1 again
    path, problems = s.file()
    assert problems == []
    assert path == registry_root / "records" / SIM_SERIAL / "2026-11-21-01.md"
    assert "**accepted** and written to the instrument" in path.read_text()
    log = path.with_suffix(".log").read_text()
    assert "*IDN?" in log and "ROUT:LEAD OPEN,FORCE" in log and "KM-EE529C1B03" in log
    instrument = json.loads((registry_root / "instruments" / f"{SIM_SERIAL}.json").read_text())
    event = instrument["events"][-1]
    assert event["verdict"] == "accepted" and event["method"] == "two_point_reference/guided"
    assert event["device"]["shunt_uohm_r0"] == round(sim.shunt_ohm * 1e6)
    assert [t["term"] for t in event["budget"]][:3] == [
        "reference_tolerance",
        "reference_tcr",
        "ina228_bus_gain",
    ]
    index = json.loads((registry_root / "index.json").read_text())
    assert index["instruments"][SIM_SERIAL]["calibrations"][0]["event"] == "2026-11-21-01"
    # A second session on the same day takes the next number.
    second = session(SimulatedMeter(), registry_root)
    second.run()
    path, problems = second.file()
    assert problems == [] and path.name == "2026-11-21-02.md"


def test_an_instrument_filed_under_a_placeholder_is_calibrated_and_filed_under_it(
    registry_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    placeholder = "KM-0000000001"  # made up, as every test's serial
    file_under(registry_root, placeholder)
    with pytest.raises(Refused, match="map its serial to that one in KLEIDOS_METER_SERIAL"):
        session(SimulatedMeter(), registry_root).run()
    s = session(SimulatedMeter(), registry_root, aliases={SIM_SERIAL: placeholder})
    assert s.run().accepted
    path, problems = s.file()
    assert problems == [] and path.parent == registry_root / "records" / placeholder
    log = path.with_suffix(".log").read_text()
    assert "*IDN?" in log and placeholder in log and SIM_SERIAL not in log
    assert SIM_SERIAL not in path.read_text()
    instrument = json.loads((registry_root / "instruments" / f"{placeholder}.json").read_text())
    assert instrument["events"][-1]["verdict"] == "accepted"
    assert not (registry_root / "instruments" / f"{SIM_SERIAL}.json").exists()
    assert problems == check(Registry.from_directory(registry_root))[0] == []
    args = ["--sim", "--yes", "--null-seconds", "1", "--samples", "10"]
    monkeypatch.setenv("KLEIDOS_METER_SERIAL_ALIASES", f"{SIM_SERIAL}={placeholder}")
    assert main([*args, "--registry", str(registry_root)]) == 0
    assert len(list((registry_root / "records" / placeholder).glob("*.log"))) == 2
    monkeypatch.setenv("KLEIDOS_METER_SERIAL_ALIASES", f"{SIM_SERIAL}")
    with pytest.raises(SystemExit) as exited:
        main([*args, "--registry", str(registry_root)])
    assert exited.value.code == 2
    assert "KLEIDOS_METER_SERIAL_ALIASES: pair 1" in capsys.readouterr().err


def test_declining_the_store_writes_nothing(registry_root: Path) -> None:
    sim = SimulatedMeter()
    answers = iter(["", "y", "n", ""])
    s = session(sim, registry_root)
    s.ask = lambda prompt: next(answers)
    assert s.run().accepted
    assert not s.stored and not sim.shunt_measured
    assert "nothing written" in s.record("2026-11-21-01")


def test_a_drifting_joint_fails_the_closure_gate_and_is_filed_refused(registry_root: Path) -> None:
    sim = SimulatedMeter()
    sim.drift_ppm_per_point = 40  # about the 2026-11-20 joints, a point a minute
    s = session(sim, registry_root)
    result = s.run()
    assert not result.accepted
    assert any("first and last block" in f for f in result.failed())
    assert not sim.shunt_measured
    path, problems = s.file()
    assert problems == []
    instrument = json.loads((registry_root / "instruments" / f"{SIM_SERIAL}.json").read_text())
    assert instrument["events"][-1]["verdict"] == "refused"
    assert "first and last block" in instrument["events"][-1]["why"]
    assert "**refused**" in path.read_text()


def test_a_null_that_moves_fails_the_null_closure(registry_root: Path) -> None:
    sim = SimulatedMeter()
    sim.null_drift_pv = 300_000  # 0.3 µV a null: the closing ones are 0.6 µV up
    result = session(sim, registry_root).run()
    assert any("closing null" in f for f in result.failed())


def test_ranges_that_disagree_are_refused(registry_root: Path) -> None:
    sim = SimulatedMeter()
    sim.range_gain_ppm = [0, 1500]
    result = session(sim, registry_root).run()
    assert any(f.startswith("ranges within") for f in result.failed())


def test_a_vbus_lead_left_on_the_source_fails_the_instrument_check(registry_root: Path) -> None:
    sim = SimulatedMeter()
    sim.vbus_on_reference = False
    s = session(sim, registry_root)
    assert not s.run().accepted
    assert {p.check for b in s.blocks for p in b.points} == {"reference_low"}


def test_refusals_before_anything_is_measured(registry_root: Path) -> None:
    sim = SimulatedMeter()
    with pytest.raises(Refused, match="joints must be screwed"):
        session(sim, registry_root, answer="n").run()
    sim = SimulatedMeter()
    sim.mac = None
    with pytest.raises(Refused, match="no serial"):
        session(sim, registry_root).run()
    sim = SimulatedMeter()
    sim.firmware, sim.bypass_part = "0.7.0", "hw316"  # the HW-316 image, on the Click rig
    with pytest.raises(Refused, match="bypass_part=hw316"):
        session(sim, registry_root).run()
    sim = SimulatedMeter()
    sim.firmware = "0.3.0"
    with pytest.raises(Refused, match="older than 0.4.0"):
        session(sim, registry_root).run()
    with pytest.raises(Refused, match="hardware_change"):
        session(SimulatedMeter(), registry_root, configuration="mjkdz-r100-hw316").run()


def test_a_first_calibration_names_its_configuration(registry_root: Path) -> None:
    (registry_root / "instruments" / f"{SIM_SERIAL}.json").unlink()
    with pytest.raises(Refused, match="--configuration"):
        session(SimulatedMeter(), registry_root).run()
    with pytest.raises(Refused, match="no configuration"):
        session(SimulatedMeter(), registry_root, configuration="nothing-like-it").run()
    s = session(SimulatedMeter(), registry_root, configuration="mjkdz-r100-relay2click")
    assert s.run().accepted
    _, problems = s.file()
    assert problems == []
    events = json.loads((registry_root / "instruments" / f"{SIM_SERIAL}.json").read_text())[
        "events"
    ]
    assert [e["kind"] for e in events] == ["hardware_change", "calibration"]
    assert check(Registry.from_directory(registry_root))[0] == []


def test_a_refused_null_stops_the_session(registry_root: Path) -> None:
    sim = SimulatedMeter()
    s = session(sim, registry_root)
    sim.zero_open, sim.zero_seconds = True, 5  # a window already open: CAL:ZERO is refused
    with pytest.raises(Exception, match="null window running"):
        s.run()


def test_a_null_that_ends_with_an_error_stops_the_session(registry_root: Path) -> None:
    """The lead a hold let go of during the null is an error of that null, never a figure."""
    sim = SimulatedMeter()
    s = session(sim, registry_root)
    s.sleep = lambda seconds: sim._fail(114, "ROUT:LEAD hold ended: lead closed")
    with pytest.raises(RuntimeError, match=r"^the null was refused: 114,"):
        s.run()
    assert not sim.k1_open  # the lead is back before the session gives up
    assert not sim.shunt_measured


def test_an_image_that_is_not_the_configurations_is_refused_first(registry_root: Path) -> None:
    """The HW-316 rig's ``meter`` ended at 0.7.0: today's image is another rig's."""
    path = registry_root / "instruments" / f"{SIM_SERIAL}.json"
    instrument = json.loads(path.read_text())
    instrument["events"][0]["configuration"] = "mjkdz-r100-hw316"
    path.write_text(json.dumps(instrument))
    with pytest.raises(Refused) as refused:
        session(SimulatedMeter(), registry_root).run()
    assert str(refused.value) == (
        f"{SIM_SERIAL} is not mjkdz-r100-hw316: image=meter (the configuration says meter up to"
        " 0.7.0); bypass_part=relay2click (the configuration says hw316)"
    )


def test_a_working_copy_the_instrument_lost_is_not_filed_as_stored(registry_root: Path) -> None:
    """A tag changed between the store and the last step is read back, and refused."""
    sim = SimulatedMeter()
    s = session(sim, registry_root)

    def ask(prompt: str) -> str:
        if sim.shunt_measured:
            sim.tag = "ANOTHER"
        return "y"

    s.ask = ask
    with pytest.raises(RuntimeError, match=r"did not keep the working copy: .*'ANOTHER'"):
        s.run()


def test_a_problem_elsewhere_in_the_registry_leaves_the_index_as_it_was(
    registry_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The session is filed all the same; the index is not written over a registry that fails."""
    (registry_root / "instruments" / "KM-FFFFFFFFFF.json").write_text("{not json")
    args = ["--sim", "--yes", "--null-seconds", "1", "--samples", "10"]
    assert main([*args, "--registry", str(registry_root)]) == 1
    out = capsys.readouterr().out
    assert "The registry check found problems; the index was not regenerated:" in out
    assert "  - instruments/KM-FFFFFFFFFF.json: not JSON" in out and "Left to do" not in out
    assert len(list((registry_root / "records" / SIM_SERIAL).glob("*.md"))) == 1
    assert not (registry_root / "index.json").exists()


def test_judge_flags_each_gate_on_its_own(registry_root: Path) -> None:
    configuration = Registry.from_directory(registry_root).configurations["mjkdz-r100-relay2click"]
    reference = REFERENCES["vpg-y0785-100r"]

    def point(r: int, nohm: int, check: str = "ok", die: int | None = 25_000) -> Point:
        return Point(r, nohm, check, die, 4_100_000, {})

    def blocks(values: dict[int, list[list[int]]], **kw: Any) -> list[Block]:
        out: list[Block] = []
        for r, groups in values.items():
            out.extend(Block(r, [point(r, v, **kw) for v in group]) for group in groups)
        return out

    nulls = [{"offset_pv": "1000000", "die_mc": "25000"}] * 2
    good = blocks(
        {
            0: [[100_100_000, 100_101_000], [100_101_000, 100_100_000]],
            1: [[100_120_000, 100_119_000], [100_119_000, 100_120_000]],
        }
    )
    result = judge(good, nulls, nulls, configuration, reference)
    assert result.accepted, result.failed()
    assert result.applied_nohm == 100_110_000
    assert result.u_c_ppm == round(
        (
            58**2
            + 23**2
            + 289**2
            + result.budget[3]["u_ppm"] ** 2
            + 0
            + result.budget[5]["u_ppm"] ** 2
        )
        ** 0.5
    )
    drifting = blocks({0: [[100_100_000], [100_130_000]], 1: [[100_120_000], [100_120_000]]})
    assert any(
        "first and last" in f
        for f in judge(drifting, nulls, nulls, configuration, reference).failed()
    )
    far = blocks({0: [[103_000_000], [103_000_000]], 1: [[103_000_000], [103_000_000]]})
    assert any("nominal" in f for f in judge(far, nulls, nulls, configuration, reference).failed())
    bad = blocks(
        {0: [[100_100_000], [100_100_000]], 1: [[100_100_000], [100_100_000]]}, check="signal_low"
    )
    assert any(
        "instrument check" in f for f in judge(bad, nulls, nulls, configuration, reference).failed()
    )
    hot = [{"offset_pv": "1000000", "die_mc": "19000"}] * 2
    assert any(
        "die within" in f for f in judge(good, hot, nulls, configuration, reference).failed()
    )
    unknown = [{"offset_pv": "1000000"}] * 2
    assert any(
        "die temperature known" in f
        for f in judge(good, unknown, nulls, configuration, reference).failed()
    )
    missing = [{"die_mc": "25000"}] * 2
    assert any(
        "null closure" in f for f in judge(good, missing, nulls, configuration, reference).failed()
    )
    one_block = blocks({0: [[100_100_000]], 1: [[100_120_000], [100_120_000]]})
    assert any(
        "two blocks" in f for f in judge(one_block, nulls, nulls, configuration, reference).failed()
    )
    assert not judge([], nulls, nulls, configuration, reference).accepted
    # A drop the INA228 read as nothing has no signal to weigh a null against.
    no_drop = blocks({0: [[100_100_000], [100_100_000]], 1: [[100_100_000], [100_100_000]]})
    for block in no_drop:
        block.points = [dataclasses.replace(p, vsh_nv=0) for p in block.points]
    silent = judge(no_drop, nulls, nulls, configuration, reference)
    (drift,) = [term for term in silent.budget if term["term"] == "null_drift"]
    assert drift["limit_ppm"] == 0
    (weighed,) = [
        term
        for term in judge(
            good,
            nulls,
            [{"offset_pv": "1200000", "die_mc": "25000"}] * 2,
            configuration,
            reference,
        ).budget
        if term["term"] == "null_drift"
    ]
    assert weighed["limit_ppm"] == 49  # 0.2 µV of null over a 4.1 mV drop
    assert ppm_apart(0, 5) == 1e6
    assert ppm_apart(100, 100) == 0
    assert sample_sd([5]) == 0.0


def test_the_command_line_runs_the_whole_flow_on_the_simulator(
    registry_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--sim",
        "--yes",
        "--null-seconds",
        "1",
        "--samples",
        "10",
        "--registry",
        str(registry_root),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Record:" in out and "meter-lock" in out
    assert len(list((registry_root / "records" / SIM_SERIAL).glob("*.md"))) == 1
    assert cli_main(["calibrate", *args]) == 0
    assert (
        main(
            [
                *args,
                "--reference-ohm",
                "100",
                "--reference-tolerance-ppm",
                "100",
                "--reference-tcr",
                "2",
            ]
        )
        == 0
    )
    with pytest.raises(SystemExit):
        main([])


def test_the_command_line_reports_a_refusal_and_a_failed_gate(
    registry_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--sim", "--yes", "--registry", str(registry_root), "--samples", "10"]
    assert main([*args, "--configuration", "mjkdz-r100-hw316"]) == 1
    assert "Not started" in capsys.readouterr().out
    original = SimulatedMeter.__init__

    def disagreeing(self: SimulatedMeter, *a: Any, **kw: Any) -> None:
        original(self, *a, **kw)
        self.range_gain_ppm = [0, 5000]

    monkeypatch.setattr(SimulatedMeter, "__init__", disagreeing)
    assert main(args) == 2


def test_a_record_with_no_budget_has_no_budget_section(registry_root: Path) -> None:
    """A session that measured one range only has no uncertainty to write: its record says none."""
    s = session(SimulatedMeter(), registry_root)
    s.run()
    assert "## Uncertainty budget" in s.record("2026-11-21-01")
    s.result.budget = []
    text = s.record("2026-11-21-01")
    assert "## Uncertainty budget" not in text and "## Acceptance" in text
