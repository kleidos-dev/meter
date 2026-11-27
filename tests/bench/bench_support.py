# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the bench suite needs besides the instrument: the lock, the line, the sweep.

The bench suite (``tests/bench``, ``--run-bench``) talks to the meter on the
bench. Three things are kept here, apart from the tests, so that the host
tests in ``test_bench_support.py`` can hold them without hardware:

- :class:`BenchLock`, the bench's lock file: one live process ID in a file,
  the convention every agent and person on the bench keeps before touching
  the meter;
- :class:`BenchLine`, the CH343 line opened the way the host client opens it
  (RTS released before DTR, so the open does not reset the board), which can
  also reset the board on purpose through the carrier's auto-reset circuit
  (``docs/carrier-board.md`` §7);
- the heap sweep (:data:`SWEEP`) and its expectation file
  (``heap_expectations.toml``): the counts ``DIAGnostic:HEAP?`` must report
  after the boot and what each command family may add to them, with the
  full ring the long cases fill (:func:`fill_ring`).
"""

from __future__ import annotations

import os
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The expectation file the sweep is held to.
EXPECTATIONS = Path(__file__).with_name("heap_expectations.toml")

#: The four counts by phase that ``DIAGnostic:HEAP?`` reports (FW-07).
PHASE_KEYS = ("pre_main_allocs", "pre_main_frees", "allocs_since_main", "frees_since_main")

#: The two counts that must read 0 after the boot and after every command
#: family, with no exemption (FW-17): every allocation and free since
#: ``app_main()``'s first line, and the 0.7.0 steady state's, which from FW-17
#: counts the same window.
ZERO_KEYS = ("allocs_since_main", "frees_since_main", "allocs_after_init", "frees_after_init")


#: The fewest bytes the console's stack may keep free after the sweep
#: (``SYSTem:HEALth?`` ``stack_console``): 0.16.6's margin, measured on its
#: image (ELF ``75603e5b2c364645``, 2026-11-23) after the same sweep, its own
#: static task's 8 KiB. From FW-17 the console runs on ESP-IDF's main task,
#: whose stack, the same 8 KiB, also held the boot, and it must keep at least
#: that margin.
CONSOLE_STACK_FLOOR = 5160


def since_main_problems(when: str, report: Mapping[str, str]) -> list[str]:
    """Every count of :data:`ZERO_KEYS` a ``DIAGnostic:HEAP?`` report does not read 0.

    :param when: What the report followed (``boot``, a step's name), for the message.
    :param report: The report's keys.
    :returns: One line per count missing or not 0; empty when all read 0.
    """
    problems = []
    for key in ZERO_KEYS:
        value = report.get(key)
        if value is None:
            problems.append(f"after {when}: DIAGnostic:HEAP? has no {key}")
        elif value != "0":
            problems.append(f"after {when}: {key}={value}, not 0 (an allocation since app_main)")
    if report.get("armed") != "1":
        problems.append(f"after {when}: armed={report.get('armed')}, not 1")
    return problems


class BenchLockBusy(RuntimeError):
    """Another live process holds the bench lock."""


def pid_alive(pid: int) -> bool:
    """Whether a process with this ID exists (signal 0 checks without sending)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_owner(path: Path) -> int | None:
    """The process ID the lock file names, or None when it names none or is absent."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    first = text.split()[0] if text else ""
    return int(first) if first.isdigit() else None


@dataclass
class BenchLock:
    """The bench's lock on the meter: a file holding one live process ID.

    :ivar path: The lock file.
    :ivar declared: A process ID the caller says already holds the lock for
        it (``KLEIDOS_METER_LOCK_PID``): a lock it names is used and left in
        place.
    :ivar timeout: Seconds to wait for another live owner to let go.
    :ivar poll: Seconds between looks.
    """

    path: Path
    declared: int | None = None
    timeout: float = 600.0
    poll: float = 5.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    taken: bool = field(default=False, init=False)

    def acquire(self) -> None:
        """Takes the lock: waits while another live process holds it.

        A file that names no process, or one that has exited, is stale and
        is taken over. A lock held by :attr:`declared` or by this process is
        used as it is.

        :raises BenchLockBusy: When another live owner keeps it past :attr:`timeout`.
        """
        deadline = self.clock() + self.timeout
        while True:
            owner = lock_owner(self.path)
            if owner is not None and owner in (os.getpid(), self.declared) and pid_alive(owner):
                return
            if owner is None or not pid_alive(owner):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(f"{os.getpid()}\n", encoding="utf-8")
                if lock_owner(self.path) == os.getpid():
                    self.taken = True
                    return
            if self.clock() >= deadline:
                raise BenchLockBusy(f"{self.path} is held by live process {owner}")
            self.sleep(self.poll)

    def release(self) -> None:
        """Removes the lock if this process took it and still holds it."""
        if self.taken and lock_owner(self.path) == os.getpid():
            self.path.unlink()
        self.taken = False


class BenchLine:
    """The CH343 line, opened without resetting the board, with a reset on demand.

    The carrier's auto-reset circuit pulls EN low while RTS is asserted and
    DTR released (``docs/carrier-board.md`` §7). The port is opened with the
    lines as they are and RTS released before DTR, as
    ``kleidos_meter.client.SerialTransport`` does; :meth:`reset` asserts RTS
    for a tenth of a second with DTR released, which boots the image (GPIO0
    stays high).

    :param port: The device node.
    :param baud: The line rate.
    """

    def __init__(self, port: str, baud: int = 6_000_000) -> None:
        import serial

        handle = serial.Serial()
        handle.port = port
        handle.baudrate = baud
        handle.timeout = 0.05
        handle.dsrdtr = False
        handle.rtscts = False
        handle.open()
        handle.rts = False
        handle.dtr = False
        self._serial = handle

    def write(self, data: bytes) -> int:
        written = self._serial.write(data)
        self._serial.flush()
        return written or 0

    def read(self, size: int) -> bytes:
        return self._serial.read(size)

    def close(self) -> None:
        self._serial.close()

    def reset(self) -> None:
        """Reboots the board through the auto-reset circuit, into the image."""
        self._serial.dtr = False
        self._serial.rts = True
        time.sleep(0.1)
        self._serial.rts = False


# --- The heap sweep -----------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """One program message of a step: its units, then a pause.

    :ivar units: The units, sent with ``SYSTem:ERRor:ALL?`` after them.
    :ivar wait: Seconds to let pass after the answer (a capture running).
    :ivar timeout: Seconds to wait for the answer.
    """

    units: tuple[str, ...]
    wait: float = 0.0
    timeout: float = 10.0


@dataclass(frozen=True)
class Step:
    """One command family's work, and the error numbers it may queue.

    :ivar name: Its name in the expectation file.
    :ivar messages: What it sends, in order.
    :ivar allowed: The error numbers its messages may queue; any other fails.
    """

    name: str
    messages: tuple[Message, ...]
    allowed: frozenset[int] = frozenset()


def step(name: str, *messages: Message | Sequence[str], allowed: Sequence[int] = ()) -> Step:
    """A :class:`Step` from messages, a bare sequence of units being a message."""
    built = tuple(m if isinstance(m, Message) else Message(tuple(m)) for m in messages)
    return Step(name, built, frozenset(allowed))


#: Every command family of ``docs/scpi.md``, run in this order after the boot,
#: each followed by a ``DIAGnostic:HEAP?``. Nothing here leaves the DUT without
#: its supply (another user of the bench may be working on it): the lead is
#: opened only by the null path, with the bypass closed first, and never by
#: ``ROUTe:LEAD``; nothing writes a shunt value; the one save rewrites the
#: calibration's own tag, and the null is the null path's, which
#: ``kleidos-meter calibration verify`` does not compare. ``DIAGnostic:WATChdog:TEST``
#: is left out: it reboots the instrument.
SWEEP: tuple[Step, ...] = (
    step(
        "common",
        ["*IDN?", "*ESE 0", "*ESE?", "*ESR?", "*SRE 0", "*SRE?", "*STB?", "*OPC", "*OPC?", "*WAI"],
        ["*CLS"],
        Message(("*TST?",), timeout=20.0),
    ),
    step(
        "system",
        [
            "SYST:ERR?",
            "SYST:ERR:COUN?",
            "SYST:VERS?",
            "SYST:INF?",
            "SYST:HEAL?",
            "SYST:STAT?",
            "SYST:LED?",
            "SYST:COMM:I2C:FREQ?",
        ],
    ),
    # The pin map (0.18.0): the live map, a staging of every kind and its
    # verdict, cleared again; then a map the INA228 is not on, tried and put
    # back (117, FW-05), the live map applied and saved, and the defaults.
    step(
        "pins",
        ["SYST:PINS?", "SYST:PINS:RES?"],
        [
            "SYST:PINS:STAG SDA,45",
            "SYST:PINS:STAG REL1,8,HIGH",
            "SYST:PINS:ADDR #H41",
            "SYST:PINS:CLOC 100000",
            "SYST:PINS:PEND?",
            "SYST:PINS:CLE",
            "SYST:PINS:PEND?",
        ],
        ["SYST:PINS:STAG SDA,18"],
        Message(("SYST:PINS:APPL",), timeout=20.0),
        ["SYST:PINS:RES?", "SYST:PINS:CLE"],
        Message(("SYST:PINS:APPL",), timeout=20.0),
        Message(("SYST:PINS:DEF",), timeout=20.0),
        allowed=[117],
    ),
    step(
        "status",
        [
            "STAT:OPER?",
            "STAT:OPER:COND?",
            "STAT:OPER:ENAB 0",
            "STAT:OPER:ENAB?",
            "STAT:QUES?",
            "STAT:QUES:COND?",
            "STAT:QUES:ENAB 0",
            "STAT:QUES:ENAB?",
            "STAT:QUES:PTR?",
            "STAT:QUES:NTR?",
            "STAT:OPER:PTR?",
            "STAT:OPER:NTR?",
            "STAT:PRES",
        ],
    ),
    step(
        "sense",
        [
            "SENS:PROF STAT",
            "SENS:PROF?",
            "SENS:PROF:REP?",
            "SENS:CURR:RANG?",
            "SENS:CURR:RANG:IND?",
            "SENS:CURR:RANG:REP?",
            "SENS:CURR:SHUN?",
            "SENS:CURR:SHUN:RANG0?",
            "SENS:CURR:SHUN:RANG1?",
            "SENS:CURR:APER?",
            "SENS:VOLT:APER?",
            "SENS:TEMP:APER?",
        ],
        ["SENS:AVER:COUN?", "SENS:AVER?"],
        # The averaging override on STATE, on with a count and a time of its own
        # and back to STATE's, off.
        ["SENS:AVER:COUN 4", "SENS:CURR:APER 0.00054", "SENS:AVER ON", "SENS:PROF:REP?"],
        ["SENS:AVER OFF", "SENS:AVER:COUN 1", "SENS:CURR:APER 0.001052"],
        ['SENS:FUNC:ON "CURR","VOLT"', "SENS:FUNC:ON?", "SENS:FUNC:ON:DEF"],
        ["SENS:CHAR?", "SENS:CHAR:RES"],
        [
            "SENS:CAPT DATA",
            "SENS:CAPT?",
            "SENS:CAPT:LIM OFF",
            "SENS:CAPT:LIM?",
            "SENS:CURR:THR OFF",
            "SENS:CURR:THR?",
            "SENS:CURR:THR:HYST 0",
            "SENS:CURR:THR:HYST?",
            "SENS:CURR:THR:MARK OFF",
            "SENS:CURR:THR:MARK?",
        ],
    ),
    step("range", ["SENS:CURR:RANG:IND 1"], ["SENS:CURR:RANG:IND 0"]),
    step(
        "sample_format_trigger",
        [
            "SAMP:TIM?",
            "SAMP:COUN 20",
            "SAMP:COUN?",
            "FORM TEXT",
            "FORM?",
            "TRIG:SOUR IMM",
            "TRIG:SOUR?",
            "TRIG:LEV 0",
            "TRIG:LEV?",
            "TRIG:SLOP POS",
            "TRIG:SLOP?",
            "TRIG:PRET 0",
            "TRIG:PRET?",
        ],
    ),
    step(
        "configure_measure_read_fetch",
        ["CONF:CURR", "CONF?"],
        ["MEAS:CURR?"],
        ["READ?", "READ:CURR?"],
        ["FETC?", "FETC:CURR?", "FETC:CHAR?"],
        ["MEAS:VOLT?", "MEAS:POW?", "MEAS:ENER?", "MEAS:TEMP?"],
        ["CONF:CURR", "SENS:PROF STAT"],
    ),
    step(
        "capture_state",
        Message(("SENS:PROF STAT", "INIT", "FETC:HEAD?"), wait=1.0),
        Message(('TRAC:MARK "sweep"',), wait=1.0),
        Message(("ABOR", "FETC:STAT?", "FETC:STAT:BATC?"), timeout=20.0),
    ),
    step(
        "capture_sleep",
        Message(("SENS:PROF SLE", "INIT"), wait=2.0),
        Message(("ABOR", "FETC:STAT?"), timeout=20.0),
    ),
    step(
        "capture_trace",
        Message(("SENS:PROF TRAC", "INIT"), wait=1.0),
        Message(("ABOR", "FETC:STAT?", "TRAC:POIN?", "TRAC:POIN:MAX?"), timeout=20.0),
        Message(("TRAC:DATA? RAW,0,1024",), timeout=30.0),
        Message(("TRAC:DATA? SCAL,0,64",), timeout=30.0),
    ),
    step("i2c_clock", ["SYST:COMM:I2C:FREQ 100000"], ["SYST:COMM:I2C:FREQ 400000"]),
    step(
        "capture_burst",
        ["SYST:COMM:I2C:FREQ 1000000"],
        Message(("SENS:PROF BURS", "INIT"), wait=1.0),
        Message(("ABOR", "FETC:STAT?"), timeout=20.0),
        ["SYST:COMM:I2C:FREQ 400000", "SENS:PROF STAT"],
    ),
    step(
        "calibration_reads",
        [
            "CAL:DATA?",
            "CAL:TAG?",
            "CAL:DATE?",
            "CAL:VBUS?",
            "CAL:SHUN:REF?",
            "CAL:SHUN:REF 100",
            "CAL:ZERO:RES?",
        ],
        ["CAL:SHUN:RES?"],
        allowed=[-230],
    ),
    step(
        "null",
        Message(("ROUT:NULL ON",), timeout=20.0),
        Message(("CAL:ZERO 2",), wait=2.5),
        Message(("ABOR", "CAL:ZERO:RES?"), timeout=20.0),
        Message(("ROUT:NULL OFF",), timeout=20.0),
    ),
    step(
        "diagnostic",
        [
            "DIAG:ALER?",
            "DIAG:I2C:SCAN?",
            "DIAG:REG? #H3E",
            "DIAG:REG? #H3F",
            "DIAG:HEAP?",
            "DIAG:WATC?",
        ],
    ),
    step(
        "route",
        ["ROUT:STAT?", "ROUT:RES?"],
        Message(("ROUT:BYP ON",), timeout=20.0),
        Message(("ROUT:BYP OFF",), timeout=20.0),
        ["ROUT:LEAD CLOS"],
        ["ROUT:REL3 ON"],
        allowed=[-241],
    ),
    step("status_polling", *(["SYST:STAT?"] for _ in range(500))),
    step("reset", Message(("*RST",), timeout=20.0), ["FORM?", "ROUT:STAT?"]),
)

#: The step that saves the calibration: two saves that each change the
#: stored record, the instrument's own tag (``CALibration:TAG?``, read when the
#: step runs) with its last character altered, then the tag as it was, so the
#: calibration ends as it began. Until FW-16 a save that wrote what NVS already
#: held allocated nothing, which is why the tag is changed and changed back;
#: from FW-16 every save is a new record of the flash log and none allocates,
#: so the step holds that a save that changes the record costs nothing.
SAVE_STEP = "calibration_save"


def altered(tag: str) -> str:
    """*tag* with its last character changed, its length kept (the tag holds 23 at most)."""
    if not tag:
        return "0"
    return tag[:-1] + ("1" if tag[-1] == "0" else "0")


def save_step(tag: str) -> Step:
    """The calibration save: the instrument's tag altered, then written back."""
    return step(SAVE_STEP, [f'CAL:TAG "{altered(tag)}"'], [f'CAL:TAG "{tag}"'], ["CAL:TAG?"])


def sweep_steps(tag: str) -> list[Step]:
    """The sweep in the order it runs, with the calibration save before the null.

    :param tag: The instrument's own tag (``CALibration:TAG?``), which the save
        alters and writes back.
    """
    steps = list(SWEEP)
    steps.insert([s.name for s in steps].index("null"), save_step(tag))
    return steps


def phase_counts(report: Mapping[str, str]) -> dict[str, int]:
    """The four counts by phase of a ``DIAGnostic:HEAP?`` report."""
    missing = [key for key in PHASE_KEYS if key not in report]
    assert not missing, f"DIAGnostic:HEAP? has no {', '.join(missing)}: an image before FW-07"
    return {key: int(report[key]) for key in PHASE_KEYS}


def run_step(
    meter: Any, sweep_step: Step, sleep: Callable[[float], None] = time.sleep
) -> list[str]:
    """Sends a step's messages; returns every error it queued that it may not."""
    unexpected = []
    for message in sweep_step.messages:
        _, errors, _ = meter.exchange(
            list(message.units), timeout=message.timeout, raise_errors=False
        )
        unexpected += [
            f"{sweep_step.name}: {message.units[0]}: {error}"
            for error in errors
            if error.code not in sweep_step.allowed
        ]
        if message.wait:
            sleep(message.wait)
    return unexpected


#: The TRACE ring's samples and the seconds it takes to fill at 280 us.
RING_SAMPLES = 1_048_576
RING_FILL_S = 300.0


def fill_ring(
    meter: Any,
    look: Callable[[], object],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Starts a ``TRACE`` capture and waits until the ring is full.

    :param meter: The instrument, with no window open.
    :param look: Called at every look at the ring, 15 s apart.
    :raises AssertionError: When the ring is not full a minute after it should be.
    """
    meter.exchange(["SENS:PROF TRAC", "INIT"], timeout=20.0)
    started = clock()
    while True:
        sleep(15.0)
        look()
        points = int(meter.query("TRAC:POIN?"))
        if points >= RING_SAMPLES:
            return
        assert clock() - started < RING_FILL_S + 60.0, f"the ring holds {points}"


#: The seconds a whole ring's dump may take: at the line's rate ``RAW``
#: (3.8 MB) takes 12 s and ``SCALed`` (about 37 MB) a minute and a half.
DUMP_TIMEOUT_S: Mapping[str, float] = {"RAW": 60.0, "SCAL": 300.0}

#: How many times a dump is asked for before it counts as failed. The line
#: between the UART and this process is not lossless at 6 Mbaud: on 2026-11-24
#: one 64-byte block came early by 320 bytes, more than the instrument's
#: 128-byte FIFO holds, in the place of one that was lost (``docs/testing.md``,
#: "A damaged dump is the line's"). The block's CRC-32 is what finds it, and
#: asking again is what a host does about it.
DUMP_ATTEMPTS = 3


def dump_whole(
    meter: Any,
    form: str,
    check: Callable[[bytes], bool | None],
    timeout_error: type[Exception],
) -> tuple[bytes, list[str]]:
    """Dumps the whole ring in *form* until a block matches its CRC-32.

    A block that does not match, or that never ends, is asked for again, at
    most :data:`DUMP_ATTEMPTS` times in all; after one that never ended the
    line is drained and the instrument must answer. The block must count the
    whole ring.

    :param meter: The instrument, its capture closed.
    :param form: ``RAW`` or ``SCAL``.
    :param check: ``kleidos_meter.protocol.check_dump``.
    :param timeout_error: ``kleidos_meter.client.MeterTimeout``.
    :returns: The block, and what was wrong with each attempt before it.
    :raises AssertionError: When no attempt gave a whole block.
    """
    damaged: list[str] = []
    while len(damaged) < DUMP_ATTEMPTS:
        try:
            block = meter.send(f"TRAC:DATA? {form}", timeout=DUMP_TIMEOUT_S[form]).block
            assert block is not None, form
            assert check(block) is True, f"{form}: the block carries no CRC-32"
        except (ValueError, timeout_error) as error:
            damaged.append(f"{form}: {error}")
            meter.wait_ready(timeout=20.0)
            continue
        opening = block[: block.index(b"\n")].decode("ascii")
        assert opening.startswith("METER_DUMP ") and f" count={RING_SAMPLES} " in f"{opening} ", (
            opening
        )
        return block, damaged
    raise AssertionError("no dump matched its CRC-32: " + "; ".join(damaged))


@dataclass(frozen=True)
class Expectations:
    """The counts the sweep is held to (``heap_expectations.toml``).

    Every count is exact (FW-17): a figure above it is a new allocation, one
    below it is to be lowered in the file. Until FW-16 a step or the boot
    could be marked a ceiling, its figures then the most it might add,
    because NVS allocated by the state of its pages; NVS is gone, and so is
    the exemption.

    :ivar boot: ``DIAGnostic:HEAP?``'s four phase counts once the boot settled.
    :ivar steps: What each step adds to ``allocs_since_main`` and
        ``frees_since_main``: ``{"allocs": n, "frees": n}``.
    """

    boot: Mapping[str, int]
    steps: Mapping[str, Mapping[str, int]]


#: The keys that marked a count a ceiling until FW-17: ``[boot]
#: since_main_ceiling`` and a step's ``ceiling``.
CEILING_KEYS = ("since_main_ceiling", "ceiling")


def load_expectations(text: str, *, history: bool = False) -> Expectations:
    """Parses and checks an expectation file's text.

    :param history: The text is an earlier commit's, read only to hold the
        file to falling: a ceiling it marked is read as its figure.
    :raises ValueError: When a boot key or a step's count is missing or is
        not a count, or, unless *history*, a count is marked a ceiling.
    """
    data = tomllib.loads(text)
    boot = dict(data.get("boot", {}))
    steps = {name: dict(counts) for name, counts in data.get("steps", {}).items()}
    marked = ["[boot]"] if "since_main_ceiling" in boot else []
    marked += [f"[steps.{name}]" for name, counts in steps.items() if "ceiling" in counts]
    if marked and not history:
        raise ValueError(f"{', '.join(marked)}: a ceiling exempts a count, and none is (FW-17)")
    boot.pop("since_main_ceiling", None)
    for counts in steps.values():
        counts.pop("ceiling", None)
    for key in PHASE_KEYS:
        if not isinstance(boot.get(key), int) or boot[key] < 0:
            raise ValueError(f"[boot] {key} is not a count")
    for name, counts in steps.items():
        for key in ("allocs", "frees"):
            if not isinstance(counts.get(key), int) or counts[key] < 0:
                raise ValueError(f"[steps.{name}] {key} is not a count")
    return Expectations(boot, steps)


def step_names() -> list[str]:
    """Every step of the sweep in its order, the calibration save included."""
    names = [s.name for s in SWEEP]
    names.insert(names.index("null"), SAVE_STEP)
    return names


def compare(
    expected: Expectations,
    boot: Mapping[str, int],
    deltas: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """Every difference between what was measured and the expectation file.

    :param boot: The four phase counts read after the boot.
    :param deltas: Per step, what it added to the two since-main counts.
    :returns: One line per difference, saying which way it went; empty when
        the instrument matched the file exactly.
    """
    problems = []
    for key in PHASE_KEYS:
        problems += _difference(f"boot {key}", expected.boot[key], boot[key])
    for name in step_names():
        if name not in expected.steps:
            problems.append(f"step {name}: no expectation in the file")
            continue
        if name not in deltas:
            problems.append(f"step {name}: not measured")
            continue
        for key in ("allocs", "frees"):
            problems += _difference(
                f"step {name} {key}", expected.steps[name][key], deltas[name][key]
            )
    for name in expected.steps:
        if name not in step_names():
            problems.append(f"step {name}: in the file but not in the sweep")
    return problems


def _difference(what: str, expected: int, measured: int) -> list[str]:
    if measured > expected:
        return [f"{what}: {measured}, above the {expected} expected (a new allocation)"]
    if measured < expected:
        return [f"{what}: {measured}, below the {expected} expected (lower the file)"]
    return []


def rises(old: Expectations, new: Expectations) -> list[str]:
    """Every count *new* sets above *old*'s, for the keys and steps both name.

    The expectation file only falls: a work package that removes an
    allocation lowers it, and nothing raises it.
    """
    found = [
        f"boot {k}: {old.boot[k]} -> {new.boot[k]}" for k in PHASE_KEYS if new.boot[k] > old.boot[k]
    ]
    for name, counts in new.steps.items():
        found.extend(
            f"step {name} {key}: {old.steps[name][key]} -> {counts[key]}"
            for key in ("allocs", "frees")
            if name in old.steps and counts[key] > old.steps[name][key]
        )
    return found


def committed_versions(path: Path) -> list[tuple[str, str]]:
    """The file's text at each commit that changed it, newest first.

    :returns: (commit, text) pairs; empty when git, the history or the file
        is not there (a shallow clone has only its one commit).
    """
    root = path.parent
    try:
        listing = subprocess.run(
            ["git", "log", "--format=%H", "--", path.name],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return []
    versions = []
    for commit in listing:
        shown = subprocess.run(
            ["git", "show", f"{commit}:./{path.name}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode == 0:
            versions.append((commit, shown.stdout))
    return versions
