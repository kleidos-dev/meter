# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The calibration registry: the schema subset, the checks, the index and its consumers."""

from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import SIM_SERIAL, write_json
from kleidos_meter import registry as reg
from kleidos_meter.registry import (
    ROOT,
    Registry,
    build_index,
    check,
    compare,
    dump_index,
    file_digest,
    image_problem,
    normalized_error,
    restore_messages,
    scale,
    select,
    validate,
)

#: The accepted event of 2026-11-20, as a template.
TEMPLATE = next(
    e
    for e in json.loads((ROOT / "instruments" / "KM-0000000000.json").read_text())["events"]
    if e["id"] == "2026-11-20-02"
)


def accepted(
    root: Path, event_id: str, *, configuration: str = "mjkdz-r100-relay2click", shift_nohm: int = 0
) -> dict[str, Any]:
    """An accepted calibration filed in *root*: the template, moved by *shift_nohm*."""
    event = copy.deepcopy(TEMPLATE)
    event["id"] = event_id
    event["configuration"] = configuration
    event["taken_at"] = f"{event_id[:10]}T10:00:00+01:00"
    for rng in event["ranges"].values():
        rng["points_nohm"] = [p + shift_nohm for p in rng["points_nohm"]]
        mean = sum(rng["points_nohm"]) / len(rng["points_nohm"])
        sd = (
            sum((p - mean) ** 2 for p in rng["points_nohm"]) / (len(rng["points_nohm"]) - 1)
        ) ** 0.5
        rng["mean_nohm"] = round(mean)
        rng["sd_ppm"] = round(sd / mean * 1e6)
        rng["applied_nohm"] += shift_nohm
    m0, m1 = event["ranges"]["0"]["mean_nohm"], event["ranges"]["1"]["mean_nohm"]
    event["range_split_ppm"] = round(abs(m1 - m0) * 1e6 / ((m0 + m1) / 2))
    microohm = event["ranges"]["0"]["applied_nohm"] // 1000
    event["device"] = {
        **event["device"],
        "shunt_uohm_r0": microohm,
        "shunt_uohm_r1": microohm,
        "cal_date": event_id[:10],
    }
    for key, suffix in (("record", "md"), ("raw_log", "log")):
        relative = f"records/x/{event_id}.{suffix}"
        text = f"{key} of {event_id}\n"
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
        event[key] = {"path": relative, "sha256": file_digest(text)}
    filed: dict[str, Any] = event
    return filed


def instrument_path(root: Path, serial: str = SIM_SERIAL) -> Path:
    return root / "instruments" / f"{serial}.json"


def add_events(root: Path, *events: dict[str, Any], serial: str = SIM_SERIAL) -> None:
    path = instrument_path(root, serial)
    instrument = json.loads(path.read_text())
    instrument["events"] += list(events)
    path.write_text(json.dumps(instrument))


def problems_of(root: Path) -> list[str]:
    return check(Registry.from_directory(root))[0]


# --- the schema subset ----------------------------------------------------


def test_the_schema_subset() -> None:
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {
            "a": {"type": "integer", "minimum": 1, "maximum": 9},
            "b": {"type": "string", "minLength": 2, "pattern": "^x"},
            "c": {"type": "array", "items": {"enum": [1, 2]}, "minItems": 1, "maxItems": 2},
            "d": {"const": "k"},
            "e": {"$ref": "#/$defs/n"},
            "f": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
            "g": {"allOf": [{"type": ["string", "null"]}]},
            "h": {"type": "object", "additionalProperties": {"type": "boolean"}},
        },
        "$defs": {"n": {"type": "null"}},
    }
    assert (
        validate(
            {
                "a": 1,
                "b": "xy",
                "c": [1],
                "d": "k",
                "e": None,
                "f": 1.5,
                "g": None,
                "h": {"z": True},
            },
            schema,
        )
        == []
    )
    errors = validate(
        {"a": 0, "b": "y", "c": [3, 1, 2], "d": "j", "e": 1, "f": 2, "g": 3, "h": {"z": 1}, "q": 1},
        schema,
    )
    joined = "\n".join(errors)
    for needle in (
        "below its minimum",
        "shorter than",
        "does not match",
        "must be one of",
        "at most 2",
        "must be 'k'",
        "expected null",
        "exactly one",
        "expected string",
        "expected boolean",
        "'q' is not a field",
    ):
        assert needle in joined, needle
    assert "missing 'a'" in "\n".join(validate({}, schema))
    assert "above its maximum" in "\n".join(validate({"a": 10}, schema))
    assert "at least 1" in "\n".join(validate({"a": 1, "c": []}, schema))
    assert "does not know" in validate(1, {"format": "x"})[0]
    assert "matches none" in validate("s", {"oneOf": [{"type": "integer"}]})[0]


