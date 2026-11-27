# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's bench scenario, and the bench lock it is run under.

:func:`run_scenario` drives a running serve the way the panel does and holds
what the meter must do for it: the HEALTH sequence, a STATE capture streamed
over Server-Sent Events with no sample lost on the way, a null on the relay
path, a TRACE ring whose raw dump (the run's ``trace.raw``, decoded on the
host) agrees sample for sample with the instrument's own SCALed dump read
through the passthrough, the heap counts the same at the end as at the
start, and the instrument left as found: the relays on the shunt, the
stream format TEXT, the calibration verdict unchanged.

:func:`run_page_scenario` works the same serve from its page, in a real
browser: the Pins view's live map, a wrong SDA applied and put back by the
instrument, the live chart's readout at 100 ms of smoothing against the
capture's ``FETCh:STATistics?``, and a run recorded with the INA228's
averaging on, named in its ``meta.json``; it saves the views' images when
asked (:class:`Shots`).

``test_panel_bench.py`` runs the first against ``serve --sim`` in every test
run (short spans), ``test_panel_browser.py`` the second, and both run
against the meter on the bench with ``--run-bench`` (the plan's spans: a
10 s STATE capture), so the scenario the bench runs is never untried code.

:class:`BenchLock` is the bench's convention for the meter: a file holding
the live process ID of whoever uses it, taken over when stale, waited for
while another live process holds it.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kleidos_meter.client import Meter, SerialTransport
from kleidos_meter.panel.recorder import Run
from kleidos_meter.protocol import decode_dump
from panel_harness import EventReader, Served, until

