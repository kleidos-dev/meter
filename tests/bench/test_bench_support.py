# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench suite's support, without the bench: the lock, the sweep, the expectation file.

These run in every test run (no ``--run-bench``): the bench lock's rules,
the comparison that turns a sweep into its verdict, and the expectation
file's shape, its agreement with the sweep, and its history, which only
falls.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from bench_support import (
    DUMP_ATTEMPTS,
    EXPECTATIONS,
    PHASE_KEYS,
    RING_SAMPLES,
    SAVE_STEP,
    SWEEP,
    ZERO_KEYS,
    BenchLock,
    BenchLockBusy,
    Message,
    altered,
    committed_versions,
    compare,
    dump_whole,
    fill_ring,
    load_expectations,
    lock_owner,
    phase_counts,
    pid_alive,
    rises,
    run_step,
    save_step,
    since_main_problems,
    step,
    step_names,
    sweep_steps,
)


def dead_pid() -> int:
    """A process ID no live process has: a child that has exited."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class FakeClock:
    """A clock that advances only when its sleep is called."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_free_lock_is_taken_and_released(tmp_path: Path) -> None:
    lock = BenchLock(tmp_path / "locks" / "meter.lock")
    lock.acquire()
    assert lock_owner(lock.path) == os.getpid()
    lock.release()
    assert not lock.path.exists()


def test_a_stale_lock_is_taken_over(tmp_path: Path) -> None:
    path = tmp_path / "meter.lock"
    path.write_text(f"{dead_pid()}\n", encoding="utf-8")
    lock = BenchLock(path)
    lock.acquire()
    assert lock_owner(path) == os.getpid()
    lock.release()
    path.write_text("not a pid\n", encoding="utf-8")
    BenchLock(path).acquire()
    assert lock_owner(path) == os.getpid()


def test_a_live_owner_is_waited_for_and_then_refused(tmp_path: Path) -> None:
    path = tmp_path / "meter.lock"
    owner = subprocess.Popen(["sleep", "30"])
    try:
        path.write_text(f"{owner.pid}\n", encoding="utf-8")
        clock = FakeClock()
        lock = BenchLock(path, timeout=12.0, poll=5.0, clock=clock, sleep=clock.sleep)
        with pytest.raises(BenchLockBusy, match=str(owner.pid)):
            lock.acquire()
        assert clock.now == 15.0
        assert lock_owner(path) == owner.pid
        lock.release()  # it never took the lock: the owner's file stays
        assert lock_owner(path) == owner.pid
        # A caller that holds the lock for this run names its process.
        declared = BenchLock(path, declared=owner.pid)
        declared.acquire()
        declared.release()
        assert lock_owner(path) == owner.pid
    finally:
        owner.kill()
        owner.wait()


def test_the_lock_is_not_removed_once_another_owner_wrote_it(tmp_path: Path) -> None:
    path = tmp_path / "meter.lock"
    lock = BenchLock(path)
    lock.acquire()
    path.write_text("1\n", encoding="utf-8")
    lock.release()
    assert lock_owner(path) == 1


def test_a_process_id_is_alive_only_when_it_runs() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(dead_pid())
    assert not pid_alive(0)
    assert lock_owner(Path("/nonexistent/meter.lock")) is None


def test_the_expectation_file_parses_and_names_every_step_of_the_sweep() -> None:
    expected = load_expectations(EXPECTATIONS.read_text(encoding="utf-8"))
    assert set(expected.boot) == set(PHASE_KEYS)
    assert list(expected.steps) == step_names()
    names = step_names()
    assert names.index(SAVE_STEP) == names.index("null") - 1
    assert len(set(names)) == len(names)


def test_a_file_missing_a_count_is_refused() -> None:
    with pytest.raises(ValueError, match="pre_main_frees"):
        load_expectations(
            "[boot]\npre_main_allocs = 1\nallocs_since_main = 0\nfrees_since_main = 0\n"
        )
    good_boot = "".join(f"{key} = 0\n" for key in PHASE_KEYS)
    with pytest.raises(ValueError, match="steps.common"):
        load_expectations(f"[boot]\n{good_boot}[steps]\ncommon = {{ allocs = -1, frees = 0 }}\n")


def file_of(boot: int, step: int) -> str:
    rows = "".join(f"{name} = {{ allocs = {step}, frees = {step} }}\n" for name in step_names())
    return "[boot]\n" + "".join(f"{key} = {boot}\n" for key in PHASE_KEYS) + "[steps]\n" + rows