# --- the real registry ----------------------------------------------------


def test_the_registry_in_this_repository_holds_and_its_index_is_current() -> None:
    registry = Registry.from_directory()
    problems, flags = check(registry)
    assert problems == [] and flags == []
    assert (ROOT / "index.json").read_text() == dump_index(build_index(registry))
    entry = build_index(registry)["instruments"]["KM-0000000000"]["calibrations"][-1]
    assert entry["ranges"]["0"] == {"applied_nohm": 100_116_000, "u_ppm": 424}
    assert entry["expanded_ppm"] == 848 and entry["valid_until"] == "2027-11-20"


# --- the checks -----------------------------------------------------------


def test_a_fixture_registry_holds(registry_root: Path) -> None:
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"))
    assert problems_of(registry_root) == []


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda e: e["ranges"]["0"].update(mean_nohm=1), "range 0 mean"),
        (lambda e: e["ranges"]["0"].update(sd_ppm=1), "range 0 sd"),
        (lambda e: e.update(range_split_ppm=1), "range split is"),
        (
            lambda e: e["ranges"]["1"].update(applied_nohm=100_200_000),
            "outside the two range means",
        ),
        (lambda e: e["budget"][0].update(u_ppm=1), "term reference_tolerance"),
        (lambda e: e.update(u_c_ppm=1), "u_c is"),
        (lambda e: e.update(expanded_ppm=1), "U is"),
        (lambda e: e["ranges"]["0"].update(u_ppm=1), "differs from u_c"),
        (lambda e: e.update(range_split_ppm=1200), "is past 1000"),
        (lambda e: e.update(configuration="mjkdz-r100-unknown"), "no known configuration"),
        (lambda e: e["record"].update(sha256="0" * 64), "changed since it was filed"),
        (lambda e: e["record"].update(path="records/x/none.md"), "is missing"),
        (lambda e: e.pop("budget"), "matches none"),
    ],
)
def test_each_check_refuses_on_its_own(
    registry_root: Path, mutate: Callable[[dict[str, Any]], object], needle: str
) -> None:
    event = accepted(registry_root, "2026-11-02-01")
    mutate(event)
    add_events(registry_root, event)
    assert any(needle in p for p in problems_of(registry_root)), problems_of(registry_root)


def test_a_range_outside_the_part_s_window(registry_root: Path) -> None:
    add_events(registry_root, accepted(registry_root, "2026-11-02-01", shift_nohm=2_500_000))
    assert any("from nominal" in p for p in problems_of(registry_root))


def test_the_instrument_file_itself(registry_root: Path) -> None:
    path = instrument_path(registry_root)
    instrument = json.loads(path.read_text())
    good = copy.deepcopy(instrument)
    instrument["serial"] = "KM-FFFFFFFFFF"
    path.write_text(json.dumps(instrument))
    assert any("its serial is" in p for p in problems_of(registry_root))
    instrument = copy.deepcopy(good)
    instrument["events"].append(copy.deepcopy(instrument["events"][0]))
    path.write_text(json.dumps(instrument))
    assert any("appears twice" in p for p in problems_of(registry_root))
    instrument = copy.deepcopy(good)
    instrument["events"].insert(0, {**instrument["events"][0], "id": "2026-12-01-00"})
    path.write_text(json.dumps(instrument))
    assert any("not in the order" in p for p in problems_of(registry_root))
    path.write_text("{not json")
    assert any("not JSON" in p for p in problems_of(registry_root))
    path.write_text("[]")
    assert any("not a JSON object" in p for p in problems_of(registry_root))