#: The heap counts that must not move while the panel works the meter.
HEAP_KEYS = (
    "allocs_after_init",
    "frees_after_init",
    "maint_allocs",
    "maint_frees",
    "allocs_since_main",
    "frees_since_main",
)


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
    """The process ID the lock file names, None when it names none or is absent."""
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
        it (``KLEIDOS_METER_LOCK_PID``): a lock it names is used and left in place.
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
        """Take the lock, waiting while another live process holds it.

        A file that names no process, or one that has exited, is stale and is
        taken over; a lock held by :attr:`declared` or by this process is used
        as it is.

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
        """Remove the lock if this process took it and still holds it."""
        if self.taken and lock_owner(self.path) == os.getpid():
            self.path.unlink()
        self.taken = False


@dataclass(frozen=True)
class Spans:
    """How long each part of the scenario runs, seconds.

    :ivar state: The STATE capture.
    :ivar null: The null window.
    :ivar trace: The TRACE ring.
    :ivar wait: The most a sequence may take beyond its span.
    :ivar chart: The page's STATE capture, all of it inside the chart's 10 s view.
    :ivar averaged: The page's capture with the INA228's averaging on.
    """

    state: int
    null: int
    trace: int
    wait: float = 120.0
    chart: int = 8
    averaged: int = 5


#: The plan's spans, on the bench.
BENCH = Spans(state=10, null=5, trace=3)


def heap_counts(served: Served) -> dict[str, int]:
    """The heap counts ``DIAGnostic:HEAP?`` reports that are not to move, as they are now."""
    heap = served.get("/api/health").body["heap"]
    return {key: heap[key] for key in HEAP_KEYS if key in heap}


def _done(served: Served, name: str, params: dict[str, int], wait: float) -> dict[str, Any]:
    """Run one sequence and require it done."""
    state = served.sequence(name, params, wait)
    assert state["outcome"] == "done", (name, state["outcome"], state["detail"], state["steps"])
    done: dict[str, Any] = state
    return done


def _rows_settled(reader: EventReader) -> int:
    """The sample rows the events carried, once a second has added none."""
    last = -1
    while True:
        rows = reader.sample_rows()
        if rows == last:
            return rows
        last = rows
        time.sleep(1.0)


def run_scenario(served: Served, data_dir: Path, spans: Spans) -> dict[str, Any]:
    """Work the meter behind *served* as the panel does; what was found.

    :param served: A serve with its passthrough open, recording to *data_dir*.
    :param data_dir: Its data directory.
    :param spans: How long each part runs.
    :returns: The figures a bench record keeps: the heap counts, the STATE
        run with its samples and the rows the events carried, the TRACE run
        and the samples compared.
    :raises AssertionError: At the first thing the meter or the panel got wrong.
    """
    assert served.passthrough is not None, "the scenario reads the SCALed dump through it"
    heap_before = heap_counts(served)
    verdict = served.get("/api/calibration").body["verdict"]["status"]
    health = _done(served, "HEALTH", {}, spans.wait)

    reader = EventReader(served)
    try:
        state = _done(served, "STATE", {"seconds": spans.state}, spans.state + spans.wait)
        streamed = _rows_settled(reader)
        lost = reader.events("lost")
    finally:
        reader.close()
    run = state["results"]["run"]
    meta = served.get(f"/api/runs/{run}").body
    samples = int(meta["statistics"]["fields"]["samples"])
    assert meta["complete"] is True, meta
    assert meta["statistics"]["fields"]["dropped"] == "0", meta["statistics"]
    assert meta["stream"]["lost"] == 0 and meta["stream"]["samples"] == samples
    assert lost == [], lost
    assert streamed == samples, (streamed, samples)

    null = _done(served, "NULL_RELAY", {"seconds": spans.null}, spans.null + spans.wait)
    assert served.get("/api/relays").body["state"]["path"] == "shunt"

    started = served.post("/api/capture/start", {"profile": "TRACE", "seconds": spans.trace})
    assert started.status == 200, started.body
    trace_run = started.body["run"]
    served.capture_idle(spans.trace + spans.wait)
    trace_meta = served.get(f"/api/runs/{trace_run}").body
    assert trace_meta["complete"] is True and trace_meta["trace"]["file"] == "trace.raw"
    raw = Run(data_dir / trace_run).dump()
    assert raw is not None and raw.nanoamps is not None and raw.count > 0
    meter = Meter(SerialTransport(served.passthrough))
    try:
        meter.wait_ready()
        _, errors, block = meter.exchange(["TRAC:DATA? SCAL"], timeout=240.0)
    finally:
        meter.close()
    until(
        lambda: served.get("/api/status").body["held"] is None,
        "the line back from the passthrough",
    )
    assert errors == [] and block is not None
    scaled = decode_dump(block)
    assert scaled.crc_checked and not scaled.raw
    assert list(raw.indices) == list(scaled.indices)
    assert raw.nanoamps is not None and scaled.nanoamps is not None
    assert list(raw.nanoamps) == list(scaled.nanoamps)

    heap_after = heap_counts(served)
    assert heap_after == heap_before, (heap_before, heap_after)
    assert served.get("/api/relays").body["state"]["path"] == "shunt"
    assert served.get("/api/settings").body["settings"]["format"] == "TEXT"
    assert served.get("/api/calibration").body["verdict"]["status"] == verdict
    return {
        "heap": heap_after,
        "calibration": verdict,
        "health": health["outcome"],
        "state": {"run": run, "samples": samples, "streamed": streamed},
        "null": null["outcome"],
        "trace": {"run": trace_run, "samples": raw.count},
    }


# -- the page ------------------------------------------------------------------------------

#: The live map's roles, by the word the Pins view names each with.
ROLE_LABELS = {
    "sda": "INA228 SDA",
    "scl": "INA228 SCL",
    "alert": "INA228 ALERT",
    "relay1": "Relay 1",
    "relay2": "Relay 2",
}

#: The rig's own map (rev D), which the meter boots with no record stored.
DEFAULT_MAP = {"sda": 4, "scl": 5, "alert": 6, "relay1": 7, "relay2": 15}

#: A GPIO the rig leaves unwired, free on the board: SDA moved there finds no INA228.
UNWIRED = 18

#: What the Pins view says of a map the INA228 did not answer on.
REVERTED = (
    "Reverted: the INA228 did not answer on the map's SDA, SCL and address."
    " The last good map is back."
)

#: The noise reduction the recorded run is taken with: an averaging the rig converts.
AVERAGING = {"state": "on", "count": 16, "conversion_us": 1052, "period_us": 18432}

#: Whether the ``STATe`` card of the noise reduction reads as its argument begins.
NOISE_READS = (
    "s => document.querySelector('[data-noise=STATe] [data-current]').textContent.startsWith(s)"
)

#: The readout's mean of the current in view, as the page writes it.
_MEAN = re.compile(r"Current [^(;]*\(in view mean (-?[\d.]+) ([pnµm]?)A,")

#: An SI prefix's power of ten, as the page writes it.
_PREFIX = {"p": -12, "n": -9, "µ": -6, "m": -3, "": 0}


@dataclass(frozen=True)
class Shots:
    """Where the page's images go, and what each is stamped with.

    :ivar folder: The directory the PNGs and their record are written to.
    :ivar stamp: The date and the image's ``elf=``, in every file's name.
    """

    folder: Path
    stamp: str

    def take(self, page: Any, view: str) -> Path:
        """Save the whole page as it shows *view*; the file."""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"bench-{view}-{self.stamp}.png"
        page.evaluate("() => window.scrollTo(0, 0)")
        page.screenshot(path=str(path), full_page=True)
        return path


def readout_mean(text: str) -> float:
    """The current's mean in view the chart's readout gives, nanoamps.

    :raises AssertionError: When the readout gives none.
    """
    found = _MEAN.search(text)
    assert found, text
    return float(found.group(1)) * 10.0 ** (_PREFIX[found.group(2)] + 9)


def readout_step(text: str) -> float:
    """One unit of the last digit the readout's mean in view shows, nanoamps."""
    found = _MEAN.search(text)
    assert found, text
    decimals = len(found.group(1).partition(".")[2])
    return 10.0 ** (_PREFIX[found.group(2)] + 9 - decimals)


