# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The wire format of the meter's SCPI protocol (``docs/scpi.md``), with no I/O.

Splitting messages and responses into units, the error queue's entries, the
``key=value`` reports, the stream records, the definite-length block, the
ring dump that block carries (:func:`decode_dump`), and the
:class:`LineRouter` that turns the bytes of the line, however they are cut,
into responses, stream records and block progress.
"""

from __future__ import annotations

import io
import re
import zlib
from array import array
from bisect import bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

#: One report token: ``key=value``, the value holding no space.
_FIELD = re.compile(r"\b(\w+)=(\S+)")

#: One error of ``SYSTem:ERRor:ALL?``: ``<code>,"<text>"``, a doubled quote
#: standing for one inside the text.
_ERROR = re.compile(r'([+-]?\d+),"((?:[^"]|"")*)"')

#: The stream channels, and the SCPI function each is chosen by.
CHANNEL_FUNCTIONS = {"i_ua": "CURR", "v_mv": "VOLT", "p_mw": "POW", "die_c": "TEMP"}

#: The CSV record's columns after the timestamp, in order.
CSV_CHANNELS = ("i_ua", "v_mv", "p_mw", "die_c")

#: Each channel's value as an integer: its thousandths, named by the unit they
#: are (nanoamps, microvolts, microwatts, millidegrees), as the reports name them.
CHANNEL_INTEGER_KEYS = {"i_ua": "i_na", "v_mv": "v_uv", "p_mw": "p_uw", "die_c": "die_mc"}


def split_outside_quotes(text: str, separator: str) -> list[str]:
    """Split *text* at every *separator* outside a quoted string.

    :param text: What to split.
    :param separator: One character, not a quote.
    :returns: Its pieces, in order, unstripped.
    """
    pieces: list[str] = []
    current: list[str] = []
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == separator:
            pieces.append("".join(current))
            current = []
            continue
        current.append(char)
    pieces.append("".join(current))
    return pieces


def split_units(text: str) -> list[str]:
    """Split a message or a response at every ``;`` outside a quoted string.

    :param text: A program message or a response line.
    :returns: Its units, in order, unstripped.
    """
    return split_outside_quotes(text, ";")


def parse_fields(report: str) -> dict[str, str]:
    """Return the ``key=value`` tokens of a report unit.

    :param report: The report.
    :returns: Its fields, as text.
    """
    return dict(_FIELD.findall(report))


@dataclass(frozen=True)
class ErrorEntry:
    """One entry of the instrument's error queue.

    :ivar code: The error's number: negative for a standard one, positive for the
        instrument's own (``docs/scpi.md``).
    :ivar text: Its text: the standard one, a ``;`` and the firmware's reason.
    """

    code: int
    text: str

    @property
    def standard(self) -> str:
        """The standard text, before the ``;``."""
        return self.text.split(";", 1)[0]

    @property
    def detail(self) -> str:
        """The firmware's reason, after the ``;`` (empty when there is none)."""
        return self.text.split(";", 1)[1] if ";" in self.text else ""

    def __str__(self) -> str:
        return f'{self.code},"{self.text}"'


def parse_errors(unit: str) -> list[ErrorEntry]:
    """Return the errors of a ``SYSTem:ERRor[:ALL]?`` answer, ``0`` left out.

    :param unit: The answer.
    :returns: The errors, oldest first; empty when there are none.
    """
    return [
        ErrorEntry(int(code), text.replace('""', '"'))
        for code, text in _ERROR.findall(unit)
        if int(code) != 0
    ]


@dataclass(frozen=True)
class Sample:
    """One streamed sample: its timestamp and the channels it carries.

    :ivar t_us: The sample's timestamp, microseconds since the instrument booted.
    :ivar values: Each channel's value, in its unit (``i_ua`` microamps, ...).
    :ivar line: The record as the instrument wrote it, without its newline;
        empty for a sample that was not read from a line. It takes no part in
        comparisons.
    """

    t_us: int
    values: dict[str, float]
    line: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True)
class Mark:
    """One mark: a label at a sample index.

    :ivar label: The mark's label.
    :ivar index: The sample index it marks.
    :ivar t_us: That sample's timestamp.
    :ivar line: The record as the instrument wrote it, as :attr:`Sample.line`.
    """

    label: str
    index: int
    t_us: int
    line: str = field(default="", compare=False, repr=False)


