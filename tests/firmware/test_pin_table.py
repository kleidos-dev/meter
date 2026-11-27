# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The board's pin table is the board, and docs/carrier-board.md says the same.

``firmware/board/pins.toml`` is the one description of the carrier board
(FW-01); ``scripts/gen_pin_table.py`` writes ``firmware/src/pin_table.h`` and
``host/kleidos_meter/board_pins.json`` from it. These tests hold:

- both copies to the table (``--check``), and the generator's own proof that
  the check refuses a row changed in one copy, a digest left behind and a
  terminal missing (``--selftest``);
- the panel's copy to the firmware's: the same digest, which is the CRC-32 of
  the canonical text of the rows the JSON lists;
- the table's shape: 44 terminals, 22 a rail, and the GPIOs on neither rail,
  every GPIO of the chip once;
- ``docs/carrier-board.md``'s two rail tables to the rows, row for row (the
  terminal, the silk, the GPIO, and a verdict that means the row's class),
  its SS 5 list (every pin the table reserves or marks caution, and no free
  one), its SS 2.1 list (the pins parked are the free and caution ones no
  default role holds) and its SS 2.2 table (each role's default, the pins it
  may take and why, as the table's [[role]] rows say).

Host-only: the files read as text.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import zlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_pin_table.py"
BOARD_PAGE = ROOT / "docs" / "carrier-board.md"
JSON_COPY = ROOT / "host" / "kleidos_meter" / "board_pins.json"
HEADER_COPY = ROOT / "firmware" / "src" / "pin_table.h"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_pin_table", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_pin_table"] = module
    spec.loader.exec_module(module)
    return module


GEN = _generator()
TABLE = GEN.load(ROOT / GEN.SOURCE)
BY_PLACE = {(row.rail, row.terminal): row for row in TABLE.rows if row.rail != "offboard"}
BY_GPIO = {row.gpio: row for row in TABLE.rows if row.gpio is not None}
DEFAULT_PINS = {TABLE.defaults[role] for role in GEN.ROLES}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_both_copies_are_what_the_table_makes() -> None:
    """``--check`` passes: neither copy was edited by hand or left behind."""
    done = _run("--check")
    assert done.returncode == 0, done.stderr


def test_the_check_refuses_each_planted_drift() -> None:
    """``--selftest`` plants three drifts and the check refuses each."""
    done = _run("--selftest")
    assert done.returncode == 0, done.stdout + done.stderr
    for case in ("a row changed in one output", "a digest not recomputed", "a terminal missing"):
        assert f"refused, as it must: {case}" in done.stdout


def test_the_panel_copy_has_the_firmware_digest_of_its_own_rows() -> None:
    """The JSON's digest is the header's, and the CRC-32 of the JSON's own rows."""
    document = json.loads(JSON_COPY.read_text(encoding="utf-8"))
    header = HEADER_COPY.read_text(encoding="utf-8")
    match = re.search(r"kPinTableDigest = 0x([0-9A-F]{8})U;", header)
    assert match, "pin_table.h names no kPinTableDigest"
    assert document["digest"] == f"0x{match[1]}"
    rows = [
        GEN.Row(
            rail=row["rail"],
            terminal=row["terminal"],
            silk=row["silk"],
            gpio=row["gpio"],
            pin_class=row["class"],
            group=row["group"],
            hover=row["hover"],
        )
        for row in document["rows"]
    ]
    rebuilt = GEN.Table(
        document["board"],
        document["bus"],
        document["roles"],
        document["defaults"],
        document["groups"],
        rows,
    )
    assert f"0x{zlib.crc32(GEN.canonical(rebuilt).encode('ascii')):08X}" == document["digest"]


def test_the_digest_ignores_the_words_and_follows_the_rules() -> None:
    """Rewording a sentence keeps the digest; a class or a limit moves it."""
    reworded = GEN.Table(
        TABLE.board,
        TABLE.bus,
        TABLE.roles,
        TABLE.defaults,
        TABLE.groups,
        [replace(row, hover="x", silk="y") for row in TABLE.rows],
    )
    assert GEN.digest(reworded) == GEN.digest(TABLE)
    reclassed = GEN.Table(
        TABLE.board,
        TABLE.bus,
        TABLE.roles,
        TABLE.defaults,
        TABLE.groups,
        [replace(row, pin_class="caution") if row.gpio == 18 else row for row in TABLE.rows],
    )
    assert GEN.digest(reclassed) != GEN.digest(TABLE)
    limited = GEN.Table(
        TABLE.board,
        TABLE.bus,
        [{**role, "below": 31} if role["key"] == "alert" else role for role in TABLE.roles],
        TABLE.defaults,
        TABLE.groups,
        TABLE.rows,
    )
    assert GEN.digest(limited) != GEN.digest(TABLE)


