# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pin map: the board's table, a map, the rules it must keep, and a board's wiring.

The board is ``board_pins.json``, the copy ``scripts/gen_pin_table.py``
writes from ``firmware/board/pins.toml`` beside the firmware's
``pin_table.h``: every terminal of both rails, its class (``free``,
``caution``, ``reserved``), what each of the five roles needs, the default
map and the table's digest, which ``SYSTem:PINS?`` names as ``table``.

A :class:`PinMap` gives each role (the INA228's SDA, SCL and ALERT, Relay 1
and Relay 2) one GPIO or none, each bus and alert pin its pull, each relay
the level that energizes it, the INA228's address (0 for a scan, ``auto``)
and the bus clock at boot, keyed as ``SYSTem:PINS?`` names them.

:func:`validate_map` is ``pinmap::validate()`` (``firmware/src/pinmap.h``) in
Python: the first rule a map breaks, role by role, then the address and the
clock. It is held to the firmware's verdicts by
``firmware/test/native/golden/pinmap.vectors``, every line of which the host
tests run through it.

A :class:`Wiring` is where a board really has its parts: which pads the
INA228's bus and ALERT reach, its strap address, and which pads drive each
relay's input at which level. The simulator stands on one, and
:func:`prove`, :func:`relay_check` and :func:`boot` say what an instrument
finds when it brings a map up on it: whether the INA228 answers and its
self-test passes, whether a null-path check passes, and which map a boot
runs with (a stored bus that does not answer falls back to the default bus
when its pads are free).
"""

from __future__ import annotations

import argparse
import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from importlib import resources
from typing import Any

#: The board's pin table (``board_pins.json``).
BOARD: dict[str, Any] = json.loads(
    resources.files(__package__).joinpath("board_pins.json").read_text(encoding="utf-8")
)

#: The table's digest, ``0x`` and its CRC-32, as ``SYSTem:PINS?``'s ``table`` names it.
DIGEST: str = BOARD["digest"]

#: The five roles, in the table's order (``pin_table::Role``), keyed as the map names them.
ROLES: tuple[str, ...] = tuple(role["key"] for role in BOARD["roles"])

#: The two relays' roles.
RELAYS = ("relay1", "relay2")

#: The keys of a map that hold a GPIO or none: the five roles.
_GPIO_KEYS = frozenset(ROLES)

#: What a boot takes of the default map for a stored bus that does not answer
#: (``pinmap::withBusOf()``).
_BUS_KEYS = ("sda", "scl", "addr", "scl_hz", "pull_sda", "pull_scl")

#: The words a pull may be, and a relay's active level.
PULLS = ("up", "off")
LEVELS = ("high", "low")

#: The address that asks for a scan of the INA228's strap space at boot.
ADDRESS_AUTO = 0

#: The problems :func:`validate_map` names, in the order it looks
#: (``pinmap.h`` ``kProblemWords``, ``none`` left out).
PROBLEMS = ("missing", "no_pin", "reserved", "too_high", "shared", "address", "clock")

#: What :func:`validate_map` finds: the problem's word and the key it is at.
Verdict = tuple[str, str]


def address_of(word: str) -> int:
    """An address as a report or the table writes it: ``auto`` is 0, else the number.

    :param word: ``auto``, or a number in a form :func:`int` reads with base 0 (``0x41``).
    :returns: The address, or :data:`ADDRESS_AUTO`.
    :raises ValueError: When *word* is neither.
    """
    return ADDRESS_AUTO if word == "auto" else int(word, 0)


def address_text(address: int) -> str:
    """An address as ``SYSTem:PINS?`` writes it.

    :param address: The address, or :data:`ADDRESS_AUTO`.
    :returns: ``auto``, or ``0x`` and two hex digits.
    """
    return "auto" if address == ADDRESS_AUTO else f"0x{address:02X}"


@dataclass(frozen=True)
class PinMap:
    """A pin map, keyed as ``SYSTem:PINS?`` names its twelve fields.

    :ivar sda: The INA228's SDA pad, or None.
    :ivar scl: Its SCL pad, or None.
    :ivar alert: Its ALERT pad, or None.
    :ivar addr: Its address, or :data:`ADDRESS_AUTO` for a scan.
    :ivar scl_hz: The bus clock at boot, Hz.
    :ivar pull_sda: SDA's internal pull, ``up`` or ``off``.
    :ivar pull_scl: SCL's.
    :ivar pull_alert: ALERT's.
    :ivar relay1: Relay 1's pad (the DUT's battery lead), or None.
    :ivar relay1_active: The level that energizes it, ``high`` or ``low``.
    :ivar relay2: Relay 2's pad (the shunt bypass), or None.
    :ivar relay2_active: Its level.
    """

    sda: int | None
    scl: int | None
    alert: int | None
    addr: int
    scl_hz: int
    pull_sda: str
    pull_scl: str
    pull_alert: str
    relay1: int | None
    relay1_active: str
    relay2: int | None
    relay2_active: str

    @classmethod
    def from_words(cls, words: Mapping[str, str]) -> PinMap:
        """A map from its twelve fields as a report writes them (``none``, ``auto``, numbers).

        :param words: At least the twelve keys of :data:`KEYS`; others are ignored,
            so a ``SYSTem:PINS?`` or ``SYSTem:PINS:PENDing?`` report reads whole.
        :returns: The map.
        :raises KeyError: When a key is missing.
        :raises ValueError: When a number does not read.
        """
        values: dict[str, int | str | None] = {}
        for key in KEYS:
            word = words[key]
            if key == "addr":
                values[key] = address_of(word)
            elif key in _GPIO_KEYS:
                values[key] = None if word == "none" else int(word)
            elif key == "scl_hz":
                values[key] = int(word)
            else:
                values[key] = word
        return cls(**values)  # type: ignore[arg-type]  # PinMap's fields, by key

    def words(self) -> dict[str, str]:
        """The twelve fields as ``SYSTem:PINS?`` writes them.

        :returns: Each key's word, in the report's order.
        """
        words = {}
        for key in KEYS:
            value = getattr(self, key)
            if key == "addr":
                words[key] = address_text(value)
            else:
                words[key] = "none" if value is None else str(value)
        return words

    def text(self) -> str:
        """The map as ``SYSTem:PINS?`` writes it.

        :returns: The twelve ``key=value`` fields: ``sda=4 scl=5 ...``.
        """
        return " ".join(f"{key}={word}" for key, word in self.words().items())

    def pin(self, role: str) -> int | None:
        """A role's GPIO.

        :param role: One of :data:`ROLES`.
        :returns: Its GPIO, or None.
        """
        gpio: int | None = getattr(self, role)
        return gpio

    def held(self, gpio: int) -> bool:
        """Whether a role holds a GPIO.

        :param gpio: The GPIO.
        :returns: True when one of the five roles has it.
        """
        return any(self.pin(role) == gpio for role in ROLES)

    def relays(self) -> tuple[tuple[int | None, str], ...]:
        """The relays as a null-path check depends on them.

        :returns: Relay 1's pad and active level, then Relay 2's.
        """
        return ((self.relay1, self.relay1_active), (self.relay2, self.relay2_active))

    def bus(self) -> tuple[int | None, int | None, int]:
        """The bus as the INA228's answer depends on it.

        :returns: SDA's pad, SCL's and the address.
        """
        return (self.sda, self.scl, self.addr)


#: A map's keys, in ``SYSTem:PINS?``'s order (``Console::writePinMap``).
KEYS: tuple[str, ...] = tuple(f.name for f in fields(PinMap))


def _default_map(board: Mapping[str, Any]) -> PinMap:
    """The table's default map: the rig as wired (rev D)."""
    defaults = board["defaults"]
    return PinMap.from_words(
        {key: str(defaults[key]) for key in KEYS if key != "addr"} | {"addr": defaults["address"]}
    )


