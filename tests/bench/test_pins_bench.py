# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pin map applied, reverted and put back on the meter (FW-05).

``SYSTem:PINS:APPLy`` moves the relays, ALERT and the INA228's bus, proves the
instrument there, and saves the map or puts the last good one back
(``firmware/src/pin_apply.h``, ``docs/architecture.md`` §2.7). On the meter:

* SDA staged on a GPIO the INA228 is not on, its address pinned to one nothing
  answers at, the pair swapped, and ALERT on a GPIO with no wire are each
  reverted with one ``117``: the live map, ``*TST?``, the calibration's quality
  and the record are as they were;
* Relay 1 moved to GPIO16, where nothing is wired, with GPIO7 parked: its pads
  read right, and the DUT's current is still read through the shunt, right
  after the move (GPIO7 parked toward its released level) and after a reboot
  (GPIO7 parked low like any free pin), because Relay 1 stays released; moved
  back, a null path that forms verifies the relays again when the DUT draws the
  2 mA its check needs, and a reboot on the rig's own relays does;
* each relay moved to a free GPIO (Relay 1 to GPIO16, Relay 2 to GPIO17, no
  wire on either) and back: its pads read right, the DUT's current is still
  read through the shunt with the pin it left parked, before and after a
  reboot, and a null path on the moved relay is refused with the check its
  missing contact fails (``104`` for the lead, ``103`` for the bypass) when the
  DUT draws the 2 mA a check needs; under that floor the instrument does not
  judge the steps, so the moved lead's null path forms unverified and the
  moved bypass's is not asked for at all (it would open the lead with nothing
  closed beside it); moved back, a null path forms again;
* an address and a clock stored survive a reboot, and ``SYSTem:PINS:DEFault``
  brings the default map back from the console alone;
* ``DIAGnostic:HEAP?`` reads no allocation and no free after every step.

Whatever fails, the last thing each case does is ``SYSTem:PINS:DEFault``.

    pytest tests/bench/test_pins_bench.py --run-bench \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from bench_support import BenchLine, since_main_problems
from kleidos_meter.client import Meter

#: How long a boot is let settle after a reset, as the heap sweep does.
SETTLE_S = 1.5

#: The longest an apply takes: a whole scan on a silent bus and its revert.
APPLY_TIMEOUT_S = 20.0

#: How long each ``STATE`` capture of the DUT's current runs.
CAPTURE_S = 5.0

#: The default map, the rig as wired (rev D).
DEFAULT_MAP = {
    "sda": "4",
    "scl": "5",
    "alert": "6",
    "addr": "auto",
    "scl_hz": "400000",
    "pull_sda": "up",
    "pull_scl": "up",
    "pull_alert": "up",
    "relay1": "7",
    "relay1_active": "low",
    "relay2": "15",
    "relay2_active": "high",
}

#: The free GPIO each relay is moved to: no wire on either (rev D).
FREE_PIN = {"relay1": "16", "relay2": "17"}

#: The relay's short form in ``SYSTem:PINS:STAGe``.
STAGE_ROLE = {"relay1": "REL1", "relay2": "REL2"}

#: The error a null path on each relay moved off its wire queues when the DUT
#: draws enough to judge it: the lead that never opens leaves the shunt its
#: share (``104``), the bypass that never closes takes none of it (``103``).
MOVED_CHECK = {"relay1": "104", "relay2": "103"}

#: The least a DUT must draw for a null path's steps to be judged, in
#: nanoamps: ``relay::kCheckFloorPv``, 200 uV through the 0.100 Ohm shunt.
CHECK_FLOOR_NA = 2_000_000.0

#: How far the mean of one capture may sit from another's with the same DUT at
#: rest: this many nanoamps, or this share of the first, whichever is more.
#: With the lead open the shunt carries nothing, so a sleeping T-Deck's 41 uA
#: would fall to the offset's few.
SAME_CURRENT_NA = 10_000.0
SAME_CURRENT_SHARE = 0.2


def heap_is_clean(meter: Meter, when: str) -> None:
    """``DIAGnostic:HEAP?`` counts nothing since ``app_main()``."""
    assert since_main_problems(when, meter.report("DIAG:HEAP?")) == []


def live_map(meter: Meter) -> dict[str, str]:
    """``SYSTem:PINS?``'s twelve keys of the map, without where it came from."""
    report = meter.report("SYST:PINS?")
    return {key: report[key] for key in DEFAULT_MAP}