def _settled_readout(page: Any, quiet: float = 2.5, wait: float = 60.0) -> str:
    """The chart's readout once it names a mean in view and has not changed for *quiet* s.

    The page writes the readout at most once a second, and the last samples of
    a capture reach it over the events after the recorder has ended it.
    """
    deadline = time.monotonic() + wait
    last, since = "", time.monotonic()
    while time.monotonic() < deadline:
        text = str(page.text_content("#chart-readout"))
        if text != last:
            last, since = text, time.monotonic()
        elif "in view mean" in text and time.monotonic() - since >= quiet:
            return text
        page.wait_for_timeout(250)
    raise AssertionError(f"the readout did not settle: {last}")


def _show(page: Any, view: str) -> None:
    """Show a view by its link, as a person does."""
    page.click(f"nav.views a[href='#{view}']")
    page.wait_for_selector(f"section#{view}:not([hidden])")


def _until_page(page: Any, script: str, arg: Any = None) -> None:
    """Wait for *script* to be true in the page, as long as a capture may take."""
    page.wait_for_function(script, arg=arg, timeout=180_000)


def _capture(page: Any, served: Served, seconds: int, wait: float) -> dict[str, Any]:
    """Start a ``STATE`` capture of *seconds* from the Live view; its run's meta, once ended."""
    _show(page, "live")
    page.select_option("#capture-profile", "STATe")
    page.fill("#capture-seconds", str(seconds))
    last = (served.get("/api/capture").body.get("last") or {}).get("id")
    page.click("#capture-start")
    state: dict[str, Any] = {}

    def ended() -> bool:
        state.update(served.get("/api/capture").body)
        return state.get("state") == "idle" and (state.get("last") or {}).get("id") != last

    until(ended, "the page's capture to end", seconds + wait)
    run = state["last"]["id"]
    meta: dict[str, Any] = served.get(f"/api/runs/{run}").body
    assert meta["complete"] is True, meta
    return meta