def test_what_is_not_json_in_a_directory_is_not_read_and_what_cannot_be_read_is_not_kept(
    registry_root: Path,
) -> None:
    """A README beside the records is nothing; a broken file is a problem and no configuration."""
    (registry_root / "configurations" / "README.md").write_text("not a configuration\n")
    (registry_root / "instruments" / "README.md").write_text("not an instrument\n")
    assert problems_of(registry_root) == []
    (registry_root / "configurations" / "broken.json").write_text("{not json")
    registry = Registry.from_directory(registry_root)
    assert "broken" not in registry.configurations and "README" not in registry.configurations
    assert set(registry.instruments) == {SIM_SERIAL}
    assert any(p.startswith("configurations/broken.json: not JSON") for p in check(registry)[0])


def test_a_registry_without_its_schemas_is_refused_for_them_alone(registry_root: Path) -> None:
    """Nothing is validated against a schema that is not there, and the check says which is gone."""
    shutil.rmtree(registry_root / "schemas")
    assert problems_of(registry_root) == [
        f"schemas/{name}.schema.json: missing" for name in ("configuration", "instrument", "index")
    ]


def test_a_budget_of_one_point_and_one_range_is_recomputed_for_what_it_holds(
    registry_root: Path,
) -> None:
    """One point has no deviation to recompute, and one range no split."""
    event = accepted(registry_root, "2026-11-02-01")
    only = event["ranges"]["0"]
    only["points_nohm"] = [only["mean_nohm"]]
    event["ranges"] = {"0": only}
    event["range_split_ppm"] = 12_345
    assert reg.budget_problems(event) == []
    only["mean_nohm"] += 10
    assert reg.budget_problems(event) == [
        f"event 2026-11-02-01: range 0 mean is {only['points_nohm'][0]}, not {only['mean_nohm']}"
    ]


def test_a_configuration_whose_image_ended_says_so_in_the_index(registry_root: Path) -> None:
    """The HW-316 rig's ``meter`` ends at 0.7.0, and an index without that would accept 0.18."""
    change = {
        "id": "2026-12-01-00",
        "kind": "hardware_change",
        "configuration": "mjkdz-r100-hw316",
        "voids_calibration": True,
        "what": "the HW-316 rig",
    }
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"), change)
    index = build_index(Registry.from_directory(registry_root))
    configurations = index["instruments"][SIM_SERIAL]["configurations"]
    assert "max_version" not in configurations["mjkdz-r100-relay2click"]
    assert configurations["mjkdz-r100-hw316"]["max_version"] == "0.7.0"


def test_a_configuration_named_apart_from_its_file(registry_root: Path) -> None:
    source = registry_root / "configurations" / "mjkdz-r100-hw316.json"
    configuration = json.loads(source.read_text())
    write_json(registry_root / "configurations" / "renamed.json", configuration)
    assert any("its id is" in p for p in problems_of(registry_root))


def test_a_calibration_under_a_configuration_the_instrument_was_not_in(registry_root: Path) -> None:
    event = accepted(registry_root, "2026-11-02-01", configuration="mjkdz-r100-hw316")
    add_events(registry_root, event)
    assert any("but the instrument was" in p for p in problems_of(registry_root))


def test_successive_calibrations_by_their_normalized_error(registry_root: Path) -> None:
    first = accepted(registry_root, "2026-11-02-01")
    near = accepted(registry_root, "2026-12-02-01", shift_nohm=50_000)
    add_events(registry_root, first, near)
    assert problems_of(registry_root) == []
    assert normalized_error(first, near, "0") < 1
    index = build_index(Registry.from_directory(registry_root))
    drift = index["instruments"][SIM_SERIAL]["drift"]
    assert drift[0]["ppm_per_year"] == round(50_000 * 1e6 / 100_116_000 * 365 / 30)
    # A move past what the two calibrations explain needs an acknowledgement.
    far = accepted(registry_root, "2027-01-02-01", shift_nohm=400_000)
    add_events(registry_root, far)
    assert any("E = " in p for p in problems_of(registry_root))
    instrument = json.loads(instrument_path(registry_root).read_text())
    instrument["events"][-1]["acknowledged"] = "the shunt was re-seated"
    instrument_path(registry_root).write_text(json.dumps(instrument))
    assert problems_of(registry_root) == []


