# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shunt calibration, step by step, with the owner at the bench; it files a registry event.

The method is ``calibration/README.md`` "The shunt, two points": the DUT is
replaced by the 100 Ω ±0.01 % reference resistor, one current crosses both,
and the instrument reads the drop across the shunt on its shunt channel and,
with ``VBUS`` on the reference's top lead, the drop across the reference on
its bus channel (``CALibration:SHUNt``). The two points are the two ADC ranges
at that one current.

What 2026-11-20 taught is built in (``calibration/records/2026-11-20.md``):

- the reference's joints are screwed, never twisted or clipped, and the tool
  asks before it measures;
- the nulls are taken hands-free with K1 open (``ROUTe:LEAD OPEN,FORCe``),
  before and after the points, on the ``SLEEP`` profile;
- the points are taken in blocks in the order range 0, 1, 1, 0, which takes a
  steady drift out of the range split;
- a closure gate compares the first and the last block of each range, and a
  null-closure gate each range's closing null with its opening one: a joint
  that drifts fails the session instead of being stored.

The acceptance window comes from the instrument's configuration in the
registry, not from constants here. An accepted session, once confirmed, is
written to the instrument as its working copy and read back; then the event
is appended to ``calibration/instruments/<serial>.json``, its record and raw
SCPI log are filed under ``calibration/records/<serial>/``, and the index is
regenerated. A refused session writes nothing to the instrument and is filed
as ``refused``, because it is knowledge. Nothing is committed.

Usage::

    kleidos-meter calibrate --port "$KLEIDOS_METER_PORT"
    kleidos-meter calibrate --port ... --configuration mjkdz-r100-relay2click  # a first one
    kleidos-meter calibrate --sim --yes --registry /tmp/registry               # simulated
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import Meter, SerialTransport, Transport
from .invariant import present
from .registry import (
    ROOT,
    SERIAL_PATTERN,
    Registry,
    build_index,
    check,
    dump_index,
    file_digest,
    image_problem,
    parse_idn,
    restore_messages,
)
from .sim import SimulatedMeter

#: The gates (``calibration/README.md``, "Acceptance limits").
CLOSURE_PPM = 200  # first and last block of one range: the bench did not move
NULL_CLOSURE_PV = 500_000  # a range's closing null within 0.5 µV of its opening one
DIE_DRIFT_MC = 5_000  # each point within ±5 °C of its range's opening null
PROFILE = "SLEEP"
NULL_SECONDS = 60
SAMPLES = 600
POINTS_PER_BLOCK = 4
ORDER = (0, 1, 1, 0)

#: Known references, by the name ``--reference`` takes.
REFERENCES: dict[str, dict[str, Any]] = {
    "vpg-y0785-100r": {
        "part": "VPG Y0785100R000T9L",
        "nominal_nohm": 100_000_000_000,
        "tolerance_ppm": 100,
        "tcr_ppm_per_c": 2,
    },
}
#: The bound on the reference's temperature excursion the TCR term assumes:
#: the bench's range plus the 0.16 W the reference dissipates.
REFERENCE_TEMPERATURE_BOUND_C = 20


#: What each exit status says.
EXIT_STATUS = {
    0: "the session was accepted and filed",
    1: "it could not start, or the registry's check found problems with what it filed; or an"
    " error ended it, its traceback on stderr: the port is held elsewhere, the instrument did not"
    " answer or refused a step",
    2: "a gate failed: the session was filed as ``refused`` and no working copy was"
    " written; or a usage error",
}


def ppm_apart(first: float, second: float) -> float:
    """How far two resistances are apart, in parts per million of their mean.

    :param first: One.
    :param second: The other, in the same unit.
    :returns: The figure; 1,000,000 when either is not positive.
    """
    if first <= 0 or second <= 0:
        return 1e6
    return abs(first - second) * 1e6 / ((first + second) / 2)


def mean(values: list[int] | list[float]) -> float:
    """The mean of *values*.

    :param values: One or more.
    :returns: Their mean.
    """
    return sum(values) / len(values)


def sample_sd(values: list[int] | list[float]) -> float:
    """The sample standard deviation of *values*.

    :param values: The values.
    :returns: The figure; 0 for fewer than two.
    """
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _int(text: str | None) -> int | None:
    if text is None or not text.lstrip("-").isdigit():
        return None
    return int(text)