def _ask(served: Served, message: str) -> list[str]:
    """One program message through serve's transport; its answers, with no error."""
    reply = served.post("/api/scpi", message).body["replies"][0]
    assert reply["errors"] == [], reply
    units: list[str] = reply["units"]
    return units


def _wrong_sda_put_back(
    page: Any, served: Served, shots: Shots | None, taken: dict[str, str]
) -> tuple[str, str]:
    """SDA moved to the unwired pin from the Pins view, applied, put back; the two sentences."""
    page.click(f".pin[data-gpio='{UNWIRED}']")
    page.wait_for_selector("#pin-dialog[open]")
    page.check("#pin-uses input[value='sda']")
    page.click("#pin-keep")
    page.wait_for_selector("#pin-dialog:not([open])", state="attached")
    assert page.text_content("#pins-diff") == f"INA228 SDA: GPIO4 → GPIO{UNWIRED}"
    page.click("#pins-apply")
    page.wait_for_selector("#confirm-dialog[open]")
    page.click("#confirm-go")
    _until_page(
        page, "t => document.querySelector('#pins-result').textContent.startsWith(t)", "Reverted"
    )
    reverted = str(page.text_content("#pins-result"))
    assert reverted == REVERTED, reverted
    _until_page(page, "() => document.querySelector('#pins-refusal').textContent.includes('117')")
    refusal = str(page.text_content("#pins-refusal"))
    if shots is not None:
        taken["pins-reverted"] = shots.take(page, "pins-reverted").name
    page.click("#pins-discard")
    _until_page(page, "() => document.querySelector('#pins-diff').textContent === ''")
    until(lambda: served.get("/api/pins").body["differs"] == [], "the staged map dropped")
    return reverted, refusal


