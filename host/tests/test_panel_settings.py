# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Each range's shunt with the registry's uncertainty, the overrides, the restore, the relays.

The arithmetic is checked against the registry's own figures for the bench
instrument; the routes run on the clock-driven simulator behind serve's
session, against a registry that files the simulator's calibration.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import SIM_SERIAL
from kleidos_meter.panel.http import Api, ApiResponse
from kleidos_meter.panel.settings import (
    NOT_QUOTABLE,
    OVERRIDE_SCHEMA,
    Calibration,
    OverrideStore,
    SettingsRefused,
    Uncertainty,
    normalized_error,
    parse_override,
    registry_uncertainty,
)
from kleidos_meter.registry import DIVISORS, ROOT, Registry, build_index, select
from test_panel_http import Panel, serving_panel
from test_registry import accepted, add_events

#: The bench instrument, and a day its calibration of 2026-11-20 holds on.
BENCH = "KM-0000000000"
TODAY = dt.date(2026, 11, 22)


@pytest.fixture
def registry(registry_root: Path) -> Path:
    """A registry that files the simulator's calibration (the bench's, moved to its serial)."""
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"))
    return registry_root


@pytest.fixture
def panel(registry: Path) -> Iterator[Panel]:
    with serving_panel(registry=registry, today=TODAY) as served:
        yield served


def overrides_of(panel: Panel) -> Path:
    """The simulator's override file."""
    return panel.calibration.overrides.directory / f"{SIM_SERIAL}.json"


def restored(panel: Panel) -> None:
    """Write the registry's copy, as after a flash: the starting point of most tests."""
    answer = panel.post("/api/calibration/restore")
    assert answer.status == 200, answer.body


# -- the arithmetic ----------------------------------------------------------------------


def test_the_uncertainty_reproduces_the_registry_figures_for_the_bench_instrument() -> None:
    index = build_index(Registry.from_directory(ROOT))
    status, entry = select(index, BENCH, TODAY)
    assert status == "applied" and entry is not None
    for range_index in (0, 1):
        figures = registry_uncertainty(entry, range_index)
        assert figures.value_nohm == 100_116_000
        assert (figures.u_ppm, figures.k, figures.expanded_ppm) == (424, 2, 848)
        # calibration/records/2026-11-20.md: "U (k = 2) = 848 ppm, ±85 µΩ".
        assert figures.expanded_uohm == 84.898 and round(figures.expanded_uohm) == 85
        assert figures.to_json() == {
            "value_uohm": 100116.0,
            "u_ppm": 424,
            "k": 2,
            "expanded_ppm": 848,
            "expanded_uohm": 84.898,
        }
    # And u_c itself, from the budget's terms: u = limit / divisor, in quadrature.
    instrument = json.loads((ROOT / "instruments" / f"{BENCH}.json").read_text())
    event = next(e for e in instrument["events"] if e["id"] == entry["event"])
    terms = [t["limit_ppm"] / DIVISORS[t["distribution"]] for t in event["budget"]]
    assert round(math.sqrt(sum(u * u for u in terms))) == entry["ranges"]["0"]["u_ppm"]


@pytest.mark.parametrize(
    ("order", "u_ppm", "expanded_uohm"),
    [
        ({"range": 1, "ohms": 0.1, "expanded_ppm": 848}, 424.0, 84.8),
        ({"range": 1, "uohm": 100_000, "expanded_uohm": 84.8}, 424.0, 84.8),
        ({"range": 0, "ohms": 0.1, "tolerance_ppm": 1000}, 577.4, 115.48),
        ({"range": 0, "uohm": 100_000, "tolerance_uohm": 100}, 577.4, 115.48),
    ],
)
def test_an_override_takes_u_at_k2_or_a_tolerance_as_a_rectangular_limit(
    order: dict[str, Any], u_ppm: float, expanded_uohm: float
) -> None:
    request = parse_override(order)
    assert request.uncertainty.u_ppm == u_ppm
    assert request.uncertainty.expanded_uohm == expanded_uohm
    assert request.uncertainty.value_nohm == 100_000_000
    assert request.given == {
        k: float(v) for k, v in order.items() if k not in ("range", "ohms", "uohm")
    }
    assert request.unit == f"SENS:CURR:SHUN:RANG{order['range']} 0.100000"


