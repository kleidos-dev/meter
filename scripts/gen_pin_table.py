#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The board's pin table, written both ways from ``firmware/board/pins.toml``.

    python3 scripts/gen_pin_table.py             # write both copies
    python3 scripts/gen_pin_table.py --check     # fail when either differs (pre-commit, tests)
    python3 scripts/gen_pin_table.py --selftest  # plant each drift, prove --check fails on it

``firmware/board/pins.toml`` is the one description of the carrier board: every
terminal of both screw rails, the GPIOs the module does not break out, each
with its class (``free``, ``caution``, ``reserved``), its reason group and the
sentence the panel shows on hover; what each role needs of a pin; the default
map; the bus's ranges. This script reads it, refuses a table that breaks its
rules (a rail without its 22 terminals, a GPIO twice or missing, a class its
group contradicts, a default the table refuses), and writes:

- ``firmware/src/pin_table.h``: the rows, the roles' limits, the defaults and
  the ranges as constants, and ``kPinTableDigest``, for ``pinmap.h``'s
  validator;
- ``host/kleidos_meter/board_pins.json``: the same rows with their silk and
  hover text, the roles' labels, the groups' sentences, and the same digest,
  for the panel's drawing of the board.

**The digest** is the CRC-32 (IEEE 802.3, Python's :func:`zlib.crc32`) of the
table's canonical text, :func:`canonical`: one line per fact the validator
reads (the GPIO count, the bus's ranges, each role's limit, the defaults, each
row's rail, terminal, GPIO, class and group), and nothing it does not (the
silk, the labels, the hover sentences), so that rewording a sentence keeps the
digest and a change the validator would see moves it. The firmware reports it
(``table=`` from FW-02) and the panel compares it with its own copy's.

``--check`` writes nothing: it regenerates both copies in memory and names each
file that differs, and it is the gate. ``--selftest`` plants, in temporary
copies, a row changed in one output only, a digest left as it was after a row
moved, and a terminal missing from the table, and fails unless the check
refuses each.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import tomllib
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("firmware/board/pins.toml")
HEADER = Path("firmware/src/pin_table.h")
JSON = Path("host/kleidos_meter/board_pins.json")

#: The classes a row may have, as the header spells them.
CLASSES = {"free": "Free", "caution": "Caution", "reserved": "Reserved"}
#: The rails, as the header spells them.
RAILS = {"left": "Left", "right": "Right", "offboard": "OffBoard"}
#: The reason groups, in the order of the header's enumeration, with the one
#: class each allows.
GROUPS = {
    "none": "free",
    "power": "reserved",
    "ground": "reserved",
    "reset": "reserved",
    "strapping": "reserved",
    "memory": "reserved",
    "usb": "reserved",
    "uart0": "reserved",
    "led": "reserved",
    "jtag": "caution",
    "vdd_spi": "caution",
}
#: The groups of a terminal that is not a GPIO.
NOT_GPIO = ("power", "ground", "reset")
#: The roles, in PinMap's order, and the header's enumerator for each.
ROLES = {"sda": "Sda", "scl": "Scl", "alert": "Alert", "relay1": "Relay1", "relay2": "Relay2"}
#: The terminals of each rail.
TERMINALS = 22
#: The GPIO numbers the ESP32-S3 does not have.
ABSENT = range(22, 26)
#: The default map's keys, in the vectors' and the wire's order.
DEFAULT_KEYS = (
    "sda",
    "scl",
    "alert",
    "address",
    "scl_hz",
    "pull_sda",
    "pull_scl",
    "pull_alert",
    "relay1",
    "relay1_active",
    "relay2",
    "relay2_active",
)


class TableError(Exception):
    """The table breaks one of its rules."""


@dataclass(frozen=True)
class Row:
    """One terminal, or one GPIO the module does not break out."""

    rail: str
    terminal: int
    silk: str
    gpio: int | None
    pin_class: str
    group: str
    hover: str


@dataclass(frozen=True)
class Table:
    """What ``pins.toml`` says, checked."""

    board: dict[str, object]
    bus: dict[str, int]
    roles: list[dict[str, object]]
    defaults: dict[str, object]
    groups: dict[str, dict[str, str]]
    rows: list[Row]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TableError(message)


def _row(entry: dict[str, object], rail: str) -> Row:
    gpio = entry.get("gpio")
    _require(gpio is None or isinstance(gpio, int), f"{entry}: gpio is not a number")
    pin_class = str(entry.get("class", ""))
    group = str(entry.get("group", ""))
    _require(pin_class in CLASSES, f"{entry}: class {pin_class!r} is not one of {list(CLASSES)}")
    _require(group in GROUPS, f"{entry}: group {group!r} is not one of {list(GROUPS)}")
    _require(
        GROUPS[group] == pin_class,
        f"{entry}: group {group} is {GROUPS[group]}, not {pin_class}",
    )
    _require((gpio is None) == (group in NOT_GPIO), f"{entry}: a GPIO row has a GPIO group")
    hover = str(entry.get("hover", ""))
    _require(bool(hover), f"{entry}: no hover sentence")
    return Row(
        rail=rail,
        terminal=int(str(entry.get("number", 0))),
        silk=str(entry.get("silk", "")),
        gpio=gpio if isinstance(gpio, int) else None,
        pin_class=pin_class,
        group=group,
        hover=hover,
    )


def load(path: Path) -> Table:
    """Read and check ``pins.toml``.

    :param path: The table's file.
    :returns: The checked table.
    :raises TableError: On the first rule the file breaks.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    board = dict(data["board"])
    bus = {key: int(value) for key, value in data["bus"].items()}
    roles = [dict(role) for role in data["role"]]
    defaults = dict(data["defaults"])
    groups = {name: dict(group) for name, group in data["groups"].items()}
    count = int(board["gpio_count"])

    _require([role["key"] for role in roles] == list(ROLES), f"the roles are not {list(ROLES)}")
    for role in roles:
        below = role["below"]
        _require(
            isinstance(below, int) and 0 < below <= count, f"{role['key']}: below {below} is wrong"
        )
    _require(sorted(groups) == sorted(GROUPS), f"the groups are not {sorted(GROUPS)}")
    _require(bus["address_first"] <= bus["address_last"], "the address range is empty")
    _require(bus["clock_min_hz"] < bus["clock_max_hz"], "the clock range is empty")

    rows: list[Row] = []
    for rail in ("left", "right"):
        terminals = [_row(entry, rail) for entry in data["terminal"] if entry["rail"] == rail]
        numbers = [row.terminal for row in terminals]
        expected = list(range(1, TERMINALS + 1))
        for number in expected:
            _require(number in numbers, f"the {rail} rail lacks terminal {number}")
        _require(numbers == expected, f"the {rail} rail's terminals are not 1 to 22 in order")
        rows.extend(terminals)
    _require(
        all(entry["rail"] in ("left", "right") for entry in data["terminal"]),
        "a terminal names another rail",
    )
    for entry in data.get("offboard", []):
        row = _row({**entry, "number": 0, "silk": ""}, "offboard")
        _require(row.gpio is not None, f"{entry}: an off-board row is a GPIO")
        rows.append(row)

    gpios = [row.gpio for row in rows if row.gpio is not None]
    for gpio in gpios:
        _require(0 <= gpio < count and gpio not in ABSENT, f"GPIO{gpio} is not an ESP32-S3 GPIO")
        _require(gpios.count(gpio) == 1, f"GPIO{gpio} has {gpios.count(gpio)} rows")
    for gpio in range(count):
        _require(gpio in ABSENT or gpio in gpios, f"GPIO{gpio} has no row")

    _require(list(defaults) == [*DEFAULT_KEYS, "source"], f"the defaults are not {DEFAULT_KEYS}")
    by_gpio = {row.gpio: row for row in rows if row.gpio is not None}
    pins = [defaults[role] for role in ROLES]
    for name, below in zip(ROLES, (role["below"] for role in roles), strict=True):
        gpio = defaults[name]
        _require(isinstance(gpio, int) and gpio in by_gpio, f"default {name}: GPIO{gpio} is no row")
        _require(by_gpio[gpio].pin_class != "reserved", f"default {name}: GPIO{gpio} is reserved")
        _require(gpio < int(str(below)), f"default {name}: GPIO{gpio} is not below {below}")
        _require(pins.count(gpio) == 1, f"default {name}: GPIO{gpio} is shared")
    for key in ("pull_sda", "pull_scl", "pull_alert"):
        _require(defaults[key] in ("up", "off"), f"default {key} is not up or off")
    for key in ("relay1_active", "relay2_active"):
        _require(defaults[key] in ("low", "high"), f"default {key} is not low or high")
    _require(defaults["address"] == "auto", "the default address is not auto")
    _require(
        bus["clock_min_hz"] <= int(str(defaults["scl_hz"])) <= bus["clock_max_hz"],
        "the default clock is outside the range",
    )
    return Table(board, bus, roles, defaults, groups, rows)


def canonical(table: Table) -> str:
    """The text the digest is taken over: what the validator reads, one fact a line.

    :param table: The checked table.
    :returns: The canonical text, each line ending in a newline.
    """
    lines = [f"gpio_count {table.board['gpio_count']}"]
    lines += [f"bus {key} {table.bus[key]}" for key in sorted(table.bus)]
    lines += [f"role {role['key']} below {role['below']}" for role in table.roles]
    lines += [f"default {key} {table.defaults[key]}" for key in DEFAULT_KEYS]
    for row in table.rows:
        gpio = "-" if row.gpio is None else str(row.gpio)
        lines.append(f"row {row.rail} {row.terminal} {gpio} {row.pin_class} {row.group}")
    return "".join(f"{line}\n" for line in lines)


def digest(table: Table) -> int:
    """The table's CRC-32.

    :param table: The checked table.
    :returns: :func:`zlib.crc32` of :func:`canonical`'s text.
    """
    return zlib.crc32(canonical(table).encode("ascii"))


_SPDX = (
    "SPDX-" + "FileCopyrightText: 2026 José M. Requena Plens",
    "SPDX-" + "License-Identifier: GPL-3.0-or-later",
)


def _enum(name: str, brief: str, members: list[tuple[str, str]]) -> list[str]:
    out = [f"/// {brief}", f"enum class {name} : std::uint8_t {{"]
    for member, doc in members:
        out += [f"    /// {doc}", f"    {member},"]
    return [*out, "};", ""]


def render_header(table: Table) -> str:
    """``firmware/src/pin_table.h``, as the generator writes it.

    :param table: The checked table.
    :returns: The header's text.
    """
    roles = table.roles
    defaults = table.defaults
    terminals = sum(1 for row in table.rows if row.rail != "offboard")
    out = [
        f"// {_SPDX[0]}",
        f"// {_SPDX[1]}",
        "/**",
        " * @file pin_table.h",
        " * @ingroup pin_map",
        " * @brief The carrier board's pins, generated from `firmware/board/pins.toml` by",
        " *        `scripts/gen_pin_table.py`: never edited by hand.",
        " *",
        " * Every terminal of both screw rails (`docs/carrier-board.md` SS 2) and every",
        " * GPIO the module does not break out, each with its class and its reason",
        " * group; what each role needs of a pin; the default map, the rig as wired",
        " * (rev D); the bus's ranges; and kPinTableDigest, the CRC-32 of the table's",
        " * canonical text (the script says what it covers). `pinmap.h` holds the rules",
        " * that read it. The silk, the labels and the hover sentences are in the",
        " * panel's copy, `host/kleidos_meter/board_pins.json`, written from the same",
        " * file; `python3 scripts/gen_pin_table.py --check` fails when either differs.",
        " */",
        "#ifndef KLEIDOS_METER_PIN_TABLE_H",
        "#define KLEIDOS_METER_PIN_TABLE_H",
        "",
        "#include <array>",
        "#include <cstddef>",
        "#include <cstdint>",
        "#include <string_view>",
        "",
        "namespace kleidos::meter::pin_table {",
        "",
        "/// @addtogroup pin_map",
        "/// @{",
        "",
    ]
    out += _enum(
        "Rail",
        "Where a pin's terminal is.",
        [
            ("Left", "The left screw rail, the module's header P1."),
            ("Right", "The right screw rail, the module's header P2."),
            ("OffBoard", "On neither rail: a GPIO the module does not break out."),
        ],
    )
    out += _enum(
        "PinClass",
        "What a role may do with a pin.",
        [
            ("Free", "Any role whose limit it is below may take it."),
            ("Caution", "A role may take it, at the cost its group states."),
            ("Reserved", "No role may take it."),
        ],
    )
    out += _enum(
        "Group",
        "Why a pin has its class (`pins.toml` [groups] gives each one's sentence).",
        [(_camel(name), table.groups[name]["sentence"]) for name in GROUPS],
    )
    out += _enum(
        "Role",
        "What a pin can be given to, in the order of PinMap's pins.",
        [(ROLES[str(role["key"])], str(role["label"])) for role in roles],
    )
    out += [
        "/// The number of roles.",
        f"constexpr std::size_t kRoleCount = {len(roles)};",
        "",
        "/// Each role's key, as the wire and the vectors spell it.",
        "constexpr std::array<std::string_view, kRoleCount> kRoleWords = { {",
        *[f'    "{role["key"]}",' for role in roles],
        "} };",
        "",
        "/// The first GPIO number each role cannot take, in Role's order (`pins.toml`",
        "/// says why).",
        "constexpr std::array<std::int32_t, kRoleCount> kRoleBelow = { {",
        *[f"    {role['below']}," for role in roles],
        "} };",
        "",
        "/// The highest GPIO number of the ESP32-S3, plus one.",
        f"constexpr std::int32_t kGpioCount = {table.board['gpio_count']};",
        "",
        "/// What a row's GPIO is when the terminal is not a GPIO, and a role's when it has",
        "/// no pin.",
        "constexpr std::int32_t kNoGpio = -1;",
        "",
        "/// One terminal of a rail, or one GPIO the module does not break out.",
        "struct Row {",
        "    Rail         rail{};      ///< Where it is.",
        "    std::uint8_t terminal{};  ///< 1 to 22 on a rail, 0 off the board.",
        "    std::int32_t gpio{};      ///< Its GPIO, or kNoGpio.",
        "    PinClass     pinClass{};  ///< What a role may do with it.",
        "    Group        group{};     ///< Why.",
        "};",
        "",
        "/// The number of terminals, both rails.",
        f"constexpr std::size_t kTerminalCount = {terminals};",
        "",
        "/// The number of rows: the terminals, then the GPIOs on neither rail.",
        f"constexpr std::size_t kRowCount = {len(table.rows)};",
        "",
        "/// Every row: the left rail's 22 terminals, the right rail's, then the GPIOs",
        "/// the module does not break out.",
        "constexpr std::array<Row, kRowCount> kRows = { {",
    ]
    for row in table.rows:
        gpio = "kNoGpio" if row.gpio is None else str(row.gpio)
        out.append(
            f"    {{ Rail::{RAILS[row.rail]}, {row.terminal}, {gpio}, "
            f"PinClass::{CLASSES[row.pin_class]}, Group::{_camel(row.group)} }},"
        )
    out += [
        "} };",
        "",
        f"static_assert( kRows.size() == kTerminalCount + {len(table.rows) - terminals}U,",
        '               "the rows are the terminals and the GPIOs on neither rail" );',
        "",
        "/// The first address of the INA228's strap space.",
        f"constexpr std::uint8_t kAddressFirst = 0x{table.bus['address_first']:02X};",
        "/// The last address of the INA228's strap space.",
        f"constexpr std::uint8_t kAddressLast = 0x{table.bus['address_last']:02X};",
        "/// The slowest bus clock, in Hz.",
        f"constexpr std::uint32_t kClockMinHz = {table.bus['clock_min_hz']};",
        "/// The fastest bus clock, in Hz.",
        f"constexpr std::uint32_t kClockMaxHz = {table.bus['clock_max_hz']};",
        "",
        "/// The default map's pin for each role, in Role's order: the rig as wired",
        "/// (rev D).",
        "constexpr std::array<std::int32_t, kRoleCount> kDefaultPins = { {",
        *[f"    {defaults[key]}," for key in ROLES],
        "} };",
        "/// The default bus clock at boot, in Hz.",
        f"constexpr std::uint32_t kDefaultSclHz = {defaults['scl_hz']};",
        "/// Whether the default map pulls SDA up.",
        f"constexpr bool kDefaultPullSdaUp = {_flag(defaults['pull_sda'] == 'up')};",
        "/// Whether the default map pulls SCL up.",
        f"constexpr bool kDefaultPullSclUp = {_flag(defaults['pull_scl'] == 'up')};",
        "/// Whether the default map pulls ALERT up.",
        f"constexpr bool kDefaultPullAlertUp = {_flag(defaults['pull_alert'] == 'up')};",
        "/// Whether the default map's Relay 1 is energized by a high level.",
        f"constexpr bool kDefaultRelay1ActiveHigh = {_flag(defaults['relay1_active'] == 'high')};",
        "/// Whether the default map's Relay 2 is energized by a high level.",
        f"constexpr bool kDefaultRelay2ActiveHigh = {_flag(defaults['relay2_active'] == 'high')};",
        "",
        "/// The CRC-32 of the table's canonical text (`scripts/gen_pin_table.py`).",
        f"constexpr std::uint32_t kPinTableDigest = 0x{digest(table):08X}U;",
        "",
        "/// @}",
        "",
        "}  // namespace kleidos::meter::pin_table",
        "",
        "#endif  // KLEIDOS_METER_PIN_TABLE_H",
    ]
    return "\n".join(out) + "\n"


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _flag(value: bool) -> str:
    return "true" if value else "false"


def render_json(table: Table) -> str:
    """``host/kleidos_meter/board_pins.json``, as the generator writes it.

    :param table: The checked table.
    :returns: The file's text: two-space JSON, keys in the table's order.
    """
    document = {
        "generated_by": "scripts/gen_pin_table.py from firmware/board/pins.toml; never edit",
        "digest": f"0x{digest(table):08X}",
        "board": table.board,
        "bus": table.bus,
        "roles": table.roles,
        "defaults": table.defaults,
        "groups": table.groups,
        "rows": [
            {
                "rail": row.rail,
                "terminal": row.terminal,
                "silk": row.silk,
                "gpio": row.gpio,
                "class": row.pin_class,
                "group": row.group,
                "hover": row.hover,
            }
            for row in table.rows
        ],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def outputs(root: Path) -> dict[Path, str]:
    """What the two copies must hold, from ``root``'s table.

    :param root: A repository root.
    :returns: Each output's path, relative to ``root``, and its text.
    :raises TableError: When the table breaks a rule.
    """
    table = load(root / SOURCE)
    return {HEADER: render_header(table), JSON: render_json(table)}


def check(root: Path) -> list[str]:
    """Every way ``root``'s copies differ from what its table makes.

    :param root: A repository root.
    :returns: One finding a line; empty when both copies are current.
    """
    try:
        wanted = outputs(root)
    except (TableError, KeyError, ValueError, TypeError) as error:
        return [f"{SOURCE}: {error}"]
    findings: list[str] = []
    for path, text in wanted.items():
        target = root / path
        if not target.is_file():
            findings.append(f"{path}: missing (run scripts/gen_pin_table.py)")
        elif target.read_text(encoding="utf-8") != text:
            findings.append(f"{path}: differs from {SOURCE} (run scripts/gen_pin_table.py)")
    return findings


def write(root: Path) -> None:
    """Write both copies from ``root``'s table.

    :param root: A repository root.
    """
    for path, text in outputs(root).items():
        (root / path).write_text(text, encoding="utf-8")


def _planted(root: Path, scratch: Path) -> Path:
    tree = scratch / "tree"
    for path in (SOURCE, HEADER, JSON):
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, tree / path)
    return tree


def selftest(root: Path) -> int:
    """Plant each drift in a copy and require :func:`check` to refuse it.

    :param root: A repository root whose copies are current.
    :returns: 0 when the check refused every planted drift and passed the tree.
    """
    failures: list[str] = []
    if check(root):
        failures.append("the tree itself does not pass --check")

    def drift_json(tree: Path) -> None:
        text = (tree / JSON).read_text(encoding="utf-8")
        (tree / JSON).write_text(text.replace('"free"', '"caution"', 1), encoding="utf-8")

    def drift_digest(tree: Path) -> None:
        source = (tree / SOURCE).read_text(encoding="utf-8")
        planted = source.replace("below = 32", "below = 31", 1)
        (tree / SOURCE).write_text(planted, encoding="utf-8")
        old = (tree / HEADER).read_text(encoding="utf-8")
        write(tree)
        header = (tree / HEADER).read_text(encoding="utf-8")
        old_digest = old.split("kPinTableDigest = ", 1)[1].split(";", 1)[0]
        new_digest = header.split("kPinTableDigest = ", 1)[1].split(";", 1)[0]
        (tree / HEADER).write_text(header.replace(new_digest, old_digest), encoding="utf-8")

    def drift_terminal(tree: Path) -> None:
        source = (tree / SOURCE).read_text(encoding="utf-8")
        start = source.index('[[terminal]]\nrail = "left"\nnumber = 7\n')
        end = source.index("[[terminal]]", start + 1)
        (tree / SOURCE).write_text(source[:start] + source[end:], encoding="utf-8")

    cases = {
        "a row changed in one output": (drift_json, str(JSON)),
        "a digest not recomputed": (drift_digest, str(HEADER)),
        "a terminal missing": (drift_terminal, "lacks terminal 7"),
    }
    for name, (plant, expected) in cases.items():
        with tempfile.TemporaryDirectory() as scratch:
            tree = _planted(root, Path(scratch))
            plant(tree)
            findings = check(tree)
            if not any(expected in finding for finding in findings):
                failures.append(f"{name}: --check said {findings or 'nothing'}")
            else:
                print(f"refused, as it must: {name} ({findings[0]})")
    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Run the generator.

    :param argv: The arguments, without the program's name.
    :returns: The exit status: 0 done, 1 a finding.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when a copy differs")
    mode.add_argument("--selftest", action="store_true", help="prove --check on planted drifts")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest(ROOT)
    if args.check:
        findings = check(ROOT)
        for finding in findings:
            print(f"gen_pin_table: {finding}", file=sys.stderr)
        return 1 if findings else 0
    write(ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
