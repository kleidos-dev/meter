# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's bench test (``--run-bench``), and the same scenario on the simulator.

On the bench::

    cd host
    .venv/bin/python -m pytest -q -W error tests/test_panel_bench.py --run-bench \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>

It takes the bench lock, runs ``serve`` on the meter's port with its
passthrough, works the meter as the panel does (``panel_bench.run_scenario``:
HEALTH, a 10 s STATE capture with no sample lost, NULL_RELAY, a TRACE ring
decoded on the host against the instrument's SCALed dump, the heap counts
unchanged), then the same from the page in a headless Chromium
(``panel_bench.run_page_scenario``: the Pins view's live map, a wrong SDA
applied and put back, the live chart at 100 ms of smoothing whose readout
gives the capture's mean, a run with the INA228's averaging on), and requires
the calibration verdict ``applied`` at the start and the end, the relays on
the shunt and FORMat TEXT, so the meter is left in the bench's end state.
With ``--bench-shots <dir>`` it saves the Live, Pins and Calibration and
health views there, each named by the date and the image's ``elf=``. It
prints the run ids and counts, never the port.

Without ``--run-bench`` that case skips; the scenario itself runs on
``serve --sim`` in every test run, with short spans.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import conftest
import pytest
from conftest import FakeClock
from panel_bench import (
    BENCH,
    HEAP_KEYS,
    BenchLock,
    BenchLockBusy,
    Shots,
    Spans,
    lock_owner,
    pid_alive,
    run_page_scenario,
    run_scenario,
)
from panel_harness import Served, serving


def test_the_scenario_runs_on_the_simulator(tmp_path: Path) -> None:
    data_dir = tmp_path / "runs"
    argv = ["--sim", "--data-dir", str(data_dir), "--scpi-passthrough", "0"]
    with serving(argv) as served:
        report = run_scenario(served, data_dir, Spans(state=2, null=1, trace=1))
    assert served.status == [0], served.err.getvalue()
    assert report["health"] == report["null"] == "done"
    assert report["heap"] == dict.fromkeys(HEAP_KEYS, 0)
    # STATE streams about 868 rows a second, and every one reached the events.
    assert 1600 < report["state"]["samples"] == report["state"]["streamed"] < 1900
    assert report["trace"]["samples"] > 3000