@pytest.mark.parametrize(
    ("order", "error", "detail"),
    [
        ({"range": 1, "ohms": 0.1}, "uncertainty_needed", "needs its uncertainty"),
        ({"range": 1, "ohms": 0.1, "note": "datasheet"}, "uncertainty_needed", "once"),
        (
            {"range": 1, "ohms": 0.1, "expanded_ppm": 848, "tolerance_ppm": 1000},
            "uncertainty_needed",
            "once",
        ),
        ({"range": 1, "expanded_ppm": 848}, "bad_override", "as ohms or as uohm"),
        ({"range": 1, "ohms": 0.1, "uohm": 1, "expanded_ppm": 8}, "bad_override", "once"),
        ({"range": 2, "ohms": 0.1, "expanded_ppm": 8}, "bad_override", "range is 0 or 1"),
        ({"range": True, "ohms": 0.1, "expanded_ppm": 8}, "bad_override", "range is 0 or 1"),
        ({"range": 1.0, "ohms": 0.1, "expanded_ppm": 8}, "bad_override", "range is 0 or 1"),
        ({"ohms": 0.1, "expanded_ppm": 8}, "bad_override", "range is 0 or 1"),
        ({"range": 1, "ohms": "0.1", "expanded_ppm": 8}, "bad_override", "ohms is a number"),
        ({"range": 1, "ohms": -0.1, "expanded_ppm": 8}, "bad_override", "above 0"),
        ({"range": 1, "ohms": math.inf, "expanded_ppm": 8}, "bad_override", "above 0"),
        ({"range": 1, "ohms": 0.1, "expanded_ppm": 0}, "bad_override", "above 0"),
        ({"range": 1, "uohm": 0.0004, "expanded_ppm": 8}, "bad_override", "rounds to 0"),
        ({"range": 1, "ohms": 0.1, "expanded_ppm": 8, "note": 5}, "bad_override", "note"),
        ({"range": 1, "ohms": 0.1, "expanded_ppm": 8, "note": "x" * 201}, "bad_override", "note"),
        ({"range": 1, "ohms": 0.1, "expanded_ppm": 8, "k": 3}, "bad_override", "takes no k"),
    ],
)
def test_an_override_of_the_wrong_form_is_refused(
    order: dict[str, Any], error: str, detail: str
) -> None:
    with pytest.raises(SettingsRefused) as refused:
        parse_override(order)
    assert refused.value.status == 400 and refused.value.error == error
    assert detail in refused.value.detail


def test_the_normalized_error_weighs_the_difference_by_both_uncertainties() -> None:
    first = Uncertainty(100_000_000, 424)
    second = Uncertainty(100_100_000, 424)
    spread = math.hypot(first.expanded_nohm, second.expanded_nohm)
    assert normalized_error(first, second) == pytest.approx(100_000 / spread)
    assert normalized_error(first, first) == 0.0


# -- the overrides on the host -------------------------------------------------------------


def test_the_store_keeps_one_file_per_serial_readable_by_the_user_alone(tmp_path: Path) -> None:
    store = OverrideStore(tmp_path / "overrides")
    assert store.load(SIM_SERIAL) is None and store.clear(SIM_SERIAL) is False
    store.save(SIM_SERIAL, 1, {"value_uohm": 1})
    document = store.save(SIM_SERIAL, 0, {"value_uohm": 2})
    assert document["schema"] == OVERRIDE_SCHEMA and document["banner"] == NOT_QUOTABLE
    assert document["quotable"] is False and set(document["ranges"]) == {"0", "1"}
    path = store.path(SIM_SERIAL)
    assert json.loads(path.read_text()) == document
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == [path]  # no temporary file left
    assert store.clear(SIM_SERIAL) is True and store.load(SIM_SERIAL) is None
    for serial in ("0", "../x", "KM-0000000000.json"):
        assert store.load(serial) is None and store.clear(serial) is False
        with pytest.raises(SettingsRefused) as refused:
            store.path(serial)
        assert refused.value.status == 409 and refused.value.error == "no_serial"


# -- through the API -----------------------------------------------------------------------


def test_the_shunt_of_a_flashed_instrument_is_nominal_and_not_quotable(panel: Panel) -> None:
    shunt = panel.get("/api/settings").body["shunt"]
    assert shunt["serial"] == SIM_SERIAL and shunt["verdict"]["status"] == "working_copy_lost"
    assert shunt["quotable"] is False and shunt["banner"] is None
    for range_view in shunt["ranges"]:
        assert range_view["source"] == "nominal" and range_view["device_uohm"] == 100_000
        assert range_view["uncertainty"] is None and range_view["override"] is None
        assert range_view["registry"]["event"] == "2026-11-02-01"
        assert range_view["registry"]["expanded_ppm"] == 848
        assert range_view["deviation_ppm"] == -1159  # 100000 against 100116 µΩ


