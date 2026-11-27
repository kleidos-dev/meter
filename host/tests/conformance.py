# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The client suite every instrument the host talks to must answer alike.

The host has three instruments it can talk to: the simulator
(``kleidos_meter.sim``), the firmware itself built for the host (the native
bridge, ``make -C firmware/test/native bridge``: a ``Bench`` whose UART0 is
the process's standard input and output) and either of them behind serve's
SCPI passthrough. :data:`CASES` is the suite: every parameterless query the
simulator implements (the commands it shares with the firmware's
``kCommands``) asked on a fresh instrument, a setting written and read back,
and the command errors a client meets first. :func:`run_suite` asks
them through :class:`~kleidos_meter.client.Meter`, as every host tool does,
and reduces each answer to its :class:`Signature`: how many units it has, the
keys of each report or the shape of each value, and the error codes. Two
instruments conform when every case has the same signature on both, and an
exact case (:attr:`Case.exact`: the pin map and the averaging override, whose
every byte is the rules' and none an instrument's own) the same raw answer and
the same text in each error.

A signature leaves the values out on purpose: a serial, an uptime or a
reading differs between two instruments that are both right. Where both ends
are the simulator (the passthrough), :func:`run_suite` also keeps the raw
answer, so a test can hold the passthrough byte for byte.

:class:`ProcessTransport` is the byte pipe to the bridge: the process's
standard input and output, read on a thread so that a read waits at most its
timeout, as a serial port's does.
"""

from __future__ import annotations

import itertools
import os
import re
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kleidos_meter.client import Meter, MeterTimeout
from kleidos_meter.protocol import parse_fields, split_units
from kleidos_meter.sim import IMPLEMENTED
from kleidos_meter.sim_table import COMMANDS, compile_pattern

#: The environment variable that names the firmware's bridge binary.
BRIDGE_ENV = "KLEIDOS_METER_NATIVE_BRIDGE"

#: Why the bridge's cases skip when it names none.
BRIDGE_SKIP = (
    f"{BRIDGE_ENV} is not set: it names the firmware's native bridge binary"
    " (make -C firmware/test/native bridge), so the suite runs against the simulator alone"
)

#: Queries whose answer moves by itself between two asks (the self-test runs, a scan
#: probes, a window starts when it was asked for): their raw answers are not compared,
#: only their signatures.
_MOVING = frozenset(
    {
        "SYST:HEAL?",
        "SYST:STAT?",
        "*TST?",
        "DIAG:I2C:SCAN?",
        "DIAG:WATC?",
        "FETC:STAT?",
        "FETC:STAT:BATC?",
        "FETC:HEAD?",
    }
)

_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def short_header(pattern: str) -> str:
    """The header *pattern* matches in short forms, optional nodes left out, suffix 1."""
    if pattern.startswith("*"):
        return pattern
    nodes = [
        node.short + ("1" if node.suffix else "")
        for node in compile_pattern(pattern)
        if not node.optional
    ]
    return ":".join(nodes) + ("?" if pattern.endswith("?") else "")


def shared_queries() -> tuple[str, ...]:
    """Every parameterless query the simulator implements, in the firmware's order."""
    return tuple(
        short_header(pattern)
        for pattern, _, most in COMMANDS
        if pattern in IMPLEMENTED and pattern.endswith("?") and most == 0
    )


@dataclass(frozen=True)
class Case:
    """One message of the suite.

    :ivar name: Its name in a report.
    :ivar units: The program message's units, sent with ``SYSTem:ERRor:ALL?`` after them.
    :ivar exact: Whether every instrument must answer it byte for byte alike.
    """

    name: str
    units: tuple[str, ...]
    exact: bool = False


#: What every command that takes a parameter is asked with, and cannot take or must
#: judge: nothing, a word, a number past any range, a quoted string with a space (a
#: token nowhere) and a negative number.
MALFORMED = ("", "ZZZ", "99999999999", '"a b"', "-1")


def malformed() -> tuple[Case, ...]:
    """Every command both answer that takes a parameter, with each of :data:`MALFORMED`."""
    cases = []
    for pattern, _, most in COMMANDS:
        if pattern not in IMPLEMENTED or most == 0:
            continue
        for parameter in MALFORMED:
            unit = f"{short_header(pattern)} {parameter}".strip()
            cases.append(Case(f"malformed: {unit}", (unit,)))
    return tuple(cases)


#: The counts and the shunt conversion times, microseconds, the INA228 takes.
COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)
TIMES_US = (50, 84, 150, 280, 540, 1052, 2074, 4120)