#: The rig as wired, rev D: the table's default map (``pinmap.h`` ``kDefaultMap``).
DEFAULT_MAP: PinMap = _default_map(BOARD)


def validate_map(pins: PinMap, board: Mapping[str, Any] = BOARD) -> Verdict | None:
    """The first rule *pins* breaks on *board*, as ``pinmap::validate()`` finds it.

    Role by role, in the table's order: no pin (``missing``), no GPIO of the
    chip (``no_pin``), a reserved pin (``reserved``), a pin at or above the
    role's limit (``too_high``), a pin an earlier role holds (``shared``, at
    the later role); then an address that is neither ``auto`` nor in the
    strap space (``address``) and a clock outside the I2C master's range
    (``clock``).

    :param pins: The map.
    :param board: The board's table, :data:`BOARD` by default (a test plants others).
    :returns: The problem's word and the key it is at, or None for a valid map.
    """
    rows = {row["gpio"]: row for row in board["rows"] if row["gpio"] is not None}
    roles = board["roles"]
    for index, role in enumerate(roles):
        key = role["key"]
        gpio = pins.pin(key)
        if gpio is None:
            return "missing", key
        row = rows.get(gpio)
        if row is None:
            return "no_pin", key
        if row["class"] == "reserved":
            return "reserved", key
        if gpio >= role["below"]:
            return "too_high", key
        if any(pins.pin(earlier["key"]) == gpio for earlier in roles[:index]):
            return "shared", key
    bus = board["bus"]
    if pins.addr != ADDRESS_AUTO and not bus["address_first"] <= pins.addr <= bus["address_last"]:
        return "address", "addr"
    if not bus["clock_min_hz"] <= pins.scl_hz <= bus["clock_max_hz"]:
        return "clock", "scl_hz"
    return None