def test_the_restore_writes_the_registry_copy_and_its_figures_apply(panel: Panel) -> None:
    answer = panel.post("/api/calibration/restore")
    assert answer.status == 200
    body = answer.body
    assert body["before"]["status"] == "working_copy_lost" and body["after"]["ok"] is True
    assert body["written"] is True and body["cleared_override"] is False
    assert any(line.startswith("Will send: SENS:CURR:SHUN 0.100116") for line in body["said"])
    shunt = panel.get("/api/settings").body["shunt"]
    assert shunt["quotable"] is True and shunt["verdict"]["status"] == "applied"
    for range_view in shunt["ranges"]:
        assert range_view["source"] == "registry" and range_view["deviation_ppm"] == 0
        assert range_view["uncertainty"]["expanded_ppm"] == 848
        assert range_view["uncertainty"]["expanded_uohm"] == 84.898
    again = panel.post("/api/calibration/restore").body
    assert again["written"] is False and "Nothing to restore" in again["said"][-1]
    verdict = panel.get("/api/calibration").body["verdict"]
    assert verdict["ok"] and verdict["event"] == "2026-11-02-01"
    assert verdict["lines"][0] == f"{SIM_SERIAL}: applied"


def test_a_preview_sends_only_queries_and_sets_the_value_against_the_registry(
    panel: Panel,
) -> None:
    restored(panel)
    panel.sent.clear()
    answer = panel.get("/api/settings/shunt/preview?range=1&ohms=0.1002&tolerance_ppm=1000")
    assert answer.status == 200
    preview = answer.body
    assert preview["unit"] == "SENS:CURR:SHUN:RANG1 0.100200"
    assert preview["uncertainty"]["u_ppm"] == 577.4 and preview["given"] == {"tolerance_ppm": 1000}
    assert preview["device_uohm"] == 100_116 and preview["deviation_ppm"] == 839
    registry = Uncertainty(100_116_000, 424)
    mine = Uncertainty(100_200_000, 577.4)
    assert preview["normalized_error"] == round(normalized_error(registry, mine), 3)
    assert preview["quotable"] is False and preview["banner"] == NOT_QUOTABLE
    assert all(info.message.endswith("?") or "?;:" in info.message for info in panel.sent)
    assert panel.sim.range_shunt_ohm(1) == pytest.approx(0.100116)


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("range=1&ohms=0.1002", "uncertainty_needed"),
        ("range=1&ohms=abc&tolerance_ppm=1000", "bad_parameter"),
        ("range=1&range=0&ohms=0.1&tolerance_ppm=1000", "bad_parameter"),
        ("range=one&ohms=0.1&tolerance_ppm=1000", "bad_override"),
        ("range=1&ohms=0.1&tolerance_ppm=1000&note=a%20datasheet&k=2", "bad_override"),
    ],
)
def test_a_preview_of_the_wrong_form_is_refused_unsent(
    panel: Panel, query: str, error: str
) -> None:
    answer = panel.get(f"/api/settings/shunt/preview?{query}")
    assert answer.status == 400 and answer.body["error"] == error
    assert panel.sent == []


def test_an_override_is_written_kept_on_the_host_and_reverted_to_the_registry(
    panel: Panel,
) -> None:
    restored(panel)
    order = {"range": 1, "ohms": 0.1002, "tolerance_ppm": 1000, "note": "a swapped shunt"}
    unconfirmed = panel.post("/api/settings/shunt", order)
    assert unconfirmed.status == 400 and unconfirmed.body["error"] == "confirm_needed"
    answer = panel.post("/api/settings/shunt", {**order, "confirm": True})
    assert answer.status == 200 and answer.body["written"] == "SENS:CURR:SHUN:RANG1 0.100200"
    assert panel.sim.range_shunt_ohm(1) == pytest.approx(0.1002)
    record = json.loads(overrides_of(panel).read_text())["ranges"]["1"]
    assert record["value_uohm"] == 100_200 and record["u_ppm"] == 577.4
    assert record["note"] == "a swapped shunt"
    assert record["replaced"] == {"device_uohm": 100_116, "source": "registry"}
    assert record["against_registry"]["deviation_ppm"] == 839
    shunt = answer.body["shunt"]
    assert shunt["quotable"] is False and shunt["banner"] == NOT_QUOTABLE
    assert shunt["verdict"]["status"] == "working_copy_differs"
    one = shunt["ranges"][1]
    assert one["source"] == "override" and one["uncertainty"]["expanded_ppm"] == 1154.8
    assert shunt["ranges"][0]["source"] == "registry"
    revert = panel.post("/api/calibration/restore")
    assert revert.status == 200 and revert.body["cleared_override"] is True
    assert not overrides_of(panel).exists()
    after = panel.get("/api/settings").body["shunt"]
    assert after["quotable"] is True and after["ranges"][1]["source"] == "registry"


