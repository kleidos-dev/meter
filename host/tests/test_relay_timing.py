# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The relay-timing tool: the step finder and the measurement on a scripted ring."""

from __future__ import annotations

import random
from typing import cast

import pytest
from kleidos_meter.client import Meter
from kleidos_meter.relay_timing import MAX_REPEATS, counts_between, main, measure, split, summarize


def test_split_finds_a_step_in_noise_as_large_as_it() -> None:
    rng = random.Random(7)  # noqa: S311  seeded noise for a test, not a secret
    values = [12 + rng.gauss(0, 12) for _ in range(300)] + [rng.gauss(0, 12) for _ in range(300)]
    index, before, after, contrast = split(values)
    assert abs(index - 300) < 25
    assert before > after
    assert contrast > 0.5
    assert split([1.0, 2.0])[0] == 0
    assert split([5.0] * 10)[3] == 0.0 or split([5.0] * 10)[3] == float("inf")


def test_counts_between_reads_raw_rows() -> None:
    rows = ["METER_DUMP count=32", "R n=0 c=1,2,3,4", "R n=4 c=5,6,7,8", "METER_DUMP_END"]
    assert counts_between(rows, 2, 6) == [3.0, 4.0, 5.0, 6.0]


class ScriptedMeter:
    """Answers the tool's messages with a ring that steps 40 samples after each mark.

    *opened_again*, when given, is the index from which the lead reads open
    again with no mark: a move the instrument drove once its 16 marks were
    taken, as ``--repeats 16`` did on 2026-11-21 (``docs/relay-timing.md``).
    """

    def __init__(self, opened_again: int | None = None) -> None:
        self.sent: list[list[str]] = []
        self.opened_again = opened_again

    def exchange(
        self, units: list[str], timeout: float = 8.0
    ) -> tuple[list[str], list[str], bytes | None]:
        self.sent.append(list(units))
        if units == ["TRAC:DATA? RAW"]:
            counts = []
            for i in range(4500):
                in_open = 1000 + 40 <= i < 2000 + 40
                if self.opened_again is not None and i >= self.opened_again:
                    in_open = True
                counts.append(0 if in_open else 12)
            rows = ["METER_DUMP count=4500 t0_us=0 period_us=280 fmt=raw20"]
            rows.append("K n=1000 label=k1:open")
            rows.append("K n=2000 label=k1:closed")
            rows.extend(
                f"R n={base} c=" + ",".join(str(c) for c in counts[base : base + 16])
                for base in range(0, 4500, 16)
            )
            rows.append("METER_DUMP_END")
            return [], [], ("\n".join(rows) + "\n").encode()
        return [], [], None


def test_measure_finds_each_edge_at_its_delay() -> None:
    meter = ScriptedMeter()
    edges = measure(cast(Meter, meter), "k1", 1, 300, pause=lambda s: None)
    assert [e.order for e in edges] == ["k1:open", "k1:closed"]
    assert all(e.delay_us == 40 * 280 for e in edges)
    assert ["ROUT:LEAD OPEN,FORC,LIVE"] in meter.sent
    summary = summarize(edges)
    assert summary["k1:open"]["median_us"] == 11200
    assert summary["k1:closed"]["n"] == 1


def test_the_last_move_is_searched_only_until_the_next_order() -> None:
    """The last mark's window ends at its hold, not 2,000 samples on (2026-11-21).

    The lead reads open again 1,300 samples (364 ms) after the last mark, past
    the 300 ms hold (1,071 samples) and inside the 2,000 the tool searched: the
    split landed there, 364,000 us after the reclose, and not at its 40 samples.
    """
    meter = ScriptedMeter(opened_again=2000 + 1300)
    edges = measure(cast(Meter, meter), "k1", 1, 300, pause=lambda s: None)
    closed = [e for e in edges if e.order == "k1:closed"]
    assert [e.delay_us for e in closed] == [40 * 280]
    assert closed[0].after_counts == 12