def parse_record(line: str) -> Sample | Mark | None:
    """Parse one stream record, ``TEXT`` or ``CSV``.

    A ``TEXT`` sample carries one channel per line; the caller merges lines of
    one timestamp if it wants rows.

    :param line: One line of the stream, without its newline.
    :returns: The sample or the mark; None for any other line.
    """
    if line.startswith("METER ") and " t=" in line and " ch=" in line:
        fields = parse_fields(line)
        try:
            return Sample(int(fields["t"]), {fields["ch"]: float(fields["v"])}, line)
        except (KeyError, ValueError):
            return None
    if line.startswith("METER_MARK "):
        fields = parse_fields(line)
        try:
            return Mark(fields["mark"], int(fields["n"]), int(fields["t"]), line)
        except (KeyError, ValueError):
            return None
    if line.startswith("S,"):
        parts = line.split(",")
        if len(parts) != 2 + len(CSV_CHANNELS):
            return None
        values = {
            name: float(text) for name, text in zip(CSV_CHANNELS, parts[2:], strict=True) if text
        }
        return Sample(int(parts[1]), values, line)
    if line.startswith("K,"):
        parts = line.split(",", 3)
        if len(parts) != 4:
            return None
        return Mark(parts[3], int(parts[1]), int(parts[2]), line)
    return None


def block_header(data: bytes) -> tuple[int, int] | None:
    """Read a definite-length block's header at the start of *data*.

    :param data: Bytes that start with ``#``.
    :returns: ``(header length, payload length)``, or ``None`` until enough
        bytes are there.
    :raises ValueError: When *data* starts with ``#`` but is no definite block.
    """
    if len(data) < 2:
        return None
    digits = data[1:2].decode("ascii")
    if not digits.isdigit() or digits == "0":
        raise ValueError(f"not a definite-length block: {data[:12]!r}")
    width = int(digits)
    if len(data) < 2 + width:
        return None
    return 2 + width, int(data[2 : 2 + width].decode("ascii"))


def quote(text: str) -> str:
    """Write *text* as a SCPI string parameter.

    :param text: The string.
    :returns: It in double quotes, each one inside it doubled.
    """
    return '"' + text.replace('"', '""') + '"'


def check_dump(block: bytes) -> bool | None:
    """Hold a ``TRACe:DATA?`` block to the CRC-32 its closing row carries (0.11.0).

    The closing row is ``METER_DUMP_END crc32=<8 hex> bytes=<n>``: the IEEE
    CRC-32 (``zlib.crc32``) of the block's first *n* bytes, every row before
    it. A block from an older image ends in a bare ``METER_DUMP_END``.

    :param block: The block's bytes, as ``TRAC:DATA?`` delivered them.
    :returns: True when the CRC matches, None when the block carries none.
    :raises ValueError: When it carries one and the bytes do not match it.
    """
    end = block.rfind(b"METER_DUMP_END")
    if end < 0:
        raise ValueError("the block has no METER_DUMP_END row")
    fields = parse_fields(block[end:].decode("ascii", "replace").strip())
    if "crc32" not in fields:
        return None
    count = int(fields.get("bytes", end))
    if count != end:
        raise ValueError(f"the closing row covers {count} bytes, the block {end} before it")
    computed = zlib.crc32(block[:end]) & 0xFFFFFFFF
    if computed != int(fields["crc32"], 16):
        raise ValueError(f"CRC-32 {computed:08x} over the block, the row says {fields['crc32']}")
    return True


# --- The ring dump ---------------------------------------------------------------

#: The VSHUNT LSB of each ADC range, picovolts (``ina228_regs.h``
#: ``kShuntLsbPvRange0`` and ``kShuntLsbPvRange1``): what ``FETCh:HEADer?``
#: reports as ``shunt_lsb_pv`` for ``range=0`` and ``range=1``.
SHUNT_LSB_PV = (312_500, 78_125)