#: ``SYSTem:PINS:STAGe``'s word of each role.
_STAGE_WORDS = {"sda": "SDA", "scl": "SCL", "alert": "ALER", "relay1": "REL1", "relay2": "REL2"}


def typed_change(key: str, value: object) -> int | str | None:
    """A change of one field of a map, typed for :func:`stage_units`.

    The form is checked, never the range: whether a GPIO, an address or a
    clock is one the board takes is the map's rules' to say.

    :param key: A field of :data:`KEYS`.
    :param value: A person's word (``none``, ``auto``, ``up``, ``0x41``, any
        case), or the number itself, or None for a role with no GPIO.
    :returns: The typed value.
    :raises ValueError: Saying what the field takes.
    """
    word = value.strip().lower() if isinstance(value, str) else value
    for prefix, allowed in (("pull_", PULLS), ("relay1_active", LEVELS), ("relay2_active", LEVELS)):
        if key.startswith(prefix):
            if word not in allowed:
                raise ValueError(f"{key} is {' or '.join(allowed)}")
            return str(word)
    if key in _GPIO_KEYS and word in ("none", None):
        return None
    if key == "addr" and word == "auto":
        return "auto"
    if isinstance(word, int) and not isinstance(word, bool):
        return word
    if isinstance(word, str):
        with contextlib.suppress(ValueError):
            return int(word, 0)
    raise ValueError(f"{key} takes a number")


def stage_units(staged: PinMap, changes: Mapping[str, int | str | None]) -> list[str]:
    """The units that stage *changes* over *staged*, in the map's order.

    A role's pull or level is staged with its GPIO, so a change of one of the
    two sends the other as it is staged.

    :param staged: The staged map the changes are made to.
    :param changes: Fields of :data:`KEYS` and their typed values.
    :returns: ``SYSTem:PINS:STAGe``, ``ADDRess`` and ``CLOCk`` units, as sent.
    """
    units = []
    for role, word in _STAGE_WORDS.items():
        option_key = f"{role}_active" if role in RELAYS else f"pull_{role}"
        if role not in changes and option_key not in changes:
            continue
        gpio = changes.get(role, staged.pin(role))
        option = str(changes.get(option_key, getattr(staged, option_key))).upper()
        units.append(f"SYST:PINS:STAG {word},{'NONE' if gpio is None else gpio},{option}")
    if "addr" in changes:
        address = changes["addr"]
        units.append(f"SYST:PINS:ADDR {'AUTO' if address in (0, 'auto') else address}")
    if "scl_hz" in changes:
        units.append(f"SYST:PINS:CLOC {changes['scl_hz']}")
    return units


def verdict_text(verdict: Verdict | None) -> str:
    """A verdict as ``SYSTem:PINS:PENDing?`` ends.

    :param verdict: :func:`validate_map`'s finding, or None.
    :returns: ``valid=1 problem=- at=-``, or ``valid=0`` and the problem and the key.
    """
    if verdict is None:
        return "valid=1 problem=- at=-"
    return f"valid=0 problem={verdict[0]} at={verdict[1]}"


# -- a board's wiring --------------------------------------------------------------------


#: The INA228's strap address on the rig (A0 and A1 to ground).
RIG_ADDRESS = 0x40


