# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the panel offers, declared once: its settings, profiles, routes, relays and sequences.

:data:`SCHEMA` is the one declaration the HTTP API dispatches on, the page
draws its controls from (it is served as ``/api/schema``) and the capture-
shaping guard restores from (:mod:`~kleidos_meter.panel.shaping`). Every
header it names is written as the panel sends it, short forms and all, beside
the firmware's pattern and flags where it matters; :func:`problems` holds
them to a command table, and a test passes it ``kCommands`` as read from
``firmware/src/console.cpp``, so a command the firmware renames or whose
flags change fails the host suite instead of the panel.

The flags are ``docs/scpi.md``'s letters: ``D`` needs the INA228, ``R`` is
refused while a capture or a null window runs, ``C`` also while one that
ended by itself is not yet closed, ``N`` takes no parameter. The page
disables a setting's control while its flags would refuse it.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..protocol import CHANNEL_INTEGER_KEYS, CSV_CHANNELS
from ..sim import CONVERSIONS, DEFAULT_CHANNELS
from ..sim import PROFILES as SIM_PROFILES
from ..sim_table import COMMANDS, Resolved, Row, resolve, split_header
from .guards import MUTATION_HEADER, MUTATION_VALUE
from .steps import DEFINITIONS, Param

__all__ = [
    "CHANNELS",
    "AVERAGING_SETTERS",
    "CLEARING_QUERIES",
    "HEALTH",
    "MARK_LABEL_EXCLUDED",
    "MARK_LABEL_MAX",
    "MARK_LABEL_PATTERN",
    "PIN_ORDERS",
    "PROFILES",
    "PIN_READBACK",
    "RELAY_CHECK",
    "RELAY_READBACK",
    "RELAYS",
    "ROUTES",
    "RUN_LABEL_MAX",
    "RUN_LABEL_PATTERN",
    "SCHEMA",
    "SEQUENCES",
    "SETTINGS",
    "TEXT_KEYS",
    "Argument",
    "Profile",
    "Relay",
    "Route",
    "Schema",
    "SequenceInfo",
    "Setting",
    "get_refusal",
    "problems",
    "resolve_unit",
    "short_form",
]

#: A number as SCPI writes one: NR1, NR2 or NR3.
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
#: An integer, NR1.
_INTEGER = re.compile(r"[+-]?\d{1,10}")

#: The channels ``SENSe:FUNCtion:ON`` takes, in the order the stream writes them.
CHANNELS = ("CURR", "VOLT", "POW", "TEMP")


def short_form(word: str) -> str:
    """The short form of a mnemonic as the table spells it: ``STATe`` is ``STAT``."""
    short = ""
    for char in word:
        if not (char.isupper() or char.isdigit()):
            break
        short += char
    return short or word.upper()