def test_the_table_has_44_terminals_and_every_gpio_once() -> None:
    """22 terminals a rail, then the GPIOs on neither rail; GPIO0 to 48 but 22 to 25."""
    terminals = [row for row in TABLE.rows if row.rail != "offboard"]
    assert len(terminals) == 44
    assert [row.terminal for row in terminals if row.rail == "left"] == list(range(1, 23))
    assert [row.terminal for row in terminals if row.rail == "right"] == list(range(1, 23))
    offboard = sorted(row.gpio for row in TABLE.rows if row.rail == "offboard")
    assert offboard == list(range(26, 35)), "the off-board rows are the memory lines 26 to 34"
    assert sorted(BY_GPIO) == [gpio for gpio in range(49) if gpio not in range(22, 26)]
    header = HEADER_COPY.read_text(encoding="utf-8")
    assert "constexpr std::size_t kTerminalCount = 44;" in header
    assert f"constexpr std::size_t kRowCount = {len(TABLE.rows)};" in header


@pytest.mark.parametrize(
    "planted, message",
    [
        (
            lambda s: s.replace(
                'class = "free"\ngroup = "none"', 'class = "reserved"\ngroup = "none"', 1
            ),
            "group none is free",
        ),
        (lambda s: s.replace("gpio = 21\n", "gpio = 18\n", 1), "GPIO18 has 2 rows"),
        (lambda s: s.replace("gpio = 34\n", "gpio = 22\n", 1), "GPIO22 is not an ESP32-S3 GPIO"),
        (
            lambda s: s.replace("relay2 = 15\n", "relay2 = 45\n", 1),
            "default relay2: GPIO45 is reserved",
        ),
        (
            lambda s: s.replace("alert = 6\n", "alert = 39\n", 1),
            "default alert: GPIO39 is not below 32",
        ),
        (lambda s: s.replace("relay1 = 7\n", "relay1 = 4\n", 1), "default sda: GPIO4 is shared"),
        (
            lambda s: s.replace('hover = "GPIO8: free; parked."', 'hover = ""', 1),
            "no hover sentence",
        ),
    ],
    ids=[
        "class-against-group",
        "gpio-twice",
        "absent-gpio",
        "default-reserved",
        "default-too-high",
        "default-shared",
        "no-hover",
    ],
)
def test_the_generator_refuses_a_table_that_breaks_a_rule(
    tmp_path: Path, planted: Callable[[str], str], message: str
) -> None:
    """Each rule of the table is refused by name when a planted table breaks it."""
    source = (ROOT / GEN.SOURCE).read_text(encoding="utf-8")
    target = tmp_path / "pins.toml"
    target.write_text(planted(source), encoding="utf-8")
    with pytest.raises(GEN.TableError, match=re.escape(message)):
        GEN.load(target)


# --- docs/carrier-board.md --------------------------------------------------------


