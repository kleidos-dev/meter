# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The CPU's interrupt lines are claimed in one place, and the GPIO's in one.

From FW-09 the ALERT pin's interrupt is routed by the firmware's own module,
``firmware/src/interrupt.cpp`` (``interrupt::routeHere()``), on the CPU's own
tables and with nothing allocated, in place of ESP-IDF's GPIO ISR service and
its interrupt allocator (``docs/architecture.md`` §5.1, ``docs/misra.md``
D1). Two owners keep it sound, and this guard holds the sources to them:

* **the CPU's lines**: only ``interrupt.cpp`` writes a handler, a matrix
  route or a line's enable (``esp_cpu_intr_set_handler()``,
  ``esp_rom_route_intr_matrix()``, ``esp_cpu_intr_enable()`` and the calls
  beneath them), and no source calls the interrupt allocator
  (``esp_intr_alloc*()``, ``esp_intr_free()``), which the allocation deny
  list also refuses;
* **the GPIO's interrupt**: only ``sampler.cpp`` touches a pin's interrupt
  type, enable or status (``gpio_ll_set_intr_type()``,
  ``gpio_ll_intr_enable_on_core()``, ``gpio_ll_intr_disable()``,
  ``gpio_ll_get_intr_status()``, ``gpio_ll_clear_intr_status()``), and no
  source uses ESP-IDF's GPIO interrupt driver (the ISR service, its
  handlers, ``gpio_intr_enable()``, ``gpio_set_intr_type()``): its state (the
  core it took at its first enable, a table of handlers) would sit beside the
  sampler's and a second route of the GPIO's source would take it from the
  ALERT pin's line.

A planted call of each kind proves the guard still sees it.

Host-only: the sources read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "firmware" / "src"

#: Calls that claim or change a CPU line, by the one source allowed them.
CPU_LINES = re.compile(
    r"\b(esp_cpu_intr_set_handler|esp_cpu_intr_enable|esp_cpu_intr_disable"
    r"|esp_rom_route_intr_matrix|intr_matrix_set|xt_set_interrupt_handler|xt_ints_on"
    r"|xt_ints_off|esp_cpu_intr_set_type|esp_cpu_intr_set_priority)\s*\("
)

#: The interrupt allocator: no source calls it.
ALLOCATOR = re.compile(r"\b(esp_intr_\w+)\s*\(")

#: A GPIO's interrupt at the register level, by the one source allowed it.
GPIO_LL = re.compile(
    r"\b(gpio_ll_set_intr_type|gpio_ll_intr_enable_on_core|gpio_ll_intr_disable"
    r"|gpio_ll_get_intr_status\w*|gpio_ll_clear_intr_status\w*)\s*\("
)

#: ESP-IDF's GPIO interrupt driver: no source calls it.
GPIO_DRIVER = re.compile(
    r"\b(gpio_install_isr_service|gpio_uninstall_isr_service|gpio_isr_register"
    r"|gpio_isr_handler_add|gpio_isr_handler_remove|gpio_intr_enable|gpio_intr_disable"
    r"|gpio_set_intr_type|gpio_intr_enable_on_core)\s*\("
)

#: The one source each owned kind of call may stand in.
OWNERS = {CPU_LINES: "interrupt.cpp", GPIO_LL: "sampler.cpp"}


def _code(text: str) -> str:
    """The text with comments, string and character literals blanked."""
    out = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = re.sub(r"//[^\n]*", "", out)
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', out)


def _findings(name: str, text: str) -> list[str]:
    code = _code(text)
    found = []

    def where(match: re.Match[str]) -> str:
        return f"{name}:{code.count(chr(10), 0, match.start()) + 1}: {match.group(1)}()"

    for pattern, owner in OWNERS.items():
        if name != owner:
            found += [f"{where(m)} outside {owner}" for m in pattern.finditer(code)]
    found += [f"{where(m)}: the interrupt allocator" for m in ALLOCATOR.finditer(code)]
    found += [f"{where(m)}: ESP-IDF's GPIO interrupt driver" for m in GPIO_DRIVER.finditer(code)]
    return found


def _sources() -> list[Path]:
    return sorted(p for p in SRC.iterdir() if p.suffix in (".cpp", ".h"))


def test_the_cpu_lines_and_the_gpio_interrupt_each_have_one_owner() -> None:
    found = [f for path in _sources() for f in _findings(path.name, path.read_text("utf-8"))]
    assert not found, "\n".join(found)


def test_the_owners_still_make_the_calls_they_own() -> None:
    # A guard over calls nobody makes proves nothing: the owners make them.
    router = _code((SRC / "interrupt.cpp").read_text("utf-8"))
    for call in ("esp_cpu_intr_set_handler", "esp_rom_route_intr_matrix", "esp_cpu_intr_enable"):
        assert re.search(rf"\b{call}\s*\(", router), call
    sampler = _code((SRC / "sampler.cpp").read_text("utf-8"))
    for call in ("gpio_ll_set_intr_type", "gpio_ll_get_intr_status", "gpio_ll_clear_intr_status"):
        assert re.search(rf"\b{call}\s*\(", sampler), call
    assert re.search(r"\binterrupt::routeHere\s*\(", sampler)


def test_a_planted_call_of_each_kind_fails_the_guard() -> None:
    planted = (
        "void f() {\n"
        "    esp_rom_route_intr_matrix( 1, 16, 5 );\n"
        "    (void)esp_intr_alloc( 0, 0, nullptr, nullptr, nullptr );\n"
        "    gpio_ll_intr_disable( &GPIO, 6 );\n"
        "    (void)gpio_intr_enable( GPIO_NUM_6 );\n"
        '    // esp_cpu_intr_enable( 1 ); "gpio_isr_handler_add("\n'
        "}\n"
    )
    assert _findings("console.cpp", planted) == [
        "console.cpp:2: esp_rom_route_intr_matrix() outside interrupt.cpp",
        "console.cpp:4: gpio_ll_intr_disable() outside sampler.cpp",
        "console.cpp:3: esp_intr_alloc(): the interrupt allocator",
        "console.cpp:5: gpio_intr_enable(): ESP-IDF's GPIO interrupt driver",
    ]
    assert _findings("interrupt.cpp", "void g() { esp_cpu_intr_enable( 1U ); }\n") == []
    assert _findings("sampler.cpp", "void h() { gpio_ll_intr_disable( &GPIO, 6 ); }\n") == []