def test_a_voiding_change_separates_calibrations(registry_root: Path) -> None:
    change = {
        "id": "2026-11-10-00",
        "kind": "hardware_change",
        "configuration": "mjkdz-r100-relay2click",
        "voids_calibration": True,
        "what": "a new shunt",
    }
    add_events(
        registry_root,
        accepted(registry_root, "2026-11-02-01"),
        change,
        accepted(registry_root, "2026-11-20-01", shift_nohm=900_000),
    )
    assert problems_of(registry_root) == []
    calibrations = build_index(Registry.from_directory(registry_root))["instruments"][SIM_SERIAL][
        "calibrations"
    ]
    assert calibrations[0]["valid_until"] == "2026-11-10"
    assert calibrations[1]["valid_until"] == "2027-11-20"


def test_a_problem_names_the_instrument_it_is_in(registry_root: Path) -> None:
    add_events(registry_root, accepted(registry_root, "2026-11-02-01", shift_nohm=2_500_000))
    found = [p for p in problems_of(registry_root) if "from nominal" in p]
    assert found and all(p.startswith(f"instruments/{SIM_SERIAL}.json: ") for p in found)


def test_events_of_one_day_count_in_the_order_they_were_filed(registry_root: Path) -> None:
    shutil.copy2(
        ROOT / "configurations" / "mjkdz-r100-relay2click.json",
        registry_root / "configurations" / "mjkdz-r100-relay2click.json",
    )
    # A change later the same day is not the configuration the calibration was
    # taken in, and a voiding one ends that calibration on that day.
    change = {
        "id": "2026-11-02-02",
        "kind": "hardware_change",
        "configuration": "mjkdz-r100-relay2click",
        "voids_calibration": True,
        "what": "the shunt was replaced after the calibration",
    }
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"), change)
    assert problems_of(registry_root) == []
    index = build_index(Registry.from_directory(registry_root))
    entry = index["instruments"][SIM_SERIAL]["calibrations"][0]
    assert entry["valid_until"] == "2026-11-02"
    assert select(index, SIM_SERIAL, dt.date(2026, 11, 2))[0] != "applied"


def test_a_group_that_mostly_agrees_still_flags_the_one_apart(registry_root: Path) -> None:
    # Three instruments at one value give a median absolute deviation of zero;
    # the scale falls back to the calibrations' own expanded uncertainty.
    serials = ["KM-0000000001", "KM-0000000002", "KM-0000000003"]
    for number, serial in enumerate(serials):
        source = json.loads(instrument_path(registry_root).read_text())
        source["serial"] = serial
        source["events"] = source["events"][:1]
        instrument_path(registry_root, serial).write_text(json.dumps(source))
        shift = 1_500_000 if number == 2 else 0
        add_events(
            registry_root,
            accepted(registry_root, f"2026-11-0{number + 2}-01", shift_nohm=shift),
            serial=serial,
        )
    instrument_path(registry_root).unlink()
    problems, flags = check(Registry.from_directory(registry_root))
    assert problems == []
    assert any("KM-0000000003 sits apart" in f for f in flags), flags
    assert not any("KM-0000000001" in f or "KM-0000000002" in f for f in flags)