def apply(meter: Meter, *units: str, order: str = "SYST:PINS:APPL") -> list[str]:
    """Stages *units*, sends *order* and returns the errors it queued, as text."""
    meter.exchange(["*CLS", "SYST:PINS:CLE"])
    if units:
        meter.exchange(list(units))
    started = time.monotonic()
    _, errors, _ = meter.exchange([order], raise_errors=False, timeout=APPLY_TIMEOUT_S)
    print(f"{order} {units}: {time.monotonic() - started:.2f} s, {errors}")
    assert meter.query("SYST:ERR:COUN?") == "0"
    return [str(error) for error in errors]


def mean_na(meter: Meter) -> float:
    """The mean of a ``STATE`` statistics capture of :data:`CAPTURE_S`, in nanoamps."""
    meter.exchange(["SENS:PROF STAT", "SENS:CAPT STAT", "INIT"], timeout=20.0)
    time.sleep(CAPTURE_S)
    meter.exchange(["ABOR"], timeout=20.0)
    stats = meter.report("FETC:STAT?")
    meter.exchange(["SENS:CAPT DATA"])
    assert stats["read_err"] == "0" and stats["dropped"] == "0", stats
    return float(stats["mean_na"])


def reboot(meter: Meter, line: BenchLine) -> None:
    """Resets the board through the auto-reset circuit and waits for it."""
    line.reset()
    assert meter.wait_ready(timeout=20.0), "no power-on bit: the reset did not reboot the meter"
    time.sleep(SETTLE_S)


@pytest.fixture
def rig(meter: Meter) -> Iterator[Meter]:
    """The instrument at rest on the default map, and back there afterwards."""
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*RST", "*CLS", "FORM TEXT"], timeout=20.0)
    assert live_map(meter) == DEFAULT_MAP
    try:
        yield meter
    finally:
        meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
        meter.exchange(["*RST"], raise_errors=False, timeout=20.0)
        if live_map(meter) != DEFAULT_MAP or meter.report("SYST:PINS?")["src"] != "default":
            meter.exchange(["SYST:PINS:DEF"], raise_errors=False, timeout=APPLY_TIMEOUT_S)
        assert live_map(meter) == DEFAULT_MAP
        meter.exchange(["SYST:PINS:CLE", "*CLS"])


@pytest.mark.bench
@pytest.mark.parametrize(
    ("units", "reason", "found"),
    [
        (["SYST:PINS:STAG SDA,18"], "no_chip", "did not answer on sda=18 scl=5 addr=auto"),
        (["SYST:PINS:STAG SCL,18"], "no_chip", "did not answer on sda=4 scl=18 addr=auto"),
        (["SYST:PINS:ADDR #H41"], "no_chip", "did not answer on sda=4 scl=5 addr=0x41"),
        (
            ["SYST:PINS:STAG SDA,5", "SYST:PINS:STAG SCL,4"],
            "no_chip",
            "did not answer on sda=5 scl=4 addr=auto",
        ),
        (["SYST:PINS:STAG ALER,18"], "alert", "no ALERT edge on GPIO18"),
    ],
    ids=["sda", "scl", "address", "swapped", "alert"],
)
def test_a_wrong_map_is_reverted_with_one_error(
    rig: Meter, units: list[str], reason: str, found: str
) -> None:
    quality = rig.report("SYST:INF?")["q"]
    before = rig.report("SYST:PINS?")
    errors = apply(rig, *units)
    assert len(errors) == 1, errors
    assert "117" in errors[0] and found in errors[0], errors
    assert errors[0].count("the last good map is back") == 1, errors
    assert "it fails too" not in errors[0], errors
    result = rig.report("SYST:PINS:RES?")
    assert result["result"] == "reverted" and result["reason"] == reason, result
    assert result["saved"] == "0", result
    assert rig.report("SYST:PINS?") == before
    assert rig.query("*TST?", timeout=10.0) == "0"
    info = rig.report("SYST:INF?")
    assert info["q"] == quality == "cal", info
    assert info["chip"] == "ina228", info
    assert rig.query("DIAG:I2C:SCAN?") == "expected=0x40 found=0x40 n=1"
    assert rig.report("ROUT:STAT?")["pads"] == "ok"
    heap_is_clean(rig, f"the revert of {units}")