def test_more_moves_than_the_marks_hold_are_refused() -> None:
    """Sixteen marks a capture: a move past the eighth would be driven unmarked."""
    assert MAX_REPEATS == 8
    meter = ScriptedMeter()
    with pytest.raises(ValueError, match="keeps 16 marks"):
        measure(cast(Meter, meter), "k1", MAX_REPEATS + 1, 300, pause=lambda s: None)
    assert meter.sent == []
    with pytest.raises(SystemExit):
        main(["--sim", "--relay", "k1", "--repeats", str(MAX_REPEATS + 1)])
    with pytest.raises(SystemExit):
        main(["--sim", "--relay", "k1", "--hold", "0"])


def test_simulator_cycles_the_lead_and_refuses_the_unwired_spares() -> None:
    from kleidos_meter.sim import SimulatedMeter

    sim = SimulatedMeter()
    now = [100.0]
    sim.clock = lambda: now[0]

    def ask(line: str) -> str:
        sim.write((line + "\n").encode())
        return sim.read(1 << 16).decode().strip()

    assert ask("ROUT:LEAD:CYCL 200,FORC;:ROUT:STAT?").startswith("k1=lead:open")
    assert "k3=spare:unwired k4=spare:unwired path=none" in ask("ROUT:STAT?")
    ask("CAL:ZERO 2")  # 0.15.0: a null window would refuse the reclose
    assert ask("SYST:ERR?").startswith('-221,"Settings conflict;a lead cycle is pending')
    now[0] += 0.25
    assert "path=shunt" in ask("ROUT:STAT?")
    ask("ROUT:LEAD:CYCL 200")
    assert ask("SYST:ERR?").startswith("109,")
    ask("ROUT:LEAD:CYCL 0,FORC")
    assert ask("SYST:ERR?").startswith("-222,")
    ask("ROUTe:RELay3 ON")
    assert ask("SYST:ERR?").startswith("-241,")


def test_simulator_recloses_an_open_lead_under_a_capture_and_marks_it() -> None:
    """The hold's teardown (0.16.0): an open capture no longer keeps the lead open."""
    from kleidos_meter.sim import SimulatedMeter

    sim = SimulatedMeter()
    now = [100.0]
    sim.clock = lambda: now[0]

    def ask(line: str) -> str:
        sim.write((line + "\n").encode())
        return sim.read(1 << 16).decode().strip()

    ask("SENS:PROF STAT;:INIT")
    ask("ROUT:LEAD OPEN,FORC,LIVE")
    assert "hold_s=120" in ask("ROUT:STAT?")
    now[0] += 100
    ask("ROUT:LEAD OPEN,FORC,LIVE")  # an order that moves nothing arms it again
    now[0] += 100
    assert "path=none" in ask("ROUT:STAT?")
    now[0] += 21
    out = ask("ROUT:STAT?")
    assert "METER_MARK mark=hold:k1:closed" in out
    assert "path=shunt" in out and "hold_s=-" in out and "hold_released=1" in out
    assert ask("SYST:ERR?").endswith(
        '114,"Lead reclosed;the hold reclosed the lead after 120 s under a capture,'
        ' marked hold: (ROUTe:LEAD OPEN again)"'
    )
    assert "hold_teardowns=1" in ask("ABOR;:FETC:STAT?")
    # Idle, the reclose marks nothing; a null window holds it; ABORt re-arms it.
    ask("ROUT:LEAD OPEN,FORC;:CAL:ZERO 2")
    now[0] += 200
    assert "path=none" in ask("ROUT:STAT?")
    ask("ABOR")
    now[0] += 121
    assert "path=shunt" in ask("ROUT:STAT?")
    assert ask("SYST:ERR?").endswith('after 120 s (ROUTe:LEAD OPEN again)"')
    assert "reset_released=-" in ask("SYST:HEAL?")
    ask("ROUT:LEAD OPEN,FORC;:*RST")
    assert "hold_s=-" in ask("ROUT:STAT?")
