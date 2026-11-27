# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A synchronous client for the meter's SCPI protocol (``docs/scpi.md``).

Every exchange is one program message ending in ``SYSTem:ERRor:ALL?``, so each
answer carries its own verdict and no error is left queued for the next one;
the first message of a session also clears the queue (``*CLS``). Stream records
arriving while the client waits are kept, never mistaken for the response:
the bytes of the line go through :class:`~kleidos_meter.protocol.LineRouter`,
which tells responses, blocks and records apart however the bytes are cut.
"""

from __future__ import annotations

import errno
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from . import pins
from .pins import PinMap
from .protocol import (
    CHANNEL_FUNCTIONS,
    Dump,
    ErrorEntry,
    LineRouter,
    Mark,
    Response,
    RouterEvent,
    Sample,
    check_dump,
    decode_dump,
    parse_errors,
    parse_fields,
    quote,
)

__all__ = [
    "ESR_POWER_ON",
    "Capture",
    "Meter",
    "MeterError",
    "MeterTimeout",
    "PortBusyError",
    "Response",
    "SerialTransport",
    "Transport",
]

#: The power-on bit of the Standard Event register (``*ESR?``, ``docs/scpi.md``).
ESR_POWER_ON = 0x80


class Transport(Protocol):
    """A byte pipe to the instrument: what a :class:`Meter` is made on.

    :class:`SerialTransport` is the line, and
    :class:`~kleidos_meter.sim.SimulatedMeter` is one too.
    """

    def write(self, data: bytes) -> int:
        """Send *data*.

        :param data: The bytes to send.
        :returns: How many were taken.
        """

    def read(self, size: int) -> bytes:
        """Read what has arrived, waiting a moment for some.

        :param size: The most bytes to return.
        :returns: What arrived, possibly nothing.
        """

    def close(self) -> None:
        """Release the pipe."""


class MeterError(RuntimeError):
    """The instrument refused a unit, or did not answer.

    :param message: What happened, for a person.
    :param errors: The instrument's errors, oldest first.
    :ivar errors: The same, as a list.
    """

    def __init__(self, message: str, errors: Sequence[ErrorEntry] = ()) -> None:
        super().__init__(message)
        self.errors = list(errors)

    @property
    def codes(self) -> list[int]:
        """The error numbers."""
        return [entry.code for entry in self.errors]


class MeterTimeout(MeterError):
    """No response arrived in time."""


class PortBusyError(MeterError):
    """The port is held by another opener: ``serve``, a QA run or another tool."""


@dataclass
class Capture:
    """What a capture streamed: samples merged by timestamp, and marks.

    :ivar rows: Each timestamp's channels and values, by microseconds since boot.
    :ivar marks: The marks, in the order they arrived.
    """

    rows: dict[int, dict[str, float]] = field(default_factory=dict)
    marks: list[Mark] = field(default_factory=list)

    def channel(self, name: str) -> list[tuple[int, float]]:
        """One channel as ``(t_us, value)`` pairs, in time order.

        :param name: The channel: ``i_ua``, ``v_mv``, ``p_mw`` or ``die_c``.
        :returns: Its samples; empty when the capture did not carry it.
        """
        return [(t, row[name]) for t, row in sorted(self.rows.items()) if name in row]


class SerialTransport:
    """The CH343 line, opened exclusively and so that the open does not reset the board.

    The carrier's auto-reset circuit pulls EN low for one line state only: DTR
    released while RTS is still asserted (``docs/carrier-board.md`` §7). The
    port is opened with the lines as the OS and pyserial leave them, then RTS
    is released, then DTR (``docs/scpi.md``, "The line"). Setting both before
    ``open()`` does not keep that order: pyserial applies DTR first and RTS
    second inside ``open()``, which passes through the reset state.

    The open takes an exclusive lock on the port by default (pyserial's
    ``exclusive``, an ``flock`` on POSIX; Windows ports are exclusive anyway):
    a second opener would otherwise read some of the lines meant for the
    first, and both would lose them. A port another process holds is refused
    at once with :class:`PortBusyError`.

    *port* may also be a pyserial URL (``socket://127.0.0.1:5025``, the
    passthrough of ``kleidos-meter serve``; ``loop://`` in tests), opened
    through ``serial.serial_for_url``; the control lines are then whatever the
    URL's handler makes of them.

    :param port: The device node, or a pyserial URL.
    :param baud: The line rate, 6,000,000 on the bench.
    :param exclusive: Lock the port against every other opener.
    :raises PortBusyError: When another opener holds the port.
    """

    def __init__(self, port: str, baud: int = 6_000_000, *, exclusive: bool = True) -> None:
        import serial  # deferred, so the protocol and the simulator need no pyserial

        if "://" in port:
            handle = serial.serial_for_url(port, do_not_open=True)
        else:
            handle = serial.Serial()
            handle.port = port
        handle.baudrate = baud
        handle.timeout = 0.05
        handle.dsrdtr = False
        handle.rtscts = False
        handle.exclusive = exclusive
        try:
            handle.open()
        except OSError as error:  # serial.SerialException is an OSError
            if getattr(error, "errno", None) in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise PortBusyError(
                    f"{port} is held by another opener (kleidos-meter serve, a QA run or "
                    "another tool): close that one first"
                ) from error
            raise
        handle.rts = False  # RTS first: DTR still asserted holds EN high
        handle.dtr = False
        self._serial = handle

    def write(self, data: bytes) -> int:
        """Write *data* and wait until it has left.

        :param data: The bytes to send.
        :returns: How many were written.
        """
        written = self._serial.write(data)
        self._serial.flush()
        return written or 0

    def read(self, size: int) -> bytes:
        """Read up to *size* bytes, waiting at most the port's 50 ms timeout.

        :param size: The most bytes to return.
        :returns: What arrived, possibly nothing.
        """
        return self._serial.read(size)

    def close(self) -> None:
        """Close the port, which releases its lock."""
        self._serial.close()


class Meter:
    """The instrument.

    A method raises :class:`MeterError`, with the instrument's errors, when the
    instrument refuses what it sent, and :class:`MeterTimeout` when no response
    comes; :meth:`send` returns whatever was answered, and :meth:`close_zero`
    the errors. It is a context manager that closes the transport, and closes
    nothing else: a capture or a null window left open, or a relay off its
    rest, stays so.

    :param transport: Its byte pipe.
    :param clock: The monotonic clock timeouts are measured on.
    :ivar capture: What the last capture started here streamed.
    """

    def __init__(
        self, transport: Transport, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._router = LineRouter()
        self._events: deque[RouterEvent] = deque()
        self._cleared = False
        self.capture = Capture()

    def __enter__(self) -> Meter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the transport."""
        self._transport.close()

    # -- readiness ----------------------------------------------------------

    def drain(self, *, quiet: float = 0.1, limit: float = 1.0) -> int:
        """Discard what the line holds: read until it has been silent for *quiet*.

        A running capture never goes silent, so the drain also ends after
        *limit* seconds.

        :param quiet: How long the line must be silent, seconds.
        :param limit: The longest the drain may take, seconds.
        :returns: How many bytes were discarded, counting none for events
            already routed.
        """
        discarded = self._router.held
        self._router.reset()
        self._events.clear()
        start = last = self._clock()
        while True:
            now = self._clock()
            if now - last >= quiet or now - start >= limit:
                return discarded
            chunk = self._transport.read(4096)
            if chunk:
                discarded += len(chunk)
                last = self._clock()

    def wait_ready(self, *, timeout: float = 15.0, attempt: float = 0.25) -> bool:
        """Wait until the instrument answers, and say whether it had rebooted.

        A bare newline ends any partial line the instrument holds, the line is
        drained, then ``*ESR?`` is asked with short timeouts until it answers:
        the instrument discards what arrives while it boots (the boot runs a
        PSRAM test first), so the first queries may go unanswered. ``*ESR?``
        reads and clears the Standard Event register, so the power-on bit is
        reported once, to this call. The line is drained again after the
        answer, so that a late reply to an earlier attempt is not taken for
        the response to the next message.

        :param timeout: How long to wait for the first answer, seconds.
        :param attempt: How long each ``*ESR?`` waits, seconds.
        :returns: True when the power-on bit (128) was set: the instrument
            rebooted since its Standard Event register was last read.
        :raises MeterTimeout: When no attempt was answered within *timeout*.
        :raises MeterError: When ``*ESR?`` was answered with no number.
        """
        self._transport.write(b"\n")
        self.drain()
        deadline = self._clock() + timeout
        while True:
            try:
                response = self.send("*ESR?", timeout=attempt)
            except MeterTimeout:
                if self._clock() >= deadline:
                    raise MeterTimeout(f"*ESR?: no answer within {timeout:g} s") from None
                continue
            self.drain()
            try:
                value = int(response.units[0]) if response.units else 0
            except ValueError as exc:
                raise MeterError(f"*ESR? answered {response.units[0]!r}") from exc
            return bool(value & ESR_POWER_ON)

    # -- the exchange -------------------------------------------------------

    def send(self, message: str, *, timeout: float = 8.0) -> Response:
        """Send one program message and return its response line.

        Stream records routed before the response are kept in :attr:`capture`;
        whatever was routed after it waits for the next call.

        :param message: The program message, without its newline.
        :param timeout: How long to wait for the response, seconds.
        :returns: The response line, its units and any block.
        :raises MeterTimeout: When no response arrives within *timeout*.
        """
        self._transport.write((message + "\n").encode("ascii"))
        deadline = self._clock() + timeout
        while True:
            while self._events:
                event = self._events.popleft()
                if isinstance(event, Response):
                    return event
                if isinstance(event, Sample | Mark):
                    self._keep(event)
                # A block's progress, or an over-long line given up: nothing to keep.
            if self._clock() > deadline:
                raise MeterTimeout(f"{message!r}: no response within {timeout:g} s")
            self._events.extend(self._router.feed(self._transport.read(4096)))

    def exchange(
        self, units: Sequence[str], *, timeout: float = 8.0, raise_errors: bool = True
    ) -> tuple[list[str], list[ErrorEntry], bytes | None]:
        """Send *units* plus ``SYSTem:ERRor:ALL?`` as one message.

        The first message of a session starts with ``*CLS``, so that an error
        queued before it is not taken for one of its own.

        :param units: The message's units, in order, each from the root.
        :param timeout: How long to wait for the response, seconds.
        :param raise_errors: Raise the instrument's errors, rather than return them.
        :returns: The query responses, the errors, and a block if one came.
        :raises MeterError: When a unit was refused and *raise_errors*.
        :raises MeterTimeout: When no response arrives within *timeout*.
        """
        body = ";:".join([*units, "SYST:ERR:ALL?"])
        if not self._cleared:
            body = "*CLS;:" + body
        response = self.send(body, timeout=timeout)
        self._cleared = True
        errors = parse_errors(response.units[-1]) if response.units else []
        if raise_errors and errors:
            raise MeterError(f"{units[0]!r}: " + "; ".join(map(str, errors)), errors)
        return response.units[:-1], errors, response.block

    def query(self, query: str, *, timeout: float = 8.0) -> str:
        """Ask one query and return its response unit.

        :param query: The query.
        :param timeout: How long to wait for the response, seconds.
        :returns: Its response, empty when it answered only a block.
        :raises MeterError: When it was refused.
        """
        units, _, _ = self.exchange([query], timeout=timeout)
        return units[0] if units else ""

    def report(self, query: str, *, timeout: float = 8.0) -> dict[str, str]:
        """Ask one report query and return its fields.

        :param query: The query, of a ``key=value`` report (``docs/scpi.md``).
        :param timeout: How long to wait for the response, seconds.
        :returns: The report's fields, as text.
        :raises MeterError: When it was refused.
        """
        return parse_fields(self.query(query, timeout=timeout))

    def _keep(self, record: Sample | Mark) -> None:
        """File a stream record in :attr:`capture`: a mark in order, a sample by time."""
        if isinstance(record, Mark):
            self.capture.marks.append(record)
        else:
            self.capture.rows.setdefault(record.t_us, {}).update(record.values)

    # -- what the instrument is -----------------------------------------------

    def idn(self) -> str:
        """``*IDN?``.

        :returns: The four fields, comma-separated:
            :func:`~kleidos_meter.identity.parse_idn` splits them.
        """
        return self.query("*IDN?")

    def info(self) -> dict[str, str]:
        """``SYSTem:INFormation?``.

        :returns: What the instrument is: its chip, shunt, calibration, image, serial.
        """
        return self.report("SYST:INF?")

    def status(self) -> dict[str, str]:
        """``SYSTem:STATus?``.

        :returns: What it is doing: the window open, the profile, the range, the counts.
        """
        return self.report("SYST:STAT?")

    def calibration(self) -> dict[str, str]:
        """``CALibration:DATA?``.

        :returns: Its working copy of the calibration: the shunt, the nulls, the tag.
        """
        return self.report("CAL:DATA?")

    def heap(self) -> dict[str, str]:
        """``DIAGnostic:HEAP?`` (0.7.0 and later): heap use since the boot settled.

        ``allocs_after_init`` and ``frees_after_init`` are 0 on a firmware that
        allocates nothing in its steady state; ``maint_allocs`` and
        ``maint_frees`` count what calibration saves and I2C clock changes
        allocated inside ESP-IDF. The zero-allocation image appends
        ``pre_main_allocs``, ``pre_main_frees`` (what ESP-IDF allocated before
        ``app_main``), ``allocs_since_main`` and ``frees_since_main`` (0 on a
        healthy image); an older one reports no such keys.

        :returns: The report's fields.
        """
        return self.report("DIAG:HEAP?")

    def watchdog(self) -> dict[str, str]:
        """``DIAGnostic:WATChdog?`` (0.14.0): the task watchdog as the instrument runs it.

        ``console`` and ``sampler`` are 1 when that task's subscription was
        accepted, ``panic`` is 1 when a starved task reboots the instrument,
        and ``stalls`` counts the captures the console found taking no sample.

        :returns: The report's fields.
        """
        return self.report("DIAG:WATC?")

    # -- configuration ----------------------------------------------------------

    def set_profile(self, name: str) -> dict[str, str]:
        """Select a profile.

        :param name: ``TRACE``, ``STATE``, ``SLEEP`` or ``BURST``, or its short form.
        :returns: ``SENSe:PROFile:REPort?``'s fields.
        :raises MeterError: When it was refused: a window is open, or no such profile.
        """
        units, _, _ = self.exchange([f"SENS:PROF {name}", "SENS:PROF:REP?"])
        return parse_fields(units[0])

    def set_range_index(self, index: int) -> dict[str, str]:
        """Select the INA228's range by its ``ADCRANGE``.

        :param index: 0, ±163.84 mV, or 1, ±40.96 mV.
        :returns: ``SENSe:CURRent:RANGe:REPort?``'s fields.
        :raises MeterError: When it was refused: a window is open, or no such range.
        """
        units, _, _ = self.exchange([f"SENS:CURR:RANG:IND {index}", "SENS:CURR:RANG:REP?"])
        return parse_fields(units[0])

    # -- the pin map (0.18.0) ---------------------------------------------------

    def pins(self, *, pending: bool = False) -> dict[str, str]:
        """``SYSTem:PINS?``: the live pin map, or with *pending* the staged one.

        :param pending: Ask ``SYSTem:PINS:PENDing?`` instead: the staged map and
            its verdict, ``valid`` and, when 0, ``problem`` and ``at``.
        :returns: The report's fields: each role's GPIO, the address, the boot
            clock, the pulls and the relays' levels, then ``src``, ``verified``
            and ``table`` (the board table's digest), as ``docs/scpi.md`` has them.
        """
        return self.report("SYST:PINS:PEND?" if pending else "SYST:PINS?")

    def stage(self, **changes: int | str | None) -> dict[str, str]:
        """Stage a pin map: the staged map with *changes*, judged and not applied.

        A role (``sda``, ``scl``, ``alert``, ``relay1``, ``relay2``) takes a
        GPIO or None; ``pull_sda``, ``pull_scl`` and ``pull_alert`` take ``up``
        or ``off``; ``relay1_active`` and ``relay2_active`` ``high`` or
        ``low``; ``addr`` an address, 0x40 to 0x4F, or 0 or ``auto`` for a
        scan; ``scl_hz`` the bus clock at boot. Nothing on the board changes
        until :meth:`apply`. ``meter.stage(sda=18, scl=21)`` stages the bus on
        GPIO18 and GPIO21; the returned ``valid`` says whether the rules take it.

        :param changes: The fields to change, keyed as :meth:`pins` names them.
        :returns: ``SYSTem:PINS:PENDing?``'s fields after the change.
        :raises ValueError: On a key the map does not have.
        :raises MeterError: When the instrument refused a unit.
        """
        unknown = sorted(set(changes) - set(pins.KEYS))
        if unknown:
            raise ValueError(f"no such field of a pin map: {', '.join(unknown)}")
        staged = PinMap.from_words(self.pins(pending=True))
        units = pins.stage_units(staged, changes)
        answer, _, _ = self.exchange([*units, "SYST:PINS:PEND?"])
        return parse_fields(answer[0])

    def apply(self) -> tuple[dict[str, str], list[ErrorEntry]]:
        """``SYSTem:PINS:APPLy``: make the staged map the live one, proved, or keep the last.

        The instrument brings the map up and proves the INA228 on it; a map it
        cannot prove is reverted with one error (``117``), and one it refuses
        changes nothing: ``result=refused`` with its error for a relay not at
        rest or a map the rules refuse, and only the error (``-221``, ``112``)
        under a window, which the order's flags refuse before it runs. None of
        them raises.

        :returns: ``SYSTem:PINS:RESult?``'s fields (``result`` is ``ok``,
            ``reverted`` or ``refused``, or the last apply's after a window's
            refusal) and the errors the order queued.
        """
        units, errors, _ = self.exchange(
            ["SYST:PINS:APPL", "SYST:PINS:RES?"], raise_errors=False, timeout=10.0
        )
        return parse_fields(units[0]), errors

    def pins_defaults(self) -> tuple[dict[str, str], list[ErrorEntry]]:
        """``SYSTem:PINS:DEFault``: the rig's map back, with :meth:`apply`'s checks.

        It needs no INA228: the way back from a map that left none.

        :returns: ``SYSTem:PINS:RESult?``'s fields and the errors the order queued.
        """
        units, errors, _ = self.exchange(
            ["SYST:PINS:DEF", "SYST:PINS:RES?"], raise_errors=False, timeout=10.0
        )
        return parse_fields(units[0]), errors

    # -- measurements -----------------------------------------------------------

    def measure(self, function: str = "CURR", *, timeout: float = 30.0) -> float:
        """``MEASure:<function>?``: one acquisition, its mean in base units.

        It configures the function first, and takes ``SAMPle:COUNt`` samples.

        :param function: ``CURR``, ``VOLT``, ``POW``, ``ENER`` or ``TEMP``.
        :param timeout: How long to wait for the answer, seconds: longer than the
            sample count times the profile's period.
        :returns: Amperes, volts, watts, joules over the window, or degrees Celsius.
        :raises MeterError: When it was refused.
        """
        units, _, _ = self.exchange([f"MEAS:{function}?"], timeout=timeout)
        return float(units[0])

    def start(self, channels: Sequence[str] = ()) -> dict[str, str]:
        """Choose the channels, start a capture, and return its header.

        :attr:`capture` starts empty; a streamed capture's records fill it as the
        responses that follow are read.

        :param channels: The stream channels, ``i_ua``, ``v_mv``, ``p_mw`` and
            ``die_c`` among them; the profile's own when empty.
        :returns: ``FETCh:HEADer?``'s fields.
        :raises MeterError: When the capture was refused.
        :raises KeyError: For a channel that is none of the four.
        """
        self.capture = Capture()
        if channels:
            functions = ",".join(quote(CHANNEL_FUNCTIONS[name]) for name in channels)
            self.exchange([f"SENS:FUNC:ON {functions}"])
        else:
            self.exchange(["SENS:FUNC:ON:DEF"])
        units, _, _ = self.exchange(["INIT", "FETC:HEAD?"])
        return parse_fields(units[0])

    def start_statistics(self) -> dict[str, str]:
        """Start a statistics-only capture (``SENSe:CAPTure STATistics``, 0.10.0).

        Nothing is streamed and the ring is untouched; :meth:`stop` returns the
        statistics. The setting stays: :meth:`set_capture` puts it back.

        :returns: The capture's header.
        :raises MeterError: When the capture was refused.
        """
        self.capture = Capture()
        units, _, _ = self.exchange(["SENS:CAPT STAT", "INIT", "FETC:HEAD?"])
        return parse_fields(units[0])

    def set_capture(self, statistics_only: bool) -> None:
        """``SENSe:CAPTure DATA|STATistics``: what the next ``INITiate`` keeps.

        :param statistics_only: The statistics alone, rather than the samples too.
        """
        self.exchange([f"SENS:CAPT {'STAT' if statistics_only else 'DATA'}"])

    def set_threshold(self, amps: float | None, hysteresis: float = 0.0) -> None:
        """The current level the statistics count against.

        :param amps: The level, amperes; None for ``OFF``.
        :param hysteresis: How far below it the count falls back, amperes.
        :raises MeterError: When a value is outside the instrument's range.
        """
        level = "OFF" if amps is None else f"{amps:.9f}"
        self.exchange([f"SENS:CURR:THR {level}", f"SENS:CURR:THR:HYST {hysteresis:.9f}"])

    def self_test(self) -> int:
        """``*TST?``: the instrument's self-test.

        :returns: 0 when every part passed, else one bit per part (``docs/scpi.md``).
        """
        return int(self.query("*TST?", timeout=10.0))

    def health(self) -> dict[str, str]:
        """``SYSTem:HEALth?``: the reset reason, the image, the stacks and the counts.

        :returns: The report's fields.
        """
        return self.report("SYST:HEAL?")

    def batches(self) -> dict[str, str]:
        """``FETCh:STATistics:BATCh?``: the last closed capture's batch means.

        :returns: The report's fields.
        """
        return self.report("FETC:STAT:BATC?")

    def stop(self, *, timeout: float = 20.0) -> dict[str, str]:
        """Close the capture and return its statistics.

        :param timeout: How long to wait, seconds.
        :returns: ``FETCh:STATistics?``'s fields.
        :raises MeterError: When no window was open (``111``), or an error was queued.
        """
        units, _, _ = self.exchange(["ABOR", "FETC:STAT?"], timeout=timeout)
        return parse_fields(units[0])

    def mark(self, label: str) -> Mark:
        """Mark the running capture.

        :param label: The mark's label.
        :returns: The mark record the instrument emitted.
        :raises MeterError: When it was refused, or no mark record came.
        """
        before = len(self.capture.marks)
        self.exchange([f"TRAC:MARK {quote(label)}"])
        for mark in reversed(self.capture.marks[before:]):
            if mark.label == label:
                return mark
        raise MeterError(f"TRAC:MARK {label!r} was accepted but no mark record came")

    def zero(self, seconds: int) -> dict[str, str]:
        """Open a null window on the live range and profile.

        :param seconds: How long, 1 to 600.
        :returns: ``CALibration:ZERO:RESult?``'s fields, the window running.
        :raises MeterError: When it was refused.
        """
        units, _, _ = self.exchange([f"CAL:ZERO {seconds}", "CAL:ZERO:RES?"])
        return parse_fields(units[0])

    def close_zero(self, *, timeout: float = 20.0) -> tuple[dict[str, str], list[ErrorEntry]]:
        """Close the null window.

        :param timeout: How long to wait, seconds.
        :returns: Its result, and any errors: a null out of bounds is not stored
            (``102``, ``106``) and is returned, not raised.
        """
        units, errors, _ = self.exchange(
            ["ABOR", "CAL:ZERO:RES?"], timeout=timeout, raise_errors=False
        )
        return (parse_fields(units[0]) if units else {}), errors

    # -- the shunt calibration --------------------------------------------------

    def set_reference(self, ohms: float) -> None:
        """``CALibration:SHUNt:REFerence``: the reference resistor's value.

        :param ohms: Ohms, 1 to 4000.
        """
        self.exchange([f"CAL:SHUN:REF {ohms:.6f}"])

    def shunt_point(self, *, timeout: float = 60.0) -> dict[str, str]:
        """``CALibration:SHUNt``: one point of the shunt on the live range.

        The reference must be in place of the DUT (``calibration/README.md``).

        :param timeout: How long the acquisition may take, seconds.
        :returns: ``CALibration:SHUNt:RESult?``'s fields.
        :raises MeterError: When it was refused.
        """
        units, _, _ = self.exchange(["CAL:SHUN", "CAL:SHUN:RES?"], timeout=timeout)
        return parse_fields(units[0])

    def set_shunt(self, ohms: float, tag: str) -> dict[str, str]:
        """Store a measured shunt and its tag, as the instrument's working copy.

        The registry is the calibration: ``kleidos-meter calibration restore`` is
        how the copy is written from it.

        :param ohms: The shunt, ohms.
        :param tag: The calibration tag.
        :returns: ``CALibration:DATA?``'s fields.
        :raises MeterError: When it was refused.
        """
        units, _, _ = self.exchange(
            [f"SENS:CURR:SHUN {ohms:.6f}", f"CAL:TAG {quote(tag)}", "CAL:DATA?"]
        )
        return parse_fields(units[0])

    def dump(self, *, raw: bool = True, timeout: float = 180.0) -> list[str]:
        """``TRACe:DATA?``: the ring's rows.

        :param raw: ``RAW`` counts, rather than ``SCALed`` currents.
        :param timeout: How long the whole dump may take, seconds.
        :returns: The block's lines, its opening and closing rows among them.
        :raises MeterError: When no block came, or it arrived damaged.
        """
        _, _, block = self.exchange([f"TRAC:DATA? {'RAW' if raw else 'SCAL'}"], timeout=timeout)
        if block is None:
            raise MeterError("TRAC:DATA? answered no block")
        try:
            check_dump(block)
        except ValueError as error:
            raise MeterError(f"TRAC:DATA? arrived damaged: {error}") from error
        return block.decode("ascii").splitlines()

    def trace(self, *, timeout: float = 240.0) -> Dump:
        """The ring, decoded: ``FETCh:HEADer?`` and ``TRACe:DATA? RAW`` in one message.

        The header is read in the same message as the dump, so both describe
        the same capture, and the raw counts become currents exactly as the
        instrument converts them (:func:`~kleidos_meter.protocol.decode_dump`).

        :param timeout: Seconds the whole answer may take; a full ring at
            6 Mbaud takes about 20.
        :returns: The decoded dump.
        :raises MeterError: When no block came, or it arrived damaged.
        """
        units, _, block = self.exchange(["FETC:HEAD?", "TRAC:DATA? RAW"], timeout=timeout)
        if block is None or not units:
            raise MeterError("FETC:HEAD?;TRAC:DATA? RAW answered no header and block")
        try:
            return decode_dump(block, units[0])
        except ValueError as error:
            raise MeterError(f"TRAC:DATA? arrived damaged: {error}") from error
