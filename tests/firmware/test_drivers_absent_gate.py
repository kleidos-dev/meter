# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The retired-driver gate reads a link map right, and nothing asks for a retired driver.

``scripts/check_drivers_absent.py`` reads the image's link map and fails on
any member of an ESP-IDF driver the firmware replaced with its own code on
the low-level layer (FW-10 on: the RMT driver; FW-12: the UART driver; FW-14: the
I2C master; FW-16: NVS, and its key provider). These tests feed it maps in
ld's shape, hold every retired component out of the firmware's component
requirements and its sources' includes (so the image cannot link it by
asking), and check that CI's firmware job runs the gate on the image's map.

Host-only: no toolchain, no image.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_drivers_absent.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
COMPONENT = ROOT / "firmware" / "src" / "CMakeLists.txt"
SOURCES = ROOT / "firmware" / "src"

#: The public headers of each retired component, which no source may include.
HEADERS = {
    "esp_driver_rmt": re.compile(r"#\s*include\s*[<\"]driver/rmt_"),
    "esp_driver_uart": re.compile(r"#\s*include\s*[<\"]driver/uart"),
    "esp_driver_i2c": re.compile(r"#\s*include\s*[<\"]driver/i2c"),
    "nvs_flash": re.compile(r"#\s*include\s*[<\"]nvs"),
    "nvs_sec_provider": re.compile(r"#\s*include\s*[<\"]nvs_sec_provider"),
}


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_drivers_absent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_selftest_holds() -> None:
    assert _module().selftest() == 0


def test_a_linked_member_fails_wherever_the_map_names_it() -> None:
    gate = _module()
    clean = gate.SELFTEST_CLEAN
    assert gate.verdict(clean)[1] == []
    in_memory_map = clean + (
        " .literal       0x40370000        0x8 "
        ".pio/build/meter/esp-idf/esp_driver_rmt/libesp_driver_rmt.a(rmt_encoder.c.o)\n"
    )
    assert gate.verdict(in_memory_map)[1] == [
        "esp_driver_rmt is retired, and the image links its rmt_encoder.c.o"
    ]


def test_every_retired_component_says_what_replaced_it() -> None:
    gate = _module()
    assert gate.RETIRED, "the list is empty: nothing is held"
    for component, why in gate.RETIRED.items():
        assert re.fullmatch(r"(esp_driver_|nvs_)\w+", component), component
        assert re.match(r"FW-\d+: ", why), f"{component}: name the work package that retired it"
        assert component in HEADERS, f"{component}: list its headers in this test"


def test_no_requirement_and_no_source_asks_for_a_retired_driver() -> None:
    gate = _module()
    cmake = re.sub(r"#[^\n]*", "", COMPONENT.read_text("utf-8"))
    for component in gate.RETIRED:
        assert not re.search(rf"\b{component}\b", cmake), f"{COMPONENT.name} requires {component}"
        for source in sorted(SOURCES.glob("*.[ch]*")):
            text = source.read_text("utf-8")
            assert not HEADERS[component].search(text), f"{source.name} includes {component}"


def test_ci_runs_the_gate_on_the_image_s_map() -> None:
    runs = [
        line
        for line in WORKFLOW.read_text("utf-8").splitlines()
        if "python3 scripts/check_drivers_absent.py" in line
    ]
    assert any("--selftest" in line for line in runs), "CI does not run the gate's selftest"
    on_map = [line for line in runs if "--selftest" not in line]
    assert on_map, "CI's firmware job does not run the gate on the image's map"
    assert all("firmware/.pio/build/meter/kleidos-meter.map" in line for line in on_map)
