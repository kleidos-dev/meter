#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The radio gate: the linked image holds no radio and no USB-Serial-JTAG.

    python3 scripts/check_radio_absent.py --nm <xtensa-esp32s3-elf-nm> \\
        firmware/.pio/build/meter/firmware.elf

The meter is serial-only by the owner's ruling of 2026-11-21: the NW-00 night
test found a radio on the meter shifts the null whatever its transmit power
(``calibration/records/radio-ab-2026-11-21.md``), and the hardware it does not
use is parked (``docs/architecture.md`` SS 9). Neither Wi-Fi nor
Bluetooth is linked, and from 0.16.6 the USB-Serial-JTAG driver is not either
(``firmware/sdkconfig.defaults``); this holds the linked image to it. It lists
every symbol of ``firmware.elf`` with ``nm`` and fails on a **defined** one,
code or data, whose name belongs to one of the :data:`FAMILIES`: Wi-Fi,
Bluetooth, the PHY, coexistence, the USB-Serial-JTAG. The ROM's own functions
and data are absolute symbols (``A``), aliases the ROM linker scripts give
every image whatever it links, and are not counted: nothing of ours or of
ESP-IDF's put them there. A name the families match that is in the image by
design is in :data:`ALLOWED`, with the reason; an allowed name the image no
longer holds fails too (a stale exception is a false statement about it).

It prints how many defined symbols it read and how many belong to each
family (zero), the counts CI writes to the job summary.

    python3 scripts/check_radio_absent.py --selftest

checks the gate against listings in ``nm``'s shape, with no toolchain.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: What each family's symbols are called: the prefixes and words ESP-IDF 6.0.3's
#: Wi-Fi, Bluetooth, PHY and coexistence libraries and the USB-Serial-JTAG
#: driver name theirs with (read off an image that linked the Wi-Fi station,
#: the NW-00 spike's, and off 0.16.5's, which linked the USB-Serial-JTAG).
FAMILIES: dict[str, re.Pattern[str]] = {
    "wifi": re.compile(
        r"wifi|80211|^_*wpa|^_*wps_|^_*wdev|^_*wDev|^_*pp_|^_*ppTask|^_*hostap|^_*sae_|^_*pmksa"
        r"|^_*cnx_|^_*chm_|^_*esf_|^_*espnow|^_*esp_now|^_*smartconfig|^_*esp_mesh|^_*mesh_"
        r"|^_*esp_supplicant|^_*g_ic$|^_*ic_",
        re.IGNORECASE,
    ),
    "bluetooth": re.compile(
        r"^_*esp_bt_|^_*esp_ble_|^_*btdm|^_*bt_|^_*ble_|^_*nimble|^_*btc_|^_*bta_|^_*btu_"
        r"|^_*bluedroid|^_*esp_vhci|^_*vhci_|^_*esp_gap_|^_*esp_gatt|^_*r_ble|^_*r_bt|^_*r_lld"
        r"|^_*hci_|^_*l2c"
    ),
    "phy": re.compile(r"(^|_)phy(_|$)|^_*g_phy|^_*phy[A-Z]|chipv7_phy|^_*rf_cal|^_*rfcal|_rfpll"),
    "coexistence": re.compile(r"coex", re.IGNORECASE),
    "usb_serial_jtag": re.compile(r"usb_serial_jtag|(^|_)usj(_|$)"),
}

#: Names the families match that the image holds by design, and why: none.
#: Until FW-16 it held ``include_esp_phy_override``, ESP-IDF's empty link
#: anchor, for which esp_phy's CMakeLists.txt passes ``-u`` while
#: CONFIG_ESP_PHY_ENABLED; from FW-16 the build holds only the components the
#: firmware requires (``firmware/CMakeLists.txt``, ``COMPONENTS``), and esp_phy
#: is not one of them.
ALLOWED: dict[str, str] = {}

#: The allowance the self-test holds the gate's handling of one to.
SELFTEST_ALLOWED = {"include_esp_phy_override": "an empty link anchor, for the self-test"}

#: nm's type letters for a symbol the image defines; ``A`` (absolute, the ROM's)
#: and ``U``/``w`` (undefined) are not among them.
DEFINED_TYPES = set("TtDdBbRrWVvCSsGg")