@pytest.mark.bench
def test_a_relay_moves_and_the_one_it_left_stays_released(
    rig: Meter, meter_line: BenchLine
) -> None:
    driven = mean_na(rig)
    assert apply(rig, "SYST:PINS:STAG REL1,16") == []
    result = rig.report("SYST:PINS:RES?")
    assert (result["result"], result["tst"], result["saved"]) == ("ok", "0", "1"), result
    pins = rig.report("SYST:PINS?")
    assert (pins["relay1"], pins["src"], pins["verified"]) == ("16", "stored", "0"), pins
    route = rig.report("ROUT:STAT?")
    assert (route["pads"], route["check"], route["path"]) == ("ok", "-", "shunt"), route
    heap_is_clean(rig, "Relay 1 moved to GPIO16")
    parked_high = mean_na(rig)

    # A reboot parks GPIO7 low, like any pin no role holds.
    reboot(rig, meter_line)
    rig.exchange(["*RST", "*CLS", "FORM TEXT"], timeout=20.0)
    assert rig.report("SYST:PINS?")["relay1"] == "16"
    parked_low = mean_na(rig)
    heap_is_clean(rig, "the boot on GPIO16")
    print(
        f"the DUT's current with GPIO7 driven high {driven / 1000:.2f} uA, parked high"
        f" {parked_high / 1000:.2f} uA, parked low after a reboot {parked_low / 1000:.2f} uA"
    )

    assert apply(rig, "SYST:PINS:STAG REL1,7") == []
    pins = rig.report("SYST:PINS?")
    assert (pins["relay1"], pins["src"], pins["verified"]) == ("7", "default", "0"), pins
    heap_is_clean(rig, "Relay 1 moved back")
    back = mean_na(rig)
    print(f"and with GPIO7 driven high again {back / 1000:.2f} uA")
    rig.exchange(["*CLS", "ROUT:NULL ON"], timeout=20.0)
    check = rig.report("ROUT:STAT?")["check"]
    verified = rig.report("SYST:PINS?")["verified"]
    rig.exchange(["ROUT:NULL OFF"], timeout=20.0)
    print(f"the null path: check={check} verified={verified}")
    # A DUT that draws under 2 mA gives the check nothing to judge by.
    assert (check, verified) in {("ok", "1"), ("unverified", "0")}
    allowed = max(SAME_CURRENT_NA, SAME_CURRENT_SHARE * abs(driven))
    assert abs(parked_high - driven) <= allowed
    assert abs(parked_low - driven) <= allowed
    assert abs(back - driven) <= allowed
    heap_is_clean(rig, "the null path")
    reboot(rig, meter_line)
    rig.exchange(["*CLS"])
    assert rig.report("SYST:PINS?")["verified"] == "1"


@pytest.mark.bench
def test_a_stored_map_survives_a_reboot_and_default_brings_the_rig_back(
    rig: Meter, meter_line: BenchLine
) -> None:
    assert apply(rig, "SYST:PINS:ADDR #H40", "SYST:PINS:CLOC 100000") == []
    reboot(rig, meter_line)
    rig.exchange(["*CLS"])
    pins = rig.report("SYST:PINS?")
    assert (pins["addr"], pins["scl_hz"], pins["src"]) == ("0x40", "100000", "stored"), pins
    assert pins["verified"] == "1", pins
    assert rig.query("SYST:COMM:I2C:FREQ?") == "100000"
    assert rig.query("*TST?", timeout=10.0) == "0"
    assert rig.report("SYST:INF?")["q"] == "cal"
    heap_is_clean(rig, "the boot from the stored map")
    assert apply(rig, order="SYST:PINS:DEF") == []
    result = rig.report("SYST:PINS:RES?")
    assert (result["result"], result["reason"], result["saved"]) == ("ok", "-", "1"), result
    pins = rig.report("SYST:PINS?")
    assert live_map(rig) == DEFAULT_MAP and pins["src"] == "default", pins
    assert rig.query("SYST:COMM:I2C:FREQ?") == "400000"
    heap_is_clean(rig, "DEFault")
    reboot(rig, meter_line)
    rig.exchange(["*CLS"])
    assert rig.report("SYST:PINS?")["src"] == "default"
    heap_is_clean(rig, "the boot from the defaults")