def test_an_override_without_its_uncertainty_is_refused_unsent(panel: Panel) -> None:
    answer = panel.post("/api/settings/shunt", {"range": 1, "ohms": 0.1002, "confirm": True})
    assert answer.status == 400 and answer.body["error"] == "uncertainty_needed"
    assert panel.sent == [] and not overrides_of(panel).exists()


def test_a_value_the_instrument_refuses_is_answered_with_its_error_and_kept_nowhere(
    panel: Panel,
) -> None:
    raw = panel.post("/api/scpi", "SENS:CURR:SHUN:RANG1 0.300000").body["replies"][0]
    order = {"range": 1, "ohms": 0.3, "expanded_ppm": 1000, "confirm": True}
    answer = panel.post("/api/settings/shunt", order)
    assert answer.status == 409 and answer.body["error"] == "refused"
    assert answer.body["errors"] == raw["errors"] and raw["errors"][0]["code"] == -222
    assert answer.body["unit"] == "SENS:CURR:SHUN:RANG1 0.300000"
    assert not overrides_of(panel).exists()


def test_an_instrument_with_no_serial_takes_no_override(panel: Panel) -> None:
    panel.sim.mac = None
    order = {"range": 0, "ohms": 0.1002, "expanded_ppm": 1000, "confirm": True}
    answer = panel.post("/api/settings/shunt", order)
    assert answer.status == 409 and answer.body["error"] == "no_serial"
    assert not any(info.message.startswith("SENS:CURR:SHUN:RANG") for info in panel.sent)


def test_a_value_nobody_filed_has_no_uncertainty(panel: Panel) -> None:
    restored(panel)
    panel.post("/api/scpi", "SENS:CURR:SHUN 0.1003")
    shunt = panel.get("/api/settings").body["shunt"]
    assert [r["source"] for r in shunt["ranges"]] == ["device", "device"]
    assert all(r["uncertainty"] is None for r in shunt["ranges"])
    assert shunt["quotable"] is False and shunt["verdict"]["status"] == "working_copy_differs"


def test_an_override_the_instrument_no_longer_holds_is_shown_and_not_applied(
    panel: Panel,
) -> None:
    restored(panel)
    order = {"range": 0, "ohms": 0.1002, "expanded_ppm": 1000, "confirm": True}
    assert panel.post("/api/settings/shunt", order).status == 200
    panel.post("/api/scpi", "SENS:CURR:SHUN 0.100116")  # a restore by another tool
    shunt = panel.get("/api/settings").body["shunt"]
    zero = shunt["ranges"][0]
    assert zero["source"] == "registry" and zero["override"]["value_uohm"] == 100_200
    assert shunt["quotable"] is True and shunt["banner"] is None


def test_the_registry_entry_shows_only_for_the_instrument_it_describes(
    registry: Path,
) -> None:
    with serving_panel(registry=registry, today=dt.date(2028, 1, 1)) as panel:
        shunt = panel.get("/api/settings").body["shunt"]
        assert shunt["verdict"]["status"] == "expired"
        assert shunt["ranges"][0]["registry"]["valid_until"] == "2027-11-02"
        panel.sim.bypass_part = "hw316"
        shunt = panel.get("/api/settings").body["shunt"]
        assert shunt["verdict"]["status"] == "configuration_mismatch"
        assert shunt["ranges"][0]["registry"] is None
        assert shunt["ranges"][0]["deviation_ppm"] is None
        preview = panel.get("/api/settings/shunt/preview?range=0&uohm=100200&expanded_ppm=900")
        assert preview.body["registry"] is None and preview.body["normalized_error"] is None