def current_nanoamps(counts: int, shunt_lsb_pv: int, offset_pv: int, shunt_uohm: int) -> int:
    """The current of one raw count, as ``scale::currentNanoamps`` computes it.

    ``(counts x shunt_lsb_pv - offset_pv) x 1000 / shunt_uohm``, in 64-bit
    integers, the division truncated toward zero as C++ truncates it: the
    LSB of the capture's range, the null it subtracted and the shunt it
    converted with, so the result is the integer the instrument printed
    (``firmware/src/scale.h``). A shunt of 0 gives 0, as the firmware's guard
    does.

    :param counts: The sample's raw 20-bit VSHUNT count, as a ``RAW`` row has it.
    :param shunt_lsb_pv: The range's VSHUNT LSB, picovolts.
    :param offset_pv: The zero-current null the capture subtracted, picovolts.
    :param shunt_uohm: The shunt the capture converted with, micro-ohms.
    :returns: The current, nanoamps.
    """
    if shunt_uohm == 0:
        return 0
    numerator = (counts * shunt_lsb_pv - offset_pv) * 1000
    quotient = abs(numerator) // abs(shunt_uohm)
    return quotient if (numerator < 0) == (shunt_uohm < 0) else -quotient


def parse_thousandths(text: str) -> int:
    """Read a value ``formatThousandths`` wrote back into its integer.

    :param text: A ``v=`` value, a sign, digits, a point and three decimals.
    :returns: The value in thousandths of its unit (nanoamps for ``i_ua``).
    :raises ValueError: When *text* is not that shape.
    """
    match = _THOUSANDTHS.fullmatch(text)
    if match is None:
        raise ValueError(f"not a three-decimal value: {text!r}")
    sign, whole, fraction = match.groups()
    magnitude = int(whole) * 1000 + int(fraction)
    return -magnitude if sign else magnitude


def format_thousandths(value: int) -> str:
    """Write *value* thousandths as ``scale::formatThousandths`` does: three decimals.

    The inverse of :func:`parse_thousandths`: ``88066019`` is ``88066.019``,
    ``-500`` is ``-0.500``.

    :param value: A value in thousandths of its unit.
    :returns: Its text, a sign only when negative.
    """
    whole, fraction = divmod(abs(value), 1000)
    return f"{'-' if value < 0 else ''}{whole}.{fraction:03d}"


#: A value ``scale::formatThousandths`` writes: exactly three decimals.
_THOUSANDTHS = re.compile(r"(-?)(\d+)\.(\d{3})")


@dataclass(frozen=True)
class DumpScale:
    """What a capture converted its raw counts with, from its ``FETCh:HEADer?``.

    :ivar shunt_lsb_pv: The range's VSHUNT LSB, picovolts.
    :ivar offset_pv: The null the capture subtracted, picovolts.
    :ivar shunt_uohm: The shunt the capture converted with, micro-ohms: the
        range's applied value, whose ratio to the nominal 100,000 is the
        correction ``scale.h`` applies.
    """

    shunt_lsb_pv: int
    offset_pv: int
    shunt_uohm: int

    @classmethod
    def from_header(cls, header: Mapping[str, str] | str) -> DumpScale:
        """The scale a ``FETCh:HEADer?`` report states.

        ``shunt_lsb_pv`` is read when the report has it, else derived from
        ``range`` (:data:`SHUNT_LSB_PV`).

        :param header: The report, as text or as :func:`parse_fields` returns it.
        :returns: The capture's scale.
        :raises ValueError: When ``shunt_uohm``, ``offset_pv`` or both of
            ``shunt_lsb_pv`` and a known ``range`` are missing or not integers.
        """
        fields = parse_fields(header) if isinstance(header, str) else header
        try:
            if "shunt_lsb_pv" in fields:
                lsb = int(fields["shunt_lsb_pv"])
            else:
                lsb = SHUNT_LSB_PV[int(fields["range"])]
            return cls(lsb, int(fields["offset_pv"]), int(fields["shunt_uohm"]))
        except (KeyError, IndexError, ValueError) as error:
            raise ValueError(
                "the header lacks the scale (shunt_lsb_pv or range, offset_pv, shunt_uohm):"
                f" {error}"
            ) from error

    def nanoamps(self, counts: int) -> int:
        """The current of raw *counts*, as :func:`current_nanoamps` computes it.

        :param counts: A raw 20-bit VSHUNT count.
        :returns: The current, nanoamps.
        """
        return current_nanoamps(counts, self.shunt_lsb_pv, self.offset_pv, self.shunt_uohm)


@dataclass(frozen=True)
class Anchor:
    """A drift anchor: the real timestamp of one sample index (``A`` row).

    :ivar index: The capture's sample index.
    :ivar t_us: When that sample was taken, microseconds since boot.
    """

    index: int
    t_us: int