def null_path(meter: Meter) -> tuple[list[str], dict[str, str], str]:
    """``ROUTe:NULLpath ON``: its errors, then ``ROUTe:STATe?`` and ``verified``."""
    _, errors, _ = meter.exchange(["*CLS", "ROUT:NULL ON"], raise_errors=False, timeout=20.0)
    route = meter.report("ROUT:STAT?")
    verified = meter.report("SYST:PINS?")["verified"]
    return [str(error) for error in errors], route, verified


@pytest.mark.bench
@pytest.mark.parametrize("relay", ["relay1", "relay2"])
def test_a_relay_moved_off_its_wire_fails_its_null_path_check_and_comes_back(
    rig: Meter, meter_line: BenchLine, relay: str
) -> None:
    driven = mean_na(rig)
    judged = abs(driven) >= CHECK_FLOOR_NA
    role = STAGE_ROLE[relay]
    assert apply(rig, f"SYST:PINS:STAG {role},{FREE_PIN[relay]}") == []
    result = rig.report("SYST:PINS:RES?")
    assert (result["result"], result["tst"], result["saved"]) == ("ok", "0", "1"), result
    pins = rig.report("SYST:PINS?")
    assert (pins[relay], pins["src"], pins["verified"]) == (FREE_PIN[relay], "stored", "0"), pins
    route = rig.report("ROUT:STAT?")
    assert (route["pads"], route["check"], route["path"]) == ("ok", "-", "shunt"), route
    heap_is_clean(rig, f"{relay} moved to GPIO{FREE_PIN[relay]}")
    moved = mean_na(rig)

    if judged:
        errors, route, verified = null_path(rig)
        print(f"{relay} on GPIO{FREE_PIN[relay]}: the null path {errors}")
        assert len(errors) == 1 and MOVED_CHECK[relay] in errors[0], errors
        assert "released to path=shunt" in errors[0], errors
        assert (route["path"], verified) == ("shunt", "0"), route
    elif relay == "relay1":
        # The bypass is the rig's own and closes; the lead on GPIO16 never
        # opens, so the DUT keeps both feeds and the residual limit holds.
        errors, route, verified = null_path(rig)
        rig.exchange(["ROUT:NULL OFF"], timeout=20.0)
        print(f"{relay} on GPIO16 under the floor: check={route['check']} {errors}")
        assert errors == [] and (route["check"], verified) == ("unverified", "0"), route
    else:
        print(
            f"the DUT draws {driven / 1000:.2f} uA, under the check's 2 mA: a null path on"
            " the moved bypass would open the lead with nothing closed beside it, so none runs"
        )
    assert rig.report("ROUT:STAT?")["path"] == "shunt"
    heap_is_clean(rig, f"the null path on GPIO{FREE_PIN[relay]}")

    reboot(rig, meter_line)
    rig.exchange(["*RST", "*CLS", "FORM TEXT"], timeout=20.0)
    pins = rig.report("SYST:PINS?")
    assert (pins[relay], pins["src"], pins["verified"]) == (FREE_PIN[relay], "stored", "0"), pins
    rebooted = mean_na(rig)
    heap_is_clean(rig, f"the boot on GPIO{FREE_PIN[relay]}")

    assert apply(rig, f"SYST:PINS:STAG {role},{DEFAULT_MAP[relay]}") == []
    pins = rig.report("SYST:PINS?")
    assert (pins[relay], pins["src"], pins["verified"]) == (DEFAULT_MAP[relay], "default", "0")
    back = mean_na(rig)
    errors, route, verified = null_path(rig)
    rig.exchange(["ROUT:NULL OFF"], timeout=20.0)
    print(
        f"{relay}: the DUT's current driven {driven / 1000:.2f} uA, moved {moved / 1000:.2f} uA,"
        f" after the reboot {rebooted / 1000:.2f} uA, back {back / 1000:.2f} uA; the null path"
        f" back home: check={route['check']} verified={verified} {errors}"
    )
    assert errors == []
    assert (route["check"], verified) == (("ok", "1") if judged else ("unverified", "0"))
    assert rig.report("ROUT:STAT?")["path"] == "shunt"
    allowed = max(SAME_CURRENT_NA, SAME_CURRENT_SHARE * abs(driven))
    assert abs(moved - driven) <= allowed
    assert abs(rebooted - driven) <= allowed
    assert abs(back - driven) <= allowed
    heap_is_clean(rig, f"{relay} moved back")
    reboot(rig, meter_line)
    rig.exchange(["*CLS"])
    assert rig.report("SYST:PINS?")["verified"] == "1"
