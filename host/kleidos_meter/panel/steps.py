# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's measurement sequences as data: their parameters, their steps and their cleanup.

Each sequence the panel offers as one button is a :class:`Definition`: a
title, the parameters a person may set (:class:`Param`, each with its
default and its bounds), the steps it runs in order and the cleanup it runs
whatever happened (a step that failed, an abort, serve stopping). The steps
are frozen values that name what they send; nothing here talks to the
instrument. :mod:`~kleidos_meter.panel.sequences` runs them on the
session's executor and recorder, and :mod:`~kleidos_meter.panel.schema`
declares each sequence for the page and holds what it sends to the
firmware's command table (:meth:`Definition.uses`).

The steps:

- :class:`Send` a program message, which fails on any error the instrument
  queues for it; :class:`Query` one and keep its report, :class:`Check` a
  field of a kept report.
- :class:`Wait` some seconds; :class:`WaitUntil` a report's field reads one
  of some values, polling, up to a time-out.
- :class:`Prompt` a person: the sequence goes on only on the answer that
  says so, which comes through the API.
- :class:`StartCapture`, :class:`WaitCapture`, :class:`StopCapture` and
  :class:`MarkStep`: a capture of the recorder's, recorded into a run like
  any other, and marks in it.
- :class:`CloseNull` (a cleanup) closes a null window the sequence opened,
  and :class:`CalibrationStep` verifies the instrument against the registry
  or writes the registry's copy back.

A message, a prompt's text and a checked value may name a parameter or a
kept value in braces (``CAL:ZERO {seconds}``); a time is a :class:`Span`, a
constant plus a parameter scaled. Every cleanup puts the relays back on the
shunt (``ROUTe:NULLpath OFF`` recloses Relay 1 and opens Relay 2 from any path,
make-before-break) and gives the I2C clock back what it was.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..sim_table import split_header

__all__ = [
    "DEFINITIONS",
    "CalibrationStep",
    "Check",
    "CloseNull",
    "Definition",
    "MarkStep",
    "Param",
    "Prompt",
    "Query",
    "Send",
    "Span",
    "StartCapture",
    "Step",
    "StopCapture",
    "Wait",
    "WaitCapture",
    "WaitUntil",
    "message_headers",
]


def message_headers(message: str) -> tuple[str, ...]:
    """The header of each unit of *message*, as written: ``CAL:ZERO {seconds}`` is ``CAL:ZERO``."""
    return tuple(
        split_header(unit.strip().lstrip(":"))[0] for unit in message.split(";") if unit.strip()
    )


def placeholders(text: str) -> set[str]:
    """The names *text* puts in braces."""
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@dataclass(frozen=True)
class Param:
    """A parameter of a sequence a person may set.

    :ivar name: Its key in the start request and in braces.
    :ivar label: What the page calls it.
    :ivar unit: Its unit, as the page shows it.
    :ivar default: Its value when none is given.
    :ivar low: The least it takes.
    :ivar high: The most it takes.
    """

    name: str
    label: str
    unit: str
    default: int
    low: int
    high: int


@dataclass(frozen=True)
class Span:
    """A time, seconds: ``base`` plus ``scale`` times the parameter ``param``.

    :ivar base: The constant part, seconds.
    :ivar param: The parameter added, None for none.
    :ivar scale: What the parameter is multiplied by (0.001 for milliseconds).
    """

    base: float = 0.0
    param: str | None = None
    scale: float = 1.0

    def seconds(self, params: Mapping[str, int]) -> float:
        """The time, for the sequence's *params*."""
        return self.base + (0.0 if self.param is None else params[self.param] * self.scale)


@dataclass(frozen=True)
class Send:
    """Send a program message; any error the instrument queues for it fails the step.

    :ivar message: The message, with braces for parameters and kept values.
    """

    message: str

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return message_headers(self.message)

    def describe(self) -> str:
        """The step, for the page's log."""
        return f"send {self.message}"


@dataclass(frozen=True)
class Query:
    """Ask one query and keep its answer.

    :ivar query: The query.
    :ivar keep: The name it is kept under, in the sequence's results.
    :ivar var: A name to keep a value of the answer under, for braces later.
    :ivar key: The field kept as *var*; None for the whole answer.
    """

    query: str
    keep: str
    var: str | None = None
    key: str | None = None

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return message_headers(self.query)

    def describe(self) -> str:
        """The step, for the page's log."""
        return f"ask {self.query}"