def test_without_a_registry_the_shunt_is_unregistered_and_nothing_is_restored() -> None:
    with serving_panel() as panel:
        shunt = panel.get("/api/settings").body["shunt"]
        assert shunt["verdict"]["status"] == "unregistered"
        assert shunt["registry_problem"] == "serve was given no registry"
        assert shunt["verdict"]["differences"][-1] == "serve was given no registry"
        answer = panel.post("/api/calibration/restore")
        assert answer.status == 409 and answer.body["written"] is False
        assert "Not restored" in answer.body["said"][-1]


def test_a_registry_that_cannot_be_read_is_said_so(panel: Panel) -> None:
    def broken(root: Path) -> dict[str, Any]:
        raise ValueError("calibration/index.json is not JSON")

    calibration = Calibration(
        panel.executor, Path("calibration"), panel.calibration.overrides, index_loader=broken
    )
    verdict = calibration.verify()
    assert verdict["registry_problem"] == (
        "the registry could not be read: calibration/index.json is not JSON"
    )
    assert verdict["verdict"]["status"] == "unregistered"


def test_a_registry_file_that_cannot_be_read_is_named_by_no_absolute_path(
    panel: Panel, tmp_path: Path
) -> None:
    missing = tmp_path / "calibration" / "instruments" / "KM-X.json"

    def broken(root: Path) -> dict[str, Any]:
        index: dict[str, Any] = json.loads(missing.read_text(encoding="utf-8"))
        return index

    calibration = Calibration(
        panel.executor, tmp_path / "calibration", panel.calibration.overrides, index_loader=broken
    )
    problem = calibration.verify()["registry_problem"]
    assert problem == (
        "the registry could not be read: No such file or directory: instruments/KM-X.json"
    )


def test_without_a_calibration_side_the_shunt_routes_answer_503(panel: Panel) -> None:
    bare = Api(panel.api.session)
    host = {"Host": "127.0.0.1", "X-Kleidos-Meter": "1"}
    for method, path in (
        ("GET", "/api/calibration"),
        ("POST", "/api/calibration/restore"),
        ("GET", "/api/settings/shunt/preview?range=1"),
        ("POST", "/api/settings/shunt"),
    ):
        answer = bare.handle(method, path, host)
        assert isinstance(answer, ApiResponse)
        assert answer.status == 503 and answer.body["error"] == "unavailable", path
    settings = bare.handle("GET", "/api/settings", host)
    assert isinstance(settings, ApiResponse)
    assert settings.status == 200 and settings.body["shunt"] is None


# -- the relays: the instrument's refusal, unchanged ---------------------------------------


@pytest.mark.parametrize(
    ("relay", "order", "unit", "code"),
    [
        ("lead", {"state": "OPEN"}, "ROUT:LEAD OPEN", 109),
        ("bypass", {"state": "ON", "live": True}, "ROUT:BYP ON,LIVE", 110),
        ("nullpath", {"state": "ON", "live": True}, "ROUT:NULL ON,LIVE", 110),
        ("bypass", {"state": "ON"}, "ROUT:BYP ON", -221),
    ],
)
def test_a_relay_order_without_the_flag_it_needs_answers_the_instruments_refusal(
    panel: Panel, relay: str, order: dict[str, Any], unit: str, code: int
) -> None:
    if code != 109:
        panel.post("/api/capture/start", {"profile": "STATE"})  # a capture runs
    raw = panel.post("/api/scpi", unit).body["replies"][0]
    answer = panel.post(f"/api/relays/{relay}", order)
    assert answer.status == 409 and answer.body["order"] == unit  # no flag added
    assert answer.body["errors"] == raw["errors"] and raw["errors"][0]["code"] == code
    assert answer.body["state"]["path"] == "shunt"


def test_the_link_answers_as_a_meter_does(panel: Panel) -> None:
    from kleidos_meter.client import MeterError
    from kleidos_meter.panel.settings import InstrumentLink

    link = InstrumentLink(panel.executor)
    assert link.idn().startswith("KLEIDOS,") and link.info()["serial"] == SIM_SERIAL
    assert link.calibration()["shunt_measured"] == "0"
    answers, errors, block = link.exchange(["SAMP:COUN 7", "SAMP:COUN?"])
    assert (answers, errors, block) == (["7"], [], None)
    with pytest.raises(MeterError) as refused:
        link.exchange(["SAMP:COUN 0", "SAMP:COUN?"])
    assert refused.value.codes == [-222]
    _, errors, _ = link.exchange(["SAMP:COUN 0"], raise_errors=False)
    assert [entry.code for entry in errors] == [-222]