def run_page_scenario(
    page: Any, served: Served, data_dir: Path, spans: Spans, shots: Shots | None = None
) -> dict[str, Any]:
    """Work the meter behind *served* from the panel's page, in a real browser.

    The Pins view draws the live map the instrument reports over the cable; SDA
    moved to a pin the rig leaves unwired is applied from the page and reverted
    by the instrument, with its sentence and error ``117``, and the live map
    and ``*TST?`` are as they were. The live chart at 100 ms of smoothing on
    the meter's own ``STATE`` samples gives in its readout the mean
    ``FETCh:STATistics?`` gives of the capture. A run recorded with the
    INA228's averaging on names it in its ``meta.json``, and the averaging is
    off again at the end. With *shots*, the Live, Pins and Calibration and
    health views are saved as images.

    :param page: A Playwright page, nothing opened yet.
    :param served: A serve on the meter (or its simulator).
    :param data_dir: Its data directory.
    :param spans: How long the captures run (``state``).
    :param shots: Where to save the views' images, None for none.
    :returns: The figures a bench record keeps.
    :raises AssertionError: At the first thing the meter or the page got wrong.
    """
    heap_before = heap_counts(served)
    page.goto(served.url)
    _until_page(
        page,
        "() => !['', '-'].includes(document.querySelector('[data-status=serial]').textContent)",
    )
    taken: dict[str, str] = {}

    # The live chart, smoothed at 100 ms, on the meter's own samples.
    _show(page, "live")
    before = page.get_attribute("#chart", "data-caption")
    page.click("#chart-smoothing label:has-text('100 ms')")
    _until_page(page, "c => document.querySelector('#chart').dataset.caption !== c", before)
    state = _capture(page, served, spans.chart, spans.wait)
    _until_page(
        page,
        "() => document.querySelector('#chart').dataset.caption.startsWith('moving average')",
    )
    readout = _settled_readout(page)
    assert "moving average" in readout, readout
    mean, step = readout_mean(readout), readout_step(readout)
    stats = float(state["statistics"]["fields"]["mean_na"])
    # The page's four significant digits: within one unit of the last one shown.
    assert abs(mean - stats) <= step, (readout, stats)
    if shots is not None:
        taken["live"] = shots.take(page, "live").name

    # The Pins view: the live map over the cable, and a wrong SDA put back.
    _show(page, "pins")
    page.wait_for_selector(".pin[data-state]")
    _until_page(
        page, "() => document.querySelector('[data-status=pins]').textContent === 'default'"
    )
    pins = served.get("/api/pins").body
    assert pins["differs"] == [], f"a staged map is left on the instrument: {pins['differs']}"
    live = pins["live"]
    assert {role: live[role] for role in DEFAULT_MAP} == DEFAULT_MAP, live
    for role, gpio in DEFAULT_MAP.items():
        pin = page.locator(f".pin[data-gpio='{gpio}']")
        assert pin.get_attribute("data-state") == "used", (role, gpio)
        assert ROLE_LABELS[role] in str(pin.get_attribute("aria-label")), (role, gpio)
    if shots is not None:
        taken["pins"] = shots.take(page, "pins").name
    try:
        reverted, refusal = _wrong_sda_put_back(page, served, shots, taken)
    finally:  # a staged map outlives the page on the instrument: never leave one
        _ask(served, "SYST:PINS:CLE")
    after = served.get("/api/pins").body["live"]
    assert (after["src"], {role: after[role] for role in DEFAULT_MAP}) == ("default", DEFAULT_MAP)
    assert _ask(served, "*TST?") == ["0"]

    # A run recorded with the INA228's averaging on, and off again.
    _show(page, "settings")
    card = page.locator("article[data-noise='STATe']")
    card.locator("[data-noise-setting=average]").select_option("ON")
    card.locator("[data-noise-setting=average_count]").select_option(str(AVERAGING["count"]))
    card.locator("[data-noise-setting=average_aperture]").select_option("0.001052")
    card.locator("[data-action=set]").click()
    _until_page(page, NOISE_READS, "On: ")
    averaged = _capture(page, served, spans.averaged, spans.wait)
    assert averaged["noise_reduction"] == AVERAGING, averaged["noise_reduction"]
    stored = json.loads((data_dir / averaged["id"] / "meta.json").read_text(encoding="utf-8"))
    assert stored["noise_reduction"] == AVERAGING
    _show(page, "settings")
    card.locator("[data-noise-setting=average]").select_option("OFF")
    card.locator("[data-action=set]").click()
    _until_page(page, NOISE_READS, "Off: ")
    assert _ask(served, "SENS:PROF STAT;:SENS:AVER?") == ["0"]

    # Calibration and health, and the instrument as found.
    _show(page, "health")
    _until_page(page, "() => document.querySelectorAll('#health-identity dt').length > 0")
    page.click("#health-selftest")
    _until_page(
        page,
        "s => !document.querySelector(s).textContent.startsWith('Not run')",
        "#health-selftest-result",
    )
    selftest = str(page.text_content("#health-selftest-result"))
    if shots is not None:
        taken["health"] = shots.take(page, "health").name
    heap_after = heap_counts(served)
    assert heap_after == heap_before, (heap_before, heap_after)
    assert served.get("/api/relays").body["state"]["path"] == "shunt"
    assert served.get("/api/settings").body["settings"]["format"] == "TEXT"
    return {
        "heap": heap_after,
        "state": {"run": state["id"], "readout_mean_na": mean, "statistics_mean_na": stats},
        "pins": {"live": live["src"], "reverted": reverted, "refusal": refusal},
        "averaged": {"run": averaged["id"], "noise_reduction": averaged["noise_reduction"]},
        "selftest": selftest,
        "shots": taken,
    }