def override_sweep() -> tuple[Case, ...]:
    """Every count and conversion time, turned on on each profile at three bus clocks.

    Byte for byte: the word and the period an accepted one gives, and what a
    refused one is told. Two pairs to a message, which is as long as one may be.
    """
    cases = []
    pairs = [(count, time) for count in COUNTS for time in TIMES_US]
    for profile, hz in itertools.product(("TRAC", "STAT", "SLE"), (50_000, 400_000, 1_000_000)):
        cases.append(
            Case(
                f"override sweep: {profile} at {hz} Hz",
                ("*RST", f"SENS:PROF {profile}", f"SYST:COMM:I2C:FREQ {hz}"),
            )
        )
        for (count, time), (other, longer) in zip(pairs[::2], pairs[1::2], strict=True):
            units = [
                unit
                for n, us in ((count, time), (other, longer))
                for unit in (
                    "SENS:AVER OFF",
                    f"SENS:AVER:COUN {n}",
                    f"SENS:CURR:APER {us / 1e6:.6f}",
                    "SENS:AVER ON",
                    "SENS:PROF:REP?",
                )
            ]
            name = f"override sweep: {profile} at {hz} Hz, {count} x {time} and {longer} us"
            cases.append(Case(name, tuple(units), exact=True))
    cases.append(Case("override sweep: over", ("*RST",)))
    return tuple(cases)


