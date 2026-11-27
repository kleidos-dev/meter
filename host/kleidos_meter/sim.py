# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A simulated instrument: the meter's SCPI protocol over an in-memory pipe.

It implements the framing exactly as the firmware does (units, root paths,
long and short forms, the error queue, a response line per message, stream
records outside responses, the definite-length block) and resolves every
header against a copy of the firmware's command table
(:mod:`kleidos_meter.sim_table`), with its flags: a command the simulator
does not model is in :data:`UNDEFINED` and answers ``-113`` as an unknown
header does. A DUT drawing a fixed current at a fixed voltage stands behind
it, or a scripted waveform (:class:`ScriptedWaveform`).

The board has a wiring (:class:`~kleidos_meter.pins.Wiring`, the rig's by
default) and the instrument a pin map (0.18.0): the INA228 answers only on a
map whose bus is where the wiring has it, so ``SYSTem:PINS:APPLy`` proves a
staged map on the wiring and reverts one it cannot prove, as the firmware
does; a saved map survives :meth:`SimulatedMeter.reboot`, and one whose bus no
longer answers falls back at boot. The averaging override
(``SENSe:AVERage``) changes each profile's word and period, and with
``noise_a`` the samples carry a noise that falls with the square root of the
samples averaged.

Two modes:

- **Deterministic** (the default): time stands still. A streamed capture
  emits four samples per program message and two more at ``ABORt``, a ring
  holds 64 samples as soon as ``INITiate`` starts it, and a statistics-only
  capture counts 64. The client's tests are written against this mode.
- **Clock-driven** (``clocked=True``, for ``kleidos-meter serve --sim`` and
  the panel's tests): samples are due at the profile's period on
  :attr:`SimulatedMeter.clock`, and :meth:`SimulatedMeter.read` emits them
  as they fall due. A streamed sample that would overflow the output buffer
  (``tx_capacity`` bytes the reader has not taken) is lost and counted in
  ``dropped=``, as the instrument counts a line it could not write. A ring
  fills over time with a drift anchor every 1024 samples and ends by
  itself when full; ``SENSe:CAPTure:LIMit`` and a level trigger end their
  captures by themselves; a null window ends after its seconds; the relay
  hold runs in real time; ``DIAGnostic:WATChdog:TEST`` reboots after the
  watchdog's timeout; :meth:`SimulatedMeter.inject_reboot` reboots now. A
  ``MEASure`` holds the console for its ``SAMPle:COUNt`` periods: what
  arrives meanwhile waits, and a message whose first unit is ``ABORt`` ends
  it with ``-230``, as the firmware's acquisition does.
  ``FETCh:STATistics?`` reports the samples taken, of which the stream
  carried ``samples - dropped``.

Every entry point takes one lock, so a reader thread and a writer thread can
share an instance.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import re
import threading
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from . import pins
from .identity import serial_from_mac, version_tuple
from .invariant import present
from .protocol import quote, split_outside_quotes, split_units
from .sim_table import COMMANDS, resolve, split_header

#: The longest program message the console takes, bytes (``kConsoleLineCapacity - 1``).
MAX_MESSAGE = 255

#: The most parameters the firmware's parser keeps of one unit (``scpi::kMaxParameters``).
MAX_PARAMETERS = 8

#: The longest window one acquisition may ask for (``kMaxAcquisitionUs``).
MAX_ACQUISITION_US = 600_000_000

#: The profiles: name, period in microseconds, whether they capture to the ring.
PROFILES = {
    "TRACE": (280, True),
    "STATE": (1152, False),
    "SLEEP": (67520, False),
    "BURST": (100, True),
}

#: What each profile converts (``sampler.cpp``, ``kProfileSpecs``): its ADC_CONFIG word
#: and MODE, the bus's, the shunt's and the temperature's conversion times in
#: microseconds (0 for an input its MODE leaves out) and the averages.
CONVERSIONS = {
    "TRACE": (0xA0C0, 0xA, 0, 280, 0, 1),
    "STATE": (0xF140, 0xF, 50, 1052, 50, 1),
    "SLEEP": (0xF1C2, 0xF, 50, 4120, 50, 16),
    "BURST": (0xA000, 0xA, 0, 50, 0, 1),
}

#: One conversion of the INA228 at its reset ADC_CONFIG, microseconds: what a range
#: switch discards before any capture has written a profile's word.
_RESET_PERIOD_US = 3 * 1052

#: The channels each stream profile carries when ``SENSe:FUNCtion:ON`` named
#: none, as the firmware's profile table has them (``sampler.cpp``). The
#: deterministic mode keeps its own pair, ``i_ua`` and ``v_mv``.
DEFAULT_CHANNELS = {"STATE": ("i_ua",), "SLEEP": ("i_ua", "v_mv", "die_c")}

#: The ring's size on the instrument (``sampler.h`` ``kTraceSamples``).
RING_CAPACITY = 1 << 20

#: A drift anchor every this many ring samples (``sampler.h`` ``kAnchorEvery``).
ANCHOR_EVERY = 1024

#: The longest null window, seconds (``kZeroMaxSeconds``).
MAX_ZERO_S = 600

#: Marks one capture keeps for ``TRACe:MARK`` (error ``105`` past them).
MAX_MARKS = 16

#: Threshold marks one capture writes at most.
MAX_THRESHOLD_MARKS = 1024

#: The INA228's address on the rig, the one ``DIAGnostic:I2C:SCAN?`` expects.
INA228_ADDRESS = 0x40

#: The headers of the firmware's table the simulator does not model: each
#: answers ``-113`` and changes nothing, as an unknown header does.
UNDEFINED: frozenset[str] = frozenset(
    {
        "SYSTem:LED?",
        "SENSe:CURRent:RANGe[:UPPer]",
        "SENSe:CURRent:RANGe[:UPPer]?",
        "SENSe:VOLTage:APERture?",
        "SENSe:TEMPerature:APERture?",
        "SENSe:CHARge?",
        "SENSe:CHARge:RESet",
        "CONFigure[:SCALar]:CURRent[:DC]",
        "CONFigure[:SCALar]:VOLTage[:DC]",
        "CONFigure[:SCALar]:POWer[:DC]",
        "CONFigure[:SCALar]:ENERgy",
        "CONFigure[:SCALar]:TEMPerature",
        "CONFigure?",
        "MEASure[:SCALar]:ENERgy?",
        "READ?",
        "READ[:SCALar]:CURRent[:DC]?",
        "READ[:SCALar]:VOLTage[:DC]?",
        "READ[:SCALar]:POWer[:DC]?",
        "READ[:SCALar]:ENERgy?",
        "READ[:SCALar]:TEMPerature?",
        "FETCh?",
        "FETCh[:SCALar]:VOLTage[:DC]?",
        "FETCh[:SCALar]:POWer[:DC]?",
        "FETCh[:SCALar]:ENERgy?",
        "FETCh[:SCALar]:CHARge?",
        "FETCh[:SCALar]:TEMPerature?",
        "INITiate:FORCe",
        "DIAGnostic:ALERt?",
        "DIAGnostic:REGister?",
    }
)

#: How long the instrument holds a path other than ``shunt`` before it puts the
#: coils back to rest by itself, in seconds (``relay.h`` ``kNullPathHoldS``).
HOLD_S = 120

#: The part of the DUT's current the shunt keeps with the bypass closed: about
#: 40 %, what the firmware's host-test circuit gives (its bypass takes 60 %).
BYPASS_SHUNT_SHARE = 0.4

#: The share of the DUT's current below which no relay move can be judged, in
#: amperes: 2 mA, 200 uV through the shunt (``relay.h`` ``kCheckFloorPv``).
CHECK_FLOOR_A = 0.002

#: ``SYSTem:PINS:STAGe``'s role words (``REL1`` and ``REL2`` the relays' short
#: forms) and the role each stages, as the firmware's ``kRoleNames`` has them.
_STAGE_ROLES = ("SDA", "SCL", "ALERt", "RELAY1", "RELAY2", "REL1", "REL2")
_STAGE_KEYS = ("sda", "scl", "alert", "relay1", "relay2", "relay1", "relay2")

#: The highest GPIO ``SYSTem:PINS:STAGe`` takes and the highest address
#: ``SYSTem:PINS:ADDRess`` takes (``kStagedGpioMax``, ``kStagedAddressMax``).
_STAGED_GPIO_MAX = 63
_STAGED_ADDRESS_MAX = 255

#: ``SYSTem:PINS:RESult?`` before any apply since boot.
_NO_RESULT = "result=none reason=- mfg=- die=- tst=- saved=0"

#: The INA228's conversion times, microseconds, by their 3-bit code
#: (``ina228_regs.h`` ``kConversionTimeUs``), and its averaging counts by theirs
#: (``kAverageCount``).
_CONVERSION_US = (50, 84, 150, 280, 540, 1052, 2074, 4120)
_AVERAGE_COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)


@dataclass
class _Averaging:
    """A profile's averaging override (``SENSe:AVERage``): off by default.

    :ivar on: Whether it replaces the profile's own count and shunt conversion time.
    :ivar count: The count it averages, one of :data:`_AVERAGE_COUNTS`.
    :ivar shunt_us: The shunt conversion time it converts with, one of :data:`_CONVERSION_US`.
    """

    on: bool
    count: int
    shunt_us: int


@dataclass(frozen=True)
class _Conversion:
    """What a profile converts with: its own ``ADC_CONFIG`` or the override's.

    :ivar word: The ``ADC_CONFIG`` word.
    :ivar mode: Its MODE.
    :ivar bus_us: The bus conversion time, 0 when MODE leaves the bus out.
    :ivar shunt_us: The shunt's.
    :ivar die_us: The temperature's, 0 when MODE leaves it out.
    :ivar count: The averaging count.
    :ivar period_us: Microseconds per sample.
    :ivar override: Whether the override is on.
    """

    word: int
    mode: int
    bus_us: int
    shunt_us: int
    die_us: int
    count: int
    period_us: int
    override: bool


#: The path K1 (open) and K2 (closed) make, as ``relay.h`` ``pathOf`` names it.
_PATHS = {
    (False, False): "shunt",
    (False, True): "both",
    (True, True): "bypass",
    (True, False): "none",
}

#: The hold's moves, K1 first, and whether each applies to the coils now.
_HOLD_MOVES: tuple[tuple[str, Callable[[SimulatedMeter], bool]], ...] = (
    ("hold:k1:closed", lambda sim: sim.k1_open),
    ("hold:k2:open", lambda sim: sim.k2_closed),
)

#: What the hold's release queues, by the path it released.
_HOLD_RELEASES = {
    "bypass": (
        113,
        "the hold released the null path after {hold} s{under} (ROUTe:NULLpath ON again)",
    ),
    "none": (114, "the hold reclosed the lead after {hold} s{under} (ROUTe:LEAD OPEN again)"),
    "both": (115, "the hold opened the bypass after {hold} s{under} (ROUTe:BYPass ON again)"),
}

#: The current LSB of each range, picoamps.
_LSB_PA = (3_125_000, 781_250)

#: The VSHUNT LSB of each range, picovolts (``ina228_regs.h``).
_SHUNT_LSB_PV = (312_500, 78_125)

#: Each range's full scale in milliamperes (``rangeCeilingMilliamps``) and in
#: microamperes (``kFullScaleUaRange0``, ``kFullScaleUaRange1``).
_CEILING_MA = (1638, 409)
_FULL_SCALE_UA = (1_638_400, 409_600)

#: A contact's settle, microseconds: to operate and to release.
_OPERATE_SETTLE_US, _RELEASE_SETTLE_US = 20_000, 50_000

#: A parameter the firmware copies as one token (``isTokenSafe``): printable, with
#: no space, comma, quote, ``;`` or ``=``.
_TOKEN = re.compile(r"[!-~]+")
_NOT_IN_A_TOKEN = ",;=\"'"  # noqa: S105  the characters a bare token cannot hold

#: A decimal number as ``Parameters::scaled`` reads it.
_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

#: The level a trigger or a threshold may take, nanoamps (range 0's full scale).
_FULL_SCALE_NA = 1_638_400_000

#: The bit times of one retained 3-byte read, and of what a streamed sample
#: reads: DIAG_ALRT, VSHUNT, VBUS and DIETEMP, each with its pointer written
#: (``ina228_regs.h`` ``registerReadBitTimes``, ``sampler.cpp``).
_RETAINED_READ_BITS = 38
_STREAMED_READ_BITS = 210

#: The shortest period of a ring's override and the longest of any, microseconds
#: (``sampler.cpp`` ``kShortestRingPeriodUs``, ``kLongestAveragedPeriodUs``).
_SHORTEST_RING_US = 100
_LONGEST_AVERAGED_US = 200_000

#: The Operation and Questionable bits (``console.cpp``).
_OPER_CALIBRATING, _OPER_MEASURING = 1 << 0, 1 << 4
_QUES_CURRENT, _QUES_TIME, _QUES_CAL, _QUES_LOST, _QUES_HOLD = 2, 4, 1 << 8, 1 << 9, 1 << 10

#: What a unit's parameter may start with that is not character data
#: (``Parameters::characterData``).
_NOT_A_WORD = "\"'#+-.0123456789"

#: The INA228's clip flags in ``DIAG_ALRT``: MATHOF, SHNTOL, SHNTUL.
_CLIP_FLAGS = 0x0200 | 0x0040 | 0x0020

#: How often a clock-driven read that found nothing looks again, seconds.
_READ_POLL_S = 0.005

#: The transition filters' and enable masks' width.
_STATUS_MASK = 0x7FFF

_TEXT = {
    -101: "Invalid character",
    -102: "Syntax error",
    -104: "Data type error",
    -108: "Parameter not allowed",
    -109: "Missing parameter",
    -113: "Undefined header",
    -114: "Header suffix out of range",
    -120: "Numeric data error",
    -221: "Settings conflict",
    -222: "Data out of range",
    -224: "Illegal parameter value",
    -230: "Data corrupt or stale",
    101: "Shunt bypassed",
    105: "Marks full",
    109: "Order disconnects the DUT",
    111: "Not running",
    112: "Null window running",
    114: "Lead reclosed",
    103: "Bypass check failed",
    104: "Lead check failed",
    116: "Relays released by reset",
    117: "Pin map reverted",
    -241: "Hardware missing",
    -363: "Input buffer overrun",
}

#: A handler's answer: a response unit, a block, or nothing.
Reply = str | bytes | None


def _esr_bit(code: int) -> int:
    """The Standard Event bit an error sets (``scpi.cpp`` ``esrBitFor``)."""
    if -200 < code <= -100:
        return 0x20  # command error
    if -300 < code <= -200:
        return 0x10  # execution error
    if -500 < code <= -400:
        return 0x04  # query error
    return 0x08  # device-dependent: -3xx and every positive code


def _scaled(value: int, decimals: int) -> str:
    """``scpi::formatScaled``: *value* tenths-to-the-*decimals*, trailing zeros trimmed."""
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), 10**decimals)
    digits = f"{fraction:0{decimals}d}".rstrip("0") or "0"
    return f"{sign}{whole}.{digits}"


def _bus_us(bits: int, scl_hz: int) -> int:
    """The microseconds *bits* bit times take at *scl_hz*, rounded up."""
    return (bits * 1_000_000 + scl_hz - 1) // scl_hz


def _read_us(scl_hz: int) -> int:
    """The microseconds one retained read takes at *scl_hz*, rounded up."""
    return _bus_us(_RETAINED_READ_BITS, scl_hz)


def _raw_counts(capture: _Capture, amps: float) -> int:
    """The raw VSHUNT count *amps* reads in *capture*: the shunt's voltage plus the null.

    :param capture: The capture, whose latched shunt and null are the rig's.
    :param amps: What the DUT draws.
    :returns: The count the INA228 converts, as the ring stores it.
    """
    picovolts = amps * capture.shunt_uohm * 1e6 + capture.offset_pv
    return round(picovolts / _SHUNT_LSB_PV[capture.range])


def _nanoamps(capture: _Capture, counts: int) -> int:
    """``scale::currentNanoamps`` of *counts* with *capture*'s latched calibration.

    ``(counts x LSB - null) x 1000 / shunt``, the quotient truncated toward
    zero as C++ truncates it (``firmware/src/scale.h``).

    :param capture: The capture, whose range, shunt and null convert.
    :param counts: A raw count.
    :returns: The current, nanoamps.
    """
    numerator = (counts * _SHUNT_LSB_PV[capture.range] - capture.offset_pv) * 1000
    quotient = abs(numerator) // capture.shunt_uohm
    return -quotient if numerator < 0 else quotient


def _normal(seed: int) -> float:
    """A standard normal deviate that *seed* alone decides (SplitMix64, then Box-Muller)."""
    words = []
    state = seed & 0xFFFFFFFFFFFFFFFF
    for _ in range(2):
        state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        words.append(((z ^ (z >> 31)) >> 11) / (1 << 53))
    return math.sqrt(-2.0 * math.log(1.0 - words[0])) * math.cos(2.0 * math.pi * words[1])


def _thousandths(value: int) -> str:
    """``scale::formatThousandths``: *value* thousandths with exactly three decimals."""
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), 1000)
    return f"{sign}{whole}.{fraction:03d}"