def test_compare_says_which_way_each_count_went() -> None:
    expected = load_expectations(file_of(10, 2))
    boot = dict.fromkeys(PHASE_KEYS, 10)
    deltas = {name: {"allocs": 2, "frees": 2} for name in step_names()}
    assert compare(expected, boot, deltas) == []
    boot["allocs_since_main"] = 11
    deltas["i2c_clock"] = {"allocs": 1, "frees": 3}
    del deltas["reset"]
    problems = compare(expected, boot, deltas)
    assert "boot allocs_since_main: 11, above the 10 expected (a new allocation)" in problems
    assert "step i2c_clock allocs: 1, below the 2 expected (lower the file)" in problems
    assert "step i2c_clock frees: 3, above the 2 expected (a new allocation)" in problems
    assert "step reset: not measured" in problems
    unknown = load_expectations(file_of(10, 2) + "gone = { allocs = 0, frees = 0 }\n")
    unknown = replace(unknown, steps={k: v for k, v in unknown.steps.items() if k != "common"})
    full = {name: {"allocs": 2, "frees": 2} for name in step_names()}
    problems = compare(unknown, dict.fromkeys(PHASE_KEYS, 10), full)
    assert problems == [
        "step common: no expectation in the file",
        "step gone: in the file but not in the sweep",
    ]


def test_a_ceiling_is_refused_and_read_as_its_figure_only_in_history() -> None:
    """FW-17: no count is exempt, so a ceiling in the file is refused; an
    earlier commit's ceilings still parse, as their figures, for the history."""
    step = file_of(10, 2).replace(
        "null = { allocs = 2, frees = 2 }", "null = { allocs = 2, frees = 2, ceiling = true }"
    )
    with pytest.raises(ValueError, match=r"\[steps\.null\]: a ceiling exempts a count"):
        load_expectations(step)
    boot = file_of(10, 2).replace("[steps]", "since_main_ceiling = true\n[steps]")
    with pytest.raises(ValueError, match=r"\[boot\]: a ceiling exempts a count"):
        load_expectations(boot)
    for text in (step, boot):
        old = load_expectations(text, history=True)
        assert old == load_expectations(file_of(10, 2))


def test_every_zero_count_is_held_to_zero_with_no_exemption() -> None:
    report = dict.fromkeys(ZERO_KEYS, "0") | {"armed": "1", "pre_main_allocs": "31"}
    assert since_main_problems("boot", report) == []
    report["frees_since_main"] = "2"
    report["armed"] = "0"
    del report["allocs_after_init"]
    assert since_main_problems("reset", report) == [
        "after reset: frees_since_main=2, not 0 (an allocation since app_main)",
        "after reset: DIAGnostic:HEAP? has no allocs_after_init",
        "after reset: armed=0, not 1",
    ]


def test_the_expectation_file_holds_every_since_main_count_at_zero() -> None:
    expected = load_expectations(EXPECTATIONS.read_text(encoding="utf-8"))
    assert expected.boot["allocs_since_main"] == expected.boot["frees_since_main"] == 0
    assert all(counts == {"allocs": 0, "frees": 0} for counts in expected.steps.values())


#: docs/misra.md, whose D1 lists ESP-IDF's allocations before app_main().
MISRA = Path(__file__).resolve().parents[2] / "docs" / "misra.md"


def residue_rows(text: str) -> list[list[str]]:
    """The cells of each row of D1's residue table in *text* (docs/misra.md).

    :raises ValueError: When the document has no such table.
    """
    start = text.find("#### The residue\n")
    if start < 0:
        raise ValueError("docs/misra.md has no '#### The residue' table under D1")
    following = re.search(r"\n#{2,4} ", text[start + 1 :])
    section = text[start : start + 1 + following.start()] if following else text[start:]
    rows = [line for line in section.splitlines() if re.match(r"\| R\d+ \|", line)]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def test_the_boot_counts_are_d1s_residue_row_for_row() -> None:
    """FW-18: pre_main_allocs is the number of D1's rows, pre_main_frees the
    number it marks freed, and the rows' bytes add up to the census's total;
    so the device, the expectation file and the register say the same."""
    expected = load_expectations(EXPECTATIONS.read_text(encoding="utf-8"))
    rows = residue_rows(MISRA.read_text(encoding="utf-8"))
    assert [row[0] for row in rows] == [f"R{n}" for n in range(1, len(rows) + 1)]
    assert expected.boot["pre_main_allocs"] == len(rows)
    freed = [row for row in rows if row[4].startswith("yes")]
    assert expected.boot["pre_main_frees"] == len(freed)
    assert all(row[4] == "no" or row[4].startswith("yes") for row in rows)
    census = [int(row[1]) for row in rows]
    assert census == sorted(set(census)), "each row is one census event, in order"
    # The review of 2026-11-24: its wrap of malloc() took the last event, the
    # C library's environ (HM2-04), so D1 has no row and the boot no allocation.
    assert sum(int(row[3].replace(",", "")) for row in rows) == 0


