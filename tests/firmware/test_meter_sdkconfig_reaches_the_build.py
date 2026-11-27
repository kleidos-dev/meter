# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every option ``sdkconfig.defaults`` declares must reach the generated config.

Why this exists: on 2026-10-28 the meter was rebuilt with ``CONFIG_SPIRAM=y``
in ``sdkconfig.defaults`` and the build **succeeded, silently, without PSRAM**.
The cause was mundane -- a ``rm -f sdkconfig.*`` meant to clear the generated
file took the source one with it -- but the failure mode is the point: a
declared option that never arrives produces no warning, no error and a working
binary that is not the one asked for. The capability it was supposed to add (a
TRACE ring in PSRAM, 294 s instead of 9.18 s) would have been reported from the
source file rather than from the instrument.

A build is not evidence. The generated file is.

From FW-16 the build holds only the firmware's component and what it requires
(``firmware/CMakeLists.txt``, ``COMPONENTS``), and Kconfig reads the options of
those components alone: a symbol of a component left out (Bluetooth's
``CONFIG_BT_ENABLED``, the USB-Serial-JTAG driver's
``CONFIG_USJ_ENABLE_USB_SERIAL_JTAG``) no longer appears in the generated file
at all. For an ``=n`` declaration that is the same answer, because an absent
symbol is not set (``#if CONFIG_X`` is false), and it is accepted; any other
value must appear as declared.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

METER = Path(__file__).resolve().parents[2] / "firmware"
DEFAULTS = METER / "sdkconfig.defaults"
GENERATED = METER / "sdkconfig.meter"
DESCRIPTION = METER / ".pio" / "build" / "meter" / "project_description.json"

#: Components the build must not hold (FW-16): NVS and its key provider, and
#: every radio, which the image never linked.
LEFT_OUT = (
    "nvs_flash",
    "nvs_sec_provider",
    "esp_wifi",
    "esp_phy",
    "esp_coex",
    "bt",
    "wpa_supplicant",
    "esp_driver_rmt",
    "esp_driver_uart",
    "esp_driver_i2c",
    "esp_driver_usb_serial_jtag",
    # FW-18: no file system, so nothing registers a VFS driver at startup.
    "vfs",
)

#: ``KEY=VALUE`` in a defaults file, ignoring comments and blank lines.
_DECLARATION = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.+)$")


def _declared() -> dict[str, str]:
    """Return every option the defaults file states, keyed by symbol."""
    out: dict[str, str] = {}
    for line in DEFAULTS.read_text(encoding="utf-8").splitlines():
        match = _DECLARATION.match(line.strip())
        if match is not None:
            out[match.group(1)] = match.group(2).strip()
    return out


def _expected_line(symbol: str, value: str) -> str:
    """Return the line the generated config carries for *symbol*.

    Kconfig writes a disabled symbol as a comment, not as ``=n``, so the two
    forms are different spellings of the same declaration.
    """
    if value == "n":
        return f"# {symbol} is not set"
    return f"{symbol}={value}"


def test_the_defaults_file_is_not_empty() -> None:
    """A guard over an empty set passes while checking nothing."""
    assert DEFAULTS.exists(), f"{DEFAULTS} is missing: it is a SOURCE file, not a generated one"
    assert _declared(), f"{DEFAULTS} declares no CONFIG_ option, so this test checked nothing"


def test_every_declared_option_reaches_the_generated_config(
    built: Callable[[Path, str], Path],
) -> None:
    """The generated config carries what the defaults file asked for.

    It skips on a checkout that was never built, and fails there under
    ``METER_BUILD_REQUIRED=1`` (CI's firmware job, after the build): this test
    is the one that proves the build honored the defaults.
    """
    generated = built(GENERATED, "`pio run --project-dir firmware -e meter`")
    text = generated.read_text(encoding="utf-8")
    lines = set(text.splitlines())
    missing = [
        (symbol, value)
        for symbol, value in _declared().items()
        if _expected_line(symbol, value) not in lines
        and not (value == "n" and not re.search(rf"\b{symbol}\b", text))
    ]
    assert not missing, (
        "the generated config does not carry what sdkconfig.defaults declares, so the "
        "firmware is not configured the way the source says it is:\n"
        + "\n".join(f"  {s}={v!r} -> expected {_expected_line(s, v)!r}" for s, v in missing)
    )


def test_the_build_holds_only_the_firmware_s_components(
    built: Callable[[Path, str], Path],
) -> None:
    """FW-16: NVS, the radios and the retired drivers are not in the build at all."""
    description = built(DESCRIPTION, "`pio run --project-dir firmware -e meter`")
    components = set(json.loads(description.read_text(encoding="utf-8"))["build_components"])
    assert "src" in components and "esp_psram" in components and "spi_flash" in components
    present = sorted(set(LEFT_OUT) & components)
    assert not present, f"the build holds components it must not: {present}"


