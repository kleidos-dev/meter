# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The serial, derived as the firmware derives it, and the simulator that reports it."""

from __future__ import annotations

import pytest
from kleidos_meter import Meter, SimulatedMeter
from kleidos_meter.identity import parse_idn, serial_from_mac, version_tuple


def test_the_serial_is_the_firmware_s_digest_of_the_mac() -> None:
    # firmware/test/native/test_identity.cpp checks the same three.
    assert serial_from_mac(bytes([0x02, 0x00, 0x5E, 0x10, 0x20, 0x30])) == "KM-EE529C1B03"
    assert serial_from_mac(bytes(6)) == "KM-2E53FD936E"
    assert serial_from_mac(bytes([0xFF] * 6)) == "KM-72B3D45A8D"
    with pytest.raises(ValueError):
        serial_from_mac(b"\x00")


def test_idn_and_versions() -> None:
    assert parse_idn("KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.4.0")["serial"] == "KM-EE529C1B03"
    assert parse_idn("KLEIDOS")["firmware"] == ""
    assert version_tuple("0.4.0") == (0, 4, 0)
    assert version_tuple("-") == ()


def test_the_simulator_reports_serial_image_and_per_range_shunt() -> None:
    sim = SimulatedMeter()
    meter = Meter(sim)
    info = meter.info()
    assert info["serial"] == "KM-EE529C1B03" and info["image"] == "meter"
    assert info["bypass_part"] == "relay2click"  # one image, the Click (0.8.0)
    sim.firmware = "0.7.0"  # before 0.8.0 the Click's image had its own name
    assert meter.info()["image"] == "meter_relay2"
    sim.bypass_part = "hw316"
    assert meter.info()["image"] == "meter"
    sim.firmware = "0.8.0"
    meter.exchange(
        ["SENS:CURR:SHUN 0.100116", "SENS:CURR:SHUN:RANG1 0.100144", "CAL:DATE 2026,11,20"]
    )
    cal = meter.calibration()
    assert (cal["shunt_uohm_r0"], cal["shunt_uohm_r1"], cal["cal_date"]) == (
        "100116",
        "100144",
        "2026-11-20",
    )
    assert meter.query("SENS:CURR:SHUN:RANG1?") == "0.100144"
    assert meter.query("CAL:DATE?") == "2026,11,20"
    _, errors, _ = meter.exchange(
        [
            "SENS:CURR:SHUN:RANG2 0.1",
            "SENS:CURR:SHUN:RANG0 0.3",
            "CAL:DATE 2026,2,30",
            "CAL:DATE 2025,1,1",
        ],
        raise_errors=False,
    )
    assert [e.code for e in errors] == [-114, -222, -222, -222]
    sim.mac = None
    assert meter.idn() == "KLEIDOS,KLEIDOS-METER,0,0.8.0"