@dataclass
class Point:
    """One ``CALibration:SHUNt:RESult?``.

    :ivar range: The range it was taken on.
    :ivar nano_ohm: The shunt it read, nano-ohms.
    :ivar check: The instrument's own ``check=``.
    :ivar die_mc: The die's temperature, millidegrees, when it was reported.
    :ivar vsh_nv: The drop across the shunt, nanovolts.
    :ivar fields: The report it was read from.
    """

    range: int
    nano_ohm: int
    check: str
    die_mc: int | None
    vsh_nv: int
    fields: dict[str, str]

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> Point:
        """The point a report states.

        :param fields: ``CALibration:SHUNt:RESult?``'s fields.
        :returns: The point.
        """
        return cls(
            range=int(fields.get("range", "-1")),
            nano_ohm=int(fields.get("r_nohm", "0")),
            check=fields.get("check", "?"),
            die_mc=_int(fields.get("die_mc")),
            vsh_nv=_int(fields.get("vsh_nv")) or 0,
            fields=fields,
        )


@dataclass
class Block:
    """Consecutive points on one range.

    :ivar range: The range.
    :ivar points: Its points, in the order they were taken.
    """

    range: int
    points: list[Point] = field(default_factory=list)

    @property
    def mean_nohm(self) -> float:
        """The mean of its points, nano-ohms."""
        return mean([p.nano_ohm for p in self.points])


@dataclass
class Result:
    """The gates, each with what it measured and whether it held, and the figures.

    :ivar checks: Each gate's name, what it measured and whether it held.
    :ivar ranges: Each range's points, mean, scatter and nulls, by its name.
    :ivar split_ppm: How far the two ranges' means are apart.
    :ivar budget: The uncertainty's terms.
    :ivar u_c_ppm: The combined standard uncertainty.
    :ivar coverage_k: The coverage factor.
    :ivar expanded_ppm: The expanded uncertainty.
    :ivar applied_nohm: The shunt the session gives, nano-ohms.
    """

    checks: list[tuple[str, str, bool]] = field(default_factory=list)
    ranges: dict[str, dict[str, Any]] = field(default_factory=dict)
    split_ppm: int = 0
    budget: list[dict[str, Any]] = field(default_factory=list)
    u_c_ppm: int = 0
    coverage_k: float = 2.0
    expanded_ppm: int = 0
    applied_nohm: int = 0

    @property
    def accepted(self) -> bool:
        """Whether there were gates and every one held."""
        return bool(self.checks) and all(ok for _, _, ok in self.checks)

    def add(self, name: str, measured: str, ok: bool) -> None:
        """File one gate.

        :param name: What it asks.
        :param measured: What was measured.
        :param ok: Whether it held.
        """
        self.checks.append((name, measured, ok))

    def failed(self) -> list[str]:
        """The gates that did not hold.

        :returns: Each one's name and what it measured.
        """
        return [f"{name}: {measured}" for name, measured, ok in self.checks if not ok]


def _term(name: str, limit: float, distribution: str, source: str) -> dict[str, Any]:
    divisor = math.sqrt(3) if distribution == "rectangular" else 1.0
    limit_ppm = round(limit)
    return {
        "term": name,
        "limit_ppm": limit_ppm,
        "distribution": distribution,
        "u_ppm": round(limit_ppm / divisor),
        "source": source,
    }