@dataclass(frozen=True)
class Wiring:
    """Where a board's parts really are: the pads each one is wired to.

    :ivar sda: The pad the INA228's SDA reaches.
    :ivar scl: The pad its SCL reaches.
    :ivar alert: The pad its ALERT reaches.
    :ivar address: Its strap address, 0x40 to 0x4F.
    :ivar relay1: The pad that drives Relay 1's input.
    :ivar relay1_active: The level that energizes Relay 1's module.
    :ivar relay2: The pad that drives Relay 2's input.
    :ivar relay2_active: Its module's level.
    """

    sda: int
    scl: int
    alert: int
    address: int
    relay1: int
    relay1_active: str
    relay2: int
    relay2_active: str

    @classmethod
    def parse(cls, text: str) -> Wiring:
        """A wiring from ``key=value`` pairs, comma-separated, over :data:`RIG_WIRING`.

        ``sda=18,addr=0x41`` is the rig with the INA228's SDA on GPIO18 and its
        address strapped to 0x41; an empty text is the rig. The keys are the
        fields' (``addr`` is ``address``'s short form), a pad a number, an
        address a number in any base :func:`int` reads, a level ``high`` or ``low``.

        :param text: The pairs.
        :returns: The wiring.
        :raises ValueError: On a pair that is not ``key=value``, an unknown key,
            a value that does not read, or an address outside the strap space.
        """
        changes: dict[str, int | str] = {}
        names = {f.name for f in fields(cls)} | {"addr"}
        for pair in filter(None, (part.strip() for part in text.split(","))):
            key, sep, value = pair.partition("=")
            key = key.strip().lower()
            if not sep or key not in names:
                raise ValueError(f"{pair!r}: not one of {', '.join(sorted(names))}=<value>")
            key = "address" if key == "addr" else key
            value = value.strip().lower()
            if key.endswith("_active"):
                if value not in LEVELS:
                    raise ValueError(f"{pair!r}: a relay's level is high or low")
                changes[key] = value
            else:
                changes[key] = int(value, 0)
        wiring = replace(RIG_WIRING, **changes)  # type: ignore[arg-type]  # Wiring's fields, by key
        bus = BOARD["bus"]
        if not bus["address_first"] <= wiring.address <= bus["address_last"]:
            raise ValueError(f"address 0x{wiring.address:X}: the INA228 straps 0x40 to 0x4F")
        return wiring

    def text(self) -> str:
        """The wiring as :meth:`parse` reads it.

        :returns: Every field as ``key=value``, comma-separated.
        """
        return ",".join(
            f"{f.name}=0x{self.address:02X}"
            if f.name == "address"
            else f"{f.name}={getattr(self, f.name)}"
            for f in fields(self)
        )


def wiring_argument(text: str) -> Wiring:
    """``--sim-wiring``'s pairs, as :meth:`Wiring.parse` reads them, for a command line.

    :param text: The pairs.
    :returns: The wiring.
    :raises argparse.ArgumentTypeError: Saying why the pairs do not read.
    """
    try:
        return Wiring.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


#: The rig as wired, rev D: the default map's pads, the INA228 strapped to 0x40.
RIG_WIRING = Wiring(
    sda=DEFAULT_MAP.sda,  # type: ignore[arg-type]  # the default map assigns every role
    scl=DEFAULT_MAP.scl,  # type: ignore[arg-type]  # as above
    alert=DEFAULT_MAP.alert,  # type: ignore[arg-type]  # as above
    address=RIG_ADDRESS,
    relay1=DEFAULT_MAP.relay1,  # type: ignore[arg-type]  # as above
    relay1_active=DEFAULT_MAP.relay1_active,
    relay2=DEFAULT_MAP.relay2,  # type: ignore[arg-type]  # as above
    relay2_active=DEFAULT_MAP.relay2_active,
)


#: ``*TST?``'s bits a map's bring-up can set: the INA228 did not come up, and
#: the ``ALE`` wire (STATE's pacing probe counted no edges).
TST_NO_CHIP, TST_ALERT = 1, 32


@dataclass(frozen=True)
class Proof:
    """What bringing a map up on a board finds.

    :ivar answered: Whether the INA228 answered on the map's bus and address.
    :ivar tst: The self-test's bits the map sets (:data:`TST_NO_CHIP`,
        :data:`TST_ALERT`); 0 when it passes.
    :ivar reason: ``-`` when it passes, else ``no_chip`` or ``alert``:
        ``SYSTem:PINS:RESult?``'s ``reason``.
    """

    answered: bool
    tst: int
    reason: str

    @property
    def passed(self) -> bool:
        """Whether every part passed: the map may be kept."""
        return self.tst == 0


def answers(pins: PinMap, wiring: Wiring, *, fitted: bool = True) -> bool:
    """Whether the INA228 answers on *pins*' bus: its pads, and its address or a scan.

    :param pins: The map.
    :param wiring: The board.
    :param fitted: Whether the board has an INA228 that answers at all.
    :returns: True when the bus pads are the wiring's and the address is its or a scan.
    """
    address_found = pins.addr in (ADDRESS_AUTO, wiring.address)
    return fitted and pins.sda == wiring.sda and pins.scl == wiring.scl and address_found