def defined_symbols(listing: str) -> list[str]:
    """The names of the symbols *listing* (``nm`` of the image) says it defines."""
    names = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] in DEFINED_TYPES:
            names.append(fields[2])
    return names


def family_of(name: str) -> str | None:
    """The family *name* belongs to, or None."""
    for family, pattern in FAMILIES.items():
        if pattern.search(name):
            return family
    return None


def verdict(listing: str, allowed: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """The report and the failures for one image's ``nm`` listing."""
    allowed = ALLOWED if allowed is None else allowed
    names = defined_symbols(listing)
    present = set(names)
    counts = dict.fromkeys(FAMILIES, 0)
    failures = []
    for name in sorted(present):
        family = family_of(name)
        if family is None or name in allowed:
            continue
        counts[family] += 1
        failures.append(f"{name} is a {family} symbol the image defines")
    failures += [
        f"allowed {name} is not in the image: the list is stale"
        for name in sorted(allowed)
        if name not in present
    ]
    report = [f"{len(present)} defined symbol(s) read"]
    report += [f"{family}: {count} defined symbol(s)" for family, count in counts.items()]
    report += [f"allowed: {name} ({allowed[name]})" for name in sorted(allowed) if name in present]
    return report, failures


SELFTEST_CLEAN = """\
40000000 A coex_core_request
40002a78 A bt_rf_coex_get_dft_cfg
40005c58 A phy_get_romfuncs
4201d818 T include_esp_phy_override
420020e0 T _ZN7kleidos5meter12Ina228Driver26enableConversionReadyAlertEv
4038187c T esp_flash_get_physical_size
40375748 T esp_intr_enable_source
4037c1b4 t enable_timer_group0_for_calibration
         U esp_wifi_init
"""

SELFTEST_DIRTY = """\
4201d818 T include_esp_phy_override
42050000 T esp_wifi_start
42050100 t ieee80211_output
3fc90000 b g_phyFuns
42050200 T esp_phy_enable
42050300 T coex_init_wrapper
403755c8 t usb_serial_jtag_sof_tick_hook
4200ea94 t __esp_system_init_fn_init_vfs_usj_sec
42050400 T esp_ble_gap_start_advertising
42050500 T nimble_port_init
"""


def selftest() -> int:
    """Hold the gate to listings in ``nm``'s shape; 0 when every check holds."""
    checks = []
    report, failures = verdict(SELFTEST_CLEAN, SELFTEST_ALLOWED)
    checks.append(("a clean image passes", failures == []))
    checks.append(("ROM aliases and undefined names are not counted", report[0].startswith("5 ")))
    checks.append(("the allowed anchor is reported", any("include_esp_phy" in r for r in report)))
    _, failures = verdict(SELFTEST_DIRTY, SELFTEST_ALLOWED)
    named = {line.split()[0] for line in failures}
    checks.append(
        (
            "every family is caught",
            named
            == {
                "esp_wifi_start",
                "ieee80211_output",
                "g_phyFuns",
                "esp_phy_enable",
                "coex_init_wrapper",
                "usb_serial_jtag_sof_tick_hook",
                "__esp_system_init_fn_init_vfs_usj_sec",
                "esp_ble_gap_start_advertising",
                "nimble_port_init",
            },
        )
    )
    _, failures = verdict(
        SELFTEST_CLEAN.replace("include_esp_phy_override", "other"), SELFTEST_ALLOWED
    )
    checks.append(("a stale allowance fails", any("stale" in f for f in failures)))
    _, failures = verdict(SELFTEST_CLEAN)
    checks.append(
        (
            "with no allowance the anchor is a phy symbol",
            failures == ["include_esp_phy_override is a phy symbol the image defines"],
        )
    )
    for name, held in checks:
        print(f"{'ok' if held else 'FAIL'}: {name}")
    return 0 if all(held for _, held in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nm", default="nm", help="the nm of the toolchain that linked the image")
    parser.add_argument("--selftest", action="store_true", help="check the gate, no image needed")
    parser.add_argument("elf", nargs="?", type=Path, help="the linked image (firmware.elf)")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.elf is None:
        parser.error("an image is needed (or --selftest)")
    listing = subprocess.run(
        [args.nm, str(args.elf)], capture_output=True, text=True, check=True
    ).stdout
    report, failures = verdict(listing)
    for line in report:
        print(line)
    for line in failures:
        print(f"FAIL: {line}")
    print(f"{args.elf.name}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