def judge(
    blocks: list[Block],
    nulls_before: list[dict[str, str]],
    nulls_after: list[dict[str, str]],
    configuration: dict[str, Any],
    reference: dict[str, Any],
    *,
    coverage_k: float = 2.0,
    temperature_bound_c: float = REFERENCE_TEMPERATURE_BOUND_C,
) -> Result:
    """Apply every gate to a session, and compute its figures and its budget.

    :param blocks: The points, in the order they were taken.
    :param nulls_before: Each range's opening null, its result's fields.
    :param nulls_after: Each range's closing null.
    :param configuration: The instrument's configuration, whose limits apply.
    :param reference: The reference resistor, as :data:`REFERENCES` describes one.
    :param coverage_k: The coverage factor.
    :param temperature_bound_c: How far the reference's temperature may be from its
        nominal one, degrees Celsius.
    :returns: The gates and the figures.
    """
    result = Result(coverage_k=coverage_k)
    shunt = configuration["sense"]["shunt"]
    nominal = shunt["nominal_nohm"]
    window = shunt["tolerance_ppm"] or shunt["plausible_ppm"]
    points = [p for block in blocks for p in block.points]
    opening_die = [_int(n.get("die_mc")) for n in nulls_before]
    for point in points:
        result.add(f"range {point.range} point: instrument check", point.check, point.check == "ok")
        deviation = (point.nano_ohm - nominal) * 1e6 / nominal
        result.add(
            f"range {point.range} point: within ±{window} ppm of nominal",
            f"{deviation:+.0f} ppm",
            abs(deviation) <= window,
        )
        reference_die = opening_die[point.range] if point.range in (0, 1) else None
        if point.die_mc is None or reference_die is None:
            result.add(f"range {point.range} point: die temperature known", "unknown", False)
        else:
            drift = point.die_mc - reference_die
            result.add(
                f"range {point.range} point: die within ±{DIE_DRIFT_MC // 1000} °C of its null",
                f"{drift / 1000:+.3f} °C",
                abs(drift) <= DIE_DRIFT_MC,
            )
    signal_pv: list[float] = []
    null_drift_ppm: list[float] = []
    block_scatter_ppm: list[float] = []
    means: dict[int, float] = {}
    for r in (0, 1):
        mine = [b for b in blocks if b.range == r]
        values = [p.nano_ohm for b in mine for p in b.points]
        if len(mine) < 2 or not values:
            result.add(f"range {r}: two blocks", f"{len(mine)} taken", False)
            continue
        closure = ppm_apart(mine[0].mean_nohm, mine[-1].mean_nohm)
        result.add(
            f"range {r}: first and last block within {CLOSURE_PPM} ppm",
            f"{closure:.0f} ppm",
            closure <= CLOSURE_PPM,
        )
        before = _int(nulls_before[r].get("offset_pv"))
        after = _int(nulls_after[r].get("offset_pv"))
        if before is None or after is None:
            result.add(f"range {r}: null closure", "a null is missing", False)
        else:
            moved = abs(after - before)
            result.add(
                f"range {r}: closing null within {NULL_CLOSURE_PV / 1e6:.1f} µV of the opening one",
                f"{moved / 1e6:.3f} µV",
                moved <= NULL_CLOSURE_PV,
            )
            signal = mean([p.vsh_nv for b in mine for p in b.points]) * 1000
            signal_pv.append(signal)
            if signal > 0:
                null_drift_ppm.append(moved * 1e6 / signal)
        means[r] = mean(values)
        if means[r] <= 0:
            continue  # no point measured a resistance: the instrument check says why
        block_means = [b.mean_nohm for b in mine]
        block_scatter_ppm.append(sample_sd(block_means) / mean(block_means) * 1e6)
        result.ranges[str(r)] = {
            "points_nohm": values,
            "mean_nohm": round(means[r]),
            "sd_ppm": round(sample_sd(values) / means[r] * 1e6),
            "null_pv": {"before": before, "after": after},
        }
    if len(result.ranges) == 2:
        split = ppm_apart(means[0], means[1])
        result.split_ppm = round(split)
        limit = configuration["expected"]["range_split_max_ppm"]
        result.add(f"ranges within {limit} ppm of each other", f"{split:.0f} ppm", split <= limit)
        result.applied_nohm = round((round(means[0]) + round(means[1])) / 2 / 1000) * 1000
        result.budget = [
            _term(
                "reference_tolerance",
                reference["tolerance_ppm"],
                "rectangular",
                f"{reference['part']}, ±{reference['tolerance_ppm']} ppm",
            ),
            _term(
                "reference_tcr",
                reference["tcr_ppm_per_c"] * temperature_bound_c,
                "rectangular",
                f"±{reference['tcr_ppm_per_c']} ppm/°C over ±{temperature_bound_c} °C",
            ),
            _term(
                "ina228_bus_gain",
                configuration["expected"]["adc_gain_error_ppm"],
                "rectangular",
                "the configuration's ADC gain error",
            ),
            _term(
                "range_split",
                result.split_ppm / 2,
                "normal",
                "half the split between the range means",
            ),
            _term(
                "null_drift",
                max(null_drift_ppm, default=0),
                "rectangular",
                "the larger null closure, over the signal across the shunt",
            ),
            _term(
                "repeatability",
                max(block_scatter_ppm, default=0),
                "measured",
                "the scatter between blocks of one range",
            ),
        ]
        result.u_c_ppm = round(math.sqrt(sum(t["u_ppm"] ** 2 for t in result.budget)))
        result.expanded_ppm = round(coverage_k * result.u_c_ppm)
        for rng in result.ranges.values():
            rng["applied_nohm"] = result.applied_nohm
            rng["u_ppm"] = result.u_c_ppm
    return result


