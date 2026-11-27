# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware project's toolchain, warnings and console settings are pinned.

The meter left the Kleidos firmware repository on 2026-11-19 with the pins and
the strict warning set that repository's ``[esp32_base]`` carried that day.
They are restated in ``firmware/platformio.ini``, and this guard keeps them
from loosening in silence: a floating platform version builds against whatever
was published last, and a lost ``-W`` flag still builds, just without the
diagnostic. It also pins the one fact that cannot be read off the ini alone:
the stream byte-rate cap the firmware refuses a capture with is derived from
``kConsoleBaud`` in ``firmware/src/sampler.h``, and the monitor talks at
``monitor_speed``.

Host-only: one ini file and one C++ header, read as text. No device, no build.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
METER_INI = REPO_ROOT / "firmware" / "platformio.ini"
SAMPLER_H = REPO_ROOT / "firmware" / "src" / "sampler.h"
SECTION = "env:meter"

#: The pins, exact: a version range is a toolchain nobody chose.
PINS = {
    "platform": "platformio/espressif32 @ 7.0.1",
    "platform_packages": "platformio/framework-espidf @ ~4.60003.0",
    "framework": "espidf",
}

#: The strict warning set, as the firmware repository's [esp32_base] had it.
WARNINGS = {
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wshadow",
    "-Wnon-virtual-dtor",
    "-Wcast-align",
    "-Wunused",
    "-Woverloaded-virtual",
    "-Wformat=2",
    "-Wold-style-cast",
    "-Wconversion",
    "-Wsign-conversion",
    "-Wdouble-promotion",
    # Added on 2026-11-20 (docs/misra.md): cross-compiler, then GCC's own.
    "-Wcast-qual",
    "-Wnull-dereference",
    "-Wswitch-enum",
    "-Wundef",
    "-Wredundant-decls",
    "-Wvla",
    "-Walloca",
    "-Wduplicated-cond",
    "-Wduplicated-branches",
    "-Wlogical-op",
    # 0.14.0: no function of the firmware's own takes more than 1 KiB of
    # stack on the device (the largest, 944 bytes at -O2, is a report).
    "-Wstack-usage=1024",
    # FW-01 (2026-11-22): the answer by name to each warning ESP-IDF's
    # build.cmake relaxes for every component before these flags
    # (tests/firmware/test_effective_compile_flags.py holds the result).
    "-Werror=unused-function",
    "-Werror=unused-variable",
    "-Werror=unused-but-set-variable",
    "-Werror=deprecated-declarations",
    "-Werror=extra",
    "-Wunused-parameter",
    "-Wsign-compare",
    "-Wenum-conversion",
}


@pytest.fixture(scope="module")
def meter() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    parser.read_string(METER_INI.read_text(encoding="utf-8"))
    return parser


def test_the_project_declares_one_image(meter: configparser.ConfigParser) -> None:
    """``meter``, which drives the bypass on the Relay 2 Click (0.8.0 and later)."""
    assert [name for name in meter.sections() if name.startswith("env:")] == [SECTION]
    assert "build_flags" not in meter[SECTION], "the image takes no build switch"


@pytest.mark.parametrize("option", sorted(PINS))
def test_the_toolchain_is_pinned(option: str, meter: configparser.ConfigParser) -> None:
    assert meter[SECTION][option].strip() == PINS[option]


def test_every_strict_warning_flag_is_kept(meter: configparser.ConfigParser) -> None:
    flags = {
        token.strip()
        for token in meter[SECTION]["build_src_flags"].splitlines()
        if token.strip().startswith("-W")
    }
    missing = sorted(WARNINGS - flags)
    assert not missing, f"firmware/platformio.ini lost warning flags: {missing}"


def test_no_warning_is_switched_off(meter: configparser.ConfigParser) -> None:
    """``-Wno-unused-parameter`` left with FW-01; no ``-Wno-`` flag of any kind comes back."""
    flags = meter[SECTION]["build_src_flags"].split()
    assert [flag for flag in flags if flag.startswith("-Wno-")] == []


def test_monitor_speed_matches_the_firmwares_console_baud(
    meter: configparser.ConfigParser,
) -> None:
    match = re.search(
        r"constexpr\s+std::uint32_t\s+kConsoleBaud\s*=\s*(\d+)\s*;",
        SAMPLER_H.read_text(encoding="utf-8"),
    )
    assert match is not None, "kConsoleBaud not found in sampler.h"
    assert meter[SECTION]["monitor_speed"].strip() == match.group(1)


def test_the_monitor_does_not_touch_the_reset_lines(meter: configparser.ConfigParser) -> None:
    """The carrier's two transistors turn DTR/RTS into a reset (``docs/carrier-board.md`` §7)."""
    for option in ("monitor_dtr", "monitor_rts"):
        assert meter[SECTION][option].strip() == "0"
