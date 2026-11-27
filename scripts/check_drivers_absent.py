#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The retired-driver gate: the image links nothing of the ESP-IDF drivers it replaced.

    python3 scripts/check_drivers_absent.py firmware/.pio/build/meter/kleidos-meter.map

The zero-allocation work (``docs/misra.md`` D1) replaces ESP-IDF's allocating
drivers with the firmware's own code on ESP-IDF's low-level layer, one driver
at a time. What the gate holds is that the linker took **no member** of a
retired component's archive: no ``lib<component>.a(<object>)`` reference
anywhere in the map, in the "Archive member included" section or in the
memory map below it. The linker-fragment rules ESP-IDF writes for every
archive (`` *lib<component>.a:<object>.*(...)``) name no member and are not
counted.

Until FW-16 ESP-IDF built every component of its tree, so a retired driver's
archive was still built and given to the linker (a ``LOAD`` line of the map).
From FW-16 the build holds only the firmware's component and what it
requires (``firmware/CMakeLists.txt``, ``COMPONENTS``), and a retired
component is not in it at all: no ``LOAD`` line, which the gate reports as
"not in the build" and accepts, the stronger of the two.

:data:`RETIRED` lists each retired component with the work package that
retired it. A map that is not a GNU ld map with its "Archive member included"
section fails, so a wrong file cannot pass.

    python3 scripts/check_drivers_absent.py --selftest

checks the gate against maps in ld's shape, with no toolchain.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Each ESP-IDF component the firmware no longer links, and what replaced it.
RETIRED: dict[str, str] = {
    "esp_driver_rmt": (
        "FW-10: the LED's off frame is written on the RMT's registers "
        "(firmware/src/status_led.cpp, hal/rmt_ll.h), with no driver object"
    ),
    "esp_driver_uart": (
        "FW-12: the console's UART0 is set and served on its registers "
        "(firmware/src/uart0.cpp, hal/uart_ll.h), its interrupt routed with nothing allocated"
    ),
    "esp_driver_i2c": (
        "FW-14: the INA228's bus is the own I2C master on I2C0's registers "
        "(firmware/src/i2c_bus.cpp, hal/i2c_ll.h), polled, its clock changed in place"
    ),
    "nvs_flash": (
        "FW-16: the calibration is a record of the firmware's own log over two sectors "
        "of raw flash (firmware/src/flash_log.cpp, flash_port.cpp), with nothing allocated"
    ),
    "nvs_sec_provider": (
        "FW-16: NVS's encryption-key provider, which went with NVS (its -u anchor kept "
        "an object of it in every image while the component was built)"
    ),
}

#: The heading GNU ld writes before the archive members it pulled in.
MEMBERS_HEADING = "Archive member included to satisfy reference by file (symbol)"


def members(text: str, component: str) -> list[str]:
    """The distinct ``lib<component>.a(<object>)`` references of a map, sorted."""
    pattern = re.compile(rf"lib{re.escape(component)}\.a\(([^)]+)\)")
    return sorted({match.group(1) for match in pattern.finditer(text)})


def loaded(text: str, component: str) -> bool:
    """Whether the map gives the component's archive to the linker at all."""
    return re.search(rf"^LOAD \S*lib{re.escape(component)}\.a$", text, re.M) is not None


def verdict(text: str, retired: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """The report and the failures for one image's link map."""
    retired = RETIRED if retired is None else retired
    if MEMBERS_HEADING not in text:
        return [], ["not a GNU ld map: no 'Archive member included' section"]
    report, failures = [], []
    for component, why in retired.items():
        linked = members(text, component)
        where = "" if loaded(text, component) else ", not in the build"
        report.append(f"{component}: {len(linked)} member(s) linked{where} ({why})")
        failures += [f"{component} is retired, and the image links its {name}" for name in linked]
    return report, failures


SELFTEST_CLEAN = f"""\
{MEMBERS_HEADING}

.pio/build/meter/esp-idf/esp_driver_gpio/libesp_driver_gpio.a(gpio.c.o)
                              .pio/build/meter/src/libsrc.a(relay.cpp.o) (gpio_config)

Discarded input sections

LOAD .pio/build/meter/esp-idf/esp_hal_rmt/libesp_hal_rmt.a
LOAD .pio/build/meter/esp-idf/esp_driver_rmt/libesp_driver_rmt.a
LOAD .pio/build/meter/esp-idf/esp_hal_uart/libesp_hal_uart.a
LOAD .pio/build/meter/esp-idf/esp_driver_uart/libesp_driver_uart.a
LOAD .pio/build/meter/esp-idf/esp_hal_i2c/libesp_hal_i2c.a
LOAD .pio/build/meter/esp-idf/esp_driver_i2c/libesp_driver_i2c.a

Linker script and memory map

 *libesp_driver_rmt.a:rmt_tx.*(.literal.rmt_isr_handle_tx_done .text.rmt_isr_handle_tx_done)
 .text          0x42000000       0x10 .pio/build/meter/src/libsrc.a(status_led.cpp.o)
"""

SELFTEST_DIRTY = SELFTEST_CLEAN.replace(
    "Discarded input sections",
    ".pio/build/meter/esp-idf/esp_driver_rmt/libesp_driver_rmt.a(rmt_tx.c.o)\n"
    "                              .pio/build/meter/src/libsrc.a(status_led.cpp.o) "
    "(rmt_new_tx_channel)\n\nDiscarded input sections",
).replace(
    " .text          0x42000000",
    " .text          0x42000100       0x40 "
    ".pio/build/meter/esp-idf/esp_driver_rmt/libesp_driver_rmt.a(rmt_common.c.o)\n"
    " .text          0x42000000",
)


def selftest() -> int:
    """Hold the gate to maps in ld's shape; 0 when every check holds."""
    checks = []
    report, failures = verdict(SELFTEST_CLEAN)
    checks.append(("a map with the archive loaded and no member linked passes", failures == []))
    checks.append(("the count is reported", report[0].startswith("esp_driver_rmt: 0 member")))
    _, failures = verdict(SELFTEST_DIRTY)
    checks.append(
        (
            "every linked member is named once",
            failures
            == [
                "esp_driver_rmt is retired, and the image links its rmt_common.c.o",
                "esp_driver_rmt is retired, and the image links its rmt_tx.c.o",
            ],
        )
    )
    _, failures = verdict(SELFTEST_CLEAN.replace(MEMBERS_HEADING, "Memory map"))
    checks.append(("a file that is not a map fails", len(failures) == 1))
    report, failures = verdict(
        SELFTEST_CLEAN.replace("LOAD .pio/build/meter/esp-idf/esp_driver_rmt", "X")
    )
    checks.append(
        (
            "a retired component the build does not hold passes, and is said to",
            failures == [] and "not in the build" in report[0],
        )
    )
    for name, held in checks:
        print(f"{'ok' if held else 'FAIL'}: {name}")
    return 0 if all(held for _, held in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true", help="check the gate, no map needed")
    parser.add_argument(
        "map", nargs="?", type=Path, help="the image's link map (kleidos-meter.map)"
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.map is None:
        parser.error("a link map is needed (or --selftest)")
    report, failures = verdict(args.map.read_text(encoding="utf-8", errors="replace"))
    for line in report:
        print(line)
    for line in failures:
        print(f"FAIL: {line}")
    print(f"{args.map.name}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