class TeeTransport:
    """A transport that keeps the SCPI exchange: what was sent and what came back.

    :param inner: The transport it stands before.
    :ivar lines: The exchange, a message or a response a line.
    """

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.lines: list[str] = []
        self._incoming = ""

    def write(self, data: bytes) -> int:
        """Send *data*, and keep it.

        :param data: The bytes to send.
        :returns: How many the inner transport took.
        """
        self._flush_incoming()
        for line in data.decode("ascii", "replace").splitlines():
            self.lines.append(f">> {line}")
        return self.inner.write(data)

    def read(self, size: int) -> bytes:
        """Read from the inner transport, and keep what came.

        :param size: The most bytes to return.
        :returns: What arrived, possibly nothing.
        """
        chunk = self.inner.read(size)
        self._incoming += chunk.decode("ascii", "replace").replace("\x00", "")
        return chunk

    def close(self) -> None:
        """Close the inner transport."""
        self.inner.close()

    def _flush_incoming(self) -> None:
        if self._incoming:
            self.lines += self._incoming.rstrip("\n").split("\n")
            self._incoming = ""

    def text(self) -> str:
        """The exchange so far.

        :returns: Its lines, what was sent behind ``>>``.
        """
        self._flush_incoming()
        return "\n".join(self.lines) + "\n"


class Refused(Exception):
    """The session cannot start: the instrument, its configuration or the bench is not ready."""