def test_the_project_names_its_components() -> None:
    """The restriction the guard above sees in the build, at its source."""
    text = (METER / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "\nset(COMPONENTS src __pio_env)\n" in text
    assert text.index("set(COMPONENTS") < text.index("include($ENV{IDF_PATH}/tools/cmake/")


def test_psram_is_on_and_octal() -> None:
    """The N16R8's PSRAM, named rather than left to the general rule above.

    Quad on an octal part degrades to no PSRAM at all rather than failing, and
    a sibling board in this fleet lost its vault that way, so the mode is worth
    its own assertion with its own message.
    """
    declared = _declared()
    assert declared.get("CONFIG_SPIRAM") == "y", (
        "PSRAM is off, so the TRACE ring falls back to 128 KiB of internal RAM"
    )
    assert declared.get("CONFIG_SPIRAM_MODE_OCT") == "y", (
        "the module is an N16R8, so the mode is octal; quad degrades to no PSRAM at all"
    )


# --- What ESP-IDF allocates before app_main(), by configuration (FW-18) ------

#: The options that take an allocation of ESP-IDF's startup out (docs/misra.md
#: D1): PSRAM in the memory map only, ESP-IDF's brownout handling off (the
#: firmware arms the detector, src/brownout.h), and no VFS, should it return.
PRE_MAIN_OPTIONS = {
    "CONFIG_SPIRAM_USE_MEMMAP": "y",
    "CONFIG_ESP_BROWNOUT_DET": "n",
    "CONFIG_VFS_SUPPORT_IO": "n",
    "CONFIG_VFS_INITIALIZE_DEV_NULL": "n",
}


def test_the_pre_main_options_are_declared() -> None:
    """Each option FW-18 relies on is stated, with the value that removes the allocation."""
    declared = _declared()
    wrong = {s: declared.get(s) for s, v in PRE_MAIN_OPTIONS.items() if declared.get(s) != v}
    assert not wrong, f"sdkconfig.defaults no longer states what FW-18 relies on: {wrong}"


def test_the_generated_config_keeps_psram_out_of_the_heap(
    built: Callable[[Path, str], Path],
) -> None:
    """PSRAM is mapped, holds the ring's section, and is no heap: no malloc
    from it and no internal DMA reserve, the 32 KiB allocation before
    app_main() (esp_psram_extram_reserve_dma_pool()); ESP-IDF's brownout
    init is not compiled, and the ring's section is still zeroed at boot."""
    generated = built(GENERATED, "`pio run --project-dir firmware -e meter`")
    lines = generated.read_text(encoding="utf-8").splitlines()
    assert "CONFIG_SPIRAM_USE_MEMMAP=y" in lines
    assert "CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY=y" in lines
    for gone in (
        "CONFIG_SPIRAM_USE_MALLOC=",
        "CONFIG_SPIRAM_USE_CAPS_ALLOC=",
        "CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL=",
        "CONFIG_ESP_BROWNOUT_DET=",
    ):
        assert not [line for line in lines if line.startswith(gone)], gone
    assert "# CONFIG_ESP_BROWNOUT_DET is not set" in lines


def test_the_brownout_level_is_the_one_esp_idf_armed() -> None:
    """With ESP-IDF's brownout handling off its level option is gone, so the
    firmware states the level itself: 7, ESP-IDF's default, the one every
    image before FW-18 armed."""
    source = (METER / "src" / "brownout.h").read_text(encoding="utf-8")
    assert re.search(r"kThreshold\s*=\s*7U;", source)
    assert "CONFIG_ESP_BROWNOUT_DET_LVL" not in "".join(_declared())


# --- The watchdog is the firmware's own (FW-11) ------------------------------

#: The own watchdog's timer, as watchdog.h states it.
WATCHDOG = METER / "src" / "watchdog.h"


def test_esp_idf_task_watchdog_is_off_and_none_of_its_options_is_stated() -> None:
    """ESP-IDF's task watchdog allocated its subscribers and its interrupt;
    the firmware runs its own on the same timer (``src/watchdog.h``).

    With ``CONFIG_ESP_TASK_WDT_EN`` off its other options no longer exist, so
    one still stated would be a declaration that never reaches the build; and
    the interrupt watchdog, on the other timer group, stays on.
    """
    declared = _declared()
    assert declared.get("CONFIG_ESP_TASK_WDT_EN") == "n", (
        "ESP-IDF's task watchdog is on: it would take Timer Group 0's MWDT from the "
        "firmware's own watchdog and allocate its subscribers again (docs/misra.md D1)"
    )
    stale = sorted(
        symbol
        for symbol in declared
        if symbol.startswith("CONFIG_ESP_TASK_WDT_") and symbol != "CONFIG_ESP_TASK_WDT_EN"
    )
    assert not stale, f"options of the task watchdog that no longer exist: {stale}"
    assert declared.get("CONFIG_ESP_INT_WDT", "y") == "y", "the interrupt watchdog is off"


def test_the_generated_config_has_no_task_watchdog(
    built: Callable[[Path, str], Path],
) -> None:
    """The build agrees: no task watchdog option is set, the interrupt watchdog is."""
    generated = built(GENERATED, "`pio run --project-dir firmware -e meter`")
    lines = generated.read_text(encoding="utf-8").splitlines()
    assert "# CONFIG_ESP_TASK_WDT_EN is not set" in lines
    on = [line for line in lines if re.match(r"^CONFIG_(ESP_)?TASK_WDT\w*=", line)]
    assert not on, f"the generated config still sets the task watchdog: {on}"
    assert "CONFIG_ESP_INT_WDT=y" in lines


def test_the_own_watchdog_keeps_the_five_seconds_the_report_states() -> None:
    """``DIAGnostic:WATChdog?`` reports ``timeout_s=5`` (docs/scpi.md), which is
    ``kTimeoutSeconds``; the timer's hold is that many 500 us ticks."""
    source = WATCHDOG.read_text(encoding="utf-8")
    assert re.search(r"kTimeoutSeconds\s*=\s*5;", source)
    assert re.search(r"kPrescaler\s*=\s*40000;", source)
    assert "WDT_STAGE_ACTION_RESET_SYSTEM" in (METER / "src" / "watchdog.cpp").read_text(
        encoding="utf-8"
    )


# --- The console baud is stated twice, so it can disagree with itself --------

#: ``constexpr std::uint32_t kConsoleBaud = <n>;`` in the sampler header.
_BAUD_IN_SOURCE = re.compile(r"kConsoleBaud\s*=\s*(\d+)\s*;")

#: ``monitor_speed = <n>`` in the meter's own platformio.ini.
_BAUD_IN_INI = re.compile(r"^monitor_speed\s*=\s*(\d+)\s*$", re.MULTILINE)

#: ``METER_BAUD = <n>`` in the campaign tool that drives the instrument.
_BAUD_IN_CLIENT = re.compile(r"baud: int = ([0-9_]+)")

#: Where the campaign tool lives, relative to this file.
CLIENT = Path(__file__).resolve().parents[2] / "host" / "kleidos_meter" / "client.py"


def _one(pattern: re.Pattern[str], path: Path, what: str) -> int:
    """Return the single integer ``pattern`` finds in ``path``."""
    found = pattern.findall(path.read_text(encoding="utf-8"))
    assert len(found) == 1, f"{path.name}: expected one {what}, found {found}"
    return int(str(found[0]).replace("_", ""))


def test_every_statement_of_the_console_baud_agrees() -> None:
    """One baud rate is written in three files, and they must all match.

    The firmware configures UART0 from ``kConsoleBaud``; ``monitor_speed`` is
    what ``pio device monitor`` opens the port at; the host client's default is
    what ``kleidos_meter.SerialTransport`` opens it at (the Kleidos QA harness's
    ``METER_BAUD`` is the same number in that repository). Nothing makes any
    of them follow another, and a mismatch is not an error anywhere: the UART
    comes up fine, the host reads framing noise, and the instrument looks dead
    or, worse, mostly alive.

    Raising the line to 6 Mbps on 2026-10-28 proved the point. The header and
    the ini were edited together and the campaign tool was left at 2 Mbps,
    where it would have failed to talk to the instrument at all -- found by
    grep, not by any gate. Hence this one.
    """

    stated = {
        "kConsoleBaud": _one(_BAUD_IN_SOURCE, METER / "src" / "sampler.h", "kConsoleBaud"),
        "monitor_speed": _one(_BAUD_IN_INI, METER / "platformio.ini", "monitor_speed"),
        "client": _one(_BAUD_IN_CLIENT, CLIENT, "the client's default baud"),
    }

    assert len(set(stated.values())) == 1, f"the console baud disagrees with itself: {stated}"


def test_the_baud_stays_within_what_the_bridge_is_rated_for() -> None:
    """6 Mbps is the CH343P's rated maximum, and the link is measured there.

    Past it the bridge is out of specification, and the failure would be
    occasional corrupted samples rather than a dead link -- the kind a capture
    carries into a stored measurement.
    """

    firmware = _one(_BAUD_IN_SOURCE, METER / "src" / "sampler.h", "kConsoleBaud")

    assert 115200 <= firmware <= 6000000, f"{firmware} baud is outside the CH343P's range"
