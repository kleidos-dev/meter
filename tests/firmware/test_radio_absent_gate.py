# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The radio gate reads an image's symbols right, and CI runs it on the image.

``scripts/check_radio_absent.py`` lists the linked ``firmware.elf`` with ``nm``
and fails on a defined symbol of Wi-Fi, Bluetooth, the PHY, coexistence or the
USB-Serial-JTAG driver. These tests feed it listings in the shape ``nm``
prints and hold it to what it must see: the ROM's absolute aliases and the
undefined names are not counted, every family is caught, the one allowed
anchor passes and a stale allowance fails. They also check that the options
that keep the USB-Serial-JTAG out are where the build reads them, and that
CI's firmware job runs the gate.

Host-only: no toolchain, no image.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_radio_absent.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
FIRMWARE = ROOT / "firmware"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_radio_absent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_selftest_holds() -> None:
    assert _module().selftest() == 0


def test_rom_aliases_and_undefined_names_are_not_counted() -> None:
    gate = _module()
    listing = (
        "40005b80 A coex_bt_request\n"
        "40002a90 A btdm_con_maxevtime_cal_impl\n"
        "         U esp_wifi_init\n"
        "4201d818 T app_main\n"
    )
    report, failures = gate.verdict(listing)
    assert failures == []
    assert report[0] == "1 defined symbol(s) read"


def test_each_family_is_caught_in_code_and_in_data() -> None:
    gate = _module()
    cases = {
        "esp_wifi_start": "wifi",
        "ieee80211_output": "wifi",
        "wpa_sm_init": "wifi",
        "esp_ble_gap_start_advertising": "bluetooth",
        "btdm_controller_init": "bluetooth",
        "esp_phy_enable": "phy",
        "g_phyFuns": "phy",
        "coex_init_wrapper": "coexistence",
        "usb_serial_jtag_sof_tick_hook": "usb_serial_jtag",
        "__esp_system_init_fn_init_vfs_usj_sec": "usb_serial_jtag",
    }
    for name, family in cases.items():
        assert gate.family_of(name) == family, name
        for kind in ("T", "t", "D", "b"):
            _, failures = gate.verdict(f"42000000 {kind} {name}\n4201d818 T app_main\n")
            assert failures == [f"{name} is a {family} symbol the image defines"], (name, kind)


def test_the_meter_s_own_names_are_not_radio() -> None:
    gate = _module()
    for name in (
        "esp_flash_get_physical_size",
        "esp_intr_enable_source",
        "enable_timer_group0_for_calibration",
        "_ZN7kleidos5meter7Console10statusByteEv",
        "gpio_hal_intr_enable_on_core",
        "tick_hook",
        "esp_register_freertos_tick_hook_for_cpu",
    ):
        assert gate.family_of(name) is None, name


def test_every_allowance_says_why_and_a_stale_one_fails() -> None:
    gate = _module()
    # FW-16: esp_phy left the build with its link anchor, and the list with it.
    assert gate.ALLOWED == {}
    for name, reason in gate.SELFTEST_ALLOWED.items():
        assert reason.strip(), name
    _, failures = gate.verdict("42000000 T something_else\n", gate.SELFTEST_ALLOWED)
    assert failures == ["allowed include_esp_phy_override is not in the image: the list is stale"]
    _, failures = gate.verdict("4201d818 T include_esp_phy_override\n")
    assert failures == ["include_esp_phy_override is a phy symbol the image defines"]


def test_the_usb_serial_jtag_is_off_where_the_build_reads_it() -> None:
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text("utf-8")
    for option in (
        "CONFIG_ESP_CONSOLE_NONE=y",
        "CONFIG_ESP_CONSOLE_SECONDARY_NONE=y",
        "CONFIG_USJ_ENABLE_USB_SERIAL_JTAG=n",
        "CONFIG_BT_ENABLED=n",
    ):
        assert re.search(rf"^{re.escape(option)}$", defaults, re.M), option
    cmake = (FIRMWARE / "src" / "CMakeLists.txt").read_text("utf-8")
    for component in ("esp_wifi", "bt", "esp_phy", "esp_coex", "esp_driver_usb_serial_jtag"):
        assert not re.search(rf"^\s+{component}\s*$", cmake, re.M), component


def test_ci_runs_the_gate_on_the_image() -> None:
    workflow = WORKFLOW.read_text("utf-8")
    runs = [
        line for line in workflow.splitlines() if "python3 scripts/check_radio_absent.py" in line
    ]
    assert any("--selftest" in line for line in runs), "CI does not run the gate's selftest"
    on_image = [line for line in runs if "--selftest" not in line]
    assert on_image, "CI's firmware job does not run the radio gate on the image"
    assert all(
        "firmware/.pio/build/meter/firmware.elf" in line and "--nm" in line for line in on_image
    )