class Session:
    """The guided flow of one calibration.

    :param meter: The instrument.
    :param root: The registry's directory, which the session is filed in.
    :param configuration: The configuration of an instrument's first calibration.
    :param reference: The reference resistor, as :data:`REFERENCES` describes one.
    :param ask: Shows a prompt and returns the answer.
    :param say: Prints a line.
    :param sleep: Waits out a null, seconds.
    :param null_seconds: How long each null is.
    :param samples: The samples of one point.
    :param points_per_block: The points of one block.
    :param now: The time, with its zone.
    :param transcript: The transport that keeps the exchange, filed as the raw log.
    :ivar result: The gates and figures, once :meth:`run` judged them.
    :ivar stored: Whether the working copy was written.
    """

    def __init__(
        self,
        meter: Meter,
        *,
        root: Path = ROOT,
        configuration: str | None = None,
        reference: dict[str, Any] | None = None,
        ask: Callable[[str], str] = input,
        say: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        null_seconds: int = NULL_SECONDS,
        samples: int = SAMPLES,
        points_per_block: int = POINTS_PER_BLOCK,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
        transcript: TeeTransport | None = None,
    ) -> None:
        self.meter = meter
        self.root = root
        self.configuration_id = configuration
        self.reference = reference or REFERENCES["vpg-y0785-100r"]
        self.ask = ask
        self.say = say
        self.sleep = sleep
        self.null_seconds = null_seconds
        self.samples = samples
        self.points_per_block = points_per_block
        self.now = now
        self.transcript = transcript
        self.registry: Registry | None = None
        self.instrument: dict[str, Any] | None = None
        self.configuration: dict[str, Any] = {}
        self.serial = ""
        self.firmware = ""
        self.info: dict[str, str] = {}
        self.before: dict[str, str] = {}
        self.after: dict[str, str] = {}
        self.nulls_before: list[dict[str, str]] = []
        self.nulls_after: list[dict[str, str]] = []
        self.blocks: list[Block] = []
        self.result = Result()
        self.started: dt.datetime | None = None
        self.stored = False
        self.joints = ""

    # -- the steps -------------------------------------------------------------

    def step(self, text: str) -> None:
        """Say what the person at the bench does next, and wait until it is done.

        :param text: The step.
        """
        self.say("")
        self.say(text)
        self.ask("Press Enter when done (Ctrl-C to stop) ")

    def null(self, index: int) -> dict[str, str]:
        """One null on range *index*, hands-free: K1 opens, so nothing crosses the shunt.

        :param index: 0 or 1.
        :returns: The null's result.
        :raises RuntimeError: When the instrument refused it.
        """
        m = self.meter
        m.set_range_index(index)
        m.exchange(["ROUT:LEAD OPEN,FORCE"])
        m.zero(self.null_seconds)
        self.say(f"  null on range {index}, K1 open: {self.null_seconds} s ...")
        self.sleep(self.null_seconds + 2)
        result, errors = m.close_zero(timeout=30.0)
        m.exchange(["ROUT:LEAD CLOS"])
        if errors:
            raise RuntimeError("the null was refused: " + "; ".join(map(str, errors)))
        return result

    def prepare(self) -> None:
        """Know the instrument and its configuration, or refuse before anything is measured.

        :raises Refused: When the instrument, its configuration or its image does not fit.
        """
        m = self.meter
        identity = parse_idn(m.idn())
        self.serial, self.firmware = identity["serial"], identity["firmware"]
        self.info = m.info()
        self.before = m.calibration()
        if not SERIAL_PATTERN.match(self.serial):
            raise Refused(
                f"the instrument reports no serial ({self.serial!r}): firmware 0.4.0 or later"
            )
        self.registry = Registry.from_directory(self.root)
        self.instrument = self.registry.instruments.get(self.serial)
        current = None
        if self.instrument is not None:
            for event in self.instrument["events"]:
                if event["kind"] == "hardware_change":
                    current = event["configuration"]
        if current is None and self.configuration_id is None:
            raise Refused(
                f"{self.serial} has no configuration in the registry: name it with --configuration"
            )
        if current is not None and self.configuration_id not in (None, current):
            raise Refused(
                f"{self.serial} is filed as {current}: file a hardware_change event before "
                f"calibrating it as {self.configuration_id}"
            )
        self.configuration_id = current or self.configuration_id
        if self.configuration_id not in self.registry.configurations:
            raise Refused(f"no configuration {self.configuration_id!r} in the registry")
        self.configuration = self.registry.configurations[self.configuration_id]
        problem = image_problem(
            self.configuration["firmware"], self.info.get("image", "-"), self.firmware
        )
        expected = {
            "bypass_part": self.configuration["routing"]["bypass"]["part"],
            "mfg": self.configuration["sense"]["adc"]["mfg"],
            "die": self.configuration["sense"]["adc"]["die"],
        }
        wrong = [
            f"{key}={self.info.get(key, '-')} (the configuration says {value})"
            for key, value in expected.items()
            if self.info.get(key, "").lower() != value.lower()
        ]
        if problem is not None and problem[0] == "image":
            wrong.insert(
                0, f"image={self.info.get('image', '-')} (the configuration says {problem[1]})"
            )
        if problem is not None and problem[0] == "version":
            wrong.append(f"firmware {self.firmware} is older than {problem[1]}")
        if wrong:
            raise Refused(f"{self.serial} is not {self.configuration_id}: " + "; ".join(wrong))

    def run(self) -> Result:
        """Run the session: the nulls, the points, the gates and, once confirmed, the copy.

        Nothing is filed: :meth:`file` does.

        :returns: The gates and the figures.
        :raises Refused: When it could not start.
        :raises RuntimeError: When a null was refused, or the instrument did not keep the
            working copy.
        """
        self.prepare()
        m = self.meter
        self.say(
            f"Instrument {self.serial}, firmware {self.firmware}, "
            f"configuration {self.configuration_id}"
        )
        self.say(f"Working copy as found: {self.before}")
        self.step(
            "Step 1 of 4. Everything off: unplug the DUT's battery lead (pigtail 3) and cut "
            "the DUT's hub port. Fit the reference between pigtail 3 and the INA228's GND "
            "screw, its ground lead in that same screw, and its top lead on pigtail 3. Move "
            "the cell's + to VIN+ (it normally feeds the VBUS screw through the VBUS-VIN+ "
            "jumper) and the VBUS wire to the reference's top lead itself. Every joint is "
            "screwed into a terminal: never twisted, never clipped."
        )
        self.joints = (
            self.ask("Are both reference joints screwed into terminals? [y/N] ").strip().lower()
        )
        if self.joints not in ("y", "yes"):
            raise Refused(
                "the reference's joints must be screwed (2026-11-20: twisted joints drifted 0.17 %)"
            )
        self.started = self.now()
        m.exchange([f"SENS:PROF {PROFILE}", f"SAMP:COUN {self.samples}", "ROUT:NULL OFF"])
        m.set_reference(self.reference["nominal_nohm"] / 1e9)
        self.say("Step 2 of 4. The opening nulls, K1 open:")
        self.nulls_before = [self.null(0), self.null(1)]
        self.say(
            f"Step 3 of 4. The points: blocks in the order {', '.join(map(str, ORDER))}, "
            f"{self.points_per_block} points of {self.samples} samples each."
        )
        for r in ORDER:
            block = Block(r)
            m.set_range_index(r)
            for _ in range(self.points_per_block):
                point = Point.from_fields(m.shunt_point(timeout=120.0))
                block.points.append(point)
                self.say(f"  range {r}: {point.nano_ohm} nΩ ({point.check})")
            self.blocks.append(block)
        self.say("  the closing nulls, K1 open:")
        self.nulls_after = [self.null(0), self.null(1)]
        self.result = judge(
            self.blocks, self.nulls_before, self.nulls_after, self.configuration, self.reference
        )
        self.say("Acceptance:")
        for name, measured, ok in self.result.checks:
            self.say(f"  [{'ok' if ok else 'FAIL'}] {name}: {measured}")
        if self.result.accepted:
            self.say(
                f"  R = {self.result.applied_nohm / 1e6:.6f} mΩ ± "
                f"{self.result.applied_nohm * self.result.expanded_ppm / 1e12:.6f} mΩ "
                f"(k = {self.result.coverage_k:g}; u_c {self.result.u_c_ppm} ppm)"
            )
            answer = self.ask(
                f"Store {self.result.applied_nohm / 1e9:.6f} Ω as the working copy? [y/N] "
            )
            if answer.strip().lower() in ("y", "yes"):
                m.exchange(restore_messages({"device": self.device_block()}))
                self.stored = True
        else:
            self.say("  Refused: nothing is written to the instrument.")
        self.step(
            "Step 4 of 4. Put the bench back: the VBUS wire to its screw, the cell's + to the "
            "VBUS screw, the reference out, pigtail 3 back into the DUT."
        )
        self.after = m.calibration()
        if self.stored:
            device = self.device_block()
            readback = {
                "shunt_uohm_r0": self.after.get("shunt_uohm_r0"),
                "shunt_uohm_r1": self.after.get("shunt_uohm_r1"),
                "cal_tag": self.after.get("cal_tag"),
                "cal_date": self.after.get("cal_date"),
                "vbus_side": self.after.get("vbus_side"),
            }
            if any(str(device[k]) != str(v) for k, v in readback.items()):
                raise RuntimeError(f"the instrument did not keep the working copy: {readback}")
        return self.result

    # -- what is filed ---------------------------------------------------------

    def device_block(self) -> dict[str, Any]:
        """The working copy an accepted session writes.

        :returns: The shunt of each range, the tag, the date and the VBUS side.
        """
        started = present(self.started, "the session's start")
        microohm = self.result.applied_nohm // 1000
        return {
            "shunt_uohm_r0": microohm,
            "shunt_uohm_r1": microohm,
            "cal_tag": f"R100_{started:%Y%m%d}",
            "cal_date": started.date().isoformat(),
            "vbus_side": self.configuration["sense"]["wiring"]["vbus_side"],
        }

    def event_id(self) -> str:
        """The id the session is filed under: its day and the next number of it.

        :returns: ``YYYY-MM-DD-NN``.
        """
        day = present(self.started, "the session's start").date().isoformat()
        taken = [
            int(e["id"][-2:])
            for e in (self.instrument or {}).get("events", [])
            if e["id"].startswith(day)
        ]
        return f"{day}-{max(taken, default=0) + 1:02d}"

    def record(self, event_id: str) -> str:
        """The Markdown record of this session.

        :param event_id: The id it is filed under.
        :returns: Its text.
        """
        r = self.result
        verdict = "accepted" if r.accepted else "refused"
        lines = [
            f"# {event_id} — two-point shunt calibration of {self.serial}, {verdict}",
            "",
            "Taken with `kleidos-meter calibrate` (`host/kleidos_meter/calibrate.py`), the method "
            "of `calibration/README.md`, 'The shunt, two points'. The raw SCPI exchange is "
            f"[{event_id}.log]({event_id}.log).",
            "",
            f"- Instrument: `{self.serial}`, firmware {self.firmware}, configuration "
            f"`{self.configuration_id}`",
            f"- Reference: {self.reference['part']}, {self.reference['nominal_nohm'] / 1e9:g} Ω "
            f"±{self.reference['tolerance_ppm']} ppm, {self.reference['tcr_ppm_per_c']} ppm/°C",
            f"- Joints screwed: {'yes' if self.joints in ('y', 'yes') else 'no'}",
            f"- Profile {PROFILE}, {self.samples} samples a point, nulls of {self.null_seconds} s "
            f"with K1 open, blocks in the order {', '.join(map(str, ORDER))}",
            f"- Verdict: **{verdict}**"
            + (" and written to the instrument" if self.stored else ", nothing written"),
            "",
            "## As found (`CALibration:DATA?`)",
            "",
            "```text",
            " ".join(f"{k}={v}" for k, v in self.before.items()),
            "```",
            "",
            "## The nulls (`CALibration:ZERO:RESult?`)",
            "",
            "| Range | Opening `offset_pv` | Closing `offset_pv` |",
            "|---|---|---|",
        ]
        lines.extend(
            f"| {index} | {self.nulls_before[index].get('offset_pv', '-')} | "
            f"{self.nulls_after[index].get('offset_pv', '-')} |"
            for index in (0, 1)
        )
        lines += [
            "",
            "## The points (`CALibration:SHUNt:RESult?`)",
            "",
            "| Block | Range | r_nohm | vsh_nv | vref_uv | die_mc | check |",
            "|---|---|---|---|---|---|---|",
        ]
        for number, block in enumerate(self.blocks, start=1):
            for point in block.points:
                f = point.fields
                lines.append(
                    f"| {number} | {point.range} | {point.nano_ohm} | {f.get('vsh_nv', '-')} | "
                    f"{f.get('vref_uv', '-')} | {f.get('die_mc', '-')} | {point.check} |"
                )
        lines += ["", "## Acceptance", "", "| Gate | Measured | Held |", "|---|---|---|"]
        for name, measured, ok in r.checks:
            lines.append(f"| {name} | {measured} | {'yes' if ok else '**no**'} |")
        if r.budget:
            lines += [
                "",
                "## Uncertainty budget",
                "",
                "| Term | Limit (ppm) | Distribution | u (ppm) | Source |",
                "|---|---|---|---|---|",
            ]
            for term in r.budget:
                lines.append(
                    f"| {term['term']} | {term['limit_ppm']} | {term['distribution']} | "
                    f"{term['u_ppm']} | {term['source']} |"
                )
            lines += [
                "",
                f"u_c = {r.u_c_ppm} ppm, U (k = {r.coverage_k:g}) = {r.expanded_ppm} ppm. Range "
                f"means {r.ranges['0']['mean_nohm']} and {r.ranges['1']['mean_nohm']} nΩ, "
                f"{r.split_ppm} ppm apart; applied to both: **{r.applied_nohm} nΩ**.",
            ]
        lines += [
            "",
            "## After (`CALibration:DATA?`)",
            "",
            "```text",
            " ".join(f"{k}={v}" for k, v in self.after.items()),
            "```",
            "",
        ]
        return "\n".join(lines)

    def event(
        self, event_id: str, record: dict[str, Any], raw_log: dict[str, Any]
    ) -> dict[str, Any]:
        """The registry event of this session.

        :param event_id: The id it is filed under.
        :param record: The record's path and digest.
        :param raw_log: The raw log's path and digest.
        :returns: The event.
        """
        r = self.result
        taken_at = present(self.started, "the session's start").replace(microsecond=0).isoformat()
        if not r.accepted:
            event: dict[str, Any] = {
                "id": event_id,
                "kind": "calibration",
                "verdict": "refused",
                "configuration": self.configuration_id,
                "taken_at": taken_at,
                "firmware": self.firmware,
                "why": "; ".join(r.failed()),
                "ranges": {
                    name: {"points_nohm": rng["points_nohm"], "profile": PROFILE}
                    for name, rng in r.ranges.items()
                },
            }
        else:
            dies = [p.die_mc for b in self.blocks for p in b.points if p.die_mc is not None]
            event = {
                "id": event_id,
                "kind": "calibration",
                "verdict": "accepted",
                "configuration": self.configuration_id,
                "taken_at": taken_at,
                "method": "two_point_reference/guided",
                "firmware": self.firmware,
                "reference": {**self.reference, "certificate": None},
                "conditions": {
                    "profile": PROFILE,
                    "vshct_us": 4120,
                    "avg": 16,
                    "samples_per_point": self.samples,
                    "null_s": self.null_seconds,
                    "null_path": "lead open (K1, ROUTe:LEAD OPEN,FORCe)",
                    "die_mc": [min(dies, default=0), max(dies, default=0)],
                    "joints": "screwed",
                    "order": ", ".join(
                        f"range {b} ({self.points_per_block} points)" for b in ORDER
                    ),
                    "cell_on": "VIN+, VBUS on the reference's top lead",
                },
                "ranges": r.ranges,
                "range_split_ppm": r.split_ppm,
                "budget": r.budget,
                "u_c_ppm": r.u_c_ppm,
                "coverage_k": r.coverage_k,
                "expanded_ppm": r.expanded_ppm,
                "device": self.device_block(),
            }
        event["record"] = record
        event["raw_log"] = raw_log
        return event

    def file(self) -> tuple[Path, list[str]]:
        """Append the event, file its record and log, and regenerate the index.

        :returns: The record's path, and the registry check's problems (none expected).
        """
        present(self.registry, "the registry it files into")
        event_id = self.event_id()
        folder = self.root / "records" / self.serial
        folder.mkdir(parents=True, exist_ok=True)
        record_text = self.record(event_id)
        log_text = self.transcript.text() if self.transcript is not None else ""
        (folder / f"{event_id}.md").write_text(record_text, encoding="utf-8")
        (folder / f"{event_id}.log").write_text(log_text, encoding="utf-8")
        relative = f"records/{self.serial}/{event_id}"
        event = self.event(
            event_id,
            {"path": f"{relative}.md", "sha256": file_digest(record_text)},
            {"path": f"{relative}.log", "sha256": file_digest(log_text)},
        )
        instrument = self.instrument or {
            "schema": "kleidos.meter.instrument/1",
            "serial": self.serial,
            "label": "",
            "events": [
                {
                    "id": f"{event_id[:10]}-00",
                    "kind": "hardware_change",
                    "configuration": self.configuration_id,
                    "voids_calibration": True,
                    "what": f"first filed, as {self.configuration_id}",
                }
            ],
        }
        instrument["events"].append(event)
        path = self.root / "instruments" / f"{self.serial}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(instrument, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        registry = Registry.from_directory(self.root)
        problems, _ = check(registry)
        if not problems:
            (self.root / "index.json").write_text(
                dump_index(build_index(registry)), encoding="utf-8"
            )
        return folder / f"{event_id}.md", problems


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter calibrate``.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kleidos-meter calibrate", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="calibrate the simulated instrument")
    parser.add_argument("--registry", type=Path, default=ROOT, help="the calibration/ directory")
    parser.add_argument(
        "--configuration", help="the configuration of an instrument's first calibration"
    )
    parser.add_argument(
        "--reference",
        default="vpg-y0785-100r",
        choices=sorted(REFERENCES),
        help="the reference resistor fitted",
    )
    parser.add_argument("--reference-ohm", type=float, help="another reference: its value")
    parser.add_argument(
        "--reference-tolerance-ppm", type=int, help="another reference: its tolerance"
    )
    parser.add_argument("--reference-tcr", type=float, help="another reference: its TCR, ppm/°C")
    parser.add_argument("--yes", action="store_true", help="answer every prompt (simulator, tests)")
    parser.add_argument(
        "--null-seconds", type=int, default=NULL_SECONDS, help="how long each null is"
    )
    parser.add_argument("--samples", type=int, default=SAMPLES, help="the samples of one point")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the guided calibration.

    :param argv: The arguments after ``calibrate``, ``sys.argv[1:]`` when None.
    :returns: The exit status (:data:`EXIT_STATUS`).
    :raises SystemExit: With status 2 on a usage error (argparse's).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.sim and not args.port:
        parser.error("name the port with --port or KLEIDOS_METER_PORT, or use --sim")
    reference = dict(REFERENCES[args.reference])
    if args.reference_ohm is not None:
        reference = {
            "part": f"{args.reference_ohm:g} Ω reference",
            "nominal_nohm": round(args.reference_ohm * 1e9),
            "tolerance_ppm": args.reference_tolerance_ppm or 0,
            "tcr_ppm_per_c": args.reference_tcr or 0,
        }
    transcript = TeeTransport(SimulatedMeter() if args.sim else SerialTransport(args.port))
    ask = (lambda prompt: "y") if args.yes else input
    sleep = (lambda seconds: None) if args.sim else time.sleep
    with Meter(transcript) as meter:
        session = Session(
            meter,
            root=args.registry,
            configuration=args.configuration,
            reference=reference,
            ask=ask,
            sleep=sleep,
            null_seconds=args.null_seconds,
            samples=args.samples,
            transcript=transcript,
        )
        try:
            result = session.run()
        except Refused as exc:
            print(f"Not started: {exc}")
            return 1
        path, problems = session.file()
    print(f"Record: {path}")
    if problems:
        print("The registry check found problems; the index was not regenerated:")
        print("\n".join(f"  - {p}" for p in problems))
        return 1
    print("Left to do: commit and push the registry here, then in the Kleidos firmware repository")
    print("  python -m qa_lib.power meter-lock")
    return 0 if result.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
