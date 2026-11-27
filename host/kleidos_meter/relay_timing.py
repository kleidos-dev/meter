# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure the relays' operate and release times through the shunt (0.13.0).

``kleidos-meter relay-timing`` runs a ``TRACE`` ring capture, moves one relay
under it with ``LIVE`` (so each move is a mark at the sample index of its
drive), dumps the ring around each mark and finds where the current steps:
the delay from the mark to the step is the contact's operate or release time,
at the ring's 280 us resolution. Each move is repeated, and the record states
every delay, their spread and the step each was found from, so that a weak
step (a DUT drawing tens of microamps against TRACE's per-sample noise) shows
as what it is.

A move is a pair of orders that brings the rig back to ``path=shunt``:

* ``k1``: ``ROUTe:LEAD OPEN,FORCe,LIVE`` then ``ROUTe:LEAD CLOSe,LIVE`` (the DUT
  loses its battery for ``--hold`` ms; it must have another supply or tolerate
  it);
* ``k2``: ``ROUTe:BYPass ON,FORCe,LIVE`` then ``ROUTe:BYPass OFF,LIVE``.

The step is located by the least-squares split of the samples from the mark
to the next order: the index that best divides them into two levels. The
window ends at the next mark of the relay, and never later than ``--hold``
after its own: the next order, or the tool's ``ABORt``, comes no sooner. The
last move has no next mark, so the hold is all that bounds it; before, its
window ran 2,000 samples (560 ms) on, and on 2026-11-21 it reached a move the
instrument drove unmarked (below) and split there (``docs/relay-timing.md``).

The instrument keeps 16 marks a capture (``TRACe:MARK`` and ``LIVE`` moves,
``docs/scpi.md`` error 105), and a ``LIVE`` move past them is still driven,
unmarked. A move is two marks, so ``--repeats`` is refused above
:data:`MAX_REPEATS`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from .client import Meter, SerialTransport
from .invariant import present
from .protocol import RawRow, decode_dump, parse_dump_row
from .sim import SimulatedMeter

#: TRACE's sample period, us.
PERIOD_US = 280

#: The marks the instrument keeps a capture, ``TRACe:MARK`` and ``LIVE`` moves.
MAX_MARKS = 16

#: The moves one capture can mark: each is two orders, two marks.
MAX_REPEATS = MAX_MARKS // 2

#: The orders of one move, per relay: (open or energize, close or release).
MOVES = {
    "k1": ("ROUT:LEAD OPEN,FORC,LIVE", "ROUT:LEAD CLOS,LIVE"),
    "k2": ("ROUT:BYP ON,FORC,LIVE", "ROUT:BYP OFF,LIVE"),
}


#: What each exit status says.
EXIT_STATUS = {
    0: "every move was measured and the rig is back at rest",
    1: "an error ended the run, its traceback on stderr: the port is held elsewhere, the"
    " instrument did not answer or refused an order, or the dump arrived damaged",
    2: "a usage error",
}


@dataclass
class Edge:
    """One move of one contact, found in the ring.

    :ivar relay: ``k1`` or ``k2``.
    :ivar order: The mark's label: the relay and the state it was ordered to.
    :ivar mark_index: The sample index of the drive.
    :ivar step_index: The sample index of the step.
    :ivar delay_us: From one to the other, microseconds.
    :ivar before_counts: The mean raw count before the step.
    :ivar after_counts: The mean raw count after it.
    :ivar contrast: The step against the noise: the two means' difference over the
        samples' own standard deviation.
    """

    relay: str
    order: str
    mark_index: int
    step_index: int
    delay_us: int
    before_counts: float
    after_counts: float
    contrast: float


def split(values: list[float]) -> tuple[int, float, float, float]:
    """The least-squares split of *values* into two levels.

    :param values: The series.
    :returns: ``(index, mean before, mean after, contrast)``: the first sample
        of the second level, the two means and the step over the pooled
        standard deviation. A series too short to split returns index 0.
    """
    count = len(values)
    if count < 4:
        return 0, 0.0, 0.0, 0.0
    prefix = [0.0]
    squares = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
        squares.append(squares[-1] + value * value)
    best, best_cost = 1, float("inf")
    for k in range(1, count):
        left = squares[k] - prefix[k] ** 2 / k
        right = (squares[count] - squares[k]) - (prefix[count] - prefix[k]) ** 2 / (count - k)
        if left + right < best_cost:
            best, best_cost = k, left + right
    before = prefix[best] / best
    after = (prefix[count] - prefix[best]) / (count - best)
    pooled = (best_cost / max(count - 2, 1)) ** 0.5
    contrast = abs(after - before) / pooled if pooled > 0 else float("inf")
    return best, before, after, contrast


def counts_between(rows: list[str], first: int, last: int) -> list[float]:
    """The raw counts of indices ``first`` to ``last - 1`` in a ``RAW`` dump's rows.

    Each ``R`` row is read by :func:`~kleidos_meter.protocol.parse_dump_row`;
    a whole block is decoded by :func:`~kleidos_meter.protocol.decode_dump`,
    as :func:`measure` does.

    :param rows: The dump's lines.
    :param first: The first capture index wanted.
    :param last: One past the last capture index wanted.
    :returns: The counts, in index order.
    """
    out: dict[int, float] = {}
    for row in rows:
        parsed = parse_dump_row(row)
        if isinstance(parsed, RawRow):
            for offset, counts in enumerate(parsed.counts):
                if first <= parsed.index + offset < last:
                    out[parsed.index + offset] = float(counts)
    return [out[i] for i in sorted(out)]


def refusal(repeats: int, hold_ms: int) -> str | None:
    """Why *repeats* moves held *hold_ms* each cannot be measured.

    :param repeats: The moves.
    :param hold_ms: The time between a move's two orders, milliseconds.
    :returns: The reason, or None when they can.
    """
    if not 1 <= repeats <= MAX_REPEATS:
        return (
            f"--repeats is 1 to {MAX_REPEATS}: a capture keeps {MAX_MARKS} marks, two a move,"
            f" and a move past them is driven unmarked (got {repeats})"
        )
    if hold_ms <= 0:
        return f"--hold is a positive number of ms: it bounds each edge's window (got {hold_ms})"
    return None


def measure(
    meter: Meter,
    relay: str,
    repeats: int,
    hold_ms: int,
    *,
    pause: Callable[[float], None] = time.sleep,
) -> list[Edge]:
    """Move *relay* *repeats* times under one ``TRACE`` capture and find each edge.

    :param meter: The instrument.
    :param relay: ``k1`` or ``k2``.
    :param repeats: The moves.
    :param hold_ms: The time between a move's two orders, milliseconds.
    :param pause: Waits, seconds.
    :returns: The edges, two a move, in order.
    :raises ValueError: *repeats* is not 1 to :data:`MAX_REPEATS` (a move past
        the instrument's 16 marks would be driven unmarked), or *hold_ms* is
        not positive (it bounds every edge's window). Nothing is sent.
    """
    reason = refusal(repeats, hold_ms)
    if reason:
        raise ValueError(reason)
    first_order, second_order = MOVES[relay]
    # The samples of one hold: the next order, or ABORt, comes no sooner.
    hold_samples = hold_ms * 1000 // PERIOD_US
    meter.exchange(["SENS:PROF TRAC", "SENS:FUNC:ON:DEF", "SENS:CAPT DATA", "TRIG:SOUR IMM"])
    meter.exchange(["INIT"])
    pause(0.2)
    for _ in range(repeats):
        meter.exchange([first_order], timeout=10.0)
        pause(hold_ms / 1000)
        meter.exchange([second_order], timeout=10.0)
        pause(hold_ms / 1000)
    meter.exchange(["ABOR"], timeout=20.0)
    _, _, block = meter.exchange(["TRAC:DATA? RAW"], timeout=240.0)
    dump = decode_dump(present(block, "the dump's block"))
    marks = [(m.index, m.label) for m in dump.marks if m.label.startswith(relay + ":")]
    edges: list[Edge] = []
    for position, (index, label) in enumerate(marks):
        end = index + hold_samples
        if position + 1 < len(marks):
            end = min(end, marks[position + 1][0])
        # A few samples before the mark anchor the first level.
        start = max(index - 20, 0)
        values = [float(counts) for counts in dump.counts_between(start, end)]
        step, before, after, contrast = split(values)
        at = start + step
        edges.append(
            Edge(relay, label, index, at, (at - index) * PERIOD_US, before, after, contrast)
        )
    return edges


def summarize(edges: list[Edge]) -> dict[str, Any]:
    """Per order: every delay, the median, the spread, the weakest step.

    :param edges: The edges.
    :returns: ``n``, ``delays_us``, ``median_us``, ``min_us``, ``max_us`` and
        ``min_contrast`` of each order, by its label.
    """
    out: dict[str, Any] = {}
    for order in sorted({edge.order for edge in edges}):
        delays = [edge.delay_us for edge in edges if edge.order == order]
        out[order] = {
            "n": len(delays),
            "delays_us": delays,
            "median_us": statistics.median(delays),
            "min_us": min(delays),
            "max_us": max(delays),
            "min_contrast": round(min(e.contrast for e in edges if e.order == order), 2),
        }
    return out


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter relay-timing``.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kleidos-meter relay-timing", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="talk to the simulated instrument")
    parser.add_argument(
        "--relay",
        choices=sorted(MOVES),
        action="append",
        required=True,
        help="k1, the lead, or k2, the bypass",
    )
    parser.add_argument(
        "--repeats", type=int, default=5, help=f"moves per relay, 1 to {MAX_REPEATS}"
    )
    parser.add_argument("--hold", type=int, default=300, help="ms between the two orders")
    parser.add_argument("--json", action="store_true", help="print every edge as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``kleidos-meter relay-timing``.

    :param argv: The arguments after ``relay-timing``, ``sys.argv[1:]`` when None.
    :returns: The exit status (:data:`EXIT_STATUS`).
    :raises SystemExit: With status 2 on a usage error (argparse's).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    reason = refusal(args.repeats, args.hold)
    if reason:
        parser.error(reason)
    if not args.sim and not args.port:
        parser.error("name the port with --port or KLEIDOS_METER_PORT, or use --sim")
    transport = SimulatedMeter() if args.sim else SerialTransport(args.port)
    with Meter(transport) as meter:
        meter.wait_ready()
        edges = [
            edge for relay in args.relay for edge in measure(meter, relay, args.repeats, args.hold)
        ]
        meter.exchange(["ROUT:STAT?"])
    if args.json:
        print(
            json.dumps({"edges": [asdict(e) for e in edges], "summary": summarize(edges)}, indent=1)
        )
    else:
        for order, row in summarize(edges).items():
            print(
                f"{order}: n={row['n']} median={row['median_us']} us"
                f" range={row['min_us']}..{row['max_us']} us min_contrast={row['min_contrast']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