@dataclass(frozen=True)
class Check:
    """Check a field of a kept answer: it fails the sequence when it does not hold.

    :ivar keep: The kept answer's name.
    :ivar key: The field; None for the whole answer.
    :ivar op: ``eq`` (the text is *value*) or ``contains`` (a comma-separated
        list of the text holds *value*).
    :ivar value: What it must be, with braces for parameters and kept values.
    :ivar optional: Whether a field the answer lacks passes (a key an older
        image does not report).
    """

    keep: str
    key: str | None
    op: str
    value: str
    optional: bool = False

    def headers(self) -> tuple[str, ...]:
        """Nothing: it checks what was kept."""
        return ()

    def describe(self) -> str:
        """The step, for the page's log."""
        where = self.keep if self.key is None else f"{self.keep} {self.key}"
        return f"check {where} {self.op} {self.value}"


@dataclass(frozen=True)
class Wait:
    """Let some time pass (an abort ends it at once).

    :ivar span: How long.
    """

    span: Span

    def headers(self) -> tuple[str, ...]:
        """Nothing."""
        return ()

    def describe(self) -> str:
        """The step, for the page's log."""
        return "wait"


@dataclass(frozen=True)
class WaitUntil:
    """Poll a report until a field reads one of some values, or fail at the time-out.

    :ivar query: The report query polled.
    :ivar key: Its field.
    :ivar values: What ends the wait.
    :ivar timeout: How long it may take.
    :ivar poll: Seconds between two polls.
    """

    query: str
    key: str
    values: tuple[str, ...]
    timeout: Span
    poll: float = 0.25

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return message_headers(self.query)

    def describe(self) -> str:
        """The step, for the page's log."""
        return f"wait until {self.query} {self.key} is {' or '.join(self.values)}"


@dataclass(frozen=True)
class Prompt:
    """Ask a person, through the API; the sequence goes on only on *proceed*.

    :ivar text: The question, with braces for parameters.
    :ivar choices: The answers offered.
    :ivar proceed: The answer that goes on; any other ends the sequence, cleanup first.
    """

    text: str
    choices: tuple[str, ...] = ("continue", "cancel")
    proceed: str = "continue"

    def headers(self) -> tuple[str, ...]:
        """Nothing."""
        return ()

    def describe(self) -> str:
        """The step, for the page's log."""
        return "ask the person at the bench"


@dataclass(frozen=True)
class MarkStep:
    """``TRACe:MARK`` in the sequence's running capture.

    :ivar label: The mark's label, 1 to 15 printable characters.
    """

    label: str

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return ("TRAC:MARK",)

    def describe(self) -> str:
        """The step, for the page's log."""
        return f"mark {self.label}"


@dataclass(frozen=True)
class StartCapture:
    """Start a capture of the recorder's, recorded into a run.

    :ivar profile: Its profile; the profile resets the channels to its default.
    :ivar seconds: When given, the recorder stops it after this long.
    """

    profile: str
    seconds: Span | None = None

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return ("SENS:PROF", "INIT", "FETC:HEAD?")

    def describe(self) -> str:
        """The step, for the page's log."""
        return f"start a {self.profile} capture"


@dataclass(frozen=True)
class WaitCapture:
    """Wait until the sequence's capture has ended by itself (its seconds, a full ring).

    The run must be complete: every sample the instrument took reached it.

    :ivar timeout: How long it may take.
    :ivar ring: Whether it is a ring capture, whose dump the recorder fetches.
    """

    timeout: Span
    ring: bool = False

    def headers(self) -> tuple[str, ...]:
        """What the recorder sends to close it."""
        ring = ("FETC:HEAD?", "TRAC:POIN?", "TRAC:DATA?") if self.ring else ()
        return ("ABOR", "FETC:STAT?", *ring)

    def describe(self) -> str:
        """The step, for the page's log."""
        return "wait for the capture's end"


@dataclass(frozen=True)
class StopCapture:
    """Stop the sequence's capture and finish its run, which must be complete.

    As a cleanup it stops a capture the sequence started that still runs,
    and does nothing otherwise.
    """

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return ("ABOR", "FETC:STAT?")

    def describe(self) -> str:
        """The step, for the page's log."""
        return "stop the capture"