@dataclass(frozen=True)
class Trigger:
    """Where a level trigger fired (``T`` row).

    :ivar index: The capture's sample index it fired at.
    :ivar t_us: That sample's timestamp, microseconds since boot.
    """

    index: int
    t_us: int


@dataclass(frozen=True)
class DumpMark:
    """A mark as a dump writes it (``K`` row): a label at an index, no time.

    :ivar index: The capture's sample index the mark was taken at.
    :ivar label: Its label.
    """

    index: int
    label: str


@dataclass(frozen=True)
class RawRow:
    """A ``RAW`` row: up to sixteen raw counts from an index on (``R`` row).

    :ivar index: The capture's index of the first count.
    :ivar counts: The raw 20-bit counts, in index order.
    """

    index: int
    counts: tuple[int, ...]


@dataclass(frozen=True)
class ScaledRow:
    """A ``SCALed`` row: one sample's current at its implicit timestamp.

    :ivar t_us: ``t0_us + index x period_us``, as the row prints it.
    :ivar nanoamps: The current the instrument computed, nanoamps.
    """

    t_us: int
    nanoamps: int


#: One row of a dump's body, as :func:`parse_dump_row` reads it.
DumpRow = RawRow | ScaledRow | Anchor | DumpMark | Trigger


def parse_dump_row(row: str) -> DumpRow | None:
    """Read one row of a ``TRACe:DATA?`` block's body.

    :param row: One line of the block, its newline removed.
    :returns: The row, or None for the opening and closing rows and any other line.
    :raises ValueError: When the row has a body row's prefix and a bad field.
    """
    head = row[:2]
    if head not in ("R ", "A ", "K ", "T ") and not row.startswith("METER t="):
        return None
    fields = parse_fields(row)
    try:
        if head == "R ":
            return RawRow(int(fields["n"]), tuple(int(c) for c in fields["c"].split(",")))
        if head == "A ":
            return Anchor(int(fields["n"]), int(fields["t"]))
        if head == "T ":
            return Trigger(int(fields["n"]), int(fields["t"]))
        if head == "K ":
            return DumpMark(int(fields["n"]), fields["label"])
        if fields.get("ch") != "i_ua":
            raise ValueError("a scaled row carries ch=i_ua")
        return ScaledRow(int(fields["t"]), parse_thousandths(fields["v"]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"malformed dump row {row!r}: {error}") from error


@dataclass(frozen=True)
class DumpSample:
    """One sample of a decoded dump, as :meth:`Dump.samples` yields it.

    :ivar index: The capture's sample index.
    :ivar t_us: Its timestamp, interpolated between the drift anchors.
    :ivar counts: Its raw count (None from a ``SCALed`` dump).
    :ivar nanoamps: Its current (None from a ``RAW`` dump decoded with no header).
    """

    index: int
    t_us: int
    counts: int | None
    nanoamps: int | None


@dataclass(frozen=True)
class Dump:
    """A ``TRACe:DATA?`` block, decoded by :func:`decode_dump`.

    The samples are held as columns, one integer per sample in an
    :class:`array.array`, so a full ring of 1,048,576 samples takes a few
    megabytes rather than a Python object each.

    :ivar raw: Whether the block was ``RAW`` (else ``SCALed``).
    :ivar count: The samples it carries, ``count=`` of its opening row.
    :ivar t0_us: The capture's start, microseconds since boot.
    :ivar period_us: The capture's nominal period, microseconds.
    :ivar first: The capture index of the ring's oldest sample (``first=``, else 0).
    :ivar start: The capture index of the first sample dumped.
    :ivar counts: The raw counts, in index order (None from a ``SCALed`` dump).
    :ivar nanoamps: The currents, in index order: computed from the counts by
        the header's :class:`DumpScale`, or read from a ``SCALed`` dump's rows;
        None from a ``RAW`` dump decoded with no header.
    :ivar anchors: The drift anchors, in index order.
    :ivar marks: The marks, in index order, each timed by :meth:`t_us`.
    :ivar trigger: Where a level trigger fired (its ``T`` row), or None.
    :ivar trig_n: The index a level trigger fired at, from the opening row's
        ``trig_n=``, which a partial dump carries even when it holds no ``T`` row.
    :ivar crc_checked: Whether the closing row carried a CRC (and it matched).
    """

    raw: bool
    count: int
    t0_us: int
    period_us: int
    first: int
    start: int
    counts: array[int] | None
    nanoamps: array[int] | None
    anchors: tuple[Anchor, ...]
    marks: tuple[Mark, ...]
    trigger: Trigger | None
    trig_n: int | None
    crc_checked: bool
    _anchor_index: tuple[int, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Index the anchors once, for :meth:`t_us`'s bisection."""
        object.__setattr__(self, "_anchor_index", tuple(a.index for a in self.anchors))

    @property
    def indices(self) -> range:
        """The capture indices of the samples dumped, in order."""
        return range(self.start, self.start + self.count)

    def nominal_t_us(self, index: int) -> int:
        """The implicit timestamp a ``SCALed`` row prints: ``t0_us + index x period_us``.

        :param index: A capture sample index.
        :returns: Microseconds since boot, uncorrected for drift.
        """
        return self.t0_us + index * self.period_us

    def t_us(self, index: int) -> int:
        """The timestamp of sample *index*, corrected by the drift anchors.

        Between two anchors it is interpolated linearly, rounded to the
        nearest microsecond; before the first and after the last it runs on
        from the nearest anchor at the nominal period; with no anchor it is
        :meth:`nominal_t_us`.

        :param index: A capture sample index, inside the dump or not.
        :returns: Microseconds since boot.
        """
        anchors = self.anchors
        if not anchors:
            return self.nominal_t_us(index)
        place = bisect_right(self._anchor_index, index)
        if place == 0:
            low = anchors[0]
            return low.t_us - (low.index - index) * self.period_us
        low = anchors[place - 1]
        if place == len(anchors) or low.index == index:
            return low.t_us + (index - low.index) * self.period_us
        high = anchors[place]
        span = high.index - low.index
        numerator = 2 * (index - low.index) * (high.t_us - low.t_us) + span
        return low.t_us + numerator // (2 * span)

    def timestamps(self) -> array[int]:
        """:meth:`t_us` of every sample dumped.

        :returns: Microseconds since boot, in index order.
        """
        return array("q", (self.t_us(index) for index in self.indices))

    def samples(self) -> Iterator[DumpSample]:
        """Every sample dumped, in index order.

        :returns: The samples, made as they are asked for.
        """
        for offset, index in enumerate(self.indices):
            yield DumpSample(
                index,
                self.t_us(index),
                None if self.counts is None else self.counts[offset],
                None if self.nanoamps is None else self.nanoamps[offset],
            )

    def counts_between(self, first: int, last: int) -> list[int]:
        """The raw counts of indices *first* to *last* - 1 that the dump holds.

        :param first: The first capture index wanted.
        :param last: One past the last capture index wanted.
        :returns: The counts of the indices in both the range and the dump, in order.
        :raises ValueError: On a ``SCALed`` dump, which holds no counts.
        """
        if self.counts is None:
            raise ValueError("a SCALed dump holds no raw counts")
        low = max(first, self.start) - self.start
        high = min(last, self.start + self.count) - self.start
        return list(self.counts[low:high]) if high > low else []


def decode_dump(block: bytes, header: Mapping[str, str] | str | None = None) -> Dump:
    """Decode a ``TRACe:DATA?`` block, ``RAW`` or ``SCALed`` (``docs/scpi.md``).

    The block is held to its CRC first (:func:`check_dump`). A ``RAW`` block's
    counts are converted with the capture's ``FETCh:HEADer?`` when *header* is
    given, exactly as ``scale.h`` converts them (:class:`DumpScale`), so every
    current is the integer the instrument's ``SCALed`` dump of the same ring
    prints; a ``SCALed`` block's currents are read from its rows. Timestamps
    are the capture's ``t0_us`` and period, corrected between the drift
    anchors (:meth:`Dump.t_us`).

    :param block: The block's payload, as ``TRACe:DATA?`` delivered it.
    :param header: The capture's ``FETCh:HEADer?`` report, as text or fields,
        or None to leave a ``RAW`` dump's currents out.
    :returns: The decoded dump.
    :raises ValueError: When the CRC does not match, the opening row is
        missing or malformed, a row is malformed, the samples are not
        contiguous, their number is not ``count=``, or *header* belongs to
        another capture (its ``t0_us`` or ``period_us`` differ) or lacks the
        scale.
    """
    crc = check_dump(block) is True
    # One row at a time: a full SCALed ring is a million rows.
    rows = (line.rstrip("\r\n") for line in io.StringIO(block.decode("ascii", "replace")))
    opening = next((row for row in rows if row.strip()), "")
    if not opening.startswith("METER_DUMP "):
        raise ValueError(f"the block does not open with METER_DUMP: {opening[:40]!r}")
    fields = parse_fields(opening)
    try:
        count, t0, period = int(fields["count"]), int(fields["t0_us"]), int(fields["period_us"])
        first = int(fields.get("first", "0"))
        trig_n = int(fields["trig_n"]) if "trig_n" in fields else None
    except (KeyError, ValueError) as error:
        raise ValueError(f"malformed opening row {opening!r}: {error}") from error
    raw = fields.get("fmt") == "raw20"
    scale = None
    if header is not None:
        given = parse_fields(header) if isinstance(header, str) else header
        for key, value in (("t0_us", t0), ("period_us", period)):
            if key in given and given[key] != str(value):
                raise ValueError(
                    f"the header is another capture's: {key}={given[key]}, not {value}"
                )
        scale = DumpScale.from_header(given) if raw else None
    counts = array("q")
    nanoamps = array("q")
    anchors: list[Anchor] = []
    marks: list[DumpMark] = []
    trigger: Trigger | None = None
    start: int | None = None
    for row in rows:
        parsed = parse_dump_row(row)
        if isinstance(parsed, RawRow | ScaledRow):
            if isinstance(parsed, RawRow) != raw:
                raise ValueError(f"a {'RAW' if raw else 'SCALed'} dump holds the row {row!r}")
            index = _row_index(parsed, t0, period)
            if start is None:
                start = index
            expected = start + len(counts if raw else nanoamps)
            if index != expected:
                raise ValueError(f"the row at index {index} follows index {expected - 1}")
            if isinstance(parsed, RawRow):
                counts.extend(parsed.counts)
            else:
                nanoamps.append(parsed.nanoamps)
        elif isinstance(parsed, Anchor):
            anchors.append(parsed)
        elif isinstance(parsed, DumpMark):
            marks.append(parsed)
        elif isinstance(parsed, Trigger):
            trigger = parsed
    taken = len(counts if raw else nanoamps)
    if taken != count:
        raise ValueError(f"the block holds {taken} samples, its opening row says count={count}")
    computed: array[int] | None = nanoamps
    if raw:
        computed = array("q", map(scale.nanoamps, counts)) if scale is not None else None
    dump = Dump(
        raw=raw,
        count=count,
        t0_us=t0,
        period_us=period,
        first=first,
        start=first if start is None else start,
        counts=counts if raw else None,
        nanoamps=computed,
        anchors=tuple(sorted(anchors, key=lambda anchor: anchor.index)),
        marks=(),
        trigger=trigger,
        trig_n=trig_n,
        crc_checked=crc,
    )
    timed = tuple(Mark(m.label, m.index, dump.t_us(m.index)) for m in sorted(marks, key=_by_index))
    return replace(dump, marks=timed)


def _by_index(mark: DumpMark) -> int:
    """A mark's index, the key its order is sorted by.

    :param mark: A mark of the dump.
    :returns: Its index.
    """
    return mark.index


def _row_index(row: RawRow | ScaledRow, t0_us: int, period_us: int) -> int:
    """The capture index of a sample row's first sample.

    :param row: A ``RAW`` or ``SCALed`` row.
    :param t0_us: The capture's start, from the opening row.
    :param period_us: The capture's period, from the opening row.
    :returns: The index of the row's first sample.
    :raises ValueError: When a ``SCALed`` row's timestamp is not on the
        capture's ``t0_us + index x period_us`` grid.
    """
    if isinstance(row, RawRow):
        return row.index
    index, rest = divmod(row.t_us - t0_us, period_us) if period_us else (0, 1)
    if rest or index < 0:
        raise ValueError(f"t={row.t_us} is not on the grid of t0_us={t0_us} period_us={period_us}")
    return index


# --- The line router -------------------------------------------------------------

#: The longest line, in bytes, the router holds before it gives the line up.
#: The instrument's longest response line is a full error queue of about one
#: kilobyte (``docs/scpi.md``, "The error queue"); a block's bytes do not count.
MAX_LINE = 64 * 1024

#: The bytes the router acts on: the line's end, the unit separator, the two
#: quotes, and the ``#`` a block starts with. Every other byte is a unit's text.
_SPECIAL = re.compile(rb"[\n;\"'#]")

#: The longest block header: ``#``, the width digit and up to nine digits.
_HEADER_MAX = 11


@dataclass
class Response:
    """One response line: its units, and the blocks it carried.

    A block is not one of the units: a ``TRACe:DATA?`` unit takes no place in
    :attr:`units`, so the units are the answers of the message's other queries,
    in order.

    :ivar units: The line's units, a block's place left out.
    :ivar blocks: The payloads of its definite-length blocks, in order.
    """

    units: list[str]
    blocks: list[bytes] = field(default_factory=list)

    @property
    def block(self) -> bytes | None:
        """The line's first block, or None when it carried none."""
        return self.blocks[0] if self.blocks else None


@dataclass(frozen=True)
class BlockProgress:
    """How much of the definite-length block being read has arrived.

    :ivar received: Payload bytes read so far.
    :ivar total: The payload's length, from the block's header.
    """

    received: int
    total: int


@dataclass(frozen=True)
class Overflow:
    """A line longer than the router's limit, given up whole.

    :ivar discarded: How many bytes were dropped, its ``\\n`` included.
    """

    discarded: int


#: What :meth:`LineRouter.feed` returns: a response, a stream record, a block's
#: progress, or a line given up.
RouterEvent = Response | Sample | Mark | BlockProgress | Overflow


class LineRouter:
    """Turn the bytes of the line into responses, stream records and block progress.

    Pure and incremental: :meth:`feed` takes the bytes in whatever pieces they
    arrive and returns the events they complete, the same responses and records
    for any cut (only the number of :class:`BlockProgress` events depends on
    it). A line is a stream record when :func:`parse_record` reads it as one,
    else a response; an empty line is nothing. A definite-length block starts at
    a ``#`` that begins a unit of a response line, at the line's start or after
    a ``;`` outside a quoted string, and is read by its byte count, so its rows,
    their newlines and anything else in it are never taken for the line's text
    (``docs/scpi.md``, "Responses"). A ``#`` whose header is malformed is text.

    :param max_line: The longest line held, in bytes, before it is given up as
        an :class:`Overflow`; a block's payload does not count.
    """

    def __init__(self, max_line: int = MAX_LINE) -> None:
        self._max_line = max_line
        self._buffer = bytearray()
        self._payload = bytearray()
        self.reset()

    def reset(self) -> None:
        """Forget every byte held and any line or block in progress."""
        self._buffer.clear()
        self._start = 0  # where the unconsumed bytes begin
        self._scan = 0  # how far the line in progress has been looked at
        self._discarded: int | None = None  # bytes of a line being given up
        self._payload.clear()
        self._remaining: int | None = None  # payload bytes still due, None outside a block
        self._new_line()

    @property
    def held(self) -> int:
        """How many bytes the router holds that no event has accounted for yet."""
        blocks = sum(map(len, self._blocks))
        return len(self._buffer) - self._start + len(self._payload) + blocks

    def feed(self, data: bytes) -> list[RouterEvent]:
        """Take the next bytes of the line.

        :param data: Bytes as the line delivered them, any length, empty included.
        :returns: The events they complete, in line order.
        """
        self._buffer += data
        events: list[RouterEvent] = []
        while self._step(events):
            pass
        del self._buffer[: self._start]
        self._scan -= self._start
        self._start = 0
        return events

    # -- the states ---------------------------------------------------------

    def _new_line(self) -> None:
        """Begin a line: no quote open, at a unit's start, no text or block yet."""
        self._quote = 0
        self._boundary = True
        self._segments: list[str] = []
        self._blocks: list[bytes] = []

    def _step(self, events: list[RouterEvent]) -> bool:
        """Advance by one action.

        :param events: Where a completed event is appended.
        :returns: False once the bytes held are used up.
        """
        if self._remaining is not None:
            return self._take_payload(events)
        if self._discarded is not None:
            return self._skip_line(events)
        found = _SPECIAL.search(self._buffer, self._scan)
        if found is None:
            if len(self._buffer) > self._scan:
                self._boundary = False
            self._scan = len(self._buffer)
            if self._scan - self._start > self._max_line:
                self._give_up()
            return False
        position = found.start()
        if position > self._scan:
            self._boundary = False
        char = self._buffer[position]
        self._scan = position + 1
        if char == 0x0A:  # the line's end, inside a quote or not
            self._end_line(position, events)
        elif self._quote:
            if char == self._quote:
                self._quote = 0
        elif char == 0x3B:  # ;
            self._boundary = True
        elif char in (0x22, 0x27):  # " and '
            self._quote = char
            self._boundary = False
        else:  # #
            return self._start_block(position, events)
        return True

    def _start_block(self, position: int, events: list[RouterEvent]) -> bool:
        """Open a block at the ``#`` at *position* if it begins a unit and a header follows.

        :param position: Where the ``#`` is in the buffer.
        :param events: Where the block's first progress is appended.
        :returns: False when the header has not fully arrived yet.
        """
        opens = self._boundary
        self._boundary = False
        if not opens:
            return True
        try:
            header = block_header(bytes(self._buffer[position : position + _HEADER_MAX]))
        except ValueError:
            return True  # no block: the '#' is text
        if header is None:
            self._scan, self._boundary = position, True  # look at it again with more bytes
            return False
        start, length = header
        self._segments.append(self._text(position))
        self._start = self._scan = position + start
        self._remaining = length
        events.append(BlockProgress(0, length))
        return True

    def _take_payload(self, events: list[RouterEvent]) -> bool:
        """Move the block's bytes out of the buffer.

        :param events: Where its progress is appended.
        :returns: False when more of the block must arrive.
        """
        due = self._remaining or 0
        taken = min(due, len(self._buffer) - self._start)
        if taken:
            self._payload += self._buffer[self._start : self._start + taken]
            self._start += taken
            due -= taken
            events.append(BlockProgress(len(self._payload), len(self._payload) + due))
        self._remaining = due
        self._scan = self._start
        if due:
            return False
        self._blocks.append(bytes(self._payload))
        self._payload.clear()
        self._remaining = None  # only a ';' opens the line's next unit
        return True

    def _end_line(self, position: int, events: list[RouterEvent]) -> None:
        """Close the line whose ``\\n`` is at *position*.

        :param position: Where the newline is in the buffer.
        :param events: Where the line's response or record is appended.
        """
        text = self._text(position).rstrip("\r")
        self._start = self._scan = position + 1
        if self._blocks:
            events.append(Response(self._units(text), self._blocks))
        elif text:
            record = parse_record(text)
            events.append(record if record is not None else Response(split_units(text)))
        self._new_line()

    def _units(self, text: str) -> list[str]:
        """The units of a line that carried blocks, the blocks' places left out.

        Each block sits between a separator before it, unless it opens the
        line, and one after it, unless it ends the line.

        :param text: The line's text after its last block.
        :returns: The units of every piece of text, in order.
        """
        units: list[str] = []
        pieces = [*self._segments, text]
        for index, whole in enumerate(pieces):
            piece = whole[1:] if index > 0 and whole.startswith(";") else whole
            if index < len(self._segments) and piece.endswith(";"):
                piece = piece[:-1]
            if piece:
                units += split_units(piece)
        return units

    def _give_up(self) -> None:
        """Drop the line in progress, which is longer than the limit."""
        self._discarded = len(self._buffer) - self._start
        self._start = self._scan = len(self._buffer)
        self._new_line()

    def _skip_line(self, events: list[RouterEvent]) -> bool:
        """Drop bytes up to the given-up line's end, and report how many.

        :param events: Where the :class:`Overflow` is appended.
        :returns: False when the line's end has not arrived yet.
        """
        discarded = self._discarded or 0
        end = self._buffer.find(b"\n", self._start)
        if end < 0:
            self._discarded = discarded + len(self._buffer) - self._start
            self._start = self._scan = len(self._buffer)
            return False
        events.append(Overflow(discarded + end + 1 - self._start))
        self._start = self._scan = end + 1
        self._discarded = None
        return True

    def _text(self, end: int) -> str:
        """The unconsumed bytes up to *end*, as text.

        :param end: Where the text ends in the buffer.
        :returns: Those bytes, decoded as UTF-8 with any bad byte replaced.
        """
        return self._buffer[self._start : end].decode("utf-8", "replace")
