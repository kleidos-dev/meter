# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The IRAM gate reads the call graph right, and CI runs it on the image.

``scripts/check_iram_hot_path.py`` walks the measurement's hot path through
the relocations of the objects the image was linked from and fails on a
function or a named constant it reaches in flash. These tests feed it
listings in the shape ``objdump -t``, ``objdump -r``, ``objdump -h`` and
``nm`` print, and hold it to what it must see: a call through a literal pool,
a direct call to a static function named by its section, a constant loaded
from ``.rodata``, a failure exit it must not follow, a cold exit it must stop
at, a stale exception and a missing root. They also check that the options
and the linker fragment the hot path rests on are where the build reads
them, and that CI's firmware job runs the gate.

Host-only: no toolchain, no image.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_iram_hot_path.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
FIRMWARE = ROOT / "firmware"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_iram_hot_path", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # the gate's dataclasses look their module up
    spec.loader.exec_module(module)
    return module


SYMTAB = """\
libdemo.a(loop.c.obj):     file format elf32-xtensa-le

SYMBOL TABLE:
00000000 l     F .text.helper	00000020 helper
00000000 g     F .iram1.3	00000040 loop
00000000 g     F .text.cold_path	00000010 cold_path
"""

RELOCATIONS = """\
libdemo.a(loop.c.obj):     file format elf32-xtensa-le

RELOCATION RECORDS FOR [.iram1.3.literal]:
OFFSET   TYPE              VALUE
00000000 R_XTENSA_32       xQueueSemaphoreTake
00000004 R_XTENSA_32       .rodata.kTable
00000008 R_XTENSA_32       .rodata.loop.str1.4
0000000c R_XTENSA_32       cold_path
00000010 R_XTENSA_32       abort


RELOCATION RECORDS FOR [.iram1.3]:
OFFSET   TYPE              VALUE
00000006 R_XTENSA_SLOT0_OP  .iram1.3.literal
00000010 R_XTENSA_SLOT0_OP  .text.helper
00000018 R_XTENSA_SLOT0_OP  .iram1.3+0x00000022


RELOCATION RECORDS FOR [.literal.cold_path]:
OFFSET   TYPE              VALUE
00000000 R_XTENSA_32       vTaskDelete
"""

HEADERS = """\
Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  6 .iram0.text   00010000  40374400  40374400  0002a400  2**2
  8 .dram0.data   00010000  3fc93300  3fc93300  0001a300  2**4
 11 .flash.text   00020000  42000020  42000020  0003a020  2**2
 14 .flash.rodata 00010000  3c030120  3c030120  00001120  2**4
"""


def _symbols(**placed: int) -> str:
    return "".join(f"{addr:08x} T {name}\n" for name, addr in placed.items())


IRAM, FLASH, DRAM, ROM = 0x40375000, 0x42001000, 0x3FC94000, 0x3C031000


def _graph(gate: ModuleType) -> Any:
    graph = gate.Graph()
    gate.add_relocations(graph, RELOCATIONS, gate.function_sections(SYMTAB))
    return graph


def test_the_relocations_give_every_edge() -> None:
    gate = _module()
    graph = _graph(gate)
    # Through the literal pool, to a static function by its section, never to itself.
    assert graph.calls["loop"] == {"xQueueSemaphoreTake", "helper", "cold_path", "abort"}
    assert graph.data["loop"] == {".rodata.kTable", ".rodata.loop.str1.4"}
    assert graph.calls["cold_path"] == {"vTaskDelete"}


def _verdict(
    gate: ModuleType,
    *,
    table: int = DRAM,
    helper: int = IRAM,
    take: int = IRAM,
    cold: dict[str, str] | None = None,
) -> Any:
    symbols = _symbols(loop=IRAM, helper=helper, xQueueSemaphoreTake=take, cold_path=FLASH)
    symbols += f"{table:08x} d kTable\n{FLASH:08x} T abort\n{FLASH:08x} T vTaskDelete\n"
    image = gate.parse_image(HEADERS, symbols)
    return gate.verdict(
        _graph(gate),
        image,
        roots=("loop",),
        cold=cold if cold is not None else {"cold_path": "once"},
    )


def test_a_hot_path_in_iram_passes() -> None:
    gate = _module()
    report, failures = _verdict(gate)
    assert failures == []
    # loop, helper and the semaphore take; abort and the cold exit are not counted.
    assert report[0] == "hot path: 3 function(s) in .iram0.text"
    assert "cold, left in .flash.text: cold_path (once)" in report


def test_a_hot_function_in_flash_fails() -> None:
    gate = _module()
    _, failures = _verdict(gate, take=FLASH)
    assert failures == ["xQueueSemaphoreTake is in .flash.text, reached from loop"]
    _, failures = _verdict(gate, helper=FLASH)
    assert failures == ["helper is in .flash.text, reached from loop"]


def test_a_constant_in_flash_fails_and_a_string_does_not() -> None:
    gate = _module()
    _, failures = _verdict(gate, table=ROM)
    assert failures == ["loop loads kTable, which is in .flash.rodata"]


def test_a_stale_exception_and_a_missing_root_fail() -> None:
    gate = _module()
    _, failures = _verdict(gate, cold={"cold_path": "once", "gone": "never"})
    assert failures == ["cold exit gone is never reached: the list is stale"]
    image = gate.parse_image(HEADERS, _symbols(helper=IRAM))
    _, failures = gate.verdict(_graph(gate), image, roots=("loop",), cold={})
    assert "root loop is not in the image" in failures


def test_every_cold_exit_says_why() -> None:
    gate = _module()
    assert gate.COLD
    for name, reason in gate.COLD.items():
        assert reason.strip(), name


def test_the_option_reaches_the_build_and_the_retired_driver_s_do_not() -> None:
    # FreeRTOS in IRAM is the one option the hot path asks of ESP-IDF. ESP-IDF's
    # I2C master asked for CONFIG_I2C_ISR_IRAM_SAFE and a linker fragment for
    # its synchronous path until FW-14 moved the INA228 to the own master,
    # whose transactions are IRAM_ATTR: neither may come back without the
    # driver (scripts/check_drivers_absent.py holds the driver out).
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text("utf-8")
    assert re.search(r"^CONFIG_FREERTOS_IN_IRAM=y$", defaults, re.M)
    assert not re.search(r"^CONFIG_I2C_ISR_IRAM_SAFE=", defaults, re.M)
    cmake = re.sub(r"#[^\n]*", "", (FIRMWARE / "src" / "CMakeLists.txt").read_text("utf-8"))
    assert "LDFRAGMENTS" not in cmake
    assert not (FIRMWARE / "src" / "linker.lf").exists()
    assert all("i2c_master" not in root for root in _module().ROOTS)


def test_ci_runs_the_gate_on_the_image() -> None:
    workflow = WORKFLOW.read_text("utf-8")
    runs = [
        line for line in workflow.splitlines() if "python3 scripts/check_iram_hot_path.py" in line
    ]
    assert runs, "CI's firmware job does not run the IRAM gate"
    assert all("firmware/.pio/build/meter" in line and "--objdump" in line for line in runs)