@dataclass(frozen=True)
class CloseNull:
    """A cleanup: close a null window the sequence opened, if it is still open."""

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        return ("SYST:STAT?", "ABOR")

    def describe(self) -> str:
        """The step, for the page's log."""
        return "close the null window"


@dataclass(frozen=True)
class CalibrationStep:
    """The instrument against the calibration registry.

    :ivar action: ``verify`` (fails unless the working copy is the
        registry's) or ``restore`` (writes the registry's copy where it
        differs, then verifies).
    """

    action: str

    def headers(self) -> tuple[str, ...]:
        """What it sends."""
        verify = ("*IDN?", "SYST:INF?", "CAL:DATA?")
        if self.action == "verify":
            return verify
        return (
            *verify,
            "SENS:CURR:SHUN",
            "SENS:CURR:SHUN:RANG1",
            "CAL:TAG",
            "CAL:DATE",
            "CAL:VBUS",
        )

    def describe(self) -> str:
        """The step, for the page's log."""
        return f"{self.action} the calibration against the registry"


#: Any step.
Step = (
    Send
    | Query
    | Check
    | Wait
    | WaitUntil
    | Prompt
    | MarkStep
    | StartCapture
    | WaitCapture
    | StopCapture
    | CloseNull
    | CalibrationStep
)


@dataclass(frozen=True)
class Definition:
    """A sequence: what the page shows of it, and what it runs.

    :ivar name: Its key.
    :ivar title: Its button's label.
    :ivar summary: What it does, in a line.
    :ivar steps: What it runs, in order.
    :ivar cleanup: What runs after it, whatever happened, in order.
    :ivar params: What a person may set.
    :ivar defaults: Kept values a cleanup may name before a step kept them.
    """

    name: str
    title: str
    summary: str
    steps: tuple[Step, ...]
    cleanup: tuple[Step, ...] = ()
    params: tuple[Param, ...] = ()
    defaults: Mapping[str, str] = field(default_factory=dict)

    def uses(self) -> tuple[str, ...]:
        """Every header it sends, as written, steps then cleanup, each once."""
        seen: dict[str, None] = {}
        for step in (*self.steps, *self.cleanup):
            for header in step.headers():
                seen.setdefault(header, None)
        return tuple(seen)


#: The cleanup that puts the relays back on the shunt and checks they are.
_BACK_TO_SHUNT: tuple[Step, ...] = (
    Send("ROUT:NULL OFF"),
    Query("ROUT:STAT?", "route_after"),
    Check("route_after", "path", "eq", "shunt"),
)

#: A null window of the ``seconds`` parameter, closed and its null checked stored.
_NULL: tuple[Step, ...] = (
    Send("CAL:ZERO {seconds}"),
    WaitUntil("SYST:STAT?", "running", ("0",), Span(30.0, "seconds")),
    Send("ABOR"),
    Query("CAL:ZERO:RES?", "null"),
    Check("null", "stored", "eq", "1"),
)

#: A null window's length.
_NULL_SECONDS = Param("seconds", "Null window", "s", 5, 1, 600)