#: The suite, in the order it runs: the queries on a fresh instrument first, the
#: malformed parameters last, because an accepted one changes a setting.
CASES: tuple[Case, ...] = (
    *(Case(query, (query,)) for query in shared_queries()),
    Case("range written and read back", ("SENS:CURR:RANG:IND 1", "SENS:CURR:RANG:IND?")),
    Case("range put back", ("SENS:CURR:RANG:IND 0", "SENS:CURR:RANG:IND?")),
    Case("an undefined header", ("KLEIDOS:NOTHING?",)),
    Case("a parameter where none is taken", ("SYST:ERR:COUN? 1",)),
    Case("more parameters than a command takes", ("SENS:PROF STAT,1",)),
    Case("a comma inside a quoted parameter", ('CAL:TAG "a,b"', "CAL:TAG?")),
    Case("more parameters than a unit may carry", ("SENS:FUNC:ON 1,2,3,4,5,6,7,8,9",)),
    Case("a missing parameter", ("SENS:PROF",)),
    Case("a tab ends a header", ("SENS:PROF\tSLEEP", "SENS:PROF?", "SENS:PROF\t\tSTAT")),
    Case("a tab ends a query's header", ("SENS:CURR:THR:HYST?\t1",)),
    Case("a relay the rig does not wire", ("ROUT:REL3 ON",)),
    Case("a relay the rig does not wire, with no state", ("ROUT:REL4",)),
    Case("a relay the rig does not wire, with no such state", ("ROUT:REL3 UP",)),
    Case("a relay the rig does not have", ("ROUT:REL5 ON",)),
    # The pin map (0.18.0), all byte for byte: the three refusals its contract
    # names, a staging of every kind, and the orders that change the board.
    Case("SDA staged on GPIO45", ("SYST:PINS:STAG SDA,45", "SYST:PINS:PEND?"), exact=True),
    Case(
        "one pin staged for two roles",
        ("SYST:PINS:CLE", "SYST:PINS:STAG SDA,5", "SYST:PINS:PEND?"),
        exact=True,
    ),
    Case(
        "ALERT staged on GPIO39",
        ("SYST:PINS:CLE", "SYST:PINS:STAG ALER,39", "SYST:PINS:PEND?"),
        exact=True,
    ),
    Case(
        "a staging of every kind",
        (
            "SYST:PINS:CLE",
            "SYST:PINS:STAG RELAY1,#Q13,HIGH",
            "SYST:PINS:STAG rel2,#B1100,low",
            "SYST:PINS:STAG SDA,8,OFF",
            "SYST:PINS:STAG SCL,NONE",
            "SYST:PINS:ADDR #H4F",
            "SYST:PINS:CLOC 1000001",
            "SYST:PINS:PEND?",
        ),
        exact=True,
    ),
    Case(
        "a staging refused",
        (
            "SYST:PINS:CLE",
            "SYST:PINS:STAG SDA,64",
            "SYST:PINS:STAG SDA,1e3",
            "SYST:PINS:STAG REL1,8,UP",
            "SYST:PINS:ADDR 0",
            "SYST:PINS:PEND?",
        ),
        exact=True,
    ),
    Case("the staging cleared", ("SYST:PINS:CLE", "SYST:PINS:PEND?"), exact=True),
    Case("no map applied yet", ("SYST:PINS:RES?",), exact=True),
    Case(
        "a staged map applied",
        (
            "SYST:PINS:CLOC 100000",
            "SYST:PINS:APPL",
            "SYST:PINS:RES?",
            "SYST:PINS?",
            "SYST:PINS:PEND?",
            "SYST:COMM:I2C:FREQ?",
        ),
        exact=True,
    ),
    Case(
        "a wrong SDA applied and put back",
        (
            "SYST:PINS:STAG SDA,18",
            "SYST:PINS:APPL",
            "SYST:PINS:RES?",
            "SYST:PINS?",
            "SYST:PINS:PEND?",
        ),
        exact=True,
    ),
    Case(
        "a wrong ALERT applied and put back",
        (
            "SYST:PINS:CLE",
            "SYST:PINS:STAG ALER,18",
            "SYST:PINS:APPL",
            "SYST:PINS:RES?",
            "SYST:PINS?",
        ),
        exact=True,
    ),
    Case(
        "an address nothing answers at applied and put back",
        ("SYST:PINS:CLE", "SYST:PINS:ADDR #H4F", "SYST:PINS:APPL", "SYST:PINS:RES?", "SYST:PINS?"),
        exact=True,
    ),
    Case(
        "a staged map that breaks a rule, applied",
        (
            "SYST:PINS:CLE",
            "SYST:PINS:STAG SDA,45",
            "SYST:PINS:APPL",
            "SYST:PINS:RES?",
            "SYST:PINS?",
        ),
        exact=True,
    ),
    Case(
        "a map applied with the bypass closed",
        ("SYST:PINS:CLE", "ROUT:BYP ON", "SYST:PINS:APPL", "SYST:PINS:RES?", "ROUT:BYP OFF"),
        exact=True,
    ),
    Case(
        "a map applied under a capture",
        (
            "SENS:CAPT STAT",
            "INIT",
            "SYST:PINS:APPL",
            "SYST:PINS:DEF",
            "ABOR",
            "SENS:CAPT DATA",
            "SYST:PINS:RES?",
            "SYST:PINS?",
        ),
        exact=True,
    ),
    Case(
        "a relay moved to another pin",
        (
            "SYST:PINS:STAG REL1,13,HIGH",
            "SYST:PINS:APPL",
            "SYST:PINS:RES?",
            "SYST:PINS?",
        ),
        exact=True,
    ),
    Case(
        "the default map applied",
        ("SYST:PINS:DEF", "SYST:PINS:RES?", "SYST:PINS?", "SYST:PINS:PEND?"),
        exact=True,
    ),
    Case(
        "the default map staged and applied",
        (
            "SYST:PINS:CLOC 100000",
            "SYST:PINS:APPL",
            "SYST:PINS:CLOC 400000",
            "SYST:PINS:APPL",
            "SYST:PINS?",
            "SYST:PINS:RES?",
        ),
        exact=True,
    ),
    Case(
        "a running bus clock kept by a map put back",
        (
            "SYST:COMM:I2C:FREQ 200000",
            "SYST:PINS:STAG SCL,18",
            "SYST:PINS:APPL",
            "SYST:COMM:I2C:FREQ?",
            "SYST:PINS:DEF",
            "SYST:COMM:I2C:FREQ?",
        ),
        exact=True,
    ),
    Case("no pin map order takes a parameter", ("SYST:PINS:APPL 1", "SYST:PINS:DEF 1")),
    # The averaging override (0.18.0): off, set while off, on, every refusal of
    # its own, and back to off; what it converts with byte for byte.
    Case(
        "the averaging override off",
        ("SENS:AVER?", "SENS:AVER:COUN?", "SENS:CURR:APER?", "SENS:PROF:REP?"),
        exact=True,
    ),
    Case(
        "the averaging override set while off",
        (
            "SENS:AVER:COUN 16",
            "SENS:CURR:APER 0.00028",
            "SENS:AVER:COUN?",
            "SENS:CURR:APER?",
            "SENS:PROF:REP?",
        ),
        exact=True,
    ),
    Case(
        "the averaging override on",
        ("SENS:AVER ON", "SENS:AVER?", "SENS:AVER:COUN?", "SENS:CURR:APER?", "SENS:PROF:REP?"),
        exact=True,
    ),
    Case(
        "the averaging override of another profile",
        ("SENS:PROF SLEEP", "SENS:AVER?", "SENS:PROF:REP?", "SENS:PROF STATE", "SENS:AVER?"),
        exact=True,
    ),
    Case(
        "the averaging override under a capture",
        (
            "SENS:CAPT STAT",
            "INIT",
            "SENS:AVER OFF",
            "SENS:AVER:COUN 4",
            "SENS:CURR:APER 0.00005",
            "ABOR",
            "SENS:CAPT DATA",
            "SENS:PROF:REP?",
        ),
        exact=True,
    ),
    Case(
        "a count and a conversion time the INA228 does not take",
        (
            "SENS:AVER:COUN 2",
            "SENS:CURR:APER 0.000051",
            "SENS:CURR:APER 0.0000505",
            "SENS:AVER:COUN?",
            "SENS:CURR:APER?",
        ),
        exact=True,
    ),
    Case(
        "an averaging override shorter than one read",
        (
            "SENS:PROF TRAC",
            "SENS:AVER ON",
            "SENS:AVER:COUN 1",
            "SENS:CURR:APER 0.00005",
            "SENS:PROF:REP?",
        ),
        exact=True,
    ),
    Case(
        "a bus clock lowered under an override that is on",
        (
            "SYST:COMM:I2C:FREQ 1000000",
            "SENS:AVER:COUN 1",
            "SENS:CURR:APER 0.00015",
            "SENS:AVER ON",
            "SYST:COMM:I2C:FREQ 400000",
            "INIT",
            "MEAS:CURR?",
            "CAL:ZERO 1",
            "SENS:AVER OFF",
        ),
        exact=True,
    ),
    Case(
        "no averaging override on BURST",
        (
            "SENS:PROF BURS",
            "SENS:AVER ON",
            "SENS:AVER:COUN 4",
            "SENS:CURR:APER 0.000084",
            "SENS:AVER OFF",
            "SENS:PROF:REP?",
        ),
        exact=True,
    ),
    Case(
        "a ring on a bus too slow for it",
        ("INIT", "MEAS:CURR?", "SENS:CAPT STAT", "INIT", "SENS:CAPT DATA", "CAL:ZERO 1"),
        exact=True,
    ),
    Case(
        "the averaging override reset",
        ("SENS:PROF STAT", "*RST", "SENS:AVER?", "SENS:PROF:REP?"),
        exact=True,
    ),
    *override_sweep(),
    *malformed(),
)