def prove(pins: PinMap, wiring: Wiring, *, fitted: bool = True) -> Proof:
    """Bring *pins* up on *wiring*: the INA228's answer, then its ``ALE`` wire.

    A relay pad has no proof but its own read-back, which a pad always
    passes: a relay on the wrong pad is found by :func:`relay_check` only.

    :param pins: The map.
    :param wiring: The board.
    :param fitted: Whether the board has an INA228 that answers at all.
    :returns: What the bring-up found.
    """
    if not answers(pins, wiring, fitted=fitted):
        return Proof(False, TST_NO_CHIP, "no_chip")
    if pins.alert != wiring.alert:
        return Proof(True, TST_ALERT, "alert")
    return Proof(True, 0, "-")


def rest_state(pins: PinMap, wiring: Wiring) -> tuple[bool, bool]:
    """What *pins*' relays, released, do to the board's parts on *wiring*.

    A relay is released by driving its pad to the level that is not its
    active one; a part whose pad a relay of the map drives at the level that
    energizes it is energized at rest. A part no relay drives stays released
    (its pin is parked toward release, or has no wire).

    :param pins: The map.
    :param wiring: The board.
    :returns: Whether Relay 1's part opens the lead, and whether Relay 2's
        part closes the bypass, with every relay of *pins* released.
    """
    driven = {
        pins.relay1: "low" if pins.relay1_active == "high" else "high",
        pins.relay2: "low" if pins.relay2_active == "high" else "high",
    }
    return (
        driven.get(wiring.relay1) == wiring.relay1_active,
        driven.get(wiring.relay2) == wiring.relay2_active,
    )


def relay_check(pins: PinMap, wiring: Wiring) -> str | None:
    """Which check of ``ROUTe:NULLpath ON`` fails on *wiring* with *pins*' relays.

    Relay 2 closes first: on the wrong pad, or driven at the wrong level, the
    bypass takes no current (``bypass``, error ``103``), and driving Relay 1's
    part at its level it opens the lead, which leaves the shunt nothing
    (``lost``, ``103`` too); then Relay 1 opens, and on the wrong pad or level
    the shunt still conducts (``lead``, ``104``).

    :param pins: The map.
    :param wiring: The board.
    :returns: ``bypass``, ``lost``, ``lead``, or None when both pass.
    """
    if (pins.relay2, pins.relay2_active) == (wiring.relay1, wiring.relay1_active):
        return "lost"
    if (pins.relay2, pins.relay2_active) != (wiring.relay2, wiring.relay2_active):
        return "bypass"
    if (pins.relay1, pins.relay1_active) != (wiring.relay1, wiring.relay1_active):
        return "lead"
    return None


@dataclass(frozen=True)
class Boot:
    """The map a boot runs with.

    :ivar pins: The map.
    :ivar src: Where it came from: ``default``, ``stored`` or ``fallback``.
    :ivar problem: What the boot announces as an error, or None.
    """

    pins: PinMap
    src: str
    problem: str | None = None


def boot_map(
    stored: PinMap | None, wiring: Wiring, *, fitted: bool = True, board: Mapping[str, Any] = BOARD
) -> Boot:
    """The map a boot takes from the stored record, on *wiring*.

    No record, or one the table refuses, is the default map. A stored map
    whose bus answers is taken as it is (its ALERT and relays are never
    second-guessed: nothing tells a wrong pad from an absent part). One whose
    bus is the default's has nothing to fall back to. Another whose bus does
    not answer falls back to the default bus (pads, address, clock
    and pulls) when no other role of it holds the default bus pads, and keeps
    the record; otherwise it is taken with no INA228, and the console is the
    way back (``SYSTem:PINS:DEFault``).

    :param stored: The stored map, or None for no record.
    :param wiring: The board.
    :param fitted: Whether the board has an INA228 that answers at all.
    :param board: The board's table.
    :returns: The map, its source and what to announce.
    """
    default = _default_map(board)
    if stored is None or validate_map(stored, board) is not None or stored == default:
        return Boot(default, "default")
    if answers(stored, wiring, fitted=fitted):
        return Boot(stored, "stored")
    default_bus = {key: getattr(default, key) for key in _BUS_KEYS}
    fallback = replace(stored, **default_bus)
    if fallback == stored:
        return Boot(stored, "stored")
    where = f"sda={stored.sda} scl={stored.scl} addr={address_text(stored.addr)}"
    if validate_map(fallback, board) is not None:
        return Boot(
            stored,
            "stored",
            f"the stored bus ({where}) did not answer and a role holds the default bus pins:"
            " no INA228",
        )
    return Boot(
        fallback,
        "fallback",
        f"the stored bus ({where}) did not answer: the default bus is back (the record is kept)",
    )