#: The sequences, in the page's order.
DEFINITIONS: tuple[Definition, ...] = (
    Definition(
        "HEALTH",
        "Health",
        "identity, reset reason, stacks, heap counts, watchdog, self-test and bus scan",
        (
            Query("*IDN?", "idn"),
            Query("SYST:INF?", "info"),
            Query("SYST:HEAL?", "health"),
            Query("DIAG:HEAP?", "heap"),
            Query("DIAG:WATC?", "watchdog"),
            Query("*TST?", "self_test"),
            Query("DIAG:I2C:SCAN?", "scan"),
            Check("info", "chip", "eq", "ina228"),
            Check("self_test", None, "eq", "0"),
            Check("scan", "found", "contains", "0x40"),
            Check("watchdog", "console", "eq", "1"),
            Check("watchdog", "sampler", "eq", "1"),
            Check("heap", "allocs_after_init", "eq", "0"),
            Check("heap", "allocs_since_main", "eq", "0", optional=True),
            Check("heap", "frees_since_main", "eq", "0", optional=True),
        ),
    ),
    Definition(
        "NULL_RELAY",
        "Null on the relay path",
        "a null with the DUT on the bypass (Relay 2 then Relay 1), the path released after",
        (
            Send("ROUT:NULL ON"),
            Query("ROUT:STAT?", "route_null"),
            Check("route_null", "path", "eq", "bypass"),
            *_NULL,
            Send("ROUT:NULL OFF"),
            Query("ROUT:STAT?", "route"),
            Check("route", "path", "eq", "shunt"),
        ),
        (CloseNull(), *_BACK_TO_SHUNT),
        (_NULL_SECONDS,),
    ),
    Definition(
        "NULL_LEAD",
        "Null on an open lead",
        "a null with Relay 1 open (FORCe: the DUT has no battery), the lead reclosed after",
        (
            Prompt(
                "Relay 1 opens for a null of {seconds} s: the DUT has no battery until it recloses."
                " Continue?"
            ),
            Send("ROUT:LEAD OPEN,FORC"),
            Query("ROUT:STAT?", "route_open"),
            Check("route_open", "path", "eq", "none"),
            *_NULL,
            Send("ROUT:LEAD CLOS"),
            Query("ROUT:STAT?", "route"),
            Check("route", "path", "eq", "shunt"),
        ),
        (CloseNull(), *_BACK_TO_SHUNT),
        (_NULL_SECONDS,),
    ),
    Definition(
        "STATE",
        "STATE capture",
        "a streamed capture of a state: bus, shunt and temperature at 1152 us",
        (
            StartCapture("STATE", Span(0.0, "seconds")),
            WaitCapture(Span(60.0, "seconds")),
        ),
        (StopCapture(),),
        (Param("seconds", "Length", "s", 10, 1, 3600),),
    ),
    Definition(
        "SLEEP",
        "SLEEP capture",
        "a streamed capture of deep sleep, averaged 16 at 67.52 ms",
        (
            StartCapture("SLEEP", Span(0.0, "seconds")),
            WaitCapture(Span(60.0, "seconds")),
        ),
        (StopCapture(),),
        (Param("seconds", "Length", "s", 60, 1, 86400),),
    ),
    Definition(
        "TRACE",
        "TRACE ring",
        "a ring of 1048576 shunt samples at 280 us, then its dump",
        (StartCapture("TRACE"), WaitCapture(Span(900.0), ring=True)),
        (StopCapture(),),
    ),
    Definition(
        "BURST",
        "BURST ring",
        "a ring at 100 us on a 1 MHz bus, then its dump and the bus's clock back",
        (
            Query("SYST:COMM:I2C:FREQ?", "i2c", var="i2c_hz"),
            Send("SYST:COMM:I2C:FREQ 1000000"),
            StartCapture("BURST"),
            WaitCapture(Span(600.0), ring=True),
            Send("SYST:COMM:I2C:FREQ {i2c_hz}"),
            Query("SYST:COMM:I2C:FREQ?", "i2c_after"),
            Check("i2c_after", None, "eq", "{i2c_hz}"),
        ),
        (StopCapture(), Send("SYST:COMM:I2C:FREQ {i2c_hz}")),
        defaults={"i2c_hz": "400000"},
    ),
    Definition(
        "COLD_BOOT",
        "Cold boot",
        "the DUT's boot from power-off: a capture across a lead cycle, its moves marked",
        (
            Prompt(
                "Relay 1 opens for {off_ms} ms under a STATE capture: the DUT loses its battery and"
                " boots cold. Continue?"
            ),
            StartCapture("STATE"),
            Wait(Span(1.0)),
            MarkStep("cut"),
            Send("ROUT:LEAD:CYCL {off_ms},FORC,LIVE"),
            WaitUntil("ROUT:STAT?", "path", ("shunt",), Span(10.0, "off_ms", 0.001)),
            MarkStep("reclosed"),
            Wait(Span(0.0, "seconds")),
            StopCapture(),
        ),
        (StopCapture(), *_BACK_TO_SHUNT),
        (
            Param("off_ms", "Off for", "ms", 2000, 1, 60000),
            Param("seconds", "Captured after", "s", 10, 1, 600),
        ),
    ),
    Definition(
        "CAL_VERIFY",
        "Verify the calibration",
        "the instrument's calibration against the registry's copy",
        (CalibrationStep("verify"),),
    ),
    Definition(
        "CAL_RESTORE",
        "Restore the calibration",
        "the registry's copy written back where it differs, then verified",
        (
            Prompt("Write the registry's working copy to the instrument where it differs?"),
            CalibrationStep("restore"),
        ),
    ),
)