def test_the_residue_reader_refuses_a_document_without_the_table() -> None:
    with pytest.raises(ValueError, match="residue"):
        residue_rows("### D1. Allocation\n\nno table\n")
    planted = (
        "#### The residue\n\n| Row | x |\n| R1 | 0 | a | 4 | no | b |\n"
        "| R2 | 1 | c | 8 | yes | d |\n\n#### Next\n| R3 |"
    )
    assert [row[0] for row in residue_rows(planted)] == ["R1", "R2"]


def test_rises_finds_every_count_a_change_raised() -> None:
    old = load_expectations(file_of(10, 2))
    assert rises(old, load_expectations(file_of(9, 1))) == []
    raised = rises(old, load_expectations(file_of(11, 3)))
    assert "boot pre_main_allocs: 10 -> 11" in raised
    assert "step null frees: 2 -> 3" in raised


def test_the_expectation_file_only_falls_over_its_history() -> None:
    current = load_expectations(EXPECTATIONS.read_text(encoding="utf-8"))
    versions = committed_versions(EXPECTATIONS)
    raised = [
        f"{commit[:10]}: {rise}"
        for commit, text in versions
        for rise in rises(load_expectations(text, history=True), current)
    ]
    assert not raised, "the expectation file only falls:\n" + "\n".join(raised)


def test_committed_versions_is_empty_where_git_has_no_history(tmp_path: Path) -> None:
    assert committed_versions(tmp_path / "heap_expectations.toml") == []


def test_the_save_changes_the_instruments_own_tag_and_writes_it_back() -> None:
    save = save_step("KM-1-2026")
    assert save.name == SAVE_STEP
    assert [m.units for m in save.messages] == [
        ('CAL:TAG "KM-1-2020"',),
        ('CAL:TAG "KM-1-2026"',),
        ("CAL:TAG?",),
    ]
    assert altered("ab0") == "ab1"
    assert altered("") == "0"
    assert len(altered("x" * 23)) == 23
    assert [s.name for s in SWEEP if s.name == SAVE_STEP] == []


def test_the_sweep_runs_the_save_before_the_null_and_every_other_step_in_place() -> None:
    steps = sweep_steps("T0")
    assert [s.name for s in steps] == step_names()
    assert steps[step_names().index("null") - 1] == save_step("T0")
    assert steps[-1].name == "reset"


def test_every_message_of_the_sweep_fits_the_consoles_line() -> None:
    """A message and the error query ``exchange()`` ends it with fit the line.

    A longer one is refused whole (``-363``) and answers nothing, which the
    sweep can only meet on the meter, as a time-out.
    """
    root = Path(__file__).resolve().parents[2]
    header = (root / "firmware" / "src" / "console.h").read_text(encoding="utf-8")
    found = re.search(r"kConsoleLineCapacity = (\d+);", header)
    assert found is not None
    longest = int(found.group(1)) - 1  # the console keeps the last byte for its NUL
    for sweep_step in sweep_steps("R100_TAG"):
        for message in sweep_step.messages:
            line = ";:".join(["*CLS", *message.units, "SYST:ERR:ALL?"])
            assert len(line) <= longest, f"{sweep_step.name}: {len(line)} bytes: {line}"


def test_the_phase_counts_are_read_whole_or_refused() -> None:
    report = dict.fromkeys(PHASE_KEYS, "7") | {"armed": "1"}
    assert phase_counts(report) == dict.fromkeys(PHASE_KEYS, 7)
    del report["frees_since_main"]
    with pytest.raises(AssertionError, match="no frees_since_main"):
        phase_counts(report)