class ScriptedWaveform:
    """A piecewise-constant current the simulated DUT draws, repeated.

    :param steps: ``(seconds, amperes)`` pairs, in order.
    :param repeat: Start again after the last step; otherwise hold its level.
    :raises ValueError: With no step, or a step that is not positive.
    """

    def __init__(self, steps: Sequence[tuple[float, float]], *, repeat: bool = True) -> None:
        if not steps or any(seconds <= 0 for seconds, _ in steps):
            raise ValueError("a waveform is one or more steps of a positive duration")
        self._ends: list[int] = []
        self._levels: list[float] = []
        end = 0
        for seconds, amperes in steps:
            end += round(seconds * 1e6)
            self._ends.append(end)
            self._levels.append(amperes)
        self._repeat = repeat

    @property
    def period_us(self) -> int:
        """One pass through every step, microseconds."""
        return self._ends[-1]

    def __call__(self, t_us: int) -> float:
        """The current at device time *t_us*, amperes."""
        if self._repeat:
            t_us %= self.period_us
        for end, level in zip(self._ends, self._levels, strict=True):
            if t_us < end:
                return level
        return self._levels[-1]


@dataclass
class _Call:
    """One unit as a handler sees it: its parameters, suffix, header and table key."""

    params: list[str]
    suffix: int | None
    header: str
    key: str = ""


@dataclass
class _Message:
    """A program message being run: the units left, what they answered, the path."""

    units: list[str]
    responses: list[str] = field(default_factory=list)
    block: bytes | None = None
    path: str = ""


@dataclass
class _Acquisition:
    """A clock-driven ``MEASure`` in progress: when it ends, what it will answer.

    :ivar message: The message it holds, resumed when it ends; None until
        the unit that started it has returned.
    :ivar start: When it started, on the clock.
    :ivar due: When its last sample is taken, on the clock.
    :ivar count: The samples it takes (``SAMPle:COUNt``).
    :ivar period: Its profile's period, microseconds.
    :ivar answer: Its response unit.
    :ivar capture: The capture it closes, which ``FETCh:STATistics?`` then reports.
    """

    message: _Message | None
    start: float
    due: float
    count: int
    period: int
    answer: str
    capture: _Capture


@dataclass
class _Capture:
    """An open or closed capture: what it was started with and what it took."""

    profile: str
    range: int
    period: int
    t0: int
    #: What it converts with, the averaging override included, and where the
    #: pin map it runs on came from, both as its start found them.
    conversion: _Conversion
    pins_src: str = "default"
    #: What the capture converts with, latched at its start as
    #: ``Sampler::snapshotCalibration`` latches it: the range's shunt and null.
    shunt_uohm: int = 100_000
    offset_pv: int = 0
    stats_only: bool = False
    ring: bool = False
    channels: tuple[str, ...] = ()
    #: Whether it is a ``MEASure``'s acquisition, whose samples are not streamed.
    measuring: bool = False
    #: The relays as they stood at its start, for ``FETCh:HEADer?``.
    relays: str = ""
    samples: int = 0
    dropped: int = 0
    ended: bool = False
    hold_teardowns: int = 0
    #: Whether a move under it took the shunt off the DUT's current: the
    #: firmware's ``captureBypassed_``, after which an order needs no ``FORCe``.
    bypassed: bool = False
    limit: int = 0
    level_na: int | None = None
    rising: bool = True
    pretrigger: int = 0
    trigger_ready: bool = False
    trig_n: int | None = None
    trig_t: int | None = None
    thr_na: int | None = None
    hyst_na: int = 0
    thr_marks_on: bool = False
    thr_marks: int = 0
    above: bool = False
    above_n: int = 0
    rises: int = 0
    mean_na: float = 0.0
    m2: float = 0.0
    min_na: float = math.inf
    max_na: float = -math.inf
    max_n: int = 0
    last_na: float | None = None
    end_us: int | None = None
    batch_size: int = 1
    batch_sum: float = 0.0
    batch_count: int = 0
    batches: list[float] = field(default_factory=list)


_HANDLERS: dict[str, Callable[[SimulatedMeter, _Call], Reply]] = {}


def _first_header(units: Sequence[str]) -> str | None:
    """The table pattern of a message's first unit, None when it has none or it is undefined."""
    first = units[0].strip() if units else ""
    resolved = resolve(split_header(first)[0].removeprefix(":"))
    return None if resolved is None else resolved.pattern