def test_instruments_of_one_sense_core_are_compared(registry_root: Path) -> None:
    serials = ["KM-0000000001", "KM-0000000002", "KM-0000000003", "KM-0000000004"]
    for number, serial in enumerate(serials):
        source = json.loads(instrument_path(registry_root).read_text())
        source["serial"] = serial
        source["events"] = source["events"][:1]
        instrument_path(registry_root, serial).write_text(json.dumps(source))
        shift = 10_000 * number if number < 3 else 1_500_000  # the fourth sits apart
        add_events(
            registry_root,
            accepted(registry_root, f"2026-11-0{number + 2}-01", shift_nohm=shift),
            serial=serial,
        )
    problems, flags = check(Registry.from_directory(registry_root))
    assert problems == []
    assert any("KM-0000000004 sits apart" in f for f in flags)
    groups = build_index(Registry.from_directory(registry_root))["sense_groups"]
    assert groups["mjkdz-ina228+r100-unmarked range 0"]["compared"] is True


# --- the index and its consumers ------------------------------------------


@pytest.fixture
def index(registry_root: Path) -> dict[str, Any]:
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"))
    return build_index(Registry.from_directory(registry_root))


def test_select(index: dict[str, Any]) -> None:
    assert select(index, "KM-FFFFFFFFFF", dt.date(2026, 11, 5))[0] == "unregistered"
    assert select(index, SIM_SERIAL, dt.date(2026, 11, 1))[0] == "unregistered"
    status, entry = select(index, SIM_SERIAL, dt.date(2026, 11, 5))
    assert status == "applied" and entry is not None and entry["event"] == "2026-11-02-01"
    assert select(index, SIM_SERIAL, dt.date(2027, 11, 2))[0] == "expired"


def device(**overrides: str) -> tuple[str, dict[str, str], dict[str, str]]:
    idn = f"KLEIDOS,KLEIDOS-METER,{SIM_SERIAL},0.8.0"
    info = {"image": "meter", "bypass_part": "relay2click", "mfg": "0x5449", "die": "0x228"}
    cal = {
        "shunt_measured": "1",
        "shunt_uohm_r0": "100116",
        "shunt_uohm_r1": "100116",
        "cal_tag": "R100_20261120_HR",
        "cal_date": "2026-11-02",
        "vbus_side": "source",
    }
    for key, value in overrides.items():
        if key == "idn":
            idn = value
        elif key in info:
            info[key] = value
        else:
            cal[key] = value
    return idn, info, cal


def test_compare_each_status(index: dict[str, Any]) -> None:
    day = dt.date(2026, 11, 5)
    assert compare(index, *device(), day).status == "applied"
    assert (
        compare(index, *device(idn="KLEIDOS,KLEIDOS-METER,0,0.3.0"), day).status == "unregistered"
    )
    assert compare(index, *device(), dt.date(2026, 10, 1)).status == "unregistered"
    mismatch = compare(index, *device(bypass_part="hw316"), day)
    assert (
        mismatch.status == "configuration_mismatch"
        and "bypass_part=hw316" in mismatch.differences[0]
    )
    renamed = compare(index, *device(image="other"), day)
    assert renamed.differences == ["image=other where the registry says meter_relay2 or meter"]
    old = compare(index, *device(idn=f"KLEIDOS,KLEIDOS-METER,{SIM_SERIAL},0.4.0-x"), day)
    assert old.status == "configuration_mismatch"
    assert compare(index, *device(), dt.date(2027, 12, 1)).status == "expired"
    assert compare(index, *device(shunt_measured="0"), day).status == "working_copy_lost"
    differs = compare(index, *device(cal_date="-"), day)
    assert differs.status == "working_copy_differs" and "cal_date=-" in differs.differences[0]
    late = compare(index, *device(), dt.date(2027, 10, 20))
    assert late.status == "applied" and late.warnings and late.ok