def _rail_rows(heading: str) -> list[tuple[int, str, int | None, str]]:
    """The rows of the rail table under ``heading``: terminal, silk, GPIO, verdict."""
    text = BOARD_PAGE.read_text(encoding="utf-8")
    section = text.split(f"### {heading}", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
    rows: list[tuple[int, str, int | None, str]] = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not cells[0].isdigit():
            continue
        gpio = None if cells[2] == "—" else int(cells[2].removeprefix("GPIO"))
        rows.append((int(cells[0]), cells[1].strip("`"), gpio, cells[3]))
    return rows


#: What a verdict of the page means for a row's class, by its first words,
#: in the order they are tried.
VERDICTS: list[tuple[str, str]] = [
    (r"^\*\*STRAPPING\*\*", "reserved"),
    (r"^\*\*NOT AVAILABLE\*\*", "reserved"),
    (r"^\*\*held low by the firmware\*\*", "reserved"),
    (r"^the RGB LED net", "reserved"),
    (r"^free, but it is JTAG", "caution"),
    (r"^VDD_SPI domain", "caution"),
    (r"^\*\*ALLOCATED\*\*", "free"),
    (r"^\*\*free\*\*", "free"),
]


def _verdict_class(verdict: str, gpio: int | None) -> str:
    if gpio is None:
        return "reserved"
    if verdict.startswith("**TAKEN**"):
        # Taken by the meter's own wiring (a default role: free for any role
        # once the map moves it) or by the board (UART0, USB: reserved).
        return "free" if gpio in DEFAULT_PINS else "reserved"
    for pattern, pin_class in VERDICTS:
        if re.search(pattern, verdict):
            return pin_class
    raise AssertionError(f"GPIO{gpio}: the verdict {verdict!r} has no meaning in this test")


@pytest.mark.parametrize("rail, heading", [("left", "Left rail"), ("right", "Right rail")])
def test_the_board_page_lists_each_rail_as_the_table_does(rail: str, heading: str) -> None:
    """Row for row: the terminal, the silk, the GPIO and a verdict of the row's class."""
    rows = _rail_rows(heading)
    assert [number for number, *_ in rows] == list(range(1, 23)), f"the {heading} is not 1 to 22"
    for number, silk, gpio, verdict in rows:
        row = BY_PLACE[(rail, number)]
        assert (row.silk, row.gpio) == (silk, gpio), f"{rail} {number}: the page says {silk}/{gpio}"
        assert _verdict_class(verdict, gpio) == row.pin_class, (
            f"{rail} {number} (GPIO{gpio}): the page's verdict reads "
            f"{_verdict_class(verdict, gpio)}, the table says {row.pin_class}"
        )


def test_every_default_pin_is_taken_on_the_page() -> None:
    """The pins the default map gives a role are the ones the page calls TAKEN for the meter."""
    taken = {
        gpio
        for heading in ("Left rail", "Right rail")
        for _, _, gpio, verdict in _rail_rows(heading)
        if gpio is not None
        and verdict.startswith("**TAKEN**")
        and BY_GPIO[gpio].pin_class == "free"
    }
    assert taken == DEFAULT_PINS


def _gpios(cell: str) -> set[int]:
    found: set[int] = set()
    for first, last in re.findall(r"GPIO(\d+)(?:[–-]GPIO(\d+))?", cell):
        found.update(range(int(first), int(last or first) + 1))
    return found


def _section_rows(start: str, end: str) -> list[list[str]]:
    text = BOARD_PAGE.read_text(encoding="utf-8")
    section = text.split(start, 1)[1].split(end, 1)[0]
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| ") and not line.startswith("| Pins")
    ]


def test_no_pin_the_page_bars_from_i2c_is_free() -> None:
    """SS 5: every pin listed (but the meter's own) is reserved or caution in the table."""
    rows = _section_rows("## 5. Pins that must not carry I2C", "\n## 6.")
    assert rows, "SS 5 has no table"
    for cells in rows:
        if cells[1].startswith("taken"):
            continue
        for gpio in _gpios(cells[0]):
            assert BY_GPIO[gpio].pin_class != "free", f"SS 5 bars GPIO{gpio}, the table frees it"


def test_the_parked_pins_on_the_page_are_the_free_ones_no_role_holds() -> None:
    """SS 2.1: the parked pins are every free or caution pin outside the default map."""
    rows = _section_rows("### 2.1 What the firmware does", "\n### 2.2")
    listed = set().union(*(_gpios(cells[0]) for cells in rows))
    expected = {
        gpio
        for gpio, row in BY_GPIO.items()
        if row.pin_class != "reserved" and gpio not in DEFAULT_PINS
    }
    assert listed == expected


def test_the_page_bars_every_pin_the_table_reserves_or_marks_caution() -> None:
    """SS 5 lists, besides the meter's own, exactly the reserved and caution GPIOs."""
    rows = _section_rows("## 5. Pins that must not carry I2C", "\n## 6.")
    listed = set().union(*(_gpios(cells[0]) for cells in rows if not cells[1].startswith("taken")))
    assert listed == {gpio for gpio, row in BY_GPIO.items() if row.pin_class != "free"}


def _role_pins(role: dict[str, object]) -> set[int]:
    """The GPIOs *role* may take by the table: neither reserved nor at or above its limit."""
    below = role["below"]
    assert isinstance(below, int)
    return {gpio for gpio, row in BY_GPIO.items() if row.pin_class != "reserved" and gpio < below}


def test_the_page_says_which_pins_each_role_may_take() -> None:
    """SS 2.2: a row a role, its default pin, the pins the table lets it take and its reason."""
    rows = _section_rows("### 2.2 Which pins a role may take", "\n---")
    by_label = {cells[0]: cells for cells in rows if len(cells) == 4 and cells[0] != "Role"}
    assert sorted(by_label) == sorted(str(role["label"]) for role in TABLE.roles)
    for role in TABLE.roles:
        label, default, pins, why = by_label[str(role["label"])]
        assert default.startswith(f"GPIO{TABLE.defaults[role['key']]} "), label
        assert _gpios(pins) == _role_pins(role), f"{label}: the page lists {pins}"
        assert why == role["needs"], f"{label}: the reason differs from pins.toml's"
    assert _role_pins({"below": 32}) < _role_pins({"below": 49})