def _handles(*keys: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the decorated method as the handler of every key in *keys*."""

    def register(function: Callable[..., Any]) -> Callable[..., Any]:
        """File *function* under each key; return it unchanged."""
        for key in keys:
            _HANDLERS[key] = function
        return function

    return register


class SimulatedMeter:
    """The instrument, behind ``write``/``read``/``close`` like a serial port.

    :param current_a: What the DUT draws, amperes, when no *waveform* is given.
    :param voltage_v: What it sees, volts.
    :param clocked: Run on :attr:`clock` (the module's "Clock-driven" mode).
    :param waveform: The DUT's current at a device time in microseconds; the
        clock-driven mode samples it, the deterministic mode ignores it.
    :param ring_capacity: The ring's size, :data:`RING_CAPACITY` by default.
    :param tx_capacity: The bytes of output the clock-driven mode holds for
        the reader before a streamed sample is dropped.
    :param clock: The monotonic clock, seconds.
    :param sleep: How the clock-driven :meth:`read` waits.
    :param wiring: Where the board's parts are (:class:`~kleidos_meter.pins.Wiring`):
        the rig's by default. A map whose bus is not the wiring's finds no INA228,
        and one whose relays are not its fails the null-path check.
    :param noise_a: The RMS noise, amperes, of each clock-driven sample a
        profile takes with its own ``ADC_CONFIG``; the averaging override
        divides it by the square root of the samples it averages for each one
        (count times shunt conversion time, over the profile's own). 0, the
        default, keeps every sample exactly the DUT's current.
    """

    def __init__(
        self,
        current_a: float = 0.0886,
        voltage_v: float = 4.064,
        *,
        clocked: bool = False,
        waveform: Callable[[int], float] | None = None,
        ring_capacity: int = RING_CAPACITY,
        tx_capacity: int = 1 << 18,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wiring: pins.Wiring = pins.RIG_WIRING,
        noise_a: float = 0.0,
    ) -> None:
        self.current_a = current_a
        self.voltage_v = voltage_v
        self.clocked = clocked
        self.waveform = waveform
        self.ring_capacity = ring_capacity
        self.tx_capacity = tx_capacity
        self.clock = clock
        self.sleep = sleep
        #: How long a clock-driven read waits for output, seconds (a port's timeout).
        self.read_timeout = 0.05
        #: The clock's rate error the anchors and the stream's timestamps carry.
        self.drift_ppm = 0.0
        self._lock = threading.RLock()
        self._boot_clock: float | None = None
        # What the instrument is: its factory MAC (the serial is its digest,
        # firmware/src/identity.h), its firmware and its INA228's ids. None as
        # the MAC is an eFuse that cannot be read: serial 0.
        self.mac: bytes | None = bytes([0x02, 0x00, 0x5E, 0x10, 0x20, 0x30])
        self.firmware = "0.18.0"
        # The first 8 bytes of the image's ELF SHA-256, as SYSTem:INFormation?
        # names it (made up).
        self.elf = "5e1f0ba47c2291d3"
        self.mfg, self.die = "0x5449", "0x228"
        # Whether the INA228 came up, and the addresses the bus acknowledges.
        self.device_ready = True
        self.scan_found = [INA228_ADDRESS]
        # The quality the reports carry, and the INA228's DIAG_ALRT flags.
        self.quality = "cal"
        self.diag_flags = 0xC003
        # The shunt calibration bench: the shunt as it really is, each range's
        # gain error, the reference fitted, and whether VBUS reads it.
        self.true_shunt_ohm = 0.100123
        self.range_gain_ppm = [0, 0]
        self.fitted_reference_ohm = 100.0
        self.vbus_on_reference = True
        self.reference_ohm = 100.0
        self.shunt_ohm = 0.1
        self.shunt_range_ohm: list[float | None] = [None, None]
        self.shunt_measured = False
        self.cal_date: tuple[int, int, int] | None = None
        # The bench's drift: each shunt point reads the shunt this much higher
        # than the one before (a joint warming), and each null this much
        # higher (thermal EMF), as on 2026-11-20.
        self.drift_ppm_per_point = 0.0
        self.null_offset_pv = [-449_746, -449_746]
        self.null_drift_pv = 0
        # The null each range subtracts, picovolts, as NVS keeps it: 0 until a
        # null window closes. The input offset is modeled as what the last
        # null measured, so a nulled range reads the DUT's current back
        # through the quantization of a count.
        self.offset_pv = [0, 0]
        self.points_taken = 0
        self.nulled = [False, False]
        self.tag = ""
        self.vbus_side = "unknown"
        self.shunt_point: str | None = None
        self.bypass_part = "relay2click"  # the one image drives the Click (0.8.0)
        self.nulls_taken = 0
        self.messages: list[str] = []
        self.errors: list[tuple[int, str]] = []
        self.profile = "TRACE"
        self.range = 0
        self.format = "TEXT"
        self.channels: list[str] = []
        # SENSe:CAPTure and the current threshold (0.10.0), in nanoamps.
        self.statistics_only = False
        self.threshold_na: int | None = None
        self.hysteresis_na = 0
        self.threshold_marks = False
        # TRIGger and SENSe:CAPTure:LIMit (0.11.0); corrupt_dump damages the
        # next ring dump's bytes under its CRC, as a line could.
        self.trigger_source = "IMM"
        self.trigger_level_na = 0
        self.trigger_rising = True
        self.pretrigger = 0
        self.capture_limited = False
        self.corrupt_dump = False
        # The INA228's bus (SYSTem:COMMunicate:I2C:FREQuency).
        self.scl_hz = 400_000
        # The pin map (0.18.0). The board's wiring; the record the flash keeps
        # across a reboot (the table's digest it was saved under and the map),
        # none until an apply saves one; the live map, where it came from and
        # whether its relays are known to be wired as it says (rev D's are);
        # the staged map, the live one until a unit changes it (*RST keeps
        # it); and SYSTem:PINS:RESult?'s report of the last apply.
        self.wiring = wiring
        self.pins_record: tuple[str, pins.PinMap] | None = None
        self.pins_live = pins.DEFAULT_MAP
        self.pins_src = "default"
        self.pins_verified = True
        self.pins_pending = pins.DEFAULT_MAP
        self.pins_result = _NO_RESULT
        # The averaging override of each profile (SENSe:AVERage, 0.18.0), off,
        # and the noise each clock-driven sample carries under it.
        self.averaging = self._own_averaging()
        self.noise_a = noise_a
        # *TST?'s verdict and SYSTem:HEALth?'s reset reason (0.12.0).
        self.self_test = 0
        self.reset_reason = "poweron"
        self.reset_released = "-"
        # The heap counts DIAGnostic:HEAP? reports: what ESP-IDF allocated
        # before app_main (made up) and, as on a healthy image, nothing since.
        self.pre_main_allocs = 24
        self.pre_main_frees = 3
        self.watchdog_timeout_s = 5
        self.sample_count = 100
        # The relays: K1 open takes the DUT's battery lead off, K2 closed puts
        # the bypass across the shunt; `moved` is ROUTe:RESult?'s last order.
        self.k1_open: bool = False
        self.k2_closed: bool = False
        self.moved = "-"
        # Its settle, whether a capture ran through it and how many of its
        # moves that capture recorded (ROUTe:RESult?).
        self.order_settle_us = self.order_live = self.order_marked = 0
        # The last range switch: whether it changed the range, what it then
        # discarded and whether the device had converted again.
        self.range_changed = False
        self.range_discard_us = self.range_converted = 0
        # A pending ROUTe:LEAD:CYCLe (0.13.0): when, on `clock`, K1 recloses.
        self.lead_cycle_due: float | None = None
        # The hold on an open lead (0.13.0), and from 0.16.0 its teardown
        # under a capture: when, on `clock`, K1 recloses by itself.
        self.hold_due: float | None = None
        self.hold_released = 0
        self.capture: _Capture | None = None
        self.last_stats: _Capture | None = None
        self.zero_open = False
        self.zero_ended = False
        self.zero_seconds = 0
        self.zero_due: float | None = None
        self.zero_circuit = ""  # the relay tokens the null window was opened in
        self.zero_result: str | None = None
        self.watchdog_due: float | None = None
        self._t_us = 1_000_000
        self.ring = 0
        self.marks: list[tuple[str, int, int]] = []
        self.esr = 0x80
        self.ese = self.sre = 0
        self.opc_pending = False
        self.oper_event = self.ques_event = 0
        self.oper_enable = self.ques_enable = 0
        self.oper_ptr = self.ques_ptr = _STATUS_MASK
        self.oper_ntr = self.ques_ntr = 0
        self._oper_last = self._ques_last = 0
        # A booting instrument discards what it receives until its console is
        # announced (firmware/src/console.cpp): this many writes are dropped.
        self.boot_writes = 0
        self._out = bytearray()
        # A clock-driven MEASure in progress, and the messages it holds back.
        self._acquisition: _Acquisition | None = None
        self._inbox: list[list[str]] = []

    # -- what the instrument is ---------------------------------------------

    @property
    def serial(self) -> str:
        """``*IDN?``'s serial: the digest of the MAC, or 0."""
        return "0" if self.mac is None else serial_from_mac(self.mac)

    @property
    def image(self) -> str:
        """``identity.h`` ``kImageName``: ``meter`` from 0.8.0, which has one image.

        Set ``firmware`` below 0.8.0 to model an older image: then ``meter``
        drove the HW-316 bypass and ``meter_relay2`` the Relay 2 Click.
        """
        if version_tuple(self.firmware) >= (0, 8, 0):
            return "meter"
        return "meter_relay2" if self.bypass_part == "relay2click" else "meter"

    @property
    def t_us(self) -> int:
        """Microseconds since boot: the clock's in the clock-driven mode."""
        if not self.clocked:
            return self._t_us
        return self._us_at(self.clock())

    @property
    def running(self) -> bool:
        """Whether a window samples now (``Sampler::running``)."""
        capture = self.capture is not None and not self.capture.ended
        return capture or (self.zero_open and not self.zero_ended)

    @property
    def window_open(self) -> bool:
        """Whether a window is open, ended by itself or not (``captureOpen``)."""
        return self.capture is not None or self.zero_open

    def range_shunt_ohm(self, index: int) -> float:
        """The shunt range *index* converts with: its own value or the common one.

        :param index: 0 or 1.
        :returns: Ohms.
        """
        own = self.shunt_range_ohm[index]
        return self.shunt_ohm if own is None else own

    def current_at(self, t_us: int) -> float:
        """What the DUT draws at device time *t_us*.

        :param t_us: Microseconds since boot.
        :returns: Amperes.
        """
        drawn = self.current_a if self.waveform is None else self.waveform(t_us)
        return drawn * self._rest_share()

    def _rest_share(self) -> float:
        """The part of the DUT's current the shunt carries with the relays released.

        The live map's relays drive the board's parts as :func:`pins.rest_state`
        says: a lead opened at rest leaves the shunt nothing, a bypass closed at
        rest :data:`BYPASS_SHUNT_SHARE` of it; otherwise all of it.

        :returns: A fraction, 0 to 1.
        """
        lead_open, bypass_closed = pins.rest_state(self.pins_live, self.wiring)
        if lead_open:
            return 0.0
        return BYPASS_SHUNT_SHARE if bypass_closed else 1.0

    def _shunt_a(self) -> float:
        """What the deterministic DUT puts through the shunt, amperes.

        :returns: :attr:`current_a` times :meth:`_rest_share`.
        """
        return self.current_a * self._rest_share()

    # -- the pipe -----------------------------------------------------------

    def write(self, data: bytes) -> int:
        """Take *data* as the line delivers it: every newline ends a message.

        :param data: The bytes.
        :returns: How many were taken: all of them.
        """
        with self._lock:
            self._service_watchdog()
            if self.boot_writes > 0:
                self.boot_writes -= 1
                return len(data)
            if self.watchdog_due is not None:
                return len(data)  # the console has stopped: nothing answers
            for raw in data.replace(b"\r", b"\n").split(b"\n"):
                bad = [b for b in raw if (b < 0x20 and b != 0x09) or b > 0x7E]
                if bad:
                    self._fail(
                        -101,
                        f"byte 0x{bad[0]:02X} is not printable ASCII (program message discarded)",
                    )
                    continue
                if len(raw) > MAX_MESSAGE:
                    self._fail(-363, f"program message longer than {MAX_MESSAGE} bytes")
                    continue
                line = raw.decode("ascii")
                if line:  # an empty line is no message, as on the instrument
                    self._message(line)
            return len(data)

    def reboot(self, writes: int = 0) -> None:
        """Power-cycle: settings to their defaults, the power-on bit set.

        What lives in RAM is lost (the open and the last capture, the ring,
        the results, the error queue, the status registers); the calibration
        is kept, as NVS keeps it.

        :param writes: How many writes the booting instrument discards.
        """
        with self._lock:
            self._handle_reset(_Call([], None, "*RST", "*RST"))
            self.capture = self.last_stats = None
            self.ring = 0
            self.marks.clear()
            self.zero_result = self.shunt_point = None
            self.zero_open = self.zero_ended = False
            self.errors.clear()
            self._out.clear()
            self._acquisition = None
            self._inbox.clear()
            self.esr = 0x80
            self.ese = self.sre = 0
            self.oper_event = self.ques_event = 0
            self.oper_enable = self.ques_enable = 0
            self.oper_ptr = self.ques_ptr = _STATUS_MASK
            self.oper_ntr = self.ques_ntr = 0
            self._oper_last = self._ques_last = 0
            self.hold_released = 0
            self.watchdog_due = None
            self.boot_writes = writes
            self._boot_pins()
            if self.clocked:
                self._boot_clock = self.clock()

    def _boot_pins(self) -> None:
        """Take the map a boot takes (:func:`~kleidos_meter.pins.boot`) from the record.

        A record saved under another table reads as none. A stored bus that
        does not answer falls back, or leaves no INA228, and queues one ``117``.
        """
        record = self.pins_record
        stored = record[1] if record is not None and record[0] == pins.DIGEST else None
        booted = pins.boot_map(stored, self.wiring, fitted=self.device_ready)
        self.pins_live = self.pins_pending = booted.pins
        self.pins_src = booted.src
        self.pins_verified = booted.pins.relays() == pins.DEFAULT_MAP.relays()
        self.pins_result = _NO_RESULT
        self.scl_hz = booted.pins.scl_hz
        if booted.problem is not None:
            self._fail(117, booted.problem)

    def inject_reboot(self, reason: str = "panic", *, writes: int = 0) -> None:
        """Reboot now for *reason*, as a panic or a watchdog would.

        A coil the reset found energized is released and reported, as the
        firmware's boot does (error ``116`` and ``reset_released``).

        :param reason: What ``SYSTem:HEALth?`` then names, ``esp_reset_reason()``'s word.
        :param writes: How many writes the booting instrument discards.
        """
        with self._lock:
            path = self.path
            released = ",".join(c for c, on in (("k1", self.k1_open), ("k2", self.k2_closed)) if on)
            self.reboot(writes)
            self.reset_reason = reason
            self.reset_released = released or "-"
            if released:
                self._fail(
                    116,
                    f"a reset ({reason}) released {released} (path={path});"
                    " the dut is back on its shunt",
                )

    def read(self, size: int) -> bytes:
        """Up to *size* bytes of output; the clock-driven mode waits for some.

        :param size: The most bytes to return.
        :returns: What the instrument has written, possibly nothing.
        """
        if not self.clocked:
            with self._lock:
                self._service_watchdog()
                return self._take_out(size)
        deadline = self.clock() + self.read_timeout
        # Bounded by count as well as by the clock, so a clock that does not
        # move while this sleeps cannot hold the reader forever.
        for _ in range(math.ceil(self.read_timeout / _READ_POLL_S) + 1):
            with self._lock:
                self._service_watchdog()
                self._tick()
                if self._out:
                    return self._take_out(size)
            left = deadline - self.clock()
            if left <= 0:
                break
            self.sleep(min(left, _READ_POLL_S))
        return b""

    def close(self) -> None:
        """Nothing to release."""

    def _take_out(self, size: int) -> bytes:
        """Remove and return the first *size* bytes of output."""
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def _emit(self, line: str) -> None:
        """Write *line* and its newline to the output."""
        self._out += (line + "\n").encode("ascii")

    def _fail(self, code: int, detail: str) -> None:
        """Queue error *code* with *detail* and set its Standard Event bit."""
        self.errors.append((code, f"{_TEXT.get(code, 'Error')};{detail}"))
        self.esr |= _esr_bit(code)

    # -- time ---------------------------------------------------------------

    def _us_at(self, instant: float) -> int:
        """The device time at clock *instant*: one second at boot, then the clock's."""
        if self._boot_clock is None:
            self._boot_clock = self.clock()
        return 1_000_000 + round((instant - self._boot_clock) * 1e6)

    def _sample_time(self, capture: _Capture, index: int) -> int:
        """When sample *index* of *capture* was taken, on the drifting clock."""
        return capture.t0 + round(index * capture.period * (1 + self.drift_ppm * 1e-6))

    def _service_watchdog(self) -> None:
        """Reboot once ``DIAGnostic:WATChdog:TEST`` has starved the watchdog long enough."""
        if self.watchdog_due is not None and self.clock() >= self.watchdog_due:
            self.inject_reboot("task_wdt")

    def _tick(self) -> None:
        """Bring the clock-driven instrument up to now: samples, the hold, timed windows."""
        now = self.t_us
        if self.hold_due is not None and not self.zero_open:
            due = self._us_at(self.hold_due)
            if due <= now:
                self._advance(due)  # the hold's marks fall among the samples
        self._service_relays()
        zero_due = self.zero_due if self.zero_open else None
        if zero_due is not None and self.clock() >= zero_due:
            self.zero_ended = True
        self._advance(now)
        self._latch()
        if self._acquisition is not None and self.clock() >= self._acquisition.due:
            self._end_acquisition(aborted=False)
        if self.opc_pending and not self.running:
            self.opc_pending = False
            self.esr |= 0x01

    def _service_relays(self) -> None:
        """A pending lead cycle's reclose, then the hold's."""
        if self.lead_cycle_due is not None and self.clock() >= self.lead_cycle_due:
            self.k1_open, self.lead_cycle_due = False, None  # the cycle's reclose
            self._live_mark("k1", False)  # under a capture, marked as a LIVE move
            if self.path == "shunt":
                self.hold_due = None
        self._service_hold()

    # -- the framing --------------------------------------------------------

    def _message(self, line: str) -> None:
        """Run one program message: its units in order, then its response line.

        While a clock-driven ``MEASure`` holds the console the message waits in
        the inbox, unless its first unit is ``ABORt``: that unit ends the
        acquisition and is consumed, and the rest of its message waits in its
        turn (``docs/scpi.md``, "CONFigure, MEASure, READ, FETCh").
        """
        self.messages.append(line)
        if self.clocked:
            self._tick()
        elif self.capture is not None and not self.capture.stats_only and not self.capture.ring:
            self._stream(4)  # samples taken since the last message
        units = split_units(line)
        if self._acquisition is None:
            self._run(_Message(units))
            return
        aborts = _first_header(units) == "ABORt"
        self._inbox.append(units[1:] if aborts else units)
        if aborts:
            self._end_acquisition(aborted=True)

    def _run(self, message: _Message) -> None:
        """Run *message*'s units from where it stands; write its line once they are done.

        A unit that starts an acquisition suspends the message, which
        :meth:`_end_acquisition` resumes.
        """
        while message.units:
            unit = message.units.pop(0).strip()
            if not unit:
                continue
            header, params = split_header(unit)
            if not header.startswith("*"):
                header = header[1:] if header.startswith(":") else message.path + header
                message.path = header[: header.rfind(":") + 1] if ":" in header else ""
            given = split_outside_quotes(params, ",") if params.strip() else []
            reply = self._unit(header, [p.strip() for p in given])
            if isinstance(reply, bytes):
                message.block = reply
            elif reply is not None:
                message.responses.append(reply)
            if self._acquisition is not None and self._acquisition.message is None:
                self._acquisition.message = message
                return
        if message.block is not None:
            digits = str(len(message.block))
            self._out += f"#{len(digits)}{digits}".encode("ascii") + message.block
            self._emit((";" + ";".join(message.responses)) if message.responses else "")
        elif message.responses:
            self._emit(";".join(message.responses))

    def _end_acquisition(self, *, aborted: bool) -> None:
        """End the acquisition: its answer, or ``-230`` when aborted; then what waited.

        :param aborted: Whether an ``ABORt`` ended it before its last sample.
        """
        acquisition = present(self._acquisition, "the running acquisition")
        message = present(acquisition.message, "the acquisition's message")
        self._acquisition = None
        taken = acquisition.count
        if aborted:
            elapsed_us = round((self.clock() - acquisition.start) * 1e6)
            taken = min(acquisition.count - 1, elapsed_us // acquisition.period)
        self._close_acquisition(acquisition.capture, taken)
        if aborted:
            self._fail(
                -230,
                f"the acquisition was aborted after {taken} of {acquisition.count} samples (ABORt)",
            )
        else:
            message.responses.append(acquisition.answer)
        self._run(message)
        while self._inbox and self._acquisition is None:
            self._run(_Message(self._inbox.pop(0)))

    # -- the commands -------------------------------------------------------

    def _unit(self, header: str, params: list[str]) -> Reply:
        """One unit: resolved against the table, held to its flags, then run."""
        self._service_relays()
        self._latch()
        resolved = resolve(header)
        if resolved is None or resolved.pattern in UNDEFINED:
            self._fail(-113, header)
            return None
        flags = resolved.flags
        if "D" in flags and not self.chip_up:
            self._fail(-241, "ina228 did not answer at 0x40")
            return None
        if "R" in flags and self.running:
            if self.zero_open:
                self._fail(112, "null window running (ABORt first)")
            else:
                self._fail(-221, "capture running (ABORt first)")
            return None
        if "C" in flags and self.window_open:
            if self.zero_open:
                self._fail(112, "null window ended, not closed (ABORt first)")
            else:
                self._fail(-221, "capture ended, not closed (ABORt first)")
            return None
        if not self._count_holds(header, params, resolved.most):
            return None
        call = _Call(params, resolved.suffix, header, resolved.key)
        return _HANDLERS[resolved.key](self, call)

    # -- parameters ---------------------------------------------------------

    def _count_holds(self, header: str, params: list[str], most: int) -> bool:
        """Whether a unit gives no more parameters than its command takes, else ``-108``.

        :param header: The unit's header, which the error's detail names.
        :param params: Its parameters.
        :param most: The most the command takes.
        """
        if len(params) > MAX_PARAMETERS:  # more than the firmware's parser keeps of a unit
            self._fail(-108, header)
            return False
        if len(params) > most:
            takes = "no parameter" if most == 0 else f"at most {most} parameter"
            if most > 1:
                takes += "s"
            self._fail(-108, f"{header} takes {takes}")
            return False
        return True

    def _unsigned(self, call: _Call, index: int, usage: str) -> int | None:
        """Parameter *index* as an unsigned integer, or None with its error queued."""
        if index >= len(call.params):
            self._fail(-109, usage)
            return None
        text = call.params[index]
        if text.startswith("-"):
            self._fail(-222, usage)
            return None
        digits = text.removeprefix("+")
        if not re.fullmatch("[0-9]+", digits):
            stop = digits.lstrip("0123456789")[:1]  # NR2 or NR3 where NR1 was asked for
            self._fail(-104 if stop and stop in ".eE" else -120, usage)
            return None
        value = int(digits)
        if value > 0xFFFFFFFF:
            self._fail(-222, usage)
            return None
        return value

    def _integer(self, call: _Call, index: int, usage: str) -> int | None:
        """Parameter *index* as ``scpi::Parameters::unsignedInteger`` reads it.

        NR1, or ``#H``, ``#Q`` or ``#B`` and digits of that base; a sign or a
        base it does not know, a decimal point or an exponent, and a number
        past 32 bits are refused with the firmware's codes.

        :returns: The number, or None with its error queued.
        """
        if index >= len(call.params):
            self._fail(-109, usage)
            return None
        text, base = call.params[index], 10
        if text.startswith("#"):
            base = {"H": 16, "Q": 8, "B": 2}.get(text[1:2].upper(), 0)
            if not base:
                self._fail(-120, usage)
                return None
            text = text[2:]
        elif text.startswith("-"):
            self._fail(-222, usage)
            return None
        else:
            text = text.removeprefix("+")
        digits = "0123456789ABCDEF"[:base]
        used = len(text) - len(text.upper().lstrip(digits))
        if used and int(text[:used], base) > 0xFFFFFFFF:
            self._fail(-222, usage)  # out of range is judged before what follows the digits
            return None
        if not used or used < len(text):
            stop = text[used : used + 1].upper()
            self._fail(-104 if base == 10 and stop and stop in ".E" else -120, usage)
            return None
        return int(text, base)

    def _choice(self, call: _Call, index: int, forms: Sequence[str], usage: str) -> int | None:
        """Which of *forms* (``IMMediate``: short or long) parameter *index* names."""
        if index >= len(call.params):
            self._fail(-109, usage)
            return None
        text = call.params[index].upper()
        for position, form in enumerate(forms):
            short = "".join(char for char in form if char.isupper() or char.isdigit())
            if text in (form.upper(), short):
                return position
        self._fail(-104 if text[:1] in _NOT_A_WORD else -224, usage)
        return None

    def _boolean(self, call: _Call, usage: str) -> bool | None:
        """Parameter 0 as ``ON``/``OFF``/``1``/``0``, or None with its error queued."""
        if not call.params:
            self._fail(-109, usage)
            return None
        value = {"ON": True, "1": True, "OFF": False, "0": False}.get(call.params[0].upper())
        if value is None:
            self._fail(-224, usage)
        return value

    def _decimal(self, call: _Call, usage: str) -> Decimal | None:
        """Parameter 0 as a decimal number, or None with its error queued."""
        if not call.params:
            self._fail(-109, usage)
            return None
        if not _DECIMAL.fullmatch(call.params[0]):
            self._fail(-104, usage)
            return None
        return Decimal(call.params[0])

    def _token(self, call: _Call, usage: str, longest: int, refusal: str) -> str | None:
        """Parameter 0 as one token, quoted or not, or None with its error queued.

        :param longest: The most characters it may have.
        :param refusal: What the error of one that is not a token says.
        """
        if not call.params:
            self._fail(-109, usage)
            return None
        text = call.params[0]
        if text[:1] in ("'", '"'):
            text = text[1:-1]
        safe = _TOKEN.fullmatch(text) and not any(c in _NOT_IN_A_TOKEN for c in text)
        if not safe or len(text) > longest:
            self._fail(-224, refusal)
            return None
        return text

    def _nanoamps(self, call: _Call, usage: str, *, low: int = -_FULL_SCALE_NA) -> int | None:
        """Parameter 0 in amperes as exact nanoamps, held to *low* and the full scale."""
        amps = self._decimal(call, usage)
        if amps is None:
            return None
        value = int(amps * 1_000_000_000)
        if not low <= value <= _FULL_SCALE_NA:
            self._fail(-222, usage)
            return None
        return value

    # -- common commands ----------------------------------------------------

    @_handles("*IDN?")
    def _handle_idn(self, call: _Call) -> Reply:
        """``*IDN?``."""
        return f"KLEIDOS,KLEIDOS-METER,{self.serial},{self.firmware}"

    @_handles("*CLS")
    def _handle_clear(self, call: _Call) -> Reply:
        """``*CLS``."""
        self.errors.clear()
        self.esr = 0
        self.opc_pending = False
        self.oper_event = self.ques_event = 0
        return None

    @_handles("*ESR?")
    def _handle_esr(self, call: _Call) -> Reply:
        """``*ESR?``."""
        value, self.esr = self.esr, 0
        return str(value)

    @_handles("*ESE", "*SRE")
    def _handle_enable_set(self, call: _Call) -> Reply:
        """``*ESE``, ``*SRE``."""
        name = call.key
        value = self._unsigned(call, 0, f"use {name} <0..255>")
        if value is None:
            return None
        if value > 255:
            self._fail(-224, f"use {name} <0..255>")
        elif name == "*ESE":
            self.ese = value
        else:
            self.sre = value
        return None

    @_handles("*ESE?")
    def _handle_ese_query(self, call: _Call) -> Reply:
        """``*ESE?``."""
        return str(self.ese)

    @_handles("*SRE?")
    def _handle_sre_query(self, call: _Call) -> Reply:
        """``*SRE?``."""
        return str(self.sre)

    @_handles("*STB?")
    def _handle_stb(self, call: _Call) -> Reply:
        """``*STB?``."""
        return str(self.status_byte())

    @_handles("*OPC")
    def _handle_opc(self, call: _Call) -> Reply:
        """``*OPC``."""
        if self.running and self._ends_itself():
            self.opc_pending = True
        else:
            self.esr |= 0x01
        return None

    @_handles("*OPC?")
    def _handle_opc_query(self, call: _Call) -> Reply:
        """``*OPC?``."""
        return "1"

    @_handles("*WAI")
    def _handle_wait(self, call: _Call) -> Reply:
        """``*WAI``."""
        return None

    @_handles("*TST?")
    def _handle_test(self, call: _Call) -> Reply:
        """``*TST?``: :attr:`self_test`, and the bits the live map's bring-up sets."""
        proof = pins.prove(self.pins_live, self.wiring, fitted=self.device_ready)
        return str(self.self_test | proof.tst)

    @_handles("*RST")
    def _handle_reset(self, call: _Call) -> Reply:
        """``*RST``."""
        if self.capture is not None:
            self._close_capture()  # *RST stores what an ABORt would, and streams nothing
        self.profile, self.range, self.format, self.channels = "TRACE", 0, "TEXT", []
        self.statistics_only, self.threshold_na, self.hysteresis_na = False, None, 0
        self.threshold_marks = False
        self.trigger_source, self.trigger_level_na, self.capture_limited = "IMM", 0, False
        self.trigger_rising, self.pretrigger = True, 0
        self.zero_open = self.zero_ended = self.k1_open = self.k2_closed = False
        self.lead_cycle_due, self.hold_due = None, None
        self.reference_ohm = 100.0
        self.sample_count = 100
        self.scl_hz = self.pins_live.scl_hz
        self.averaging = self._own_averaging()
        self.opc_pending = False
        return None

    # -- SYSTem -------------------------------------------------------------

    @_handles("SYST:ERR:ALL?", "SYST:ERR?")
    def _handle_errors(self, call: _Call) -> Reply:
        """``SYSTem:ERRor:ALL?``, ``SYSTem:ERRor[:NEXT]?``."""
        if not self.errors:
            return '0,"No error"'
        if call.key == "SYST:ERR?":
            code, text = self.errors.pop(0)
            return f"{code},{quote(text)}"
        out = ",".join(f"{c},{quote(t)}" for c, t in self.errors)
        self.errors.clear()
        return out

    @_handles("SYST:ERR:COUN?")
    def _handle_error_count(self, call: _Call) -> Reply:
        """``SYSTem:ERRor:COUNt?``."""
        return str(len(self.errors))

    @_handles("SYST:VERS?")
    def _handle_version(self, call: _Call) -> Reply:
        """``SYSTem:VERSion?``."""
        return "1999.0"

    @_handles("SYST:INF?")
    def _handle_information(self, call: _Call) -> Reply:
        """``SYSTem:INFormation?``."""
        return (
            f"model=kleidos-meter fw={self.firmware}"
            f" chip={'ina228' if self.chip_up else 'none'} addr=0x{self.wiring.address:02X}"
            f" mfg={self.mfg}"
            f" die={self.die} rev=0x1 shunt_uohm={round(self.shunt_ohm * 1e6)}"
            f" cal_tag={self.tag or '-'} q={self.quality} relays=1 bypass_part={self.bypass_part}"
            f" serial={self.serial} image={self.image} elf={self.elf}"
            f" pins_src={self.pins_src} pins_table={pins.DIGEST}"
        )

    @_handles("SYST:STAT?")
    def _handle_status(self, call: _Call) -> Reply:
        """``SYSTem:STATus?``."""
        capture = self.capture or self.last_stats
        running = self.running
        if self.zero_open:
            sink = "null"
        elif self.capture is None:
            sink = "idle"
        elif self.capture.stats_only:
            sink = "stats"
        else:
            sink = "ring" if self.capture.ring else "stream"
        alert = self.capture is not None and not self.capture.ring and running
        last = None if capture is None else capture.last_na
        i_na = round(self.current_at(self.t_us) * 1e9 if last is None else last)
        vbus_uv = round(self.voltage_v * 1e6 + i_na * self.shunt_ohm / 1000)
        trig = "-"
        if self.capture is not None and self.capture.level_na is not None:
            trig = "armed" if self.capture.trig_n is None else "fired"
        return (
            f"running={int(running)} sink={sink} profile={self.profile}"
            f" period_us={self.capture.period if running and self.capture else 0}"
            f" pace={'alert' if alert else 'timer'} pace_fallback=0 range={self.range}"
            f" samples={0 if capture is None else capture.samples}"
            f" dropped={0 if capture is None else capture.dropped} read_err=0"
            f" i_na={i_na} vbus_uv={vbus_uv} die_mc=25500 die_age_us=0"
            f" flags=0x{self.diag_flags:04X} scl_hz={self.scl_hz} q={self.quality} trig={trig}"
        )

    @_handles("SYST:HEAL?")
    def _handle_health(self, call: _Call) -> Reply:
        """``SYSTem:HEALth?``."""
        return (
            f"reset={self.reset_reason} uptime_us={self.t_us} elf={self.elf}"
            f" fw={self.firmware} stack_console=2048 stack_sampler=2048"
            f" allocs_after_init=0 frees_after_init=0 hold_released={self.hold_released}"
            f" errors={len(self.errors)} reset_released={self.reset_released}"
            f" pins_src={self.pins_src} pins_table={pins.DIGEST}"
        )

    @_handles("SYST:COMM:I2C:FREQ")
    def _handle_bus_set(self, call: _Call) -> Reply:
        """``SYSTem:COMMunicate:I2C:FREQuency``."""
        hz = self._unsigned(call, 0, "use SYSTem:COMMunicate:I2C:FREQuency <hz>")
        if hz is None:
            return None
        if not 50_000 <= hz <= 1_000_000:
            self._fail(-222, "use SYSTem:COMMunicate:I2C:FREQuency <50000..1000000>")
            return None
        self.scl_hz = hz
        return None

    # -- SYSTem:PINS: the pin map (0.18.0) ----------------------------------

    @property
    def chip_up(self) -> bool:
        """Whether the INA228 answers: fitted (:attr:`device_ready`) and on the live map's bus."""
        return pins.answers(self.pins_live, self.wiring, fitted=self.device_ready)

    @_handles("SYST:PINS?")
    def _handle_pins(self, call: _Call) -> Reply:
        """``SYSTem:PINS?``: the live map, where it came from, whether its relays are verified."""
        return (
            f"{self.pins_live.text()} src={self.pins_src} verified={int(self.pins_verified)}"
            f" table={pins.DIGEST}"
        )

    @_handles("SYST:PINS:PEND?")
    def _handle_pins_pending(self, call: _Call) -> Reply:
        """``SYSTem:PINS:PENDing?``: the staged map and :func:`pins.validate_map`'s verdict."""
        verdict = pins.verdict_text(pins.validate_map(self.pins_pending))
        return f"{self.pins_pending.text()} {verdict}"

    @_handles("SYST:PINS:STAG")
    def _handle_pins_stage(self, call: _Call) -> Reply:
        """``SYSTem:PINS:STAGe``: one role, read whole before anything is staged."""
        usage = "use SYSTem:PINS:STAGe SDA|SCL|ALERt|RELay1|RELay2,<gpio>|NONE[,HIGH|LOW|UP|OFF]"
        which = self._choice(call, 0, _STAGE_ROLES, usage)
        if which is None:
            return None
        role = _STAGE_KEYS[which]
        relay = role in pins.RELAYS
        given = call.params[1] if len(call.params) > 1 else ""
        gpio: int | None = None
        if given[:1].isascii() and given[:1].isalpha():
            if self._choice(call, 1, ("NONE",), usage) is None:
                return None
        else:
            gpio = self._integer(call, 1, usage)
            if gpio is None:
                return None
            if gpio > _STAGED_GPIO_MAX:
                self._fail(-222, usage)
                return None
        changes: dict[str, int | str | None] = {role: gpio}
        if len(call.params) > 2:
            option = self._choice(call, 2, ("HIGH", "LOW") if relay else ("UP", "OFF"), usage)
            if option is None:
                return None
            if relay:
                changes[f"{role}_active"] = pins.LEVELS[option]
            else:
                changes[f"pull_{role}"] = pins.PULLS[option]
        staged = replace(self.pins_pending, **changes)  # type: ignore[arg-type]  # PinMap's fields
        self.pins_pending = staged
        return None

    @_handles("SYST:PINS:ADDR")
    def _handle_pins_address(self, call: _Call) -> Reply:
        """``SYSTem:PINS:ADDRess``: ``AUTO`` or 1..255, judged by :func:`pins.validate_map`."""
        usage = "use SYSTem:PINS:ADDRess AUTO|<1..255> (the INA228 answers at #H40..#H4F)"
        given = call.params[0] if call.params else ""
        if given[:1].isascii() and given[:1].isalpha():
            if self._choice(call, 0, ("AUTO",), usage) is not None:
                self.pins_pending = replace(self.pins_pending, addr=pins.ADDRESS_AUTO)
            return None
        address = self._integer(call, 0, usage)
        if address is None:
            return None
        if not 1 <= address <= _STAGED_ADDRESS_MAX:
            self._fail(-222, usage)
            return None
        self.pins_pending = replace(self.pins_pending, addr=address)
        return None

    @_handles("SYST:PINS:CLOC")
    def _handle_pins_clock(self, call: _Call) -> Reply:
        """``SYSTem:PINS:CLOCk``: any 32-bit number, judged by :func:`pins.validate_map`."""
        hz = self._integer(call, 0, "use SYSTem:PINS:CLOCk <hz>")
        if hz is not None:
            self.pins_pending = replace(self.pins_pending, scl_hz=hz)
        return None

    @_handles("SYST:PINS:CLE")
    def _handle_pins_clear(self, call: _Call) -> Reply:
        """``SYSTem:PINS:CLEar``: the staged map is the live one again."""
        self.pins_pending = self.pins_live
        return None

    @_handles("SYST:PINS:APPL", "SYST:PINS:DEF")
    def _handle_pins_apply(self, call: _Call) -> Reply:
        """``SYSTem:PINS:APPLy`` and ``:DEFault``: one checked change (:meth:`_apply_pins`)."""
        defaults = call.key == "SYST:PINS:DEF"
        self._apply_pins(pins.DEFAULT_MAP if defaults else self.pins_pending, defaults=defaults)
        return None

    def _pins_refusal(self, target: pins.PinMap) -> tuple[str, str] | None:
        """Why *target* may not be applied now, past the flags: ``SYSTem:PINS:RESult?``'s
        ``reason`` and the error's detail; None when it may.

        A relay that is not at rest refuses it (a null path held, or any other
        path but ``shunt``), and so does a map the rules refuse.
        """
        if self.path == "bypass":
            return "relays", "a null path is held (path=bypass): ROUTe:NULLpath OFF first"
        if self.path != "shunt":
            return "relays", f"the relays are not at rest (path={self.path}): put them back first"
        verdict = pins.validate_map(target)
        if verdict is not None:
            return (
                "invalid",
                f"the staged map breaks a rule: {verdict[0]} at {verdict[1]}"
                " (SYSTem:PINS:PENDing?)",
            )
        return None

    def _apply_pins(self, target: pins.PinMap, *, defaults: bool) -> None:
        """Bring *target* up on the wiring, keep it if it is proved, else put the last good back.

        The relays move first (a relay pad is always read back as driven), then
        the bus and ALERT are brought up and proved
        (:func:`~kleidos_meter.pins.prove`). A map that passes is saved and
        read back, and is the live map (``src=stored``); one that fails leaves
        the last good map live, queues one ``117`` and saves nothing. The
        defaults are kept whatever the proof finds, since they are the way
        back with no INA228. ``src`` is ``default`` for the default map, however
        it was applied. Relays that moved are unverified until a null-path check
        passes on them, the rig's own pins included.

        :param target: The map.
        :param defaults: Whether it is ``SYSTem:PINS:DEFault``.
        """
        refusal = self._pins_refusal(target)
        if refusal is not None:
            reason, detail = refusal
            self.pins_result = f"result=refused reason={reason} mfg=- die=- tst=- saved=0"
            self._fail(-221, detail)
            return
        proof = pins.prove(target, self.wiring, fitted=self.device_ready)
        ids = f"mfg={self.mfg} die={self.die}" if proof.answered else "mfg=- die=-"
        if not (proof.passed or defaults):
            self.pins_result = (
                f"result=reverted reason={proof.reason} {ids} tst={proof.tst} saved=0"
            )
            if proof.answered:
                found = f"no ALERT edge on GPIO{target.alert}"
            else:
                found = (
                    f"the INA228 did not answer on sda={target.sda} scl={target.scl}"
                    f" addr={pins.address_text(target.addr)}"
                )
            back = pins.prove(self.pins_live, self.wiring, fitted=self.device_ready)
            also = "" if back.passed else f"; it fails too (tst={back.tst})"
            self._fail(117, f"{found}: the last good map is back{also}")
            return
        moved = target.relays() != self.pins_live.relays()
        rest = self._rest_lost(target) if moved and proof.passed else None
        if rest is not None and not defaults:
            self.pins_result = f"result=reverted reason=rest {ids} tst={proof.tst} saved=0"
            self._fail(
                117,
                f"the relays at rest took the dut's current off the shunt (vs_pv={rest}):"
                " the last good map is back",
            )
            return
        reason = "rest" if rest is not None else proof.reason
        self.pins_verified = self.pins_verified and not moved
        self.pins_live = self.pins_pending = target
        self.pins_src = "default" if target == pins.DEFAULT_MAP else "stored"
        self.pins_record = (pins.DIGEST, target)
        self.scl_hz = target.scl_hz
        self.pins_result = f"result=ok reason={reason} {ids} tst={proof.tst} saved=1"

    def _rest_lost(self, target: pins.PinMap) -> str | None:
        """The firmware's rest check of relays that moved: the shunt's readings
        before and after, picovolts, when *target*'s relays take the DUT's
        current off the shunt at rest; None when they leave it or the DUT draws
        under :data:`CHECK_FLOOR_A`.

        :param target: The map that was proved.
        :returns: ``vs_pv``'s two readings, or None.
        """
        if self.current_a < CHECK_FLOOR_A:
            return None
        lead_open, bypass_closed = pins.rest_state(target, self.wiring)
        if not (lead_open or bypass_closed):
            return None
        before = round(self.current_a * self.shunt_ohm * 1e12)
        after = 0 if lead_open else round(before * BYPASS_SHUNT_SHARE)
        return f"{before},{after}"

    @_handles("SYST:PINS:RES?")
    def _handle_pins_result(self, call: _Call) -> Reply:
        """``SYSTem:PINS:RESult?``: the last apply since boot."""
        return self.pins_result

    @_handles("SYST:COMM:I2C:FREQ?")
    def _handle_bus_query(self, call: _Call) -> Reply:
        """``SYSTem:COMMunicate:I2C:FREQuency?``."""
        return str(self.scl_hz)

    # -- STATus -------------------------------------------------------------

    def operation_condition(self) -> int:
        """``STATus:OPERation:CONDition?``: calibrating or measuring, now.

        :returns: The register.
        """
        if self.zero_open and not self.zero_ended:
            return _OPER_CALIBRATING
        if self.running:
            return _OPER_MEASURING
        return 0

    def questionable_condition(self) -> int:
        """``STATus:QUEStionable:CONDition?``: the open or last capture's doubts, now.

        :returns: The register.
        """
        condition = _QUES_CURRENT if self.diag_flags & _CLIP_FLAGS else 0
        if self.quality != "cal":
            condition |= _QUES_CAL
        capture = self.capture or self.last_stats
        if capture is not None and capture.dropped > 0:
            condition |= _QUES_LOST
        if capture is not None and capture.hold_teardowns > 0:
            condition |= _QUES_HOLD
        return condition

    def status_byte(self) -> int:
        """``*STB?``: the summaries of the queue and the three registers.

        :returns: The byte.
        """
        self._latch()
        status = 0x04 if self.errors else 0
        if self.ques_event & self.ques_enable:
            status |= 0x08
        if self.esr & self.ese:
            status |= 0x20
        if self.oper_event & self.oper_enable:
            status |= 0x80
        if status & self.sre & ~0x40:
            status |= 0x40
        return status

    def _latch(self) -> None:
        """Carry the conditions' edges through the transition filters into the events."""
        oper, ques = self.operation_condition(), self.questionable_condition()

        def edges(now: int, last: int, ptr: int, ntr: int) -> int:
            """The bits that rose through *ptr* or fell through *ntr*."""
            return (now & ~last & ptr) | (~now & last & ntr)

        self.oper_event |= edges(oper, self._oper_last, self.oper_ptr, self.oper_ntr)
        self.ques_event |= edges(ques, self._ques_last, self.ques_ptr, self.ques_ntr)
        self._oper_last, self._ques_last = oper, ques

    @_handles("STAT:OPER?", "STAT:QUES?")
    def _handle_event(self, call: _Call) -> Reply:
        """``STATus:OPERation[:EVENt]?``, ``STATus:QUEStionable[:EVENt]?``."""
        if call.key == "STAT:OPER?":
            value, self.oper_event = self.oper_event, 0
        else:
            value, self.ques_event = self.ques_event, 0
        return str(value)

    @_handles("STAT:OPER:COND?")
    def _handle_oper_condition(self, call: _Call) -> Reply:
        """``STATus:OPERation:CONDition?``."""
        return str(self.operation_condition())

    @_handles("STAT:QUES:COND?")
    def _handle_ques_condition(self, call: _Call) -> Reply:
        """``STATus:QUEStionable:CONDition?``."""
        return str(self.questionable_condition())

    @_handles(
        "STAT:OPER:ENAB",
        "STAT:QUES:ENAB",
        "STAT:OPER:PTR",
        "STAT:OPER:NTR",
        "STAT:QUES:PTR",
        "STAT:QUES:NTR",
    )
    def _handle_mask_set(self, call: _Call) -> Reply:
        """``STATus:OPERation:ENABle``,
        ``STATus:QUEStionable:ENABle``, ``STATus:OPERation:PTRansition``,
        ``STATus:OPERation:NTRansition``, ``STATus:QUEStionable:PTRansition``,
        ``STATus:QUEStionable:NTRansition``.
        """
        name = self._mask_name(call.key)
        usage = f"use {call.key} <0..32767>"
        value = self._unsigned(call, 0, usage)
        if value is None:
            return None
        if value > _STATUS_MASK:
            # An enable mask out of range is an illegal value, a filter's out of range.
            self._fail(-224 if name.endswith("enable") else -222, usage)
            return None
        setattr(self, name, value)
        return None

    @_handles(
        "STAT:OPER:ENAB?",
        "STAT:QUES:ENAB?",
        "STAT:OPER:PTR?",
        "STAT:OPER:NTR?",
        "STAT:QUES:PTR?",
        "STAT:QUES:NTR?",
    )
    def _handle_mask_query(self, call: _Call) -> Reply:
        """``STATus:OPERation:ENABle?``,
        ``STATus:QUEStionable:ENABle?``, ``STATus:OPERation:PTRansition?``,
        ``STATus:OPERation:NTRansition?``, ``STATus:QUEStionable:PTRansition?``,
        ``STATus:QUEStionable:NTRansition?``.
        """
        return str(getattr(self, self._mask_name(call.key)))

    @staticmethod
    def _mask_name(key: str) -> str:
        """The attribute a ``STATus`` mask's key names: ``oper_enable``, ``ques_ptr``."""
        _, register, kind = key.rstrip("?").split(":")
        return f"{register.lower()}_{'enable' if kind == 'ENAB' else kind.lower()}"

    @_handles("STAT:PRES")
    def _handle_preset(self, call: _Call) -> Reply:
        """``STATus:PRESet``."""
        self.oper_enable = self.ques_enable = 0
        self.oper_ptr = self.ques_ptr = _STATUS_MASK
        self.oper_ntr = self.ques_ntr = 0
        return None

    # -- SENSe ----------------------------------------------------------------

    @_handles("SENS:PROF")
    def _handle_profile_set(self, call: _Call) -> Reply:
        """``SENSe:PROFile``."""
        usage = "use SENSe:PROFile TRACe|STATe|SLEep|BURSt"
        which = self._choice(call, 0, ("TRACe", "STATe", "SLEep", "BURSt"), usage)
        if which is not None:
            self.profile, self.channels = tuple(PROFILES)[which], []
        return None

    @_handles("SENS:PROF?")
    def _handle_profile_query(self, call: _Call) -> Reply:
        """``SENSe:PROFile?``."""
        return self.profile

    @_handles("SENS:PROF:REP?")
    def _handle_profile_report(self, call: _Call) -> Reply:
        """``SENSe:PROFile:REPort?``: what the live profile converts with, override included."""
        conversion = self._conversion(self.profile)
        return (
            f"profile={self.profile} adc_config=0x{conversion.word:04X}"
            f" period_us={conversion.period_us} vshct_us={conversion.shunt_us}"
            f" avg={conversion.count} avg_state={'on' if conversion.override else 'off'}"
            f" vbusct_us={conversion.bus_us}"
        )

    # -- the averaging override (SENSe:AVERage, 0.18.0) ----------------------

    @staticmethod
    def _own_averaging() -> dict[str, _Averaging]:
        """Every profile's override off, holding the profile's own count and shunt time."""
        return {
            profile: _Averaging(False, conversion[5], conversion[3])
            for profile, conversion in CONVERSIONS.items()
        }

    def _conversion(self, profile: str, averaging: _Averaging | None = None) -> _Conversion:
        """What *profile* converts with: its own word, or its override's when that is on.

        The override changes the word's shunt conversion time and averaging
        count and nothing else, and the period follows: the conversion times
        of the inputs MODE converts, times the count (``ina228_regs.h``
        ``periodMicroseconds``).

        :param profile: The profile.
        :param averaging: The override to judge, the profile's own by default.
        """
        word, mode, bus_us, shunt_us, die_us, count = CONVERSIONS[profile]
        averaging = self.averaging[profile] if averaging is None else averaging
        if not averaging.on:
            return _Conversion(
                word, mode, bus_us, shunt_us, die_us, count, PROFILES[profile][0], False
            )
        shunt_code = _CONVERSION_US.index(averaging.shunt_us)
        count_code = _AVERAGE_COUNTS.index(averaging.count)
        word = (word & ~(0x7 << 6) & ~0x7) | shunt_code << 6 | count_code
        period = (bus_us + averaging.shunt_us + die_us) * averaging.count
        return _Conversion(
            word, mode, bus_us, averaging.shunt_us, die_us, averaging.count, period, True
        )

    def _pace_refusal(self, averaging: _Averaging) -> str | None:
        """Why the live profile's loop cannot keep *averaging*'s period (``paceAllows()``).

        The period must be over twice the bus time a sample's reads take at the
        running clock, at least :data:`_SHORTEST_RING_US` on a ring, and at
        most :data:`_LONGEST_AVERAGED_US`.

        :param averaging: The override, as if it were on.
        :returns: The refusal's text, or None for a period the loop keeps.
        """
        period = self._conversion(self.profile, replace(averaging, on=True)).period_us
        ring = PROFILES[self.profile][1]
        reads = _bus_us(_RETAINED_READ_BITS if ring else _STREAMED_READ_BITS, self.scl_hz)
        shortest = max(2 * reads + 1, _SHORTEST_RING_US if ring else 0)
        sample = f"averaging {averaging.count} x {averaging.shunt_us} us gives {period} us a sample"
        if period < shortest:
            return f"{sample}, under the {shortest} us its reads need at {self.scl_hz} Hz"
        if period > _LONGEST_AVERAGED_US:
            return f"{sample}, over the {_LONGEST_AVERAGED_US} us its pacing allows"
        return None

    def _pace_refused(self) -> bool:
        """Whether the live profile's loop keeps no window now, at the running bus clock.

        The engine's two rules (``Sampler::beginCapture()``), which every window
        meets, a stream's, a ring's, an acquisition's and a null's: an override
        that is on is judged again, and a ring's read must come in under half
        the period.
        """
        averaging = self.averaging[self.profile]
        refused = self._pace_refusal(averaging) if averaging.on else None
        period = self._conversion(self.profile).period_us
        read_us = _read_us(self.scl_hz)
        if refused is None and PROFILES[self.profile][1] and 2 * read_us >= period:
            refused = (
                f"{self.profile} needs a faster bus (a {period} us period needs a read under"
                f" {period // 2} us; it is {read_us} us at {self.scl_hz} Hz)"
            )
        if refused is None:
            return False
        self._fail(-221, refused)
        return True

    def _set_averaging(self, usage: str, changes: dict[str, bool | int] | None) -> None:
        """Change the live profile's override by *changes*, or refuse it.

        A value the INA228 does not take (*changes* None) is refused with
        *usage*'s ``-224``; ``BURST``'s 50 us conversion is its definition, so
        it has no override (``-221``); and an override that is on must leave a
        period the loop keeps (:meth:`_pace_refusal`, ``-221``).

        :param usage: The order's usage, which a refused value's error carries.
        :param changes: The :class:`_Averaging` fields to change, or None.
        """
        if changes is None:
            self._fail(-224, usage)
            return
        if self.profile == "BURST":
            self._fail(
                -221, "BURSt has no averaging override: its 50 us conversion is its definition"
            )
            return
        averaging = replace(self.averaging[self.profile], **changes)  # type: ignore[arg-type]  # by field
        refused = self._pace_refusal(averaging) if averaging.on else None
        if refused is not None:
            self._fail(-221, refused)
            return
        self.averaging[self.profile] = averaging

    @_handles("SENS:CURR:APER?")
    def _handle_current_aperture(self, call: _Call) -> Reply:
        """``SENSe:CURRent:APERture?``: the profile's shunt conversion time, seconds."""
        return _scaled(self._conversion(self.profile).shunt_us, 6)

    @_handles("SENS:AVER:COUN?")
    def _handle_average_count(self, call: _Call) -> Reply:
        """``SENSe:AVERage:COUNt?``: the count the profile converts with."""
        return str(self._conversion(self.profile).count)

    @_handles("SENS:AVER?")
    def _handle_average_query(self, call: _Call) -> Reply:
        """``SENSe:AVERage[:STATe]?``: whether the live profile's override is on."""
        return str(int(self.averaging[self.profile].on))

    @_handles("SENS:AVER")
    def _handle_average_set(self, call: _Call) -> Reply:
        """``SENSe:AVERage[:STATe]``: the live profile's override on or off."""
        usage = "use SENSe:AVERage[:STATe] ON|OFF"
        on = self._boolean(call, usage)
        if on is not None:
            self._set_averaging(usage, {"on": on})
        return None

    @_handles("SENS:AVER:COUN")
    def _handle_average_count_set(self, call: _Call) -> Reply:
        """``SENSe:AVERage:COUNt``: the override's count, one of the INA228's eight."""
        usage = "use SENSe:AVERage:COUNt 1|4|16|64|128|256|512|1024"
        count = self._integer(call, 0, usage)
        if count is not None:
            changes = {"count": count} if count in _AVERAGE_COUNTS else None
            self._set_averaging(usage, changes)
        return None

    @_handles("SENS:CURR:APER")
    def _handle_current_aperture_set(self, call: _Call) -> Reply:
        """``SENSe:CURRent:APERture``: the override's shunt conversion time, one of eight."""
        usage = "use SENSe:CURRent:APERture <seconds> (0.00005..0.00412)"
        seconds = self._decimal(call, usage)
        if seconds is not None:
            micro = seconds * 1_000_000
            taken = micro == micro.to_integral_value() and int(micro) in _CONVERSION_US
            changes = {"shunt_us": int(micro)} if taken else None
            self._set_averaging(usage, changes)
        return None

    @_handles("SENS:CURR:RANG:IND")
    def _handle_range_set(self, call: _Call) -> Reply:
        """``SENSe:CURRent:RANGe:INDex``."""
        usage = "use SENSe:CURRent:RANGe:INDex 0|1"
        index = self._unsigned(call, 0, usage)
        if index is None:
            return None
        if index > 1:
            self._fail(-222, usage)
            return None
        self._select_range(index)
        return None

    def _select_range(self, index: int) -> None:
        """Switch to range *index*; a change discards one conversion and checks for the next."""
        self.range_changed = index != self.range
        self.range = index
        last = self.capture or self.last_stats
        period = _RESET_PERIOD_US if last is None else last.period
        self.range_discard_us = period if self.range_changed else 0
        self.range_converted = int(self.range_changed)

    @_handles("SENS:CURR:RANG:IND?")
    def _handle_range_query(self, call: _Call) -> Reply:
        """``SENSe:CURRent:RANGe:INDex?``."""
        return str(self.range)

    @_handles("SENS:CURR:RANG:REP?")
    def _handle_range_report(self, call: _Call) -> Reply:
        """``SENSe:CURRent:RANGe:REPort?``."""
        return (
            f"range={self.range} current_lsb_pa={_LSB_PA[self.range]}"
            f" ceiling_ma={_CEILING_MA[self.range]} rstacc={int(self.range_changed)}"
            f" discard_us={self.range_discard_us} cnvrf={self.range_converted}"
            f" q={self.quality}"
        )

    @_handles("SENS:FUNC:ON")
    def _handle_function_on(self, call: _Call) -> Reply:
        """``SENSe:FUNCtion:ON``."""
        if not call.params:
            self._fail(-109, "use SENSe:FUNCtion:ON 'CURR'[,'VOLT'][,'POW'][,'TEMP']")
            return None
        names = {"CURR": "i_ua", "VOLT": "v_mv", "POW": "p_mw", "TEMP": "die_c"}
        found = [names.get(p.strip("\"'").upper()[:4].rstrip("E")) for p in call.params]
        chosen = [name for name in found if name is not None]
        if len(chosen) != len(found):
            self._fail(-224, "unknown function")
            return None
        self.channels = chosen
        return None

    @_handles("SENS:FUNC:ON?")
    def _handle_function_query(self, call: _Call) -> Reply:
        """``SENSe:FUNCtion:ON?``."""
        functions = {"i_ua": "CURR", "v_mv": "VOLT", "p_mw": "POW", "die_c": "TEMP"}
        chosen = self._stream_channels(self.profile)
        return ",".join(f'"{functions[c]}"' for c in chosen) if chosen else '""'

    @_handles("SENS:FUNC:ON:DEF")
    def _handle_function_default(self, call: _Call) -> Reply:
        """``SENSe:FUNCtion:ON:DEFault``."""
        self.channels = []
        return None

    def _stream_channels(self, profile: str) -> tuple[str, ...]:
        """The channels a capture of *profile* streams: the chosen ones, or its default."""
        if PROFILES[profile][1]:
            return ()
        if self.channels:
            return tuple(self.channels)
        return DEFAULT_CHANNELS[profile] if self.clocked else ("i_ua", "v_mv")

    @_handles("SENS:CAPT")
    def _handle_capture_set(self, call: _Call) -> Reply:
        """``SENSe:CAPTure``."""
        which = self._choice(call, 0, ("DATA", "STATistics"), "use SENSe:CAPTure DATA|STATistics")
        if which is not None:
            self.statistics_only = which == 1
        return None

    @_handles("SENS:CAPT?")
    def _handle_capture_query(self, call: _Call) -> Reply:
        """``SENSe:CAPTure?``."""
        return "STAT" if self.statistics_only else "DATA"

    @_handles("SENS:CAPT:LIM")
    def _handle_limit_set(self, call: _Call) -> Reply:
        """``SENSe:CAPTure:LIMit``."""
        limited = self._boolean(call, "use SENSe:CAPTure:LIMit ON|OFF")
        if limited is not None:
            self.capture_limited = limited
        return None

    @_handles("SENS:CAPT:LIM?")
    def _handle_limit_query(self, call: _Call) -> Reply:
        """``SENSe:CAPTure:LIMit?``."""
        return str(int(self.capture_limited))

    @_handles("SENS:CURR:THR")
    def _handle_threshold_set(self, call: _Call) -> Reply:
        """``SENSe:CURRent:THReshold``."""
        if call.params and call.params[0].upper() == "OFF":
            self.threshold_na = None
            return None
        value = self._nanoamps(call, "use SENSe:CURRent:THReshold <amps> (-1.6384..1.6384) or OFF")
        if value is not None:
            self.threshold_na = value
        return None

    @_handles("SENS:CURR:THR?")
    def _handle_threshold_query(self, call: _Call) -> Reply:
        """``SENSe:CURRent:THReshold?``."""
        return "OFF" if self.threshold_na is None else _scaled(self.threshold_na, 9)

    @_handles("SENS:CURR:THR:HYST")
    def _handle_hysteresis_set(self, call: _Call) -> Reply:
        """``SENSe:CURRent:THReshold:HYSTeresis``."""
        usage = "use SENSe:CURRent:THReshold:HYSTeresis <amps> (0..1.6384)"
        value = self._nanoamps(call, usage, low=0)
        if value is not None:
            self.hysteresis_na = value
        return None

    @_handles("SENS:CURR:THR:HYST?")
    def _handle_hysteresis_query(self, call: _Call) -> Reply:
        """``SENSe:CURRent:THReshold:HYSTeresis?``."""
        return _scaled(self.hysteresis_na, 9)

    @_handles("SENS:CURR:THR:MARK")
    def _handle_threshold_marks_set(self, call: _Call) -> Reply:
        """``SENSe:CURRent:THReshold:MARK``."""
        marks = self._boolean(call, "use SENSe:CURRent:THReshold:MARK ON|OFF")
        if marks is not None:
            self.threshold_marks = marks
        return None

    @_handles("SENS:CURR:THR:MARK?")
    def _handle_threshold_marks_query(self, call: _Call) -> Reply:
        """``SENSe:CURRent:THReshold:MARK?``."""
        return str(int(self.threshold_marks))

    # -- SAMPle, FORMat, TRIGger ---------------------------------------------

    @_handles("SAMP:TIM?")
    def _handle_sample_timer(self, call: _Call) -> Reply:
        """``SAMPle:TIMer?``."""
        return _scaled(self._conversion(self.profile).period_us, 6)

    @_handles("SAMP:COUN")
    def _handle_sample_count_set(self, call: _Call) -> Reply:
        """``SAMPle:COUNt``."""
        count = self._unsigned(call, 0, "use SAMPle:COUNt <n>")
        if count is None:
            return None
        if not 1 <= count <= 1_000_000:
            self._fail(-222, "use SAMPle:COUNt <1..1000000>")
            return None
        self.sample_count = count
        return None

    @_handles("SAMP:COUN?")
    def _handle_sample_count_query(self, call: _Call) -> Reply:
        """``SAMPle:COUNt?``."""
        return str(self.sample_count)

    @_handles("FORM")
    def _handle_format_set(self, call: _Call) -> Reply:
        """``FORMat[:DATA]``."""
        which = self._choice(call, 0, ("TEXT", "CSV"), "use FORMat:DATA TEXT|CSV")
        if which is not None:
            self.format = ("TEXT", "CSV")[which]
        return None

    @_handles("FORM?")
    def _handle_format_query(self, call: _Call) -> Reply:
        """``FORMat[:DATA]?``."""
        return self.format

    @_handles("TRIG:SOUR")
    def _handle_trigger_source_set(self, call: _Call) -> Reply:
        """``TRIGger:SOURce``."""
        usage = "use TRIGger:SOURce IMMediate|LEVel"
        which = self._choice(call, 0, ("IMMediate", "LEVel"), usage)
        if which is not None:
            self.trigger_source = ("IMM", "LEV")[which]
        return None

    @_handles("TRIG:SOUR?")
    def _handle_trigger_source_query(self, call: _Call) -> Reply:
        """``TRIGger:SOURce?``."""
        return self.trigger_source

    @_handles("TRIG:LEV")
    def _handle_trigger_level_set(self, call: _Call) -> Reply:
        """``TRIGger:LEVel``."""
        value = self._nanoamps(call, "use TRIGger:LEVel <amps> (-1.6384..1.6384)")
        if value is not None:
            self.trigger_level_na = value
        return None

    @_handles("TRIG:LEV?")
    def _handle_trigger_level_query(self, call: _Call) -> Reply:
        """``TRIGger:LEVel?``."""
        return _scaled(self.trigger_level_na, 9)

    @_handles("TRIG:SLOP")
    def _handle_slope_set(self, call: _Call) -> Reply:
        """``TRIGger:SLOPe``."""
        usage = "use TRIGger:SLOPe POSitive|NEGative"
        which = self._choice(call, 0, ("POSitive", "NEGative"), usage)
        if which is not None:
            self.trigger_rising = which == 0
        return None

    @_handles("TRIG:SLOP?")
    def _handle_slope_query(self, call: _Call) -> Reply:
        """``TRIGger:SLOPe?``."""
        return "POS" if self.trigger_rising else "NEG"

    @_handles("TRIG:PRET")
    def _handle_pretrigger_set(self, call: _Call) -> Reply:
        """``TRIGger:PRETrigger``."""
        samples = self._unsigned(call, 0, "use TRIGger:PRETrigger <samples>")
        if samples is None:
            return None
        if samples >= self.ring_capacity:
            self._fail(-222, f"use TRIGger:PRETrigger <0..{self.ring_capacity - 1}>")
            return None
        self.pretrigger = samples
        return None

    @_handles("TRIG:PRET?")
    def _handle_pretrigger_query(self, call: _Call) -> Reply:
        """``TRIGger:PRETrigger?``."""
        return str(self.pretrigger)

    # -- MEASure, FETCh -------------------------------------------------------

    def _acquisition_count(self) -> int:
        """The samples an acquisition takes: ``SAMPle:COUNt``, at most the ring's on a ring."""
        if PROFILES[self.profile][1]:
            return min(self.sample_count, self.ring_capacity)
        return self.sample_count

    def _acquisition_refused(self) -> bool:
        """Whether an acquisition is refused: over 600 s, or a period its loop cannot keep."""
        period, count = self._conversion(self.profile).period_us, self._acquisition_count()
        if count * period > MAX_ACQUISITION_US:
            self._fail(
                -222,
                f"acquisition of {count} x {period} us exceeds 600 s"
                " (lower SAMPle:COUNt, or INITiate a capture)",
            )
            return True
        return self._pace_refused()

    @_handles("MEAS:CURR?", "MEAS:VOLT?", "MEAS:POW?", "MEAS:TEMP?")
    def _handle_measure(self, call: _Call) -> Reply:
        """``MEASure[:SCALar]:CURRent[:DC]?``,
        ``MEASure[:SCALar]:VOLTage[:DC]?``, ``MEASure[:SCALar]:POWer[:DC]?``,
        ``MEASure[:SCALar]:TEMPerature?``.
        """
        function = call.key.split(":")[1].rstrip("?")
        values = {
            "CURR": self._shunt_a(),
            "VOLT": self.voltage_v,
            "POW": self._shunt_a() * self.voltage_v,
            "TEMP": 25.5,
        }
        if call.params and not self._apply_range(call):
            return None
        if function in ("VOLT", "POW") and PROFILES[self.profile][1]:
            # A profile that converts no bus voltage: STATE is selected (0.12.0).
            self.profile, self.channels = "STATE", []
        if self.shunt_bypassed:
            self._fail(101, f"shunt bypassed (path={self.path}: ROUTe:NULLpath OFF first)")
            return None
        if self._acquisition_refused():
            return None
        answer = f"{values[function]:.9f}"
        period, ring = self._conversion(self.profile).period_us, PROFILES[self.profile][1]
        count = self._acquisition_count()
        capture = self._new_capture(ring=ring, measuring=True)
        if not self.clocked:
            self._close_acquisition(capture, count)
            return answer
        # The clock-driven acquisition takes its samples' time and holds the console.
        now = self.clock()
        due = now + count * period / 1e6
        self._acquisition = _Acquisition(None, now, due, count, period, answer, capture)
        return None

    def _apply_range(self, call: _Call) -> bool:
        """``MEASure:CURRent?``'s range: the smallest that holds it, or a keyword's.

        :returns: Whether it was one; its error is queued when it was not.
        """
        usage = "use a range in amps (0.4096 or 1.6384), MIN, MAX or DEF"
        if call.params[0][:1].isalpha():
            which = self._choice(call, 0, ("MINimum", "MAXimum", "DEFault"), usage)
            if which is None:
                return False
            self._select_range(int(which == 0))
            return True
        amps = self._decimal(call, usage)
        if amps is None:
            return False
        if not 0 <= amps * 1_000_000 <= _FULL_SCALE_UA[0]:
            self._fail(-222, usage)
            return False
        self._select_range(int(amps * 1_000_000 <= _FULL_SCALE_UA[1]))
        return True

    def _new_capture(self, *, ring: bool, measuring: bool = False) -> _Capture:
        """A capture of the present profile, range and calibration, starting now.

        :param ring: Whether it captures to the ring.
        :param measuring: Whether it is a ``MEASure``'s acquisition.
        """
        relays = self._relay_tokens()
        conversion = self._conversion(self.profile)
        return _Capture(
            profile=self.profile,
            range=self.range,
            period=conversion.period_us,
            conversion=conversion,
            t0=self.t_us,
            shunt_uohm=round(self.range_shunt_ohm(self.range) * 1e6),
            offset_pv=self.offset_pv[self.range],
            ring=ring,
            channels=("i_ua",) if measuring else self._stream_channels(self.profile),
            measuring=measuring,
            relays=relays,
            pins_src=self.pins_src,
            thr_na=self.threshold_na,
            hyst_na=self.hysteresis_na,
        )

    def _close_acquisition(self, capture: _Capture, samples: int) -> None:
        """Close an acquisition's *capture* at *samples*: the last capture from here on."""
        self.marks.clear()
        if self.clocked:
            for index in range(samples):
                self._take(capture, index, self._sample_time(capture, index))
        else:
            capture.samples = samples
            self.ring = samples if capture.ring else 0
            self._t_us += samples * capture.period + 10_000
        capture.ended = True
        capture.end_us = self._sample_time(capture, samples) if self.clocked else self.t_us
        self.last_stats = capture

    @_handles("FETC:CURR?")
    def _handle_fetch_current(self, call: _Call) -> Reply:
        """``FETCh[:SCALar]:CURRent[:DC]?``."""
        return f"{self._shunt_a():.9f}"

    @_handles("FETC:HEAD?")
    def _handle_header(self, call: _Call) -> Reply:
        """``FETCh:HEADer?``."""
        if self.capture is None and self.last_stats is None:
            self._fail(-230, "no capture started since boot")
            return None
        capture = present(self.capture or self.last_stats, "a capture since boot")
        # From 0.9.0 STATE and SLEEP are paced by the ALERT pin, each edge
        # followed by the DIAG_ALRT read that releases it; the ring profiles
        # stay on the timer and do not probe.
        streams = not PROFILES[capture.profile][1]
        pace = (
            "pace=alert pace_edges=8 pace_expect=8"
            if streams
            else "pace=timer pace_edges=0 pace_expect=0"
        )
        conversion = capture.conversion
        word, mode, bus_us = conversion.word, conversion.mode, conversion.bus_us
        shunt_us, die_us, averages = conversion.shunt_us, conversion.die_us, conversion.count
        lsb_pa = _LSB_PA[capture.range]
        sink = "stats" if capture.stats_only else "stream" if streams else "ring"
        if capture.level_na is None:
            trigger = "trig_source=imm trig_level_na=- trig_slope=-"
        else:
            slope = "pos" if capture.rising else "neg"
            trigger = f"trig_source=level trig_level_na={capture.level_na} trig_slope={slope}"
        return (
            f"fw={self.firmware} chip=ina228 addr=0x{INA228_ADDRESS:02X} mfg={self.mfg}"
            f" die={self.die} rev=0x1 config=0x{capture.range << 4:04X} adc_config=0x{word:04X}"
            f" shunt_cal=4096 range={capture.range} diag_cfg=0xC001 alatch=1 sovl=0x7FF8"
            f" suvl=0x8008 mode=0x{mode:X} vbusct_us={bus_us} vshct_us={shunt_us}"
            f" vtct_us={die_us} avg={averages} period_us={capture.period} {pace}"
            f" osc_tol_ppm=10000 shunt_lsb_pv={_SHUNT_LSB_PV[capture.range]}"
            f" bus_lsb_pv=195312500 temp_lsb_nc=7812500 current_lsb_pa={lsb_pa}"
            f" power_lsb_pw={lsb_pa * 16 // 5} charge_lsb_pc={lsb_pa} charge_raw_start=0"
            f" shunt_uohm={capture.shunt_uohm} shunt_measured={int(self.shunt_measured)}"
            f" offset_pv={capture.offset_pv} off_die_mc={self._null_die_mc(capture.range)}"
            f" vbus_side={self.vbus_side} cal_tag={self.tag or '-'} q={self.quality}"
            f" die_mc_start=25500 vs_cur_delta=0 scl_hz={self.scl_hz} sink={sink}"
            f" ring={self._ring_count() if sink == 'ring' else 0} anchor_every={ANCHOR_EVERY}"
            f" t0_us={capture.t0} format={self.format.lower()} relays=1 {capture.relays}"
            f" {trigger} trig_pre={capture.pretrigger} limit={capture.limit}"
            f" avg_state={'on' if conversion.override else 'off'} pins_src={capture.pins_src}"
        )

    def _null_die_mc(self, index: int) -> int:
        """The die temperature range *index*'s null was taken at, millidegrees; 0 with none."""
        return 25_000 if self.nulled[index] else 0

    @_handles("FETC:STAT?")
    def _handle_statistics(self, call: _Call) -> Reply:
        """``FETCh:STATistics?``."""
        if self.last_stats is None:
            self._fail(-230, "no capture closed since boot")
            return None
        return self._statistics_report(self.last_stats)

    def _statistics_report(self, stats: _Capture) -> str:
        """``FETCh:STATistics?`` of the closed capture *stats*, in the firmware's key order."""
        bus = not PROFILES[stats.profile][1]
        if self.clocked and stats.samples:
            mean, low, high = stats.mean_na, stats.min_na, stats.max_na
            sd = f"{math.sqrt(stats.m2 / (stats.samples - 1)):.3f}" if stats.samples > 1 else "-"
            max_n, max_t = stats.max_n, self._sample_time(stats, stats.max_n)
            dur_us = (stats.end_us or stats.t0) - stats.t0
            above_n, rises = stats.above_n, stats.rises
        else:
            # The deterministic DUT is a constant: every sample is the mean.
            mean = low = high = self._shunt_a() * 1e9
            sd, max_n, max_t = "0.000", 0, stats.t0 + stats.period
            dur_us = stats.samples * stats.period
            above = stats.thr_na is not None and mean >= stats.thr_na
            above_n, rises = (stats.samples, 1) if above else (0, 0)
        uv = round(self.voltage_v * 1e6)
        uw_low, uw_high = round(self.voltage_v * low / 1000), round(self.voltage_v * high / 1000)
        extremes = (
            f" vmin_uv={uv} vmax_uv={uv} pmin_uw={uw_low} pmax_uw={uw_high}"
            if bus
            else " vmin_uv=- vmax_uv=- pmin_uw=- pmax_uw=-"
        )
        above_text = (
            " thr_na=- above_n=- above_us=- rises=-"
            if stats.thr_na is None
            else f" thr_na={stats.thr_na} above_n={above_n} above_us={above_n * stats.period}"
            f" rises={rises}"
        )
        charge_us = dur_us + 9_000
        lsb_pa = _LSB_PA[stats.range]
        charge_raw = round(mean * 1e3 * charge_us * 1e-6 / lsb_pa)
        batches, size = self._batches(stats)
        trig_n = "-" if stats.trig_n is None else str(stats.trig_n)
        trig_t = "-" if stats.trig_t is None else str(stats.trig_t)
        return (
            f"samples={stats.samples} dropped={stats.dropped} read_err=0 pace_fallback=0"
            f" pace_err_ppm=0 pace_unmet=0 dur_us={dur_us} mean_na={mean:.3f}"
            f" min_na={low:.3f} max_na={high:.3f} die_mc_end=25500 flags=0x{self.diag_flags:04X}"
            f" charge_raw_start=0 charge_raw_end={charge_raw} osc_tol_ppm=10000"
            f" q={self.quality} stale_edges=0"
            f" sd_na={sd} max_n={max_n} max_t_us={max_t}{extremes}"
            f" mean_charge_na={mean:.3f} charge_us={charge_us}"
            f"{above_text} batches={len(batches)} batch_samples={size}"
            f" trig_n={trig_n} trig_t_us={trig_t} thr_marks={stats.thr_marks} rebased=0"
            f" hold_teardowns={stats.hold_teardowns}"
        )

    def _batches(self, stats: _Capture) -> tuple[list[float], int]:
        """The closed capture's complete batch means, nanoamps, and their size."""
        if self.clocked:
            return stats.batches, stats.batch_size
        return [self._shunt_a() * 1e9] * min(stats.samples, 32), 1

    @_handles("FETC:STAT:BATC?")
    def _handle_batches(self, call: _Call) -> Reply:
        """``FETCh:STATistics:BATCh?``."""
        if self.last_stats is None:
            self._fail(-230, "no capture closed since boot")
            return None
        means, size = self._batches(self.last_stats)
        text = ",".join(f"{mean:.3f}" for mean in means) or "-"
        return f"batches={len(means)} batch_samples={size} means_na={text}"

    # -- INITiate, ABORt, TRACe -------------------------------------------------

    def _window_refused(self) -> bool:
        """A window that ended by itself but is not closed refuses a new one."""
        if self.zero_open:
            self._fail(112, "null window ended, not closed (ABORt first)")
        elif self.capture is not None:
            self._fail(-221, "capture ended, not closed (ABORt first)")
        return self.window_open

    @_handles("INIT")
    def _handle_initiate(self, call: _Call) -> Reply:
        """``INITiate[:IMMediate]``."""
        if self._window_refused():
            return None
        if self.path == "both":
            self._fail(101, "shunt bypassed (path=both: ROUTe:BYPass OFF, or INITiate:FORCe)")
            return None
        if self.path == "bypass":
            self._fail(101, "shunt bypassed (path=bypass: ROUTe:NULLpath OFF, or INITiate:FORCe)")
            return None
        ring = PROFILES[self.profile][1]
        level = self.trigger_source == "LEV"
        if ring and self.channels and not self.statistics_only:
            self._fail(
                -221,
                "a ring profile takes no channels (SENSe:FUNCtion:ON:DEFault; TRACe:DATA? after"
                " ABORt)",
            )
            return None
        if level and self.statistics_only:
            self._fail(
                -221,
                "a level trigger needs a ring capture (SENSe:CAPTure DATA, or TRIGger:SOURce"
                " IMMediate)",
            )
            return None
        if level and not ring:
            self._fail(-221, "a level trigger needs a ring profile (SENSe:PROFile TRACe or BURSt)")
            return None
        if self._pace_refused():
            return None
        capture_ring = ring and not self.statistics_only
        capture = self._new_capture(ring=capture_ring)
        capture.stats_only = self.statistics_only
        capture.limit = self.sample_count if self.capture_limited and not capture_ring else 0
        capture.level_na = self.trigger_level_na if level else None
        capture.rising = self.trigger_rising
        capture.pretrigger = self.pretrigger
        capture.thr_marks_on = self.threshold_marks and not ring and not self.statistics_only
        self.capture = capture
        self.marks.clear()
        self.ring = 0
        if not self.clocked:
            if self.statistics_only:
                capture.samples = 64
            elif ring:
                self.ring = capture.samples = 64
        return None

    @_handles("ABOR")
    def _handle_abort(self, call: _Call) -> Reply:
        """``ABORt``."""
        if self.path != "shunt" and self.window_open:
            self.hold_due = self.clock() + HOLD_S  # a window's close re-arms the hold
        if self.zero_open:
            self.zero_open = self.zero_ended = False
            self.nulled[self.range] = True
            offset = self.null_offset_pv[self.range] + self.null_drift_pv * self.nulls_taken
            self.offset_pv[self.range] = offset
            self.nulls_taken += 1
            self.zero_result = (
                f"running=0 range={self.range} offset_pv={offset} sd_pv=312500"
                f" over_s={self.zero_seconds} die_mc=25000 n={self._zero_samples()}"
                f" {self.zero_circuit} stored=1"
            )
            return None
        if self.capture is None:
            self._fail(111, "no capture or null window is open")
            return None
        capture = self.capture
        if not self.clocked and not capture.ring and not capture.stats_only:
            self._stream(2)
        self._close_capture()
        if not self.clocked:
            self._t_us += capture.samples * capture.period + 10_000
        return None

    def _close_capture(self) -> None:
        """Close the open capture into :attr:`last_stats`, its ring kept for a dump."""
        capture = present(self.capture, "the open capture")
        capture.ended = True
        capture.end_us = self.t_us
        self.last_stats = capture
        self.capture = None

    def _ends_itself(self) -> bool:
        """Whether the running window ends without an ``ABORt`` (``*OPC`` waits for it)."""
        if self.zero_open:
            return True
        capture = self.capture
        return capture is not None and (capture.ring or capture.limit > 0)

    @_handles("TRAC:MARK")
    def _handle_mark(self, call: _Call) -> Reply:
        """``TRACe:MARK``."""
        refusal = "a label is 1..15 printable characters with no space, comma, quote, ';' or '='"
        label = self._token(call, 'use TRACe:MARK "<label>"', 15, refusal)
        if label is None:
            return None
        if self.capture is None:
            self._fail(111, "no capture is open")
            return None
        if sum(1 for name, _, _ in self.marks if not name.startswith("hold:")) >= MAX_MARKS:
            self._fail(105, f"marks full ({MAX_MARKS} per capture)")
            return None
        index = self.capture.samples
        t = self._sample_time(self.capture, index)
        self.marks.append((label, index, t))
        self._mark_record(label, index, t)
        return None

    def _mark_record(self, label: str, index: int, t: int) -> None:
        """Write a mark record on the stream, in the stream's format."""
        if self.format == "CSV":
            self._emit(f"K,{index},{t},{label}")
        else:
            self._emit(f"METER_MARK mark={label} n={index} t={t}")

    @_handles("TRAC:POIN?")
    def _handle_points(self, call: _Call) -> Reply:
        """``TRACe:POINts?``."""
        return str(self._ring_count())

    @_handles("TRAC:POIN:MAX?")
    def _handle_points_max(self, call: _Call) -> Reply:
        """``TRACe:POINts:MAXimum?``."""
        return str(self.ring_capacity)

    def _ring_source(self) -> _Capture | None:
        """The capture the ring holds: the open one in the clock-driven mode, else the last."""
        if self.clocked and self.capture is not None:
            return self.capture if self.capture.ring else None
        return self.last_stats

    def _ring_count(self) -> int:
        """The samples the ring holds."""
        if not self.clocked:
            return self.ring
        source = self._ring_source()
        if source is None or not source.ring:
            return 0
        return min(source.samples, self.ring_capacity)

    @_handles("TRAC:DATA?")
    def _handle_dump(self, call: _Call) -> Reply:
        """``TRACe:DATA?``."""
        source = self._ring_source()
        if self._ring_count() == 0 or source is None:
            self._fail(-230, "no trace captured")
            return None
        usage = "use TRACe:DATA? [SCALed|RAW][,<from>[,<count>]]"
        raw = False
        offset = 0
        if call.params and call.params[0][:1].isalpha():
            which = self._choice(call, 0, ("SCALed", "RAW"), usage)
            if which is None:
                return None
            raw, offset = which == 1, 1
        if len(call.params) > offset + 2:
            self._fail(-108, usage)
            return None
        window = self._dump_window(call, offset, usage)
        if window is None:
            return None
        if self.clocked:
            return self._dump_ring(raw, source, *window)
        start_at, count = window
        stats = present(self.last_stats, "the capture the window was read from")
        counts = _raw_counts(stats, self._shunt_a())
        rows = [
            f"METER_DUMP count={count} t0_us={stats.t0} period_us={stats.period}"
            + (" fmt=raw20" if raw else f" ring={1 << 20}"),
            f"A n=0 t={stats.t0}",
        ]
        for label, index, _ in self.marks:
            rows.append(f"K n={index} label={label}")
        if raw:
            last = start_at + count
            rows.extend(
                f"R n={first} c=" + ",".join([str(counts)] * min(16, last - first))
                for first in range(start_at, last, 16)
            )
        else:
            value = _thousandths(_nanoamps(stats, counts))
            for index in range(start_at, start_at + count):
                t = stats.t0 + index * stats.period
                rows.append(f"METER t={t} ch=i_ua v={value}")
        return self._seal(rows)

    def _seal(self, rows: list[str]) -> bytes:
        """The block of *rows*, closed by its CRC row (damaged under it when asked)."""
        body = ("\n".join(rows) + "\n").encode("ascii")
        crc = zlib.crc32(body)
        if self.corrupt_dump:
            body = body.replace(b" n=0", b" n=1", 1)
        return body + f"METER_DUMP_END crc32={crc & 0xFFFFFFFF:08x} bytes={len(body)}\n".encode()

    def _dump_window(self, call: _Call, offset: int, usage: str) -> tuple[int, int] | None:
        """Where a dump starts and how many samples it takes, or None with its error queued.

        :param offset: The index of the parameter that says where it starts.
        """
        captured = self._ring_count()
        start_at = 0
        if len(call.params) > offset:
            start_at_or_none = self._unsigned(call, offset, usage)
            if start_at_or_none is None:
                return None
            start_at = start_at_or_none
        if start_at >= captured:
            self._fail(-222, f"{usage} ({captured} samples captured)")
            return None
        count = captured - start_at
        if len(call.params) > offset + 1:
            requested = self._unsigned(call, offset + 1, usage)
            if requested is None:
                return None
            if requested == 0:
                self._fail(-224, usage)
                return None
            count = min(count, requested)
        return start_at, count

    def _dump_ring(self, raw: bool, stats: _Capture, start_at: int, count: int) -> bytes:
        """The clock-driven ring as ``writeDump`` writes it.

        Anchors, marks and the trigger are interleaved with the rows they
        fall in, and every index is the capture's own.
        """
        captured = self._ring_count()
        first = stats.samples - captured
        extra = ""
        if stats.trig_n is not None:
            extra = f" first={first} trig_n={stats.trig_n}"
        elif first:
            extra = f" first={first}"
        tail = " fmt=raw20" if raw else f" ring={self.ring_capacity}"
        rows = [f"METER_DUMP count={count} t0_us={stats.t0} period_us={stats.period}{tail}{extra}"]
        start, last = first + start_at, first + start_at + count
        anchor = (first + ANCHOR_EVERY - 1) // ANCHOR_EVERY * ANCHOR_EVERY
        while anchor < start:
            rows.append(f"A n={anchor} t={self._sample_time(stats, anchor)}")
            anchor += ANCHOR_EVERY
        marks = [(index, label) for label, index, _ in self.marks if index >= start]
        index = start
        while index < last:
            row_end = min(last, index + 16) if raw else index + 1
            while anchor < row_end:
                rows.append(f"A n={anchor} t={self._sample_time(stats, anchor)}")
                anchor += ANCHOR_EVERY
            while marks and marks[0][0] < row_end:
                rows.append(f"K n={marks[0][0]} label={marks[0][1]}")
                marks.pop(0)
            if stats.trig_n is not None and index <= stats.trig_n < row_end:
                rows.append(f"T n={stats.trig_n} t={stats.trig_t}")
            if raw:
                counts = [self._counts(stats, n) for n in range(index, row_end)]
                rows.append(f"R n={index} c=" + ",".join(map(str, counts)))
            else:
                value = _thousandths(_nanoamps(stats, self._counts(stats, index)))
                rows.append(f"METER t={stats.t0 + index * stats.period} ch=i_ua v={value}")
            index = row_end
        return self._seal(rows)

    def _counts(self, capture: _Capture, index: int) -> int:
        """The raw count of sample *index* of *capture* (:func:`_raw_counts`)."""
        return _raw_counts(capture, self._reading_at(capture, self._sample_time(capture, index)))

    def _reading_at(self, capture: _Capture, t_us: int) -> float:
        """What a sample of *capture* taken at *t_us* reads: the DUT's current and the noise.

        The noise is :attr:`noise_a` for a profile's own word and falls with
        the square root of the samples the override averages for each one
        (count times shunt conversion time, over the profile's own); a sample
        time gives the same noise every time it is read, so a dumped ring and
        the statistics taken while it filled agree.

        :param capture: The capture, whose conversion the noise follows.
        :param t_us: The sample's time.
        :returns: Amperes.
        """
        amps = self.current_at(t_us)
        if not self.noise_a:
            return amps
        own = CONVERSIONS[capture.profile]
        averaged = capture.conversion.shunt_us * capture.conversion.count
        sigma = self.noise_a * math.sqrt(own[3] * own[5] / averaged)
        return amps + sigma * _normal(t_us)

    # -- the clock-driven sampler ---------------------------------------------

    def _advance(self, until_us: int) -> None:
        """Take every sample of the running capture due by *until_us*."""
        capture = self.capture
        if capture is None or capture.ended:
            return
        while not capture.ended:
            t = self._sample_time(capture, capture.samples)
            if t > until_us:
                return
            self._take(capture, capture.samples, t)

    def _take(self, capture: _Capture, index: int, t: int) -> None:
        """One clock-driven sample: counted, streamed or ringed, and checked for an end."""
        value = _nanoamps(capture, _raw_counts(capture, self._reading_at(capture, t)))
        capture.samples = index + 1
        capture.last_na = value
        delta = value - capture.mean_na
        capture.mean_na += delta / capture.samples
        capture.m2 += delta * (value - capture.mean_na)
        capture.min_na = min(capture.min_na, value)
        if value > capture.max_na:
            capture.max_na, capture.max_n = value, index
        self._batch(capture, value)
        crossing = self._threshold(capture, value)
        if not (capture.ring or capture.stats_only or capture.measuring):
            self._stream_sample(capture, index, t, value / 1000)
        if crossing and capture.thr_marks_on and capture.thr_marks < MAX_THRESHOLD_MARKS:
            capture.thr_marks += 1
            self._mark_record(crossing, index, t)
        if capture.level_na is not None and capture.trig_n is None:
            self._trigger(capture, index, t, value)
        if self._full(capture):
            capture.ended = True

    def _batch(self, capture: _Capture, value: float) -> None:
        """File *value* in the batch means: a batch doubles whenever 64 are complete."""
        capture.batch_sum += value
        capture.batch_count += 1
        if capture.batch_count < capture.batch_size:
            return
        capture.batches.append(capture.batch_sum / capture.batch_size)
        capture.batch_sum, capture.batch_count = 0.0, 0
        if len(capture.batches) == 64:
            pairs = capture.batches
            capture.batches = [(pairs[i] + pairs[i + 1]) / 2 for i in range(0, 64, 2)]
            capture.batch_size *= 2

    def _threshold(self, capture: _Capture, value: float) -> str | None:
        """Count *value* against the threshold; the crossing it makes, if any."""
        if capture.thr_na is None:
            return None
        crossing = None
        if not capture.above and value >= capture.thr_na:
            capture.above, crossing = True, "thr_rise"
            capture.rises += 1
        elif capture.above and value < capture.thr_na - capture.hyst_na:
            capture.above, crossing = False, "thr_fall"
        if capture.above:
            capture.above_n += 1
        return crossing

    def _trigger(self, capture: _Capture, index: int, t: int, value: float) -> None:
        """Arm on the level's far side, fire on crossing it in the slope's direction."""
        level = present(capture.level_na, "the trigger's level")
        beyond = value >= level if capture.rising else value <= level
        if not beyond:
            capture.trigger_ready = True
        elif capture.trigger_ready:
            capture.trig_n, capture.trig_t = index, t

    def _full(self, capture: _Capture) -> bool:
        """Whether *capture* has taken every sample it ends by itself after."""
        if capture.limit and capture.samples >= capture.limit:
            return True
        if not capture.ring:
            return False
        if capture.level_na is None:
            return capture.samples >= self.ring_capacity
        if capture.trig_n is None:
            return False
        return capture.samples >= capture.trig_n + self.ring_capacity - capture.pretrigger

    def _stream_sample(self, capture: _Capture, index: int, t: int, micro: float) -> None:
        """Write one clock-driven sample, or count it dropped when the output is full."""
        values = {
            "i_ua": f"{micro:.3f}",
            "v_mv": f"{self.voltage_v * 1e3:.3f}",
            "p_mw": f"{micro * self.voltage_v / 1000:.3f}",
            "die_c": "25.500",
        }
        if self.format == "CSV":
            row = [values[c] if c in capture.channels else "" for c in values]
            text = f"S,{t}," + ",".join(row) + "\n"
        else:
            text = "".join(f"METER t={t} ch={c} v={values[c]}\n" for c in capture.channels)
        if len(self._out) + len(text) > self.tx_capacity:
            capture.dropped += 1
            return
        self._out += text.encode("ascii")

    def _stream(self, count: int) -> None:
        """Deterministic: write *count* samples of the constant DUT."""
        capture = present(self.capture, "the running capture")
        for _ in range(count):
            t = capture.t0 + capture.samples * capture.period
            capture.samples += 1
            values = {
                "i_ua": f"{self._shunt_a() * 1e6:.3f}",
                "v_mv": f"{self.voltage_v * 1e3:.3f}",
                "p_mw": f"{self._shunt_a() * self.voltage_v * 1e3:.3f}",
                "die_c": "25.500",
            }
            if self.format == "CSV":
                row = [values[c] if c in capture.channels else "" for c in values]
                self._emit(f"S,{t}," + ",".join(row))
            else:
                for channel in capture.channels:
                    self._emit(f"METER t={t} ch={channel} v={values[channel]}")

    # -- the relays -------------------------------------------------------------

    def _service_hold(self) -> None:
        """Put the coils back to rest once the hold on a path but ``shunt`` ran out.

        A null window suspends the hold, as one that samples does on the
        instrument; nothing else does (from 0.16.0 not a capture either). The
        release is make-before-break, K1 recloses before K2 opens, and queues
        ``113`` from ``path=bypass``, ``114`` from ``path=none`` and ``115``
        from ``path=both``. Under a capture each move is marked
        (``hold:k1:closed``, ``hold:k2:open``) and counted in its ``hold_teardowns``.
        """
        if self.hold_due is None:
            return
        if self.zero_open:
            self.hold_due = self.clock() + HOLD_S
            return
        if self.clock() < self.hold_due:
            return
        path = self.path
        moves = [label for label, moved in _HOLD_MOVES if moved(self)]
        self.k1_open = self.k2_closed = False
        self.hold_due = self.lead_cycle_due = None
        self.hold_released += 1
        under = ""
        if self.capture is not None:
            self.capture.hold_teardowns += 1
            under = " under a capture, marked hold:"
            index = self.capture.samples
            t = self._sample_time(self.capture, index)
            for label in moves:
                self.marks.append((label, index, t))
                self._mark_record(label, index, t)
        code, detail = _HOLD_RELEASES[path]
        self._fail(code, detail.format(hold=HOLD_S, under=under))

    @property
    def path(self) -> str:
        """How the DUT's battery is fed, from K1 and K2 (``relay.h`` ``pathOf``)."""
        return _PATHS[(self.k1_open, self.k2_closed)]

    @property
    def shunt_bypassed(self) -> bool:
        """Whether the shunt does not carry the DUT's whole current: ``both`` or ``bypass``."""
        return self.path in ("both", "bypass")

    def _relay_order(
        self, call: _Call, steps: Sequence[tuple[str, bool]], *, null_path: bool = False
    ) -> bool:
        """Run a relay order of *steps* (``k1``/``k2``, energized), with the firmware's interlocks.

        An order that moves nothing is answered whatever runs. One that moves
        a contact is refused under a sampling null window (``112``), under a
        running capture without ``LIVE`` (``-221``), under one that it would
        take off the DUT's current without ``FORCe`` (``110``); and any that
        leaves the DUT with no supply needs ``FORCe`` (``109``). The null
        path takes ``FORCe`` only after ``LIVE`` (``-224``). Every order
        arms the hold again when it leaves a path but ``shunt``.

        :param call: The order; its parameters after the first are the flags.
        :param steps: The coils it drives, in order.
        :param null_path: Whether it is ``ROUTe:NULLpath``.
        :returns: Whether it ran.
        """
        flags = [p.upper()[:4] for p in call.params[1:]]
        force, live = "FORC" in flags, "LIVE" in flags
        if null_path and force and not live:
            self._fail(-224, "use ROUTe:NULLpath ON|OFF[,LIVE[,FORCe]]")
            return False
        self.moved = "-"  # a refused order leaves no result standing
        self.order_settle_us = self.order_live = self.order_marked = 0
        state = {"k1": self.k1_open, "k2": self.k2_closed}
        plan = [(coil, on) for coil, on in steps if state[coil] != on]
        if plan and self.running:
            if self.zero_open:
                self._fail(112, "null window running (ABORt first)")
                return False
            if not live:
                self._fail(-221, "capture running (ABORt first, or add LIVE)")
                return False
            # running and no null window: a capture runs
            capture = present(self.capture, "the running capture")
            if not force and not capture.bypassed and self._bypasses(plan):
                self._fail(110, "order bypasses the shunt under a capture (add FORCe)")
                return False
        if not force and self._unpowers(plan):
            self._fail(109, "order disconnects the dut (k1 open, k2 open: add FORCe)")
            return False
        marks = len(self.marks)
        for coil, on in plan:
            if coil == "k1":
                self.k1_open = on
            else:
                self.k2_closed = on
            self._live_mark(coil, on)
        self.order_settle_us = sum(
            _OPERATE_SETTLE_US if on else _RELEASE_SETTLE_US for _, on in plan
        )
        self.order_live = int(bool(plan) and self.running)
        self.order_marked = len(self.marks) - marks
        self.moved = ",".join(coil for coil, _ in plan) or "-"
        # Every order arms the hold again, one that moves nothing too.
        self.hold_due = None if self.path == "shunt" else self.clock() + HOLD_S
        return True

    def _live_mark(self, coil: str, energized: bool) -> None:
        """Mark a move under a running capture (``k1:open``, ``k2:closed``), as a LIVE move is.

        A move that takes the shunt off the DUT's current marks the capture
        bypassed, so later orders need no ``FORCe``; past the 16 marks a move
        is driven unmarked, as on the instrument.
        """
        capture = self.capture
        if capture is None or not self.running:
            return
        if self.shunt_bypassed:
            capture.bypassed = True
        word = ("closed", "open")[energized == (coil == "k1")]
        if sum(1 for name, _, _ in self.marks if not name.startswith("hold:")) < MAX_MARKS:
            index = capture.samples
            t = self._sample_time(capture, index)
            self.marks.append((f"{coil}:{word}", index, t))
            self._mark_record(f"{coil}:{word}", index, t)

    def _states(self, plan: Sequence[tuple[str, bool]]) -> list[str]:
        """The path now and after each step of *plan*."""
        k1, k2 = self.k1_open, self.k2_closed
        paths = [_PATHS[(k1, k2)]]
        for coil, on in plan:
            k1, k2 = (on, k2) if coil == "k1" else (k1, on)
            paths.append(_PATHS[(k1, k2)])
        return paths

    def _unpowers(self, plan: Sequence[tuple[str, bool]]) -> bool:
        """Whether a step of *plan* moves the path into ``none`` (``unpowersDut``)."""
        paths = self._states(plan)
        return any(b == "none" and a != "none" for a, b in itertools.pairwise(paths))

    def _bypasses(self, plan: Sequence[tuple[str, bool]]) -> bool:
        """Whether the shunt stops carrying the whole current at any point (``bypassesShunt``)."""
        return any(path in ("both", "bypass") for path in self._states(plan))

    @_handles("ROUT:LEAD")
    def _handle_lead(self, call: _Call) -> Reply:
        """``ROUTe:LEAD``."""
        which = self._choice(call, 0, ("OPEN", "CLOSe"), "use ROUTe:LEAD OPEN|CLOSe[,FORCe][,LIVE]")
        if which is not None and self._relay_order(call, [("k1", which == 0)]):
            self.lead_cycle_due = None  # an explicit order cancels a pending cycle
        return None

    @_handles("ROUT:LEAD:CYCL")
    def _handle_lead_cycle(self, call: _Call) -> Reply:
        """``ROUTe:LEAD:CYCLe``."""
        usage = "use ROUTe:LEAD:CYCLe <ms>,FORCe[,LIVE] (1..60000 ms)"
        milliseconds = self._unsigned(call, 0, usage)
        if milliseconds is None:
            return None
        if not 1 <= milliseconds <= 60000:
            self._fail(-222, usage)
            return None
        if "FORC" not in [p.upper()[:4] for p in call.params[1:]]:
            self._fail(109, "order disconnects the dut (k1 open, k2 open: add FORCe)")
            return None
        if self.path != "shunt":
            self._fail(-221, f"a lead cycle starts from path=shunt (path={self.path})")
            return None
        if self._relay_order(call, [("k1", True)]):
            self.lead_cycle_due = self.clock() + milliseconds / 1000
        return None

    @_handles("ROUT:BYP")
    def _handle_bypass(self, call: _Call) -> Reply:
        """``ROUTe:BYPass``."""
        on = self._boolean(call, "use ROUTe:BYPass ON|OFF[,FORCe][,LIVE]")
        if on is not None:
            self._relay_order(call, [("k2", on)])
        return None

    @_handles("ROUT:REL#")
    def _handle_relay(self, call: _Call) -> Reply:
        """``ROUTe:RELay#``."""
        number = 1 if call.suffix is None else call.suffix
        if number in (1, 2):
            self._fail(-114, f"k{number} has its own order")
        elif number in (3, 4):
            if self._boolean(call, "use ROUTe:RELay3|RELay4 ON|OFF[,LIVE]") is not None:
                self._fail(-241, f"k{number} is not wired on this rig (docs/rig-wiring.md)")
        else:
            self._fail(-114, "use ROUTe:RELay3|RELay4 ON|OFF[,LIVE]")
        return None

    @_handles("ROUT:NULL")
    def _handle_null_path(self, call: _Call) -> Reply:
        """``ROUTe:NULLpath``: make-before-break, K2 then K1 on, K1 then K2 off.

        ``ON`` checks the path it makes on the board's wiring
        (:func:`~kleidos_meter.pins.relay_check`): with the live map's relays
        where the wiring has them the bypass takes the current and the lead
        leaves the shunt dead, and the map's relays are verified; a relay on
        another pad or level fails its check (``103`` the bypass, ``104`` the
        lead) and the path is released to ``shunt``.
        """
        on = self._boolean(call, "use ROUTe:NULLpath ON|OFF[,LIVE[,FORCe]]")
        if on is None:
            return None
        steps = [("k2", True), ("k1", True)] if on else [("k1", False), ("k2", False)]
        if not self._relay_order(call, steps, null_path=True) or not on:
            return None
        failed = pins.relay_check(self.pins_live, self.wiring)
        if failed is None:
            self.pins_verified = True
            return None
        self.pins_verified = False
        self.k1_open = self.k2_closed = False
        self.hold_due = None
        shunt_pv = round(self.current_a * self.shunt_ohm * 1e12)
        if failed == "lost":
            detail = (
                f"bypass check failed (the shunt lost all of the dut's current: vs_pv={shunt_pv},0"
            )
            code = 103
        elif failed == "bypass":
            detail = f"bypass check failed (k2 took no current: vs_pv={shunt_pv},{shunt_pv}"
            code = 103
        else:
            detail = f"lead check failed (the shunt still conducts: vs_pv={shunt_pv},0,{shunt_pv}"
            code = 104
        self._fail(code, f"{detail}; released to path=shunt)")
        return None

    @_handles("ROUT:STAT?")
    def _handle_route_state(self, call: _Call) -> Reply:
        """``ROUTe:STATe?``."""
        return (
            f"{self._relay_tokens()}"
            " pads=ok operate_settle_ms=20 release_settle_ms=50"
            f" hold_s={self._hold_text()} hold_released={self.hold_released}"
            f" bypass_part={self.bypass_part} relay1_gpio={self.pins_live.words()['relay1']}"
            f" relay1_active={self.pins_live.relay1_active}"
            f" relay2_gpio={self.pins_live.words()['relay2']}"
            f" relay2_active={self.pins_live.relay2_active}"
        )

    @_handles("ROUT:RES?")
    def _handle_route_result(self, call: _Call) -> Reply:
        """``ROUTe:RESult?``."""
        return (
            f"{self._relay_tokens()}"
            f" moved={self.moved} settle_us={self.order_settle_us} live={self.order_live}"
            f" marked={self.order_marked} vs_pv=- hold_s={self._hold_text()} q={self.quality}"
        )

    def _hold_text(self) -> str:
        """The seconds left of the hold on a path but ``shunt``; ``-`` without one."""
        if self.hold_due is None:
            return "-"
        left_us = round((self.hold_due - self.clock()) * 1e6)  # whole, as the firmware counts
        return str(max(0, -(-left_us // 1_000_000)))

    def _coils(self) -> str:
        """K1's and K2's report tokens."""
        return (
            f"k1=lead:{'open' if self.k1_open else 'closed'}"
            f" k2=bypass:{'closed' if self.k2_closed else 'open'}"
        )

    def _relay_tokens(self) -> str:
        """The circuit, as every relay reply, capture header and null report gives it."""
        return f"{self._coils()} k3=spare:unwired k4=spare:unwired path={self.path} check=-"

    # -- DIAGnostic ---------------------------------------------------------------

    @_handles("DIAG:WATC?")
    def _handle_watchdog(self, call: _Call) -> Reply:
        """``DIAGnostic:WATChdog?``."""
        return f"console=1 sampler=1 timeout_s={self.watchdog_timeout_s} panic=1 stalls=0"

    @_handles("DIAG:WATC:TEST")
    def _handle_watchdog_test(self, call: _Call) -> Reply:
        """``DIAGnostic:WATChdog:TEST``."""
        self.watchdog_due = self.clock() + self.watchdog_timeout_s
        return None

    @_handles("DIAG:I2C:SCAN?")
    def _handle_scan(self, call: _Call) -> Reply:
        """``DIAGnostic:I2C:SCAN?``."""
        # Only the pads the live map's bus is on are scanned.
        on_bus = (self.pins_live.sda, self.pins_live.scl) == (self.wiring.sda, self.wiring.scl)
        found = sorted(self.scan_found) if on_bus else []
        listed = ",".join(f"0x{address:02X}" for address in found) or "-"
        return f"expected=0x{INA228_ADDRESS:02X} found={listed} n={len(found)}"

    @_handles("DIAG:HEAP?")
    def _handle_heap(self, call: _Call) -> Reply:
        """``DIAGnostic:HEAP?``."""
        # The steady state the firmware holds: nothing allocated from app_main on.
        return (
            "armed=1 allocs_after_init=0 frees_after_init=0 maint_allocs=0 maint_frees=0"
            " free_internal=307200 min_free_internal=286720 largest_internal=204800"
            f" free_spiram=4194304 pre_main_allocs={self.pre_main_allocs}"
            f" pre_main_frees={self.pre_main_frees} allocs_since_main=0 frees_since_main=0"
        )

    # -- CALibration ----------------------------------------------------------------

    @_handles("CAL:ZERO")
    def _handle_zero(self, call: _Call) -> Reply:
        """``CALibration:ZERO``."""
        usage = "use CALibration:ZERO <seconds> (1..600)"
        seconds = self._unsigned(call, 0, usage)
        if seconds is None:
            return None
        if self.path == "both":
            self._fail(
                101, "shunt bypassed (path=both: ROUTe:BYPass OFF or ROUTe:NULLpath ON first)"
            )
            return None
        if self.lead_cycle_due is not None:
            # 0.15.0: a null window would refuse the cycle's reclose.
            self._fail(-221, "a lead cycle is pending (wait for its reclose, or ROUTe:LEAD CLOSe)")
            return None
        if not 1 <= seconds <= MAX_ZERO_S:
            self._fail(-222, usage)
            return None
        if self._window_refused() or self._pace_refused():
            return None
        self.zero_open, self.zero_ended = True, False
        self.zero_seconds = seconds
        self.zero_due = self.clock() + seconds
        self.zero_circuit = self._relay_tokens()
        return None

    def _zero_samples(self) -> int:
        """The samples the null window has taken: all of them off the clock."""
        expected = self._zero_expected()
        if not self.clocked or self.zero_due is None:
            return expected
        elapsed_us = round((self.clock() - (self.zero_due - self.zero_seconds)) * 1e6)
        return min(expected, elapsed_us // self._conversion(self.profile).period_us)

    def _zero_expected(self) -> int:
        """The samples a whole null window takes, at the profile's period."""
        return self.zero_seconds * 1_000_000 // self._conversion(self.profile).period_us

    @_handles("CAL:ZERO:RES?")
    def _handle_zero_result(self, call: _Call) -> Reply:
        """``CALibration:ZERO:RESult?``."""
        if self.zero_open:
            # Open until ABORt, its samples all taken or not (Sampler::zeroOpen()).
            return (
                f"running=1 profile={self.profile} over_s={self.zero_seconds}"
                f" n_expect={self._zero_expected()} samples={self._zero_samples()}"
                f" {self.zero_circuit}"
            )
        if self.zero_result is None:
            self._fail(-230, "no null taken since boot")
            return None
        return self.zero_result

    @_handles("SENS:CURR:SHUN")
    def _handle_shunt_set(self, call: _Call) -> Reply:
        """``SENSe:CURRent:SHUNt``."""
        ohms = self._ohms(call, "use SENSe:CURRent:SHUNt <ohms> (0.05..0.2)", 0.05, 0.2)
        if ohms is not None:
            self.shunt_ohm, self.shunt_measured = ohms, True
            self.shunt_range_ohm = [None, None]
        return None

    def _ohms(self, call: _Call, usage: str, low: float, high: float) -> float | None:
        """Parameter 0 in ohms, held to *low*..*high*, or None with its error queued."""
        value = self._decimal(call, usage)
        if value is None:
            return None
        ohms = float(value)
        if not low <= ohms <= high:
            self._fail(-222, usage)
            return None
        return ohms

    @_handles("SENS:CURR:SHUN:RANG#", "SENS:CURR:SHUN:RANG#?")
    def _handle_shunt_range(self, call: _Call) -> Reply:
        """``SENSe:CURRent:SHUNt:RANGe#``, ``SENSe:CURRent:SHUNt:RANGe#?``."""
        if call.suffix not in (0, 1):
            self._fail(-114, "name the range: SENSe:CURRent:SHUNt:RANGe0|RANGe1")
            return None
        index = call.suffix
        if call.header.endswith("?"):
            return f"{self.range_shunt_ohm(index):.6f}".rstrip("0")
        usage = "use SENSe:CURRent:SHUNt:RANGe<n> <ohms> (0.05..0.2)"
        ohms = self._ohms(call, usage, 0.05, 0.2)
        if ohms is not None:
            self.shunt_range_ohm[index], self.shunt_measured = ohms, True
        return None

    @_handles("CAL:DATE")
    def _handle_date_set(self, call: _Call) -> Reply:
        """``CALibration:DATE``."""
        usage = "use CALibration:DATE <year>,<month>,<day>"
        if len(call.params) < 3:
            self._fail(-109, usage)
            return None
        fields = []
        for index in range(3):
            value = self._unsigned(call, index, usage)
            if value is None:
                return None
            fields.append(value)
        year, month, day = fields
        try:
            dt.date(year, month, day)
            valid = 2026 <= year <= 2099
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            self._fail(-222, "not a calendar date in 2026..2099")
            return None
        self.cal_date = (year, month, day)
        return None

    @_handles("CAL:DATE?")
    def _handle_date_query(self, call: _Call) -> Reply:
        """``CALibration:DATE?``."""
        return ",".join(str(v) for v in (self.cal_date or (0, 0, 0)))

    @_handles("SENS:CURR:SHUN?")
    def _handle_shunt_query(self, call: _Call) -> Reply:
        """``SENSe:CURRent:SHUNt?``."""
        return f"{self.shunt_ohm:.6f}".rstrip("0")

    @_handles("CAL:TAG")
    def _handle_tag_set(self, call: _Call) -> Reply:
        """``CALibration:TAG``."""
        usage = 'use CALibration:TAG "<text>" (23 characters at most)'
        refusal = "a tag is printable characters with no space, comma, quote, ';' or '='"
        tag = self._token(call, usage, 23, refusal)
        if tag is not None:
            self.tag = tag
        return None

    @_handles("CAL:TAG?")
    def _handle_tag_query(self, call: _Call) -> Reply:
        """``CALibration:TAG?``."""
        return quote(self.tag)

    @_handles("CAL:VBUS")
    def _handle_vbus_set(self, call: _Call) -> Reply:
        """``CALibration:VBUSside``."""
        usage = "use CALibration:VBUSside SOURce|UNKNown"
        which = self._choice(call, 0, ("SOURce", "UNKNown"), usage)
        if which is not None:
            self.vbus_side = ("source", "unknown")[which]
        return None

    @_handles("CAL:VBUS?")
    def _handle_vbus_query(self, call: _Call) -> Reply:
        """``CALibration:VBUSside?``."""
        return self.vbus_side.upper()

    @_handles("CAL:DATA?")
    def _handle_calibration_data(self, call: _Call) -> Reply:
        """``CALibration:DATA?``."""
        date = "-" if self.cal_date is None else "{:04d}-{:02d}-{:02d}".format(*self.cal_date)
        return (
            f"shunt_uohm={round(self.shunt_ohm * 1e6)}"
            f" shunt_measured={int(self.shunt_measured)}"
            f" off_pv_r0={self.offset_pv[0]} off_pv_r1={self.offset_pv[1]}"
            f" nulled_r0={int(self.nulled[0])} nulled_r1={int(self.nulled[1])}"
            f" off_die_mc_r0={self._null_die_mc(0)} off_die_mc_r1={self._null_die_mc(1)}"
            f" vbus_side={self.vbus_side} cal_tag={self.tag or '-'}"
            f" shunt_uohm_r0={round(self.range_shunt_ohm(0) * 1e6)}"
            f" shunt_uohm_r1={round(self.range_shunt_ohm(1) * 1e6)} cal_date={date}"
        )

    @_handles("CAL:SHUN:REF")
    def _handle_reference_set(self, call: _Call) -> Reply:
        """``CALibration:SHUNt:REFerence``."""
        usage = "use CALibration:SHUNt:REFerence <ohms> (1..4000)"
        ohms = self._ohms(call, usage, 1.0, 4000.0)
        if ohms is not None:
            self.reference_ohm = ohms
        return None

    @_handles("CAL:SHUN:REF?")
    def _handle_reference_query(self, call: _Call) -> Reply:
        """``CALibration:SHUNt:REFerence?``."""
        return f"{self.reference_ohm:.6f}".rstrip("0")

    @_handles("CAL:SHUN")
    def _handle_shunt_point(self, call: _Call) -> Reply:
        """One ``CALibration:SHUNt`` against the fitted reference."""
        if not self.nulled[self.range]:
            self._fail(-221, f"range {self.range} has no null (CALibration:ZERO first)")
            return None
        if self.profile not in ("STATE", "SLEEP"):
            self._fail(
                -230, "the acquisition converted no bus voltage (SENSe:PROFile STATe or SLEep)"
            )
            return None
        if self._acquisition_refused():
            return None
        drifted = self.true_shunt_ohm * (1 + self.drift_ppm_per_point * self.points_taken * 1e-6)
        self.points_taken += 1
        current = 4.1 / (self.fitted_reference_ohm + drifted)
        shunt = drifted * (1 + self.range_gain_ppm[self.range] * 1e-6)
        vsh_nv = round(current * shunt * 1e9)
        vref_uv = (
            round(current * self.fitted_reference_ohm * 1e6) if self.vbus_on_reference else 3_000
        )
        if vref_uv < 1_000_000:
            check, r_nohm = "reference_low", 0
        else:
            r_nohm = round(vsh_nv * self.reference_ohm * 1e6 / vref_uv)
            deviation = (r_nohm - 100_000_000) / 100
            check = "ok" if abs(deviation) <= 20_000 else "out_of_window"
        self.shunt_point = (
            f"range={self.range} r_nohm={r_nohm} r_uohm={round(r_nohm / 1000)}"
            f" dev_ppm={round((r_nohm - 100_000_000) / 100)} i_na={round(current * 1e9)}"
            f" vsh_nv={vsh_nv} vref_uv={vref_uv} ref_uohm={round(self.reference_ohm * 1e6)}"
            f" n={self.sample_count} die_mc=25000 check={check}"
        )
        return None

    @_handles("CAL:SHUN:RES?")
    def _handle_shunt_result(self, call: _Call) -> Reply:
        """``CALibration:SHUNt:RESult?``."""
        if self.shunt_point is None:
            self._fail(-230, "no shunt point measured since boot (CALibration:SHUNt)")
            return None
        return self.shunt_point


#: The patterns of the firmware's table the simulator answers.
IMPLEMENTED: frozenset[str] = frozenset(
    pattern for pattern, *_ in COMMANDS if pattern not in UNDEFINED
)