def _choice(value: object, choices: Sequence[str]) -> str:
    """The short form of the choice *value* names, in its long or short form, any case.

    :raises ValueError: When it names none.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # a range index given as a JSON number
    if isinstance(value, str):
        upper = value.strip().upper()
        for choice in choices:
            if upper in (choice.upper(), short_form(choice)):
                return short_form(choice)
    raise ValueError(f"one of {', '.join(choices)}")


def _number(value: object, *, integer: bool) -> str:
    """*value* as an SCPI number: a JSON number, or a string in NR1/NR2/NR3.

    :raises ValueError: For anything else (a boolean included).
    """
    if isinstance(value, bool):
        raise ValueError("an integer" if integer else "a number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and not integer and math.isfinite(value):
        return repr(value)
    if isinstance(value, str) and (_INTEGER if integer else _NUMBER).fullmatch(value.strip()):
        return value.strip()
    raise ValueError("an integer" if integer else "a number")


@dataclass(frozen=True)
class Setting:
    """One setting the panel reads and writes.

    :ivar name: Its key in the API and in a snapshot.
    :ivar label: What the page calls it.
    :ivar query: The query that reads it, as sent.
    :ivar setter: The unit that writes it, as sent, with ``{}`` for the value.
    :ivar pattern: The setter's pattern in ``kCommands``; the query's is it and ``?``.
    :ivar flags: The setter's flag letters in ``kCommands``.
    :ivar kind: How a value is checked: ``choice``, ``integer``, ``number``,
        ``number_or_off``, ``boolean`` or ``channels``.
    :ivar choices: For a ``choice``, the words as the table spells them; for a
        ``number``, the values the instrument takes when it takes only some
        (which it says itself, with ``-224``).
    :ivar unit: The unit a number is in, as SCPI writes it.
    :ivar writable: False for a setting the panel shows and never writes.
    :ivar changed_by: The table patterns whose units change it (``*RST`` aside).
    :ivar summary: One line for the page.
    :ivar per_profile: Whether the instrument keeps one for each profile, the
        query and the setter being the live profile's.
    """

    name: str
    label: str
    query: str
    setter: str
    pattern: str
    flags: str
    kind: str
    choices: tuple[str, ...] = ()
    unit: str = ""
    writable: bool = True
    changed_by: frozenset[str] = field(default_factory=frozenset)
    summary: str = ""
    per_profile: bool = False

    def write_unit(self, value: str) -> str:
        """The unit that writes *value*, an answer of :attr:`query` or a checked parameter."""
        if self.name == "channels" and value == '""':
            return "SENS:FUNC:ON:DEF"  # a ring profile's: the default is the only choice
        return self.setter.format(value)

    def parameter(self, value: object) -> str:
        """The parameter that writes *value*, checked for its kind, never anything else.

        A value is checked for its form only (a word of the choices, a number,
        a channel list); whether it is in range is the instrument's to say, and
        it says so with ``-222``. Nothing in the result can end the unit or
        start another: no ``;``, no newline, no stray quote.

        :param value: As the API's caller gave it: a string, a number, a
            boolean or, for the channels, a list of channel names or ``DEFault``.
        :returns: The parameter, ready for :meth:`write_unit`.
        :raises ValueError: Naming what the setting takes.
        """
        if self.kind == "choice":
            return _choice(value, self.choices)
        if self.kind in ("integer", "number"):
            return _number(value, integer=self.kind == "integer")
        if self.kind == "number_or_off":
            if isinstance(value, str) and value.strip().upper() == "OFF":
                return "OFF"
            return _number(value, integer=False)
        if self.kind == "boolean":
            return _boolean(value)
        return _channels(value)


def _boolean(value: object) -> str:
    """``ON`` or ``OFF`` for a JSON boolean or the words SCPI takes.

    :raises ValueError: For anything else.
    """
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, str) and value.strip().upper() in ("ON", "OFF", "1", "0"):
        return "ON" if value.strip().upper() in ("ON", "1") else "OFF"
    raise ValueError("true or false")


def _channels(value: object) -> str:
    """The channel list ``SENSe:FUNCtion:ON`` takes, or ``""`` for the profile's default.

    :raises ValueError: For anything but a list of distinct channel names, or ``DEFault``.
    """
    if isinstance(value, str) and value.strip().upper() in ("DEF", "DEFAULT"):
        return '""'
    if not isinstance(value, list) or not value:
        raise ValueError(f"a list of {', '.join(CHANNELS)}, or DEFault")
    names = [item.strip().upper() if isinstance(item, str) else "" for item in value]
    if any(name not in CHANNELS for name in names) or len(set(names)) != len(names):
        raise ValueError(f"a list of distinct {', '.join(CHANNELS)}")
    return ",".join(f'"{name}"' for name in names)


#: What ``CONFigure`` changes: the function's channel, its profile and its range.
_CONFIGURE = frozenset(
    {
        "CONFigure[:SCALar]:CURRent[:DC]",
        "CONFigure[:SCALar]:VOLTage[:DC]",
        "CONFigure[:SCALar]:POWer[:DC]",
        "CONFigure[:SCALar]:ENERgy",
        "CONFigure[:SCALar]:TEMPerature",
    }
)

#: The ``MEASure`` queries that configure first: a range, or ``STATE`` for the bus.
_MEASURE = frozenset(
    {
        "MEASure[:SCALar]:CURRent[:DC]?",
        "MEASure[:SCALar]:VOLTage[:DC]?",
        "MEASure[:SCALar]:POWer[:DC]?",
        "MEASure[:SCALar]:ENERgy?",
    }
)

#: What changes the live profile's averaging override. Another profile changes
#: what the queries answer, and nothing any profile keeps.
AVERAGING_SETTERS = frozenset(
    {"SENSe:AVERage[:STATe]", "SENSe:AVERage:COUNt", "SENSe:CURRent:APERture"}
)

#: The settings, in the order they are written: each write valid when it is made
#: (the I2C clock first, ``BURST`` needs it; the profile before the channels it resets).
SETTINGS: tuple[Setting, ...] = (
    Setting(
        "i2c_hz",
        "I2C clock",
        "SYST:COMM:I2C:FREQ?",
        "SYST:COMM:I2C:FREQ {}",
        "SYSTem:COMMunicate:I2C:FREQuency",
        "DR",
        "integer",
        unit="Hz",
        changed_by=frozenset({"SYSTem:COMMunicate:I2C:FREQuency"}),
        summary="the INA228 bus, 50000..1000000 Hz; BURST needs 1000000",
    ),
    Setting(
        "profile",
        "Profile",
        "SENS:PROF?",
        "SENS:PROF {}",
        "SENSe:PROFile",
        "DRC",
        "choice",
        ("TRACe", "STATe", "SLEep", "BURSt"),
        changed_by=frozenset({"SENSe:PROFile"}) | _CONFIGURE | _MEASURE,
        summary="the sampling profile; resets the channels to its default",
    ),
    Setting(
        "channels",
        "Channels",
        "SENS:FUNC:ON?",
        "SENS:FUNC:ON {}",
        "SENSe:FUNCtion:ON",
        "R",
        "channels",
        CHANNELS,
        changed_by=frozenset({"SENSe:FUNCtion:ON", "SENSe:FUNCtion:ON:DEFault", "SENSe:PROFile"})
        | _CONFIGURE
        | _MEASURE,
        summary="what a capture streams; none on a ring profile",
    ),
    Setting(
        "range",
        "Current range",
        "SENS:CURR:RANG:IND?",
        "SENS:CURR:RANG:IND {}",
        "SENSe:CURRent:RANGe:INDex",
        "DRC",
        "choice",
        ("0", "1"),
        changed_by=frozenset({"SENSe:CURRent:RANGe[:UPPer]", "SENSe:CURRent:RANGe:INDex"})
        | _CONFIGURE
        | _MEASURE,
        summary="ADCRANGE: 0 is ±163.84 mV (1.6384 A), 1 is ±40.96 mV (0.4096 A)",
    ),
    Setting(
        "capture",
        "Capture keeps",
        "SENS:CAPT?",
        "SENS:CAPT {}",
        "SENSe:CAPTure",
        "R",
        "choice",
        ("DATA", "STATistics"),
        changed_by=frozenset({"SENSe:CAPTure"}),
        summary="DATA streams or fills the ring; STATistics keeps the statistics only",
    ),
    Setting(
        "limit",
        "Capture limit",
        "SENS:CAPT:LIM?",
        "SENS:CAPT:LIM {}",
        "SENSe:CAPTure:LIMit",
        "R",
        "boolean",
        changed_by=frozenset({"SENSe:CAPTure:LIMit"}),
        summary="ON ends a capture by itself after the sample count",
    ),
    Setting(
        "threshold",
        "Current threshold",
        "SENS:CURR:THR?",
        "SENS:CURR:THR {}",
        "SENSe:CURRent:THReshold",
        "R",
        "number_or_off",
        unit="A",
        changed_by=frozenset({"SENSe:CURRent:THReshold"}),
        summary="a level the next captures count against, or OFF",
    ),
    Setting(
        "hysteresis",
        "Threshold hysteresis",
        "SENS:CURR:THR:HYST?",
        "SENS:CURR:THR:HYST {}",
        "SENSe:CURRent:THReshold:HYSTeresis",
        "R",
        "number",
        unit="A",
        changed_by=frozenset({"SENSe:CURRent:THReshold:HYSTeresis"}),
        summary="how far below the level the count falls back",
    ),
    Setting(
        "threshold_marks",
        "Threshold marks",
        "SENS:CURR:THR:MARK?",
        "SENS:CURR:THR:MARK {}",
        "SENSe:CURRent:THReshold:MARK",
        "R",
        "boolean",
        changed_by=frozenset({"SENSe:CURRent:THReshold:MARK"}),
        summary="ON marks each crossing of the threshold in a streamed capture",
    ),
    Setting(
        "sample_count",
        "Sample count",
        "SAMP:COUN?",
        "SAMP:COUN {}",
        "SAMPle:COUNt",
        "R",
        "integer",
        changed_by=frozenset({"SAMPle:COUNt"}),
        summary="samples one acquisition takes, and the limit's count",
    ),
    Setting(
        "trigger_source",
        "Trigger source",
        "TRIG:SOUR?",
        "TRIG:SOUR {}",
        "TRIGger:SOURce",
        "R",
        "choice",
        ("IMMediate", "LEVel"),
        changed_by=frozenset({"TRIGger:SOURce"}),
        summary="what starts a ring capture's record",
    ),
    Setting(
        "trigger_level",
        "Trigger level",
        "TRIG:LEV?",
        "TRIG:LEV {}",
        "TRIGger:LEVel",
        "R",
        "number",
        unit="A",
        changed_by=frozenset({"TRIGger:LEVel"}),
        summary="the current a level trigger fires at",
    ),
    Setting(
        "trigger_slope",
        "Trigger slope",
        "TRIG:SLOP?",
        "TRIG:SLOP {}",
        "TRIGger:SLOPe",
        "R",
        "choice",
        ("POSitive", "NEGative"),
        changed_by=frozenset({"TRIGger:SLOPe"}),
        summary="upwards or downwards through the level",
    ),
    Setting(
        "pretrigger",
        "Pretrigger",
        "TRIG:PRET?",
        "TRIG:PRET {}",
        "TRIGger:PRETrigger",
        "R",
        "integer",
        changed_by=frozenset({"TRIGger:PRETrigger"}),
        summary="samples kept from before the crossing",
    ),
    Setting(
        "average_count",
        "Averaging count",
        "SENS:AVER:COUN?",
        "SENS:AVER:COUN {}",
        "SENSe:AVERage:COUNt",
        "DRC",
        "choice",
        ("1", "4", "16", "64", "128", "256", "512", "1024"),
        changed_by=AVERAGING_SETTERS,
        summary="conversions averaged into one sample of the live profile, with averaging ON",
        per_profile=True,
    ),
    Setting(
        "average_aperture",
        "Shunt conversion time",
        "SENS:CURR:APER?",
        "SENS:CURR:APER {}",
        "SENSe:CURRent:APERture",
        "DRC",
        "number",
        ("0.00005", "0.000084", "0.00015", "0.00028", "0.00054", "0.001052", "0.002074", "0.00412"),
        unit="s",
        changed_by=AVERAGING_SETTERS,
        summary="the time one conversion of the shunt takes on the live profile, with averaging ON",
        per_profile=True,
    ),
    Setting(
        "average",
        "Averaging in the INA228",
        "SENS:AVER?",
        "SENS:AVER {}",
        "SENSe:AVERage[:STATe]",
        "DRC",
        "boolean",
        changed_by=AVERAGING_SETTERS,
        summary="changes the live profile's measurement: ON converts with the count and the time"
        " above instead of its own; OFF at every boot, refused on BURST",
        per_profile=True,
    ),
    Setting(
        "format",
        "Stream format",
        "FORM?",
        "FORM {}",
        "FORMat[:DATA]",
        "R",
        "choice",
        ("TEXT", "CSV"),
        writable=False,  # the host parses TEXT; serve never changes it
        changed_by=frozenset({"FORMat[:DATA]"}),
        summary="the stream record format, shown; serve never changes it",
    ),
)


#: The profile with no averaging: its 50 us conversion is its definition, and
#: the instrument refuses the settings on it (``-221``).
_OWN_CONVERSION = "BURSt"


@dataclass(frozen=True)
class Profile:
    """A sampling profile, as the page needs to know it before it starts a capture.

    :ivar name: Its name as the ``profile`` setting's choices spell it (``STATe``).
    :ivar period_us: Its sample period, microseconds.
    :ivar ring: Whether a capture fills the instrument's ring (read at its
        end) instead of streaming its samples.
    :ivar channels: What a streamed capture carries when ``SENSe:FUNCtion:ON``
        named none, as ``CHANNELS`` names them; none on a ring profile.
    :ivar columns: The same as the live events' integer columns (``i_na``, ...).
    :ivar averaging: Whether it takes the averaging settings.
    :ivar count: The conversions it averages into a sample of its own.
    :ivar conversion_us: Its own shunt conversion time, microseconds.
    :ivar other_us: What the other inputs it converts (the bus, the die) add to
        each conversion, microseconds. With averaging on, a sample takes
        ``(conversion + other_us) * count`` of the settings' conversion and count.
    """

    name: str
    period_us: int
    ring: bool
    channels: tuple[str, ...]
    columns: tuple[str, ...]
    averaging: bool
    count: int
    conversion_us: int
    other_us: int


def _profiles() -> tuple[Profile, ...]:
    """The profile setting's choices, each with the firmware's period and sink.

    The figures are the simulator's copy of the firmware's profile table
    (:data:`kleidos_meter.sim.PROFILES`, ``CONVERSIONS``, ``DEFAULT_CHANNELS``).
    """
    choices = next(item.choices for item in SETTINGS if item.name == "profile")
    found: list[Profile] = []
    for choice in choices:
        key = next(key for key in SIM_PROFILES if key.startswith(short_form(choice)))
        period_us, ring = SIM_PROFILES[key]
        stream = () if ring else DEFAULT_CHANNELS.get(key, ())
        channels = tuple(CHANNELS[CSV_CHANNELS.index(name)] for name in stream)
        columns = tuple(CHANNEL_INTEGER_KEYS[name] for name in stream)
        averaging = choice != _OWN_CONVERSION
        _, _, bus_us, conversion_us, die_us, count = CONVERSIONS[key]
        found.append(
            Profile(
                choice,
                period_us,
                ring,
                channels,
                columns,
                averaging,
                count,
                conversion_us,
                bus_us + die_us,
            )
        )
    return tuple(found)


#: The profiles, in the order of the ``profile`` setting's choices.
PROFILES: tuple[Profile, ...] = _profiles()


@dataclass(frozen=True)
class Relay:
    """One relay order the panel sends, and never with a flag the caller did not set.

    :ivar name: Its key in ``/api/relays/<name>``.
    :ivar label: What the page calls it.
    :ivar header: The order's header, as sent.
    :ivar pattern: Its pattern in ``kCommands``.
    :ivar choices: The words its first parameter takes; empty for a duration.
    :ivar options: The flags it takes, in the order they are written.
    :ivar wired: False for a relay the rig does not wire (the instrument refuses it).
    :ivar summary: One line for the page.
    """

    name: str
    label: str
    header: str
    pattern: str
    choices: tuple[str, ...]
    options: tuple[str, ...]
    wired: bool = True
    summary: str = ""

    def parameter(self, value: object) -> str:
        """The order's first parameter: a word of :attr:`choices`, or a duration in ms.

        :raises ValueError: Naming what it takes.
        """
        if self.choices:
            return _choice(value, self.choices)
        return _number(value, integer=True)


#: The relay orders, by name.
RELAYS: tuple[Relay, ...] = (
    Relay(
        "lead",
        "Relay 1, the DUT's battery lead",
        "ROUT:LEAD",
        "ROUTe:LEAD",
        ("OPEN", "CLOSe"),
        ("FORCe", "LIVE"),
        summary="OPEN leaves the DUT without its battery: FORCe is required",
    ),
    Relay(
        "lead_cycle",
        "Relay 1 opened and reclosed",
        "ROUT:LEAD:CYCL",
        "ROUTe:LEAD:CYCLe",
        (),
        ("FORCe", "LIVE"),
        summary="Relay 1 held open for 1..60000 ms from path=shunt; FORCe is required",
    ),
    Relay(
        "bypass",
        "Relay 2, the bypass across the shunt",
        "ROUT:BYP",
        "ROUTe:BYPass",
        ("ON", "OFF"),
        ("FORCe", "LIVE"),
        summary="ON takes the shunt off the DUT's current: FORCe under a capture",
    ),
    Relay(
        "nullpath",
        "The null path (Relay 2 then Relay 1)",
        "ROUT:NULL",
        "ROUTe:NULLpath",
        ("ON", "OFF"),
        ("LIVE", "FORCe"),
        summary="ON closes Relay 2 and opens Relay 1, make-before-break, checking each step",
    ),
    Relay(
        "relay3",
        "Relay 3, spare",
        "ROUT:REL3",
        "ROUTe:RELay#",
        ("ON", "OFF"),
        ("LIVE",),
        wired=False,
        summary="not wired on the rig: the instrument refuses the order",
    ),
    Relay(
        "relay4",
        "Relay 4, spare",
        "ROUT:REL4",
        "ROUTe:RELay#",
        ("ON", "OFF"),
        ("LIVE",),
        wired=False,
        summary="not wired on the rig: the instrument refuses the order",
    ),
)

#: What every relay route reads after its order, in the order's own message.
RELAY_READBACK = ("ROUT:RES?", "ROUT:STAT?")


@dataclass(frozen=True)
class SequenceInfo:
    """A measurement sequence the panel offers as one button.

    :ivar name: Its key.
    :ivar title: Its button's label.
    :ivar summary: What it does, in a line.
    :ivar uses: The headers it sends, as sent, held to the table.
    :ivar params: What a person may set, each with its default and bounds.
    """

    name: str
    title: str
    summary: str
    uses: tuple[str, ...]
    params: tuple[Param, ...] = ()


#: The sequences, in the page's order, as :mod:`~kleidos_meter.panel.steps` defines them.
SEQUENCES: tuple[SequenceInfo, ...] = tuple(
    SequenceInfo(d.name, d.title, d.summary, d.uses(), d.params) for d in DEFINITIONS
)


#: The pin map's orders: what they change outlives serve on purpose. The map is
#: no capture-shaping setting (it is not in :data:`SETTINGS`): it is never
#: snapshotted, and never written back on the way out. Its boot clock is the
#: bus's from the order on, so the guard adopts it
#: (:meth:`~kleidos_meter.panel.shaping.ShapingGuard.adopt_bus_clock`).
PIN_ORDERS = frozenset({"SYSTem:PINS:APPLy", "SYSTem:PINS:DEFault"})

#: What every pin route reads: the live map, the staged one, the last order's result.
PIN_READBACK = ("SYST:PINS?", "SYST:PINS:PEND?", "SYST:PINS:RES?")

#: The null-path check a change that moved a relay ends with.
RELAY_CHECK = ("ROUT:NULL ON", "ROUT:NULL OFF")

#: What applying a map sends besides its order.
_PIN_ORDER_USES = (*PIN_READBACK, *RELAY_READBACK, *RELAY_CHECK, "SYST:COMM:I2C:FREQ?")

#: What an order that changes the pin map answers.
_PIN_ORDER_ANSWER = (
    "`order` (the unit sent), `errors`, `result` (the instrument's report of it: `ok`, or"
    " `reverted` with the last good map back, 409), `live` and `pending` after it, and `check`,"
    " null unless a relay moved: the null-path check made on its new pad, `passed` (the live"
    " map then reads `verified` 1), its `errors`, its `result`, the circuit's `state` and a"
    " `detail`. Refused unsent while a sequence (409 `sequence_running`) or a capture (409"
    " `capture_running`, `window_open`) runs, while another controller holds the line (423"
    " `held`) and while a relay is not at rest (409 `relays_not_at_rest`, with the `state`)"
)

#: The most characters a run's label keeps (``meta.json``): printable ASCII.
RUN_LABEL_MAX = 80

#: A run's label as an HTML field's ``pattern`` writes it: printable ASCII.
RUN_LABEL_PATTERN = "[ -~]*"

#: The most characters ``TRACe:MARK`` takes in a label.
MARK_LABEL_MAX = 15

#: The characters a mark's label may not hold besides a space: the firmware's
#: separators and quotes.
MARK_LABEL_EXCLUDED = ",;=\"'"

#: A mark's label as an HTML field's ``pattern`` writes it: printable ASCII less
#: the space and :data:`MARK_LABEL_EXCLUDED`, 1 to :data:`MARK_LABEL_MAX` of them.
MARK_LABEL_PATTERN = f"[[!-~]--[{MARK_LABEL_EXCLUDED}]]{{1,{MARK_LABEL_MAX}}}"


@dataclass(frozen=True)
class Argument:
    """One thing a route takes, as a person calling it needs to know it.

    :ivar name: Its key: a segment of the path, a query parameter, a key of
        the JSON body, or ``body`` for a body of plain text.
    :ivar where: ``path``, ``query``, ``json`` or ``text``.
    :ivar takes: What its value is, in a line.
    :ivar required: Whether the request is refused without it.
    :ivar max_length: The most characters a text value may have, when the route
        bounds it; the page's field takes no more.
    :ivar pattern: The characters a text value may hold, as an HTML field's
        ``pattern`` attribute writes them (a whole-value match, with the ``v``
        flag's syntax), when the route bounds them; the page's field holds it.
    """

    name: str
    where: str
    takes: str
    required: bool = False
    max_length: int | None = None
    pattern: str | None = None


@dataclass(frozen=True)
class Route:
    """One route of the HTTP API.

    :ivar method: ``GET`` or ``POST``.
    :ivar path: Its path; ``{name}`` stands for one segment.
    :ivar mutates: Whether it may change the instrument (POST only, guarded).
    :ivar summary: What it does, in a line.
    :ivar uses: The headers it sends, as sent, held to the table.
    :ivar sends: Whether it talks to the instrument at all; one that does
        not (the schema, the status the polls keep) is served while another
        controller holds the line.
    :ivar takes: What it takes beside its path's fixed part.
    :ivar answers: What a 200 holds, in a line.
    """

    method: str
    path: str
    mutates: bool
    summary: str
    uses: tuple[str, ...] = ()
    sends: bool = True
    takes: tuple[Argument, ...] = ()
    answers: str = ""


#: What ``GET /api/health`` answers, by key, and the query each comes from.
HEALTH: tuple[tuple[str, str], ...] = (
    ("health", "SYST:HEAL?"),
    ("heap", "DIAG:HEAP?"),
    ("watchdog", "DIAG:WATC?"),
    ("info", "SYST:INF?"),
    ("calibration", "CAL:DATA?"),
)

#: What a route that shows the shunt or the registry's verdict reads.
_SHUNT_READ = ("*IDN?", "SYST:INF?", "CAL:DATA?")


def _override(where: str) -> tuple[Argument, ...]:
    """What states an override of a range's shunt, in the query or in the body."""
    one = "the value, once"
    once = "its uncertainty, exactly one of the four"
    return (
        Argument("range", where, "0 or 1", required=True),
        Argument("ohms", where, f"ohms ({one}: `ohms` or `uohm`)"),
        Argument("uohm", where, f"micro-ohms ({one}: `ohms` or `uohm`)"),
        Argument("expanded_ppm", where, f"`U` at `k = 2`, ppm ({once})"),
        Argument("expanded_uohm", where, f"`U` at `k = 2`, micro-ohms ({once})"),
        Argument("tolerance_ppm", where, f"a rectangular limit, ppm ({once})"),
        Argument("tolerance_uohm", where, f"a rectangular limit, micro-ohms ({once})"),
        Argument("note", where, "where the figures come from, printable ASCII, at most 200"),
    )


#: The run a run route names.
_RUN = Argument("run", "path", "the run's id, as `GET /api/runs` lists it", required=True)

#: What every sequence route answers.
_SEQUENCE_STATE = (
    "`state` (`idle`, `running`, `prompt`, `cleanup`), `sequence`, `number`, `params`, `outcome`"
    " and `detail` once it ended, `prompt` (`id`, `text`, `choices`) while it asks, `total` and the"
    " `steps` begun, the `cleanup`'s, the `results` kept, the `run` of its capture, `elapsed_s`"
)

#: What a capture route answers while one runs.
_CAPTURE_STATE = (
    "`state` (`idle`, `starting`, `running`, `stopping`), `run`, `data_dir` (relative to"
    " serve's working directory or the repository, else its last two components: never an"
    " absolute path), and `profile`, `seconds`, `elapsed_s`, `samples`, `marks` and"
    " `noise_reduction` (the INA228's averaging, null until the capture has started) of the"
    " capture"
)

#: The routes.
ROUTES: tuple[Route, ...] = (
    Route(
        "GET",
        "/api/schema",
        False,
        "this schema",
        sends=False,
        answers="the settings, profiles, relay orders, sequences and routes declared here,"
        " `mutation_header`, `clearing_queries`, `pin_orders` (what they change outlives serve)"
        " and `text_keys`",
    ),
    Route(
        "GET",
        "/api/status",
        False,
        "the line, the executor and the last status poll; sends nothing",
        sends=False,
        answers="`link`, `polling`, `in_flight`, `queued`, `held` (who holds the line, or null),"
        " `passthrough` (its URL, its client and the last one's outcome), `status` and `health`"
        " (the last poll's reports), `instrument`, `foreign_window` (a window serve did not"
        " open), `reboots`, `capture` and `live`",
    ),
    Route(
        "GET",
        "/api/health",
        False,
        "health, heap, watchdog, identity and calibration reports",
        tuple(query for _, query in HEALTH),
        answers="one report a key (" + ", ".join(f"`{key}`" for key, _ in HEALTH) + "), and"
        " `local_elf`: the local build's `elf` against the instrument's, `matches` null when"
        " either has none; its `path` is relative to serve's working directory or the"
        " repository, else the build's last two components: never an absolute path",
    ),
    Route(
        "GET",
        "/api/query",
        False,
        "queries that read without acting, as JSON",
        takes=(Argument("q", "query", "one message of queries", required=True),),
        answers="`message`, `units` (each answer as the instrument wrote it), `fields` (each"
        " report as an object, `{}` for an answer that is none), `errors`, `elapsed_s`",
    ),
    Route(
        "POST",
        "/api/scpi",
        True,
        "program messages, one per line, each with its errors",
        takes=(
            Argument(
                "body", "text", "program messages, one per line, never a ring dump", required=True
            ),
        ),
        answers="`replies`, one a message: `message`, `units`, `errors`, `elapsed_s`. It is a"
        " transport: 200 whatever the instrument queued. The messages run in order; when one"
        " after the first fails (a time-out, a full queue, the line down or handed over), the"
        " answer is still 200, with the `replies` of those that ran, `failed` (`message`,"
        " `error`, `detail`) and `unsent`, the messages never sent, and no `Retry-After`: the"
        " ones that ran have changed the instrument",
    ),
    Route(
        "GET",
        "/api/settings",
        False,
        "every setting as the instrument answers it, and each range's shunt with its uncertainty",
        _SHUNT_READ,
        answers="`settings` (each as its query answers it, text), and `shunt`: the registry's"
        " `verdict`, `quotable`, the `banner`, and for each of the two `ranges` the value it"
        " converts with, its `source`, its `uncertainty`, the `registry`'s figures, the"
        " `deviation_ppm` and the `override`",
    ),
    Route(
        "POST",
        "/api/settings",
        True,
        "settings written, then read back from the instrument",
        takes=(
            Argument(
                "<setting>", "json", "a value of the form its setting takes, one or more settings"
            ),
        ),
        answers="`written` (the units sent, in the settings' order), `errors`, and `settings`:"
        " what was written, read back (a profile reads its channels and its averaging back too)",
    ),
    Route(
        "GET",
        "/api/settings/shunt/preview",
        False,
        "what an override would write, against the registry's figure",
        _SHUNT_READ,
        takes=_override("query"),
        answers="`unit` (what would be sent), `uncertainty`, `device_uohm`, `registry`,"
        " `deviation_ppm`, `normalized_error`, `quotable` and `banner`",
    ),
    Route(
        "POST",
        "/api/settings/shunt",
        True,
        "an override with its uncertainty and confirm: true, written and kept on the host",
        (*_SHUNT_READ, "SENS:CURR:SHUN:RANG1"),
        takes=(*_override("json"), Argument("confirm", "json", "`true`", required=True)),
        answers="`written` (the unit sent), the `override` kept, and `shunt` after it",
    ),
    Route(
        "GET",
        "/api/calibration",
        False,
        "the registry's verdict on the instrument",
        _SHUNT_READ,
        answers="`verdict`: `status` (`applied` is the one to measure on), `ok`, `serial`,"
        " `event`, `differences`, `warnings` and its `lines`; and `registry_problem`",
    ),
    Route(
        "POST",
        "/api/calibration/restore",
        True,
        "the registry's working copy written back (the revert), the host's overrides removed",
        (*_SHUNT_READ, "SENS:CURR:SHUN", "SENS:CURR:SHUN:RANG1", "CAL:TAG", "CAL:DATE", "CAL:VBUS"),
        answers="the verdicts `before` and `after`, whether anything was `written`, what it"
        " `said`, `cleared_override`; 409 when the registry has no copy for the instrument",
    ),
    Route(
        "GET",
        "/api/sequences",
        False,
        "the sequence running or the last one",
        sends=False,
        answers=_SEQUENCE_STATE,
    ),
    Route(
        "POST",
        "/api/sequences/run",
        True,
        "one sequence started, its steps as data, its cleanup always run",
        takes=(
            Argument("name", "json", "the sequence's name", required=True),
            Argument("params", "json", "an object of its parameters, each an integer"),
        ),
        answers="the same as `GET /api/sequences`, as the sequence began",
    ),
    Route(
        "POST",
        "/api/sequences/answer",
        True,
        "the running sequence's question answered",
        sends=False,
        takes=(
            Argument("id", "json", "the question's `id`", required=True),
            Argument("answer", "json", "one of its `choices`", required=True),
        ),
        answers="the same as `GET /api/sequences`",
    ),
    Route(
        "POST",
        "/api/sequences/abort",
        True,
        "the running sequence aborted: its wait ends, its cleanup runs",
        sends=False,
        answers="the same as `GET /api/sequences`; 409 when none runs",
    ),
    Route(
        "GET",
        "/api/relays",
        False,
        "ROUTe:STATe? and ROUTe:RESult?, the circuit and the last order",
        RELAY_READBACK,
        answers="`result` and `state`, the two reports",
    ),
    Route(
        "POST",
        "/api/relays/{relay}",
        True,
        "one relay order, with only the flags given, its result and the circuit after it",
        RELAY_READBACK,
        takes=(
            Argument("relay", "path", "a relay order's name", required=True),
            Argument("state", "json", "a word of its choices; an order with none takes `ms`"),
            Argument("ms", "json", "a duration, milliseconds, for an order with no choices"),
            Argument("force", "json", "`true` adds `FORCe`"),
            Argument("live", "json", "`true` adds `LIVE`"),
        ),
        answers="`order` (the unit sent), `errors`, and the `result` and `state` after it",
    ),
    Route(
        "GET",
        "/api/pins",
        False,
        "the board's pins, the live pin map, the staged one and the last change's result",
        PIN_READBACK,
        answers="`board` (the package's pin table: every terminal, its class and its reason, the"
        " roles and the defaults), `table` (`package` and `instrument`, the table's digest on"
        " each side, and whether it `matches`: when it does not, staging and applying are"
        " refused), `live`, `pending` and `result` (the instrument's reports) and `differs`"
        " (the fields staged otherwise than they run)",
    ),
    Route(
        "POST",
        "/api/pins/stage",
        True,
        "fields of the pin map staged and judged; nothing on the board changes",
        (*PIN_READBACK[:2], "SYST:PINS:STAG", "SYST:PINS:ADDR", "SYST:PINS:CLOC", "SYST:PINS:CLE"),
        takes=(
            Argument(
                "<field>",
                "json",
                "a field of the map, one or more: a role's GPIO or `null`, a pull (`up`, `off`),"
                " a relay's active level (`high`, `low`), `addr` (`auto` or a number), `scl_hz`",
            ),
            Argument("clear", "json", "`true` starts from the live map, the staged one dropped"),
        ),
        answers="`sent` (the units), `errors` and `pending`, the staged map with its verdict"
        " (`valid`, and when 0 the `problem` and the field it is `at`)",
    ),
    Route(
        "POST",
        "/api/pins/apply",
        True,
        "the staged map made the live one: proved on the board and kept, or reverted",
        (*_PIN_ORDER_USES, "SYST:PINS:APPL"),
        answers=_PIN_ORDER_ANSWER + "; 409 `table_differs` when the instrument's table is another",
    ),
    Route(
        "POST",
        "/api/pins/defaults",
        True,
        "the rig's own pin map back, with the same checks; it needs no INA228",
        (*_PIN_ORDER_USES, "SYST:PINS:DEF"),
        answers=_PIN_ORDER_ANSWER,
    ),
    Route(
        "POST",
        "/api/abort",
        True,
        "ABORt, ahead of the queue under an acquisition; a running sequence is aborted too",
        ("ABOR",),
        answers="`sent`, the message's reply, and `sequence` (the one aborted, or null)",
    ),
    Route(
        "GET",
        "/api/events",
        False,
        "Server-Sent Events: samples, marks, status, errors, sequences and the ring",
        sends=False,
        answers="an event stream, resumed after the `Last-Event-ID` header when it is sent; 503"
        " while eight others are open",
    ),
    Route(
        "GET",
        "/api/capture",
        False,
        "the recorder: the capture serve owns, or the last one",
        sends=False,
        answers=_CAPTURE_STATE + "; `last`: the last run's summary",
    ),
    Route(
        "POST",
        "/api/capture/start",
        True,
        "a capture configured, started and recorded into a run",
        ("SENS:PROF", "SENS:FUNC:ON", "SENS:FUNC:ON:DEF", "INIT", "FETC:HEAD?"),
        takes=(
            Argument("profile", "json", "a profile's name, long or short", required=True),
            Argument(
                "channels",
                "json",
                f"a list of {', '.join(f'`{name}`' for name in CHANNELS)}, or `DEFault`; the"
                " instrument's as they stand when absent",
            ),
            Argument(
                "seconds",
                "json",
                "stop it after so long, at most 604800 (a week); until it is stopped when absent",
            ),
            Argument(
                "label",
                "json",
                f"a note kept in the run, printable ASCII, at most {RUN_LABEL_MAX}",
                max_length=RUN_LABEL_MAX,
                pattern=RUN_LABEL_PATTERN,
            ),
        ),
        answers=_CAPTURE_STATE + ", as it began",
    ),
    Route(
        "POST",
        "/api/capture/stop",
        True,
        "the capture closed, its statistics and its ring read, its run finished",
        ("ABOR", "FETC:STAT?", "FETC:HEAD?", "TRAC:POIN?", "TRAC:DATA?"),
        answers="`run`, its `summary` and its `meta` (`meta.json`)",
    ),
    Route(
        "POST",
        "/api/capture/mark",
        True,
        "a mark in the running capture",
        ("TRAC:MARK",),
        takes=(
            Argument(
                "label",
                "json",
                f"1 to {MARK_LABEL_MAX} printable characters, no space and none of"
                " `,` `;` `=` `\"` `'`",
                required=True,
                max_length=MARK_LABEL_MAX,
                pattern=MARK_LABEL_PATTERN,
            ),
        ),
        answers="`label` and the instrument's `errors`",
    ),
    Route(
        "GET",
        "/api/runs",
        False,
        "the recorded runs, newest first",
        sends=False,
        takes=(Argument("limit", "query", "how many, 1 to 1000 (100)"),),
        answers="`runs`, a summary each: `id`, `state`, `complete`, `profile`, `format`, `label`,"
        " `started`, `ended`, `ended_by`, `samples`, `dropped`, `stream_samples`, `marks`,"
        " `trace`, `noise_reduction` (the INA228's averaging, as `meta.json` has it: its `state`,"
        " `on` or `off`, the `count`, `conversion_us` and `period_us`; null when unknown), and"
        " `recording` (true for the run serve is still writing)",
    ),
    Route(
        "GET",
        "/api/runs/{run}",
        False,
        "a run's meta.json",
        sends=False,
        takes=(_RUN,),
        answers="the run's `meta.json`",
    ),
    Route(
        "GET",
        "/api/runs/{run}/export",
        False,
        "a run's records as a download",
        sends=False,
        takes=(_RUN, Argument("format", "query", "`text`, `csv` or `json` (`csv`)")),
        answers="the file, named `<run>.txt`, `<run>.csv` or `<run>.json`; 409 (`recording`)"
        " while the run is still being written, since an export is made once and kept",
    ),
    Route(
        "GET",
        "/api/runs/{run}/decimated",
        False,
        "a run's count, mean, root mean square, minimum and maximum per bucket, for a chart",
        sends=False,
        takes=(
            _RUN,
            Argument("column", "query", "`i_na`, `v_uv`, `p_uw` or `die_mc` (`i_na`)"),
            Argument("buckets", "query", "how many buckets of equal duration, 1 to 10000 (1000)"),
            Argument("t0_us", "query", "the first timestamp (the run's)"),
            Argument("t1_us", "query", "the last timestamp (the run's)"),
        ),
        answers="`column`, `t0_us`, `t1_us`, `bucket_us`, and the columns `t_us` (each"
        " bucket's start), `min`, `max`, `n` (its samples), `mean` (their arithmetic mean) and"
        " `rms` (their root mean square) of the buckets that hold samples",
    ),
)

#: The queries a GET never asks although they carry no ``R`` flag: reading
#: them clears what they read.
CLEARING_QUERIES = frozenset(
    {
        "*ESR?",
        "SYSTem:ERRor[:NEXT]?",
        "SYSTem:ERRor:ALL?",
        "STATus:OPERation[:EVENt]?",
        "STATus:QUEStionable[:EVENt]?",
    }
)

#: The report keys whose values stay strings in JSON, whatever their digits:
#: identifiers and versions, never quantities.
TEXT_KEYS = frozenset(
    {"elf", "serial", "fw", "model", "image", "cal_tag", "cal_date", "chip", "die", "rev", "mfg"}
)


def get_refusal(resolved: Resolved | None) -> str | None:
    """Why a GET may not send a unit, None when it may.

    A GET sends only queries that read without acting: not a command, not a
    query the ``R`` flag marks (it acts on the device, which is why a window
    refuses it: an acquisition, a self-test, a dump, a bus scan, a register
    read), not one whose reading clears what it reads (:data:`CLEARING_QUERIES`),
    and not a header the table does not have.

    :param resolved: The unit's header as :func:`resolve_unit` resolved it.
    """
    if resolved is None:
        return "not a header of the instrument's table"
    if not resolved.pattern.endswith("?"):
        return f"{resolved.pattern} changes the instrument: POST it to /api/scpi"
    if "R" in resolved.flags:
        return f"{resolved.pattern} acts on the device: POST it to /api/scpi"
    if resolved.pattern in CLEARING_QUERIES:
        return f"reading {resolved.pattern} clears it: POST it to /api/scpi"
    return None


def resolve_unit(
    unit: str, path: str = "", table: Sequence[Row] | None = None
) -> tuple[Resolved | None, str]:
    """A unit's header resolved against the table, and the path the next unit continues.

    :param unit: One unit, its parameters included.
    :param path: The path the previous unit left (``docs/scpi.md``, "Paths").
    :param table: As :func:`~kleidos_meter.sim_table.resolve`.
    :returns: The match (None for an undefined header) and the next path.
    """
    header = split_header(unit.strip())[0]
    if header.startswith("*"):
        return resolve(header, table), path
    header = header[1:] if header.startswith(":") else path + header
    following = header[: header.rfind(":") + 1] if ":" in header else ""
    return resolve(header, table), following


@dataclass(frozen=True)
class Schema:
    """Everything the panel declares; :data:`SCHEMA` is the one in use."""

    settings: tuple[Setting, ...] = SETTINGS
    profiles: tuple[Profile, ...] = PROFILES
    relays: tuple[Relay, ...] = RELAYS
    sequences: tuple[SequenceInfo, ...] = SEQUENCES
    routes: tuple[Route, ...] = ROUTES
    clearing_queries: frozenset[str] = CLEARING_QUERIES
    pin_orders: frozenset[str] = PIN_ORDERS
    text_keys: frozenset[str] = TEXT_KEYS

    def setting(self, name: str) -> Setting | None:
        """The setting called *name*, or None."""
        return next((s for s in self.settings if s.name == name), None)

    def relay(self, name: str) -> Relay | None:
        """The relay order called *name*, or None."""
        return next((r for r in self.relays if r.name == name), None)

    def to_json(self) -> dict[str, Any]:
        """The schema as ``/api/schema`` serves it."""

        def plain(item: object) -> dict[str, Any]:
            """A declaration as JSON: its fields, a set as a sorted list."""
            data = dataclasses.asdict(item)  # type: ignore[call-overload]  # every item is a dataclass
            return {
                key: sorted(value) if isinstance(value, frozenset) else value
                for key, value in data.items()
            }

        return {
            "schema": "kleidos.meter.panel/1",
            "mutation_header": {"name": MUTATION_HEADER, "value": MUTATION_VALUE},
            "settings": [plain(s) for s in self.settings],
            "profiles": [plain(p) for p in self.profiles],
            "relays": [plain(r) for r in self.relays],
            "sequences": [plain(s) for s in self.sequences],
            "routes": [plain(r) for r in self.routes],
            "clearing_queries": sorted(self.clearing_queries),
            "pin_orders": sorted(self.pin_orders),
            "text_keys": sorted(self.text_keys),
        }


#: The schema in use.
SCHEMA = Schema()


def problems(table: Sequence[Row] = COMMANDS, schema: Schema = SCHEMA) -> list[str]:
    """Every header or flag of *schema* that *table* does not have as declared.

    :param table: (pattern, flag letters, most parameters) rows, ``kCommands`` in the
        firmware's order; the simulator's copy by default.
    :param schema: The declarations to check.
    :returns: One line per problem; empty when the schema holds.
    """
    patterns = {pattern for pattern, *_ in table}
    found: list[str] = []

    def expect(
        header: str, where: str, pattern: str | None = None, flags: str | None = None
    ) -> None:
        """Note a problem unless *header* resolves, to *pattern* with *flags* when given."""
        resolved = resolve_unit(header, "", table)[0]
        if resolved is None:
            found.append(f"{where}: {header} is not in the table")
        elif pattern is not None and resolved.pattern != pattern:
            found.append(f"{where}: {header} is {resolved.pattern}, not {pattern}")
        elif flags is not None and resolved.flags != flags:
            found.append(f"{where}: {resolved.pattern} has flags {resolved.flags!r}, not {flags!r}")

    for setting in schema.settings:
        where = f"setting {setting.name}"
        expect(setting.query, where, setting.pattern + "?")
        expect(setting.setter.partition(" ")[0], where, setting.pattern, setting.flags)
        found.extend(
            f"{where}: changed by {pattern}, which is not in the table"
            for pattern in sorted(setting.changed_by - patterns)
        )
    for relay in schema.relays:
        expect(relay.header, f"relay {relay.name}", relay.pattern)
    for sequence in schema.sequences:
        for header in sequence.uses:
            expect(header, f"sequence {sequence.name}")
    for route in schema.routes:
        for header in route.uses:
            expect(header, f"route {route.method} {route.path}")
    found.extend(
        f"clearing query {pattern} is not in the table"
        for pattern in sorted(schema.clearing_queries - patterns)
    )
    found.extend(
        f"pin order {pattern} is not in the table"
        for pattern in sorted(schema.pin_orders - patterns)
    )
    return found