class FakeMeter:
    """A meter that keeps what it is sent, queues *errors* and fills its ring."""

    def __init__(self, errors: list[int] | None = None, points: list[int] | None = None) -> None:
        self.sent: list[list[str]] = []
        self.errors = [SimpleNamespace(code=code) for code in errors or []]
        self.points = points or []

    def exchange(
        self, units: list[str], *, timeout: float, raise_errors: bool = True
    ) -> tuple[list[str], list[SimpleNamespace], None]:
        assert timeout > 0.0
        self.sent.append(units)
        return [], self.errors, None

    def query(self, unit: str) -> str:
        assert unit == "TRAC:POIN?"
        return str(self.points.pop(0) if len(self.points) > 1 else self.points[0])


def test_a_step_reports_the_errors_it_may_not_queue_and_waits_where_it_says() -> None:
    clock = FakeClock()
    work = step("work", Message(("A", "B"), wait=2.0), ["C?"], allowed=[-230])
    meter = FakeMeter(errors=[-230, -113])
    found = run_step(meter, work, clock.sleep)
    assert meter.sent == [["A", "B"], ["C?"]]
    assert clock.now == pytest.approx(2.0)
    assert len(found) == 2 and all("-113" in line for line in found)
    assert found[0].startswith("work: A: ") and found[1].startswith("work: C?: ")


def test_the_ring_is_waited_for_until_it_is_full() -> None:
    clock = FakeClock()
    looks: list[float] = []
    meter = FakeMeter(points=[10, RING_SAMPLES])
    fill_ring(meter, lambda: looks.append(clock.now), clock=clock, sleep=clock.sleep)
    assert meter.sent == [["SENS:PROF TRAC", "INIT"]]
    assert looks == pytest.approx([15.0, 30.0])


def test_a_ring_that_does_not_fill_in_time_is_a_failure() -> None:
    clock = FakeClock()
    meter = FakeMeter(points=[10])
    with pytest.raises(AssertionError, match="the ring holds 10"):
        fill_ring(meter, lambda: None, clock=clock, sleep=clock.sleep)
    assert 360.0 <= clock.now < 376.0


class DumpMeter:
    """A meter whose dumps are *blocks*, an exception among them raised."""

    def __init__(self, blocks: list[bytes | Exception]) -> None:
        self.blocks = blocks
        self.asked: list[str] = []
        self.resynced = 0

    def send(self, message: str, *, timeout: float) -> SimpleNamespace:
        assert timeout > 0.0
        self.asked.append(message)
        block = self.blocks.pop(0)
        if isinstance(block, Exception):
            raise block
        return SimpleNamespace(block=block)

    def wait_ready(self, *, timeout: float) -> bool:
        assert timeout > 0.0
        self.resynced += 1
        return False


WHOLE = f"METER_DUMP count={RING_SAMPLES} fmt=raw20\nrows\n".encode()


def held(block: bytes) -> bool:
    """A CRC-32 check: refuses a block that says it is damaged."""
    if b"damaged" in block:
        raise ValueError("CRC-32 0 over the block, the row says 1")
    return True


def test_a_damaged_or_endless_dump_is_asked_for_again_and_kept_in_the_record() -> None:
    meter = DumpMeter([b"damaged", TimeoutError("no response"), WHOLE])
    block, damaged = dump_whole(meter, "RAW", held, TimeoutError)
    assert block == WHOLE
    assert meter.asked == ["TRAC:DATA? RAW"] * 3 and meter.resynced == 2
    assert damaged == ["RAW: CRC-32 0 over the block, the row says 1", "RAW: no response"]


def test_a_dump_that_never_matches_is_a_failure() -> None:
    meter = DumpMeter([b"damaged"] * (DUMP_ATTEMPTS + 1))
    with pytest.raises(AssertionError, match="no dump matched its CRC-32"):
        dump_whole(meter, "SCAL", held, TimeoutError)
    assert len(meter.asked) == DUMP_ATTEMPTS


def test_a_dump_without_a_crc_or_of_part_of_the_ring_is_a_failure() -> None:
    with pytest.raises(AssertionError, match="carries no CRC-32"):
        dump_whole(DumpMeter([WHOLE]), "RAW", lambda block: None, TimeoutError)
    with pytest.raises(AssertionError, match="count=7"):
        dump_whole(DumpMeter([b"METER_DUMP count=7 fmt=raw20\n"]), "RAW", held, TimeoutError)