def test_one_image_from_080_is_the_click_rig_and_the_old_name_stays_valid() -> None:
    """From 0.8.0 the one image ``meter`` is the Relay 2 Click rig's.

    The events filed on 2026-11-20 name ``meter_relay2`` and are never edited:
    the configuration keeps that name and accepts ``meter`` from 0.8.0 as an
    alias, while the HW-316 rig's ``meter`` ends at 0.7.0.
    """
    configurations = ROOT / "configurations"
    click = json.loads((configurations / "mjkdz-r100-relay2click.json").read_text())["firmware"]
    hw316 = json.loads((configurations / "mjkdz-r100-hw316.json").read_text())["firmware"]
    assert image_problem(click, "meter_relay2", "0.6.1") is None
    assert image_problem(click, "meter", "0.8.0") is None
    assert image_problem(click, "METER", "0.9.0") is None
    assert image_problem(click, "meter", "0.7.0") == ("version", "0.8.0")
    assert image_problem(click, "meter_relay2", "0.3.0") == ("version", "0.4.0")
    assert image_problem(click, "other", "0.8.0") == ("image", "meter_relay2 or meter")
    assert image_problem(hw316, "meter", "0.7.0") is None
    assert image_problem(hw316, "meter", "0.8.0") == ("image", "meter up to 0.7.0")
    assert image_problem(hw316, "meter_relay2", "0.6.1") == ("image", "meter")


def test_compare_accepts_the_one_image_on_the_click_rig(registry_root: Path) -> None:
    index = build_index(Registry.from_directory(ROOT))
    serial = "KM-0000000000"
    entry = index["instruments"][serial]["calibrations"][-1]
    day = dt.date.fromisoformat(entry["valid_from"])
    info = {"image": "meter", "bypass_part": "relay2click", "mfg": "0x5449", "die": "0x228"}
    cal = {
        "shunt_measured": "1",
        "shunt_uohm_r0": str(entry["device"]["shunt_uohm_r0"]),
        "shunt_uohm_r1": str(entry["device"]["shunt_uohm_r1"]),
        "cal_tag": entry["device"]["cal_tag"],
        "cal_date": entry["device"]["cal_date"],
        "vbus_side": entry["device"]["vbus_side"],
    }
    applied = compare(index, f"KLEIDOS,KLEIDOS-METER,{serial},0.8.0", info, cal, day)
    assert applied.status == "applied", applied.differences
    older = compare(index, f"KLEIDOS,KLEIDOS-METER,{serial},0.7.0", info, cal, day)
    assert older.status == "configuration_mismatch"
    assert older.differences == ["firmware 0.7.0 is older than 0.8.0"]
    named = compare(
        index, f"KLEIDOS,KLEIDOS-METER,{serial},0.6.1", {**info, "image": "meter_relay2"}, cal, day
    )
    assert named.status == "applied", named.differences


def test_restore_messages_and_the_rescale(index: dict[str, Any]) -> None:
    entry = index["instruments"][SIM_SERIAL]["calibrations"][0]
    assert restore_messages(entry) == [
        "SENS:CURR:SHUN 0.100116",
        'CAL:TAG "R100_20261120_HR"',
        "CAL:DATE 2026,11,2",
        "CAL:VBUS SOUR",
    ]
    split = copy.deepcopy(entry)
    split["device"].update(shunt_uohm_r1=100_144, vbus_side="unknown")
    messages = restore_messages(split)
    assert "SENS:CURR:SHUN:RANG1 0.100144" in messages and messages[-1] == "CAL:VBUS UNKN"
    assert scale(entry, 0, 100_116) == pytest.approx(1.0)
    assert scale(entry, 1, 100_000) == pytest.approx(100_000_000 / 100_116_000)


def test_the_registry_read_from_a_git_revision(registry_root: Path) -> None:
    repository = registry_root.parent
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"))
    (registry_root / "index.json").write_text(
        dump_index(build_index(Registry.from_directory(registry_root)))
    )
    git = ["git", "-C", str(repository), "-c", "user.email=", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repository / ".gitignore").write_text("!*.log\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "--no-gpg-sign", "-m", "fixture"], check=True)
    # A later edit in the working tree does not reach the revision.
    instrument_path(registry_root).write_text("{}")
    at_head = Registry.from_git(repository, "HEAD")
    assert check(at_head)[0] == []
    assert SIM_SERIAL in json.loads(at_head.read("index.json") or "{}")["instruments"]
    assert Registry.from_git(repository, "no-such-rev").instruments == {}
    assert reg.iter_problems(["a", "b"]) == "  - a\n  - b"