def value_shape(unit: str) -> str:
    """What one unit is, without its value: a report's keys, or a value's form."""
    if "=" in unit and not unit.startswith(("'", '"')):
        return "report:" + ",".join(parse_fields(unit))
    fields = split_units(unit) if ";" in unit else [unit]
    parts = []
    for field in fields:
        items = field.split(",")
        kinds = [
            "number"
            if _NUMBER.match(item.strip())
            else "string"
            if item.strip()[:1] in "'\""
            else "word"
            for item in items
        ]
        parts.append("|".join(kinds))
    return "value:" + ";".join(parts)


@dataclass(frozen=True)
class Signature:
    """An answer without its values.

    :ivar shapes: Each response unit's :func:`value_shape`.
    :ivar block: Whether a definite-length block came.
    :ivar errors: The error codes queued, oldest first.
    """

    shapes: tuple[str, ...]
    block: bool
    errors: tuple[int, ...]


@dataclass(frozen=True)
class Outcome:
    """One case as an instrument answered it.

    :ivar signature: The answer without its values.
    :ivar raw: The units as they arrived, None for a case whose answer moves by itself.
    :ivar exact: Whether its case is exact: its raw answer and its errors' texts
        are compared too.
    :ivar said: The errors' texts, oldest first.
    """

    signature: Signature
    raw: tuple[str, ...] | None
    exact: bool = False
    said: tuple[str, ...] = ()