@pytest.mark.bench
def test_the_panel_works_the_meter_on_the_bench(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    port = request.config.getoption("--meter-port")
    lock_file = request.config.getoption("--bench-lock")
    if not port or not lock_file:
        pytest.fail("give --meter-port (or KLEIDOS_METER_PORT) and --bench-lock")
    declared = os.environ.get("KLEIDOS_METER_LOCK_PID")
    lock = BenchLock(Path(lock_file), declared=int(declared) if declared else None)
    lock.acquire()
    try:
        data_dir = tmp_path / "runs"
        argv = ["--port", port, "--data-dir", str(data_dir), "--scpi-passthrough", "0"]
        folder = request.config.getoption("--bench-shots")
        with serving(argv) as served:
            assert served.get("/api/calibration").body["verdict"]["status"] == "applied", (
                "the meter's calibration is not the registry's: kleidos-meter calibration restore"
            )
            report = run_scenario(served, data_dir, BENCH)
            elf = served.get("/api/health").body["info"]["elf"]
            shots = (
                Shots(
                    Path(folder), f"{dt.datetime.now().astimezone().date().isoformat()}-elf-{elf}"
                )
                if folder
                else None
            )
            report["page"] = _in_chromium(served, data_dir, shots)
            assert served.get("/api/calibration").body["verdict"]["status"] == "applied"
        assert served.status == [0], served.err.getvalue()
        print(f"bench: {report}")
    finally:
        lock.release()


def _in_chromium(served: Served, data_dir: Path, shots: Shots | None) -> dict[str, Any]:
    """``run_page_scenario`` in a headless Chromium, as a person at a desktop sees the page."""
    try:
        from playwright import sync_api
    except ImportError:  # the bench needs the page too: no skip here
        pytest.fail("the bench's page needs the [browser] extra: pip install -e '.[browser]'")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            report = run_page_scenario(page, served, data_dir, BENCH, shots)
        finally:
            browser.close()
    assert errors == [], errors
    return report


# -- the lock -----------------------------------------------------------------------------


def test_a_free_or_stale_lock_is_taken_and_released(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "meter.lock"
    lock = BenchLock(path)
    lock.acquire()
    assert lock.taken and lock_owner(path) == os.getpid()
    lock.release()
    assert not path.exists() and not lock.taken
    path.write_text("not a process\n", encoding="utf-8")
    assert lock_owner(path) is None
    lock.acquire()
    assert lock_owner(path) == os.getpid()
    lock.release()
    path.write_text("999999999\n", encoding="utf-8")  # no such process
    lock.acquire()
    assert lock_owner(path) == os.getpid()
    lock.release()


def test_a_lock_another_live_process_holds_is_waited_for_then_refused(tmp_path: Path) -> None:
    path = tmp_path / "meter.lock"
    path.write_text(f"{os.getppid()}\n", encoding="utf-8")
    clock = FakeClock()
    lock = BenchLock(path, timeout=12.0, poll=5.0, clock=clock, sleep=clock.sleep)
    with pytest.raises(BenchLockBusy, match="held by live process"):
        lock.acquire()
    assert clock.now == pytest.approx(115.0)
    lock.release()
    assert lock_owner(path) == os.getppid(), "a lock this process did not take is left alone"


def test_a_lock_the_caller_declared_is_used_and_left_in_place(tmp_path: Path) -> None:
    path = tmp_path / "meter.lock"
    path.write_text(f"{os.getppid()}\n", encoding="utf-8")
    lock = BenchLock(path, declared=os.getppid())
    lock.acquire()
    assert not lock.taken
    lock.release()
    assert lock_owner(path) == os.getppid()


def test_a_process_is_alive_unless_it_is_gone_or_no_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(0) and not pid_alive(-1)
    assert not pid_alive(999_999_999)

    def denied(pid: int, signal: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(os, "kill", denied)
    assert pid_alive(1)


# -- the option -------------------------------------------------------------------------


def test_the_bench_spans_are_the_plans() -> None:
    assert (BENCH.state, BENCH.null, BENCH.trace) == (10, 5, 3)


def test_no_test_finds_the_meters_port_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "KLEIDOS_METER_PORT" not in os.environ
    monkeypatch.setenv("KLEIDOS_METER_PORT", "/dev/null-meter")
    lines: list[tuple[str, str]] = []
    config = SimpleNamespace(
        addinivalue_line=lambda name, line: lines.append((name, line)),
        getoption=lambda name: False,  # no --run-bench: nothing to make serial
        option=SimpleNamespace(dist="load"),
    )
    conftest.pytest_configure(config)  # type: ignore[arg-type]  # a stand-in Config
    assert "KLEIDOS_METER_PORT" not in os.environ
    assert [name for name, _ in lines] == ["markers"]
    assert config.option.dist == "load"


def test_a_bench_run_is_made_serial_and_a_plain_one_left_parallel() -> None:
    for run_bench, dist in ((True, "no"), (False, "load")):
        option = SimpleNamespace(dist="load", numprocesses=4, tx=["popen"])
        config = SimpleNamespace(
            addinivalue_line=lambda name, line: None,
            getoption=lambda name, run_bench=run_bench: run_bench,
            option=option,
        )
        conftest.pytest_configure(config)  # type: ignore[arg-type]  # a stand-in Config
        assert option.dist == dist
        assert option.numprocesses == (None if run_bench else 4)


def test_a_bench_case_skips_without_run_bench_and_runs_with_it() -> None:
    marked = SimpleNamespace(
        get_closest_marker=lambda name: object() if name == "bench" else None,
        added=[],
    )
    marked.add_marker = marked.added.append
    plain = SimpleNamespace(get_closest_marker=lambda name: None, added=[])
    plain.add_marker = plain.added.append

    def config(run: bool) -> Any:  # pytest's Config, as far as the hook reads it
        return SimpleNamespace(getoption=lambda name: run)

    items: list[Any] = [marked, plain]  # pytest's Items, as far as the hook reads them
    conftest.pytest_collection_modifyitems(config(True), items)
    assert marked.added == [] and plain.added == []
    conftest.pytest_collection_modifyitems(config(False), items)
    assert len(marked.added) == 1 and plain.added == []
    assert marked.added[0].kwargs["reason"] == "talks to the meter on the bench: give --run-bench"
    marks: list[pytest.Mark] = getattr(test_the_panel_works_the_meter_on_the_bench, "pytestmark")  # noqa: B009  the attribute pytest.mark sets
    assert marks[0].name == "bench"