def run_suite(meter: Meter, cases: Sequence[Case] = CASES) -> dict[str, Outcome]:
    """Ask every case of *cases* through *meter*; each one's outcome by name.

    :raises MeterTimeout: When a case is not answered.
    """
    meter.wait_ready()
    outcomes: dict[str, Outcome] = {}
    for case in cases:
        units, errors, block = meter.exchange(list(case.units), raise_errors=False)
        signature = Signature(
            tuple(value_shape(unit) for unit in units),
            block is not None,
            tuple(entry.code for entry in errors),
        )
        moving = bool(set(case.units) & _MOVING)
        said = tuple(entry.text for entry in errors)
        outcomes[case.name] = Outcome(signature, None if moving else tuple(units), case.exact, said)
    return outcomes


def differences(reference: dict[str, Outcome], other: dict[str, Outcome]) -> list[str]:
    """The cases that differ, one line each: the signatures, an exact case's bytes too.

    :returns: Empty when the two conform.
    """
    lines = []
    for name, outcome in reference.items():
        seen = other.get(name)
        if seen is None:
            lines.append(f"{name}: not answered")
        elif seen.signature != outcome.signature:
            lines.append(f"{name}: {outcome.signature} != {seen.signature}")
        elif outcome.exact and seen.raw != outcome.raw:
            lines.append(f"{name}: {outcome.raw} != {seen.raw}")
        elif outcome.exact and seen.said != outcome.said:
            lines.append(f"{name}: {outcome.said} != {seen.said}")
    return lines


class ProcessTransport:
    """A process's standard input and output as a byte pipe to the instrument.

    :param command: The program and its arguments.
    :param timeout: Seconds a read waits for bytes, as a serial port's timeout.
    :raises OSError: When the program cannot be started.
    """

    def __init__(self, command: Sequence[str], *, timeout: float = 0.05) -> None:
        self._process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._timeout = timeout
        self._buffer = bytearray()
        self._arrived = threading.Condition()
        self._ended = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self._process.stdout
        assert stream is not None
        while True:
            chunk = os.read(stream.fileno(), 65536)
            with self._arrived:
                if not chunk:
                    self._ended = True
                    self._arrived.notify_all()
                    return
                self._buffer.extend(chunk)
                self._arrived.notify_all()

    def write(self, data: bytes) -> int:
        """Write *data* to the process's standard input.

        :raises MeterTimeout: When the process has ended and takes nothing.
        """
        stream = self._process.stdin
        assert stream is not None
        try:
            stream.write(data)
            stream.flush()
        except (BrokenPipeError, ValueError):
            raise MeterTimeout("the bridge process has ended") from None
        return len(data)

    def read(self, size: int) -> bytes:
        """Up to *size* bytes of its output, waiting at most the timeout for the first."""
        with self._arrived:
            if not self._buffer and not self._ended:
                self._arrived.wait(self._timeout)
            taken = bytes(self._buffer[:size])
            del self._buffer[:size]
        return taken

    def close(self) -> None:
        """End the process: its input closed, then terminated if it lingers."""
        stdin = self._process.stdin
        if stdin is not None:
            stdin.close()
        try:
            self._process.wait(2.0)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            self._process.wait(5.0)
        self._reader.join(5.0)
        stdout = self._process.stdout
        if stdout is not None:
            stdout.close()


def bridge_command(value: str | None) -> list[str] | None:
    """The bridge's command from :data:`BRIDGE_ENV`'s value, None when it names none.

    :raises FileNotFoundError: When it names a file that is not there.
    """
    if not value:
        return None
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(f"{BRIDGE_ENV} names {value}, which is not a file")
    return [str(path)]
