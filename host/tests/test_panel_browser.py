# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's page in a real browser, headless, against ``kleidos-meter serve --sim``.

These need the ``[browser]`` extra (Python Playwright, pinned to the version
Kleidos's QA uses) and its Chromium::

    .venv/bin/python -m pip install -e '.[browser]'
    .venv/bin/python -m playwright install chromium

Without either the whole module skips and says which is missing, and fails with
``METER_BROWSER_REQUIRED=1``, which CI's panel job sets after installing both.

Every test checks, on the way out, that the page wrote no error to the
console, threw nothing and asked for nothing beyond serve's own address
(:class:`Watched`). Between them they load the page, start a live capture
and see its samples drawn, refuse a ``FORCe`` order until its dialog is
confirmed, run a sequence to its end and answer a sequence's question,
download a run's CSV and parse it, check the chart's arithmetic through the
page's own module (the column means, the moving average and its step, peak,
edge and gap rules against a reference in Python, the window's figures, a
run's weighted buckets and the smoothing captions), check the chart as drawn
on a stream they inject (the line over a faint band in both themes, measured
on the canvas's pixels; smoothing moving the line and not the band; the
readout's figures against the samples injected; each chart's smoothing
remembered on its own), hold the page's memory flat under the simulator's
real stream rate, measure the status bar's height on every view (one value
a width, whatever the calibration says), read every view for a local path
(none: no temporary or home directory, nothing absolute) and turn the
INA228's noise reduction on and off (off on a fresh page, its period shown
before it is written, named wherever its samples are, put back by serve's
exit, refused with the instrument's reason). The page's part of the
panel's bench scenario (``panel_bench.run_page_scenario``) runs here on the
simulator, as the bench runs it on the meter.

The test of record walks every view in both themes at a desktop's and a
phone's width: no error, no request beyond serve, no absolute path in any
text, one height of the status bar. The documented images'
browser half renders every image of ``docs/images/panel/`` in the scenario it
was taken in (``panel_shots``) and holds what each view renders to the
signature its manifest took, under the Mac's fonts and another's alike, and
runs the script that takes them on one image. The last test of the module holds what the
pages above ran of every module of the page to ``host/js_floors.toml``
(:mod:`js_coverage`), the pure ones (``chart-math.js``, ``pins-math.js``)
whole; a selection of the tests skips it.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import importlib.util
import json
import math
import os
import re
import socket
import struct
import time
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import js_coverage
import panel_shots
import pytest
from conftest import BROWSER_REQUIRED, DESELECTED, SIM_SERIAL, build_registry, missing
from js_coverage import Recorder
from kleidos_meter.panel import serve
from kleidos_meter.sim import SimulatedMeter
from panel_bench import (
    AVERAGING,
    HEAP_KEYS,
    REVERTED,
    Shots,
    Spans,
    readout_mean,
    readout_step,
    run_page_scenario,
)
from panel_harness import Served, serving, until
from test_registry import accepted, add_events

try:
    from playwright import sync_api
except ImportError:
    EXTRA = "the [browser] extra is not installed: pip install -e '.[browser]' in host/"
    if os.environ.get(BROWSER_REQUIRED) == "1":
        raise RuntimeError(f"{EXTRA} ({BROWSER_REQUIRED}=1)") from None
    pytest.skip(EXTRA, allow_module_level=True)

#: Milliseconds the page may take for anything a test waits for.
PAGE_WAIT = 20_000

#: What the module's pages ran of the page's modules (``js_coverage``).
COVERAGE = Recorder.of_the_package()


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    """One ``serve --sim`` for the module, its registry filing today's calibration."""
    base = tmp_path_factory.mktemp("browser")
    registry = build_registry(base)
    add_events(
        registry, accepted(registry, f"{dt.datetime.now().astimezone().date().isoformat()}-01")
    )
    argv = ["--sim", "--data-dir", str(base / "runs"), "--registry", str(registry)]
    with serving(argv) as running:
        assert running.post("/api/calibration/restore").status == 200
        yield running


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """A headless Chromium, or a skip that says how to install it."""
    with sync_api.sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(headless=True)
        except sync_api.Error as error:
            missing(
                "Playwright's Chromium is not installed: .venv/bin/python -m playwright"
                f" install chromium ({str(error).splitlines()[0]})",
                BROWSER_REQUIRED,
            )
        yield launched
        launched.close()


@dataclass
class Watched:
    """A page, and what it wrote to the console, threw and asked for.

    :ivar page: The Playwright page.
    :ivar origin: Serve's address, the only one the page may ask.
    :ivar errors: Console errors and uncaught exceptions, as text.
    :ivar requests: Every URL the page asked for.
    :ivar refusals: The HTTP statuses a test expects serve to refuse with: the
        browser logs each refused request to the console as an error, which is
        no fault of the page's when the test asked for the refusal.
    """

    page: Any
    origin: str
    errors: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    refusals: set[int] = field(default_factory=set)

    def unexpected(self) -> list[str]:
        """The errors, less the browser's notes of the refusals the test asked for."""
        expected = {
            f"console: Failed to load resource: the server responded with a status of {code}"
            for code in self.refusals
        }
        return [e for e in self.errors if not any(e.startswith(x) for x in expected)]

    def asked(self) -> set[str]:
        """What was requested of serve so far, by path."""
        return {url.removeprefix(self.origin.rstrip("/")) for url in self.requests}

    def foreign(self) -> list[str]:
        """The requests beyond serve's address (a data: URL is the page's own icon)."""
        return [
            url
            for url in self.requests
            if not url.startswith(self.origin) and not url.startswith("data:")
        ]


@pytest.fixture
def watched(browser: Any, served: Served) -> Iterator[Watched]:
    """A fresh page on the panel; it must end with no error and no foreign request."""
    context = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    seen = watch(page, served)
    page.goto(served.url)
    page.wait_for_function(
        "serial => document.querySelector('[data-status=serial]').textContent.includes(serial)",
        arg=SIM_SERIAL,
    )
    try:
        yield seen
    finally:
        COVERAGE.close(context)
    assert seen.unexpected() == []
    assert seen.foreign() == []


def watch(page: Any, served: Served) -> Watched:
    """Record what *page* writes to the console, throws and asks for, and what of it ran."""
    page.set_default_timeout(PAGE_WAIT)
    COVERAGE.start(page)
    seen = Watched(page, served.url)
    page.on(
        "console",
        lambda message: (
            seen.errors.append(f"console: {message.text}") if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda error: seen.errors.append(f"exception: {error}"))
    page.on("request", lambda request: seen.requests.append(request.url))
    return seen


def show(page: Any, view: str) -> None:
    """Show a view by its fragment, as its link does."""
    page.click(f"nav.views a[href='#{view}']")
    page.wait_for_selector(f"section#{view}:not([hidden])")


# -- loading ------------------------------------------------------------------------------


def test_the_page_loads_with_no_console_error_and_asks_only_its_own_origin(
    watched: Watched,
) -> None:
    page = watched.page
    page.wait_for_function(
        "() => document.querySelector('[data-status=link]').textContent === 'up'"
    )
    assert page.text_content("[data-status=image]") not in ("", "-")
    assert page.title() == "Kleidos meter panel"
    # The link's state may be the first GET /api/status's, before the events open.
    asked = watched.asked()
    deadline = time.monotonic() + PAGE_WAIT / 1000
    while not any(url.startswith("/api/events") for url in asked) and time.monotonic() < deadline:
        page.wait_for_timeout(50)  # lets the page's events reach this process
        asked = watched.asked()
    assert {"/", "/static/panel.css", "/static/js/main.js", "/api/schema", "/api/status"} <= asked
    assert any(url.startswith("/api/events") for url in asked)
    # Every view can be shown, and a hidden one reads nothing.
    for view in (*VIEWS[1:], "live"):
        show(page, view)


# -- live ---------------------------------------------------------------------------------


def test_start_live_streams_samples_into_the_chart_and_shows_the_statistics(
    watched: Watched, served: Served
) -> None:
    page = watched.page
    page.select_option("#capture-profile", "STATe")
    page.fill("#capture-seconds", "2")
    page.fill("#capture-label", "browser live")
    page.click("#capture-start")
    page.wait_for_function(
        "() => document.querySelector('#chart-readout').textContent.startsWith('Newest at')"
    )
    assert "Current" in page.text_content("#chart-readout")
    page.wait_for_selector("#statistics:not([hidden])")
    served.capture_idle()
    last = served.get("/api/capture").body["last"]
    assert (last["label"], last["complete"]) == ("browser live", True)
    assert page.text_content("[data-status=loss]") == "dropped 0 · lost 0"
    # The canvas holds a drawing, not a blank plot.
    inked = page.evaluate(
        """() => {
            const canvas = document.querySelector('#chart');
            const context = canvas.getContext('2d');
            const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
            const colors = new Set();
            for (let i = 0; i < data.length; i += 4 * 97) {
                colors.add(`${data[i]},${data[i + 1]},${data[i + 2]}`);
            }
            return colors.size;
        }"""
    )
    assert inked > 3


def test_the_page_keeps_its_memory_flat_under_the_real_stream_rate(
    watched: Watched, served: Served
) -> None:
    page = watched.page
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")

    def heap() -> float:
        cdp.send("HeapProfiler.collectGarbage")
        metrics = cdp.send("Performance.getMetrics")["metrics"]
        return float(next(m["value"] for m in metrics if m["name"] == "JSHeapUsedSize"))

    page.select_option("#capture-profile", "STATe")
    for box in page.query_selector_all("#capture-channels input"):
        box.check()
    page.fill("#capture-seconds", "")
    page.click("#capture-start")
    page.wait_for_function(
        "() => document.querySelector('#chart-readout').textContent.startsWith('Newest at')"
    )
    time.sleep(2.0)
    before = heap()
    rows_before = served.get("/api/capture").body["samples"]
    time.sleep(6.0)
    after = heap()
    rows_after = served.get("/api/capture").body["samples"]
    page.click("#capture-stop")
    served.capture_idle()
    # About 870 rows a second of four channels went through (5220 in 6 s on
    # 2026-11-22), and the heap stayed where it was (2.55 MB, then 2.50 MB):
    # the ring was sized at the start and nothing else keeps a sample.
    assert rows_after - rows_before > 4000
    assert after - before < 1024 * 1024, (before, after)


# -- relays -------------------------------------------------------------------------------


def test_a_force_order_goes_out_only_once_its_dialog_is_confirmed(
    watched: Watched, served: Served
) -> None:
    page = watched.page
    watched.refusals.add(409)  # the order without FORCe, refused by the instrument
    show(page, "relays")
    card = page.locator("article[data-relay=lead]")
    refusal = card.locator("[data-refusal]")
    # Without FORCe the instrument refuses, and the page says so beside the order.
    card.locator("button[data-choice=OPEN]").click()
    page.wait_for_function(
        "() => document.querySelector('article[data-relay=lead] [data-refusal]').textContent !== ''"
    )
    assert "109" in refusal.text_content()
    assert served.get("/api/relays").body["state"]["path"] == "shunt"
    # With FORCe the dialog names what moves; cancelling sends nothing.
    card.locator("input[data-flag=force]").check()
    card.locator("button[data-choice=OPEN]").click()
    dialog = page.locator("#confirm-dialog")
    dialog.wait_for(state="visible")
    lines = page.text_content("#confirm-lines")
    assert "FORCe" in lines and "path=shunt becomes path=none" in lines
    dialog.locator("button[value=cancel]").click()
    page.wait_for_function(
        "() => document.querySelector('article[data-relay=lead] [data-refusal]').textContent"
        ".includes('not confirmed')"
    )
    assert served.get("/api/relays").body["state"]["path"] == "shunt"
    # Confirmed, it goes out and the circuit is redrawn from the answer.
    card.locator("button[data-choice=OPEN]").click()
    dialog.wait_for(state="visible")
    page.click("#confirm-go")
    until(lambda: served.get("/api/relays").body["state"]["path"] == "none", "K1 open")
    page.wait_for_function(
        "() => document.querySelector('#circuit-text').textContent.includes('none')"
    )
    card.locator("input[data-flag=force]").uncheck()
    card.locator("button[data-choice=CLOSe]").click()
    until(lambda: served.get("/api/relays").body["state"]["path"] == "shunt", "K1 closed")


# -- sequences ----------------------------------------------------------------------------


def test_a_sequence_runs_to_done_and_a_question_is_asked_in_its_dialog(
    watched: Watched, served: Served
) -> None:
    page = watched.page
    show(page, "sequences")
    page.click("article[data-sequence=HEALTH] button[data-action=run]")
    page.wait_for_function(
        "() => document.querySelector('#sequence-state').textContent.startsWith('HEALTH done')"
    )
    assert page.locator("#sequence-steps li").count() == 15
    assert "idn" in page.text_content("#sequence-results")
    # A sequence that leaves the DUT without its battery asks first; the safe answer declines.
    page.fill("#seq-null-lead-seconds", "1")
    page.click("article[data-sequence=NULL_LEAD] button[data-action=run]")
    prompt = page.locator("#prompt-dialog")
    prompt.wait_for(state="visible")
    assert "no battery" in page.text_content("#prompt-text")
    prompt.locator("button[value=cancel]").click()
    page.wait_for_function(
        "() => document.querySelector('#sequence-state').textContent"
        ".startsWith('NULL_LEAD declined')"
    )
    assert served.get("/api/relays").body["state"]["path"] == "shunt"


def test_a_question_dismissed_and_asked_again_at_once_is_not_answered(watched: Watched) -> None:
    """A dismissal's close event lands after the next question is shown, and answers nothing.

    The dialog's questions are asked through the page's own module, so the
    page's sequence view may dismiss them as well; that settles a question
    with null, never with an answer.

    On the bench a sequence state without the question (an event older than
    the run's own answer) closed the dialog, and the question shown again at
    once was declined 0.1 s later: ``dialog.close()`` queues its ``close``
    event, which then reached the new question's listener with no choice.
    """
    # The page's own sequence view may close the second question first (a
    # sequence event that asks nothing dismisses the dialog), and then the
    # queued close finds the dialog shut: the guard is not reached that time.
    # So the pair is asked again, up to ten times, until the queued close has
    # met the second question still shown, which it does almost every time.
    result = watched.page.evaluate(
        """async () => {
            const d = await import('/static/js/dialogs.js');
            const dialog = document.querySelector('#prompt-dialog');
            const choices = ['go', 'cancel'];
            const rounds = [];
            for (let round = 0; round < 10; round += 1) {
                const answers = [];
                const note = (name) => (answer) => answers.push([name, answer]);
                d.askPrompt({id: 901, text: 'first', choices}).then(note('first'));
                d.dismissPrompt(null);
                d.askPrompt({id: 902, text: 'second', choices}).then(note('second'));
                await new Promise((resolve) => setTimeout(resolve, 300));
                const seen = {answers: [...answers], open: dialog.open, shown: d.promptShown()};
                d.dismissPrompt(902);
                await new Promise((resolve) => setTimeout(resolve, 100));
                rounds.push({...seen, after: answers, closed: !dialog.open});
                if (seen.open) {
                    break;
                }
            }
            return rounds;
        }"""
    )
    for item in result:
        # What the page may never do is answer a question, the safe answer least of all.
        assert item["answers"][0] == ["first", None]
        assert all(answer is None for _, answer in item["after"]), item["after"]
        assert ["second", None] in item["after"] and item["closed"] is True
    last = result[-1]
    assert last["open"] is True and last["shown"] == 902, result  # the queued close met it shown
    assert [name for name, _ in last["answers"]] == ["first"]  # and answered nothing


# -- settings -----------------------------------------------------------------------------


def test_the_status_bar_takes_the_verdict_the_shunt_section_reads(
    watched: Watched, served: Served
) -> None:
    """An override written, the bar says the verdict Settings shows, not the one read at load.

    On the bench the bar kept ``applied · not quotable`` while Settings read
    ``working_copy_differs`` under the override, until the page was reloaded.
    """
    page = watched.page
    order = {"range": 1, "ohms": 0.1002, "tolerance_ppm": 1000, "note": "browser", "confirm": True}
    bar = "[data-status=calibration]"
    try:
        assert served.post("/api/settings/shunt", order).status == 200
        show(page, "settings")
        page.wait_for_function(
            "() => document.querySelector('#shunt-verdict').textContent"
            ".includes('working_copy_differs')"
        )
        assert page.text_content(bar) == "working_copy_differs"
        assert page.text_content("[data-qualifier=calibration]") == "not quotable"
    finally:
        assert served.post("/api/calibration/restore").status == 200
    page.click("#settings-refresh")
    page.wait_for_function(f"() => document.querySelector('{bar}').textContent === 'applied'")
    assert page.text_content("[data-qualifier=calibration]") == ""


# -- the status bar and the paths ----------------------------------------------------------

#: Every view, by its fragment.
VIEWS = ("live", "sequences", "runs", "relays", "pins", "settings", "health", "console")

#: The widths the status bar is measured at: a desktop's and a phone's.
WIDTHS = (1280, 390)


@contextmanager
def unregistered_serve(
    base: Path, elf: Path, monkeypatch: pytest.MonkeyPatch, work: Path
) -> Iterator[Served]:
    """``serve --sim`` whose registry does not know the instrument, run from *work*.

    :param base: A temporary directory for its registry and runs.
    :param elf: What ``--elf`` names, absolute.
    """
    registry = build_registry(base)
    (registry / "instruments" / f"{SIM_SERIAL}.json").unlink()
    monkeypatch.chdir(work)
    argv = ["--sim", "--data-dir", str(base / "runs"), "--registry", str(registry)]
    with serving([*argv, "--elf", str(elf), "--scpi-passthrough", "0"]) as running:
        yield running


def _heights(browser: Any, served: Served, verdict: str, qualifier: str) -> dict[int, set[float]]:
    """The status bar's height on every view, at each width, the calibration reading *verdict*."""
    heights: dict[int, set[float]] = {}
    for width in WIDTHS:
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        seen = watch(page, served)
        page.goto(served.url)
        _wait_serial(page)
        show(page, "settings")  # Settings reads whether the figures may be quoted
        page.wait_for_function(
            "([verdict, qualifier]) =>"
            " document.querySelector('[data-status=calibration]').textContent === verdict"
            " && document.querySelector('[data-qualifier=calibration]').textContent === qualifier",
            arg=[verdict, qualifier],
        )
        for view in VIEWS:
            show(page, view)
            page.wait_for_timeout(100)
            heights.setdefault(width, set()).add(
                page.evaluate(
                    "() => document.querySelector('.masthead-band').getBoundingClientRect().height"
                )
            )
        COVERAGE.close(context)
        assert seen.unexpected() == [] and seen.foreign() == []
    return heights


def test_the_status_bar_keeps_its_height_on_every_view_whatever_the_calibration_says(
    browser: Any,
    served: Served,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One height a width: every cell keeps a value line and one qualifier line.

    Before, ``unregistered · not quotable`` wrapped the bar onto another row
    on the views shown after Settings had read it.
    """
    applied = _heights(browser, served, "applied", "")
    base = tmp_path_factory.mktemp("unregistered")
    (base / "work").mkdir()
    with unregistered_serve(base, base / "firmware.elf", monkeypatch, base / "work") as other:
        unquotable = _heights(browser, other, "unregistered", "not quotable")
    for width in WIDTHS:
        assert len(applied[width] | unquotable[width]) == 1, (width, applied, unquotable)
    assert max(applied[390]) > max(applied[1280])  # the phone stacks its cells, by width alone


@pytest.mark.parametrize("inside", [False, True], ids=["elsewhere", "in-the-working-directory"])
def test_no_view_and_no_health_names_a_local_path(
    browser: Any,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    inside: bool,
) -> None:
    """``--elf`` given an absolute path: the page and ``GET /api/health`` name no local path.

    The Calibration and health view printed the whole path of the local ELF.
    """
    base = tmp_path_factory.mktemp("paths")
    work = base / "work"
    elf = (work if inside else base / "elsewhere") / "build" / "firmware.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"a build")
    work.mkdir(exist_ok=True)
    local = {str(base), str(base.resolve()), str(Path.home())}
    with unregistered_serve(base, elf, monkeypatch, work) as running:
        health = running.get("/api/health").body
        text = json.dumps(health, ensure_ascii=False)
        assert health["local_elf"]["path"] == "build/firmware.elf"
        assert not any(name in text for name in local)
        assert running.get("/api/capture").body["data_dir"] == f"{base.name}/runs"
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        seen = watch(page, running)
        page.goto(running.url)
        _wait_serial(page)
        texts = []
        for view in VIEWS:
            show(page, view)
            if view == "health":
                page.wait_for_function(
                    "() => document.querySelector('#health-elf').textContent"
                    ".endsWith('build/firmware.elf')"
                )
            texts.append(page.inner_text("body"))
        COVERAGE.close(context)
    assert seen.unexpected() == [] and seen.foreign() == []
    for shown in texts:
        assert not any(name in shown for name in local)
        absolute = [word for word in shown.split() if word.startswith("/") and word != "/"]
        assert [word for word in absolute if not word.startswith("/api/")] == []


# -- every view, in both themes and at both widths ------------------------------------------

#: What a view shows once what it reads of the instrument is in.
READY = {
    "live": "document.querySelector('#chart-readout').textContent.startsWith('Newest at')",
    "sequences": "document.querySelector('#sequence-state').textContent.startsWith('HEALTH done')",
    "runs": "document.querySelectorAll('#runs-body tr').length === 1",
    "relays": "document.querySelector('#relay-state').children.length > 0",
    "pins": "document.querySelectorAll('.pin[data-state]').length === 44",
    "settings": "document.querySelector('#shunt-verdict').textContent.includes('unregistered')"
    " && document.querySelector('#settings-pins-list').children.length > 0"
    " && [...document.querySelectorAll('[data-noise] [data-current]')]"
    ".every((cell) => cell.textContent !== '')",
    "health": "document.querySelector('#health-elf').textContent.endsWith('build/firmware.elf')"
    " && document.querySelector('#health-identity').children.length > 0"
    " && document.querySelector('#health-calibration').children.length > 0",
    "console": "document.querySelector('#console-log li .verdict') !== null",
}

#: Every text of the page: its text nodes, the hidden ones included (a dialog,
#: a hidden view), and the attributes and values a person is shown or read
#: (the images' script reads the same: ``panel_shots.TEXTS``).
TEXTS = panel_shots.TEXTS

#: An absolute path, a home's shorthand or a file's URL in a text. A route of
#: the API (``/api/...``) is no path, nor is the slash of ``n/a`` or of a URL.
#: The one rule the images' script refuses a view by (``panel_images.PATH``).
_PATH = panel_shots.panel_images().PATH


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("/Users/someone/firmware.elf", True),
        ("the build (/private/tmp/x)", True),
        ("elf=/home/someone", True),
        ("/dev/cu.wchusbserial1", True),
        ("~/runs", True),
        ("file:///x", True),
        ("C:\\runs", True),
        ("build/firmware.elf", False),
        ("GET /api/health", False),
        ("http://127.0.0.1:8040/", False),
        ("socket://127.0.0.1:8041", False),
        ("n/a, 1 / 2, nV/√Hz", False),
        ("SENS:PROF? / STAT?", False),
    ],
)
def test_the_path_check_finds_a_path_and_no_route(text: str, path: bool) -> None:
    assert (_PATH.search(text) is not None) is path


def _act_out(page: Any) -> None:
    """Leave on the page what its views show of a session's work: a run opened
    in the chart, the last sequence, a self-test, a scan and a console's reply."""
    show(page, "health")
    page.click("#health-selftest")
    page.wait_for_function(
        "() => document.querySelector('#health-selftest-result').dataset.state === 'done'"
    )
    page.click("#health-scan")
    page.wait_for_function(
        "() => document.querySelector('#health-scan-result').dataset.state === 'done'"
    )
    show(page, "console")
    page.fill("#console-input", "*IDN?")
    page.click("#console-send")
    show(page, "runs")
    page.click("#runs-body button")
    page.wait_for_selector("section#live:not([hidden])")


@pytest.mark.parametrize("width", WIDTHS)
def test_every_view_is_clean_in_both_themes(
    browser: Any,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
) -> None:
    """The test of record: every view, dark and light, at a desktop's and a phone's width.

    On the way round nothing is written to the console as an error and nothing
    thrown, nothing is asked beyond serve, no text of the page (hidden ones,
    titles, names and values included) holds an absolute path, and the status
    bar keeps one height. Serve runs as a person runs it: with an absolute
    ``--elf``, a passthrough, a run recorded and a sequence done, on an
    instrument its registry does not know (the bar's longest reading).
    """
    base = tmp_path_factory.mktemp("views")
    work = base / "work"
    (work / "build").mkdir(parents=True)
    (work / "build" / "firmware.elf").write_bytes(b"a build")
    heights: dict[str, dict[str, float]] = {}
    texts: dict[str, list[str]] = {}
    with unregistered_serve(base, work / "build" / "firmware.elf", monkeypatch, work) as running:
        order = {"profile": "STATE", "channels": ["CURR"], "seconds": 1, "label": "views"}
        assert running.post("/api/capture/start", order).status == 200
        running.capture_idle()
        running.sequence("HEALTH")
        for scheme in ("dark", "light"):
            context = browser.new_context(
                color_scheme=scheme, viewport={"width": width, "height": 900}
            )
            page = context.new_page()
            seen = watch(page, running)
            page.goto(running.url)
            _wait_serial(page)
            dark = page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches")
            assert dark is (scheme == "dark")
            _act_out(page)
            # Settings reads whether the figures may be quoted: the bar says so from then on.
            show(page, "settings")
            page.wait_for_function(
                "() => document.querySelector('[data-qualifier=calibration]').textContent"
                " === 'not quotable'"
            )
            for view in VIEWS:
                show(page, view)
                page.wait_for_function(f"() => {READY[view]}")
                where = f"{view}, {scheme}, {width}"
                texts[where] = page.evaluate(TEXTS)
                heights.setdefault(scheme, {})[view] = page.evaluate(
                    "() => document.querySelector('.masthead-band').getBoundingClientRect().height"
                )
            COVERAGE.close(context)
            assert (scheme, seen.unexpected(), seen.foreign()) == (scheme, [], [])
    assert sorted(heights["dark"]) == sorted(VIEWS)
    assert len({h for shown in heights.values() for h in shown.values()}) == 1, heights
    for where, shown in texts.items():
        assert len(shown) > 100, where
        assert [text for text in shown if _PATH.search(text)] == [], where


# -- runs ---------------------------------------------------------------------------------


def test_a_runs_csv_downloads_and_parses_to_its_samples(
    watched: Watched, served: Served, tmp_path: Path
) -> None:
    started = served.post(
        "/api/capture/start", {"profile": "STATE", "seconds": 1, "label": "browser csv"}
    )
    assert started.status == 200, started.body
    run = started.body["run"]
    served.capture_idle()
    meta = served.get(f"/api/runs/{run}").body
    page = watched.page
    show(page, "runs")
    page.click("#runs-refresh")
    row = page.locator(f"tr[data-run='{run}']")
    row.wait_for()
    assert "browser csv" in row.text_content()
    with page.expect_download() as info:
        row.locator("a.export", has_text="CSV").click()
    download = info.value
    assert download.suggested_filename == f"{run}.csv"
    target = tmp_path / download.suggested_filename
    download.save_as(target)
    with target.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    samples = [r for r in rows if r[0] == "S"]
    assert len(samples) == meta["stream"]["samples"] == int(meta["statistics"]["fields"]["samples"])
    times = [int(r[1]) for r in samples]
    assert times == sorted(times) and all(float(r[2]) > 0 for r in samples)


def test_a_run_still_recording_offers_no_export(watched: Watched, served: Served) -> None:
    """Serve refuses an export of the run it is still writing (409), so the row offers none."""
    started = served.post("/api/capture/start", {"profile": "STATE"})
    assert started.status == 200, started.body
    run = started.body["run"]
    page = watched.page
    show(page, "runs")
    page.click("#runs-refresh")
    row = page.locator(f"tr[data-run='{run}']")
    row.wait_for()
    assert row.locator("a.export").count() == 0
    assert served.post("/api/capture/stop").status == 200
    served.capture_idle()
    page.click("#runs-refresh")
    page.locator(f"tr[data-run='{run}'] a.export").first.wait_for()
    assert page.locator(f"tr[data-run='{run}'] a.export").count() == 3


# -- the chart's arithmetic ---------------------------------------------------------------


def _columns(times: list[float], values: list[float], t0: float, t1: float, n: int) -> list[Any]:
    """Each pixel column's (min, max, count), as the chart must reduce them."""
    out: list[list[float]] = [[math.inf, -math.inf, 0] for _ in range(n)]
    per = n / (t1 - t0)
    for t, v in zip(times, values, strict=True):
        if t0 <= t < t1:
            column = min(n - 1, math.floor((t - t0) * per))
            out[column] = [min(out[column][0], v), max(out[column][1], v), out[column][2] + 1]
    return [None if c[2] == 0 else c for c in out]


def test_the_charts_arithmetic_checked_through_the_page(watched: Watched) -> None:
    times = [float(1_000 + 37 * i) for i in range(400)]
    values = [float(((i * 7919) % 211) - 100) for i in range(400)]
    result = watched.page.evaluate(
        """async ([times, values]) => {
            const m = await import('/static/js/chart-math.js');
            const ring = new m.SampleRing(300, ['i_na']);
            times.forEach((t, i) => ring.push(t, () => values[i]));
            const extrema = new m.ColumnExtrema().reduce(ring, 'i_na', 5000, 15000, 50);
            const columns = [];
            for (let c = 0; c < extrema.columns; c += 1) {
                columns.push(extrema.count[c] === 0 ? null
                    : [extrema.min[c], extrema.max[c], extrema.count[c]]);
            }
            const back = new m.SampleRing(4, ['i_na']);
            [10, 20, 5].forEach((t) => back.push(t, () => t));
            return {
                size: ring.size, evicted: ring.evicted, first: ring.first, last: ring.last,
                lower: ring.lowerBound(5000),
                columns, range: extrema.range(),
                afterGoingBack: [back.size, back.first],
                ticks: m.niceTicks(0, 1, 5), ticksOdd: m.niceTicks(-0.37, 2.9, 6),
                padded: m.paddedRange([2, 4]), flat: m.paddedRange([5, 5]),
                current: m.formatRaw(88600000, m.channelOf('i_na')),
                volts: m.formatRaw(3712000, m.channelOf('VOLT')),
                missed: [m.rowsMissed(null, 5), m.rowsMissed(10, 17), m.rowsMissed(10, 3)],
            };
        }""",
        [times, values],
    )
    kept_t, kept_v = times[100:], values[100:]
    assert (result["size"], result["evicted"]) == (300, 100)
    assert (result["first"], result["last"]) == (kept_t[0], kept_t[-1])
    lower = next(i for i, t in enumerate(kept_t) if t >= 5000)
    assert result["lower"] == lower
    expected = _columns(kept_t, kept_v, 5000, 15000, 50)
    assert result["columns"] == expected
    filled = [c for c in expected if c is not None]
    assert result["range"] == [min(c[0] for c in filled), max(c[1] for c in filled)]
    assert result["afterGoingBack"] == [1, 5]
    assert result["ticks"]["step"] == pytest.approx(0.2)
    assert result["ticks"]["ticks"] == pytest.approx([0, 0.2, 0.4, 0.6, 0.8, 1.0])
    assert result["ticksOdd"]["step"] == pytest.approx(1.0)
    assert result["ticksOdd"]["ticks"] == pytest.approx([0, 1, 2])
    assert result["padded"] == pytest.approx([1.9, 4.1])
    assert result["flat"] == pytest.approx([4.95, 5.05])
    assert (result["current"], result["volts"]) == ("88.60 mA", "3.712 V")
    assert result["missed"] == [0, 7, 0]


def test_the_ring_wraps_and_answers_for_a_column_it_does_not_keep(watched: Watched) -> None:
    result = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            const ring = new m.SampleRing(3, ['i_na']);
            for (let t = 1; t <= 7; t += 1) { ring.push(t, () => 10 * t); }
            const held = [0, 1, 2].map((i) => [ring.timeAt(i), ring.valueAt('i_na', i)]);
            const buckets = new m.SampleRing(3, ['i_na'], 3);
            buckets.pushBucket(0, 10, () => ({n: 1, mean: 1, rms: 1, min: 1, max: 1}));
            const reduced = (r) => {
                const ext = new m.ColumnExtrema().reduce(r, 'v_uv', 0, 10, 2);
                return [Array.from(ext.count.slice(0, 2)), ext.range()];
            };
            return {
                held, evicted: ring.evicted, size: ring.size,
                absent: Number.isNaN(ring.valueAt('v_uv', 0)),
                reduced: [reduced(ring), reduced(buckets)],
            };
        }"""
    )
    assert result["held"] == [[5, 50], [6, 60], [7, 70]]
    assert (result["evicted"], result["size"], result["absent"]) == (4, 3, True)
    assert result["reduced"] == [[[0, 0], None]] * 2


def test_a_batch_without_a_column_or_with_an_empty_cell_gives_no_value(watched: Watched) -> None:
    result = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            const ring = new m.SampleRing(8, ['i_na', 'v_uv', 'p_uw']);
            const empty = [ring.first, ring.last].map(Number.isNaN);
            const pushed = ring.pushBatch(
                {n0: 0, count: 3, t_us: [1, 2, 3], i_na: [10, null, 30], p_uw: 'none'});
            const column = (key) => [0, 1, 2].map((i) => ring.valueAt(key, i))
                .map((v) => (Number.isNaN(v) ? null : v));
            return {empty, pushed, ends: [ring.first, ring.last],
                    columns: ['i_na', 'v_uv', 'p_uw'].map(column)};
        }"""
    )
    assert result["empty"] == [True, True] and result["pushed"] == 3
    assert result["ends"] == [1, 3]
    assert result["columns"] == [[10, None, 30], [None] * 3, [None] * 3]


def test_a_ring_of_no_room_is_refused(watched: Watched) -> None:
    refused = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            return [[0], [1.5], [3, 4], [3, -1], [3, 0.5], [3, 3]].map(([capacity, buckets]) => {
                try {
                    new m.SampleRing(capacity, ['i_na'], buckets);
                    return 'made';
                } catch (error) {
                    return `${error.name}: ${error.message}`;
                }
            });
        }"""
    )
    samples = "RangeError: a ring holds at least one sample"
    buckets = "RangeError: a ring holds 0 to its capacity of buckets"
    assert refused == [samples, samples, buckets, buckets, buckets, "made"]


def test_a_reduction_of_no_span_is_empty_and_an_edge_sample_stays_in_the_view(
    watched: Watched,
) -> None:
    """The second: over [4, 107] in one column and one margin column, the sample
    at 4 is at 0.9999999999999999 columns of the buffers, and it is the view's."""
    result = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            const ring = new m.SampleRing(4, ['i_na']);
            [4, 50].forEach((t) => ring.push(t, () => 10 * t));
            const counts = (...window) => {
                const ext = new m.ColumnExtrema().reduce(ring, 'i_na', ...window);
                return Array.from(ext.count.slice(0, ext.total));
            };
            return {flat: counts(10, 10, 3), back: counts(10, 5, 3), none: counts(0, 10, 0),
                    edge: counts(4, 107, 1, 1)};
        }"""
    )
    assert (4 - (4 - 103)) * (3 / ((107 + 103) - (4 - 103))) < 1  # the rounding the clamp is for
    assert result == {"flat": [0] * 3, "back": [0] * 3, "none": [], "edge": [0, 2, 0]}


def test_a_bucket_that_holds_nothing_or_has_no_length_takes_no_column(watched: Watched) -> None:
    result = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            const ring = new m.SampleRing(8, ['i_na'], 8);
            const held = {n: 4, mean: 7, rms: 7, min: 7, max: 7};
            ring.pushBucket(0, 10, () => null);
            ring.pushBucket(10, 10, () => held);
            ring.pushBucket(20, 30, () => held);
            const ext = new m.ColumnExtrema().reduce(ring, 'i_na', 0, 40, 4);
            const late = new m.ColumnExtrema().reduce(ring, 'i_na', 35, 45, 2);
            return [ext, late].map((e) => Array.from(e.count.slice(0, e.total)));
        }"""
    )
    assert result == [[0, 0, 4, 0], [0, 0]]


def test_the_merge_of_a_run_that_holds_nothing_is_empty(watched: Watched) -> None:
    none = {"t0_us": None, "t1_us": None, "bucket_us": None}
    figures: dict[str, Any] = {k: [] for k in ("t_us", "n", "mean", "rms", "min", "max")}
    merged = _math(
        watched.page,
        "return m.mergeBuckets(args);",
        [{"column": key, **none, **figures} for key in ("i_na", "v_uv")],
    )
    empty: dict[str, Any] = {k: [] for k in ("n", "mean", "rms", "min", "max")}
    assert merged == {"t": [], "end": [], "columns": {"i_na": empty, "v_uv": empty}}


def test_quantities_axes_and_ticks_at_their_edges(watched: Watched) -> None:
    result = _math(
        watched.page,
        """
        const q = m.formatQuantity;
        return {
            quantities: [q(NaN, 'A'), q(25.04, '°C'), q(25.04, '°C', 2), q(0, 'A'), q(-0.0886, 'A'),
                         q(1.5e9, 'W'), q(2e-15, 'A'), q(0.5, 's', 4, 0), q(1234, 's', 3, 0),
                         q(Infinity, 'A')],
            prefixes: [-4, -3, -2, -1, 0, 1, 2].map((p) => q(3 * 1000 ** p, 'A', 1)),
            padded: [m.paddedRange(null), m.paddedRange([0, 0]), m.paddedRange([-5, -5])],
            ticks: [m.niceTicks(3, 3, 5), m.niceTicks(5, 3, 5), m.niceTicks(0, Infinity, 5),
                    m.niceTicks(NaN, NaN, 5), m.niceTicks(0, 1, 0), m.niceTicks(0, 1.9, 1),
                    m.niceTicks(0, 4, 1), m.niceTicks(0, 7, 1)],
            labels: [m.tickLabels([], 0, 'A'), m.tickLabels([5], 0, 'A'),
                     m.tickLabels([20, 20.5], 0.5, '°C'),
                     m.tickLabels([0, 0.0005, 0.001], 0.0005, 'A'),
                     m.tickLabels([-1e-18, 1], 1, 'V')],
        };
        """,
        None,
    )
    assert result["quantities"] == [
        "-",
        "25.04 °C",
        "25 °C",
        "0.000 A",
        "-88.60 mA",
        "1500 MW",  # beyond the prefixes the page uses: the last of them
        "0.002 pA",
        "0.500 s",  # the zero before the point counts as a digit
        "1234 s",
        "Infinity A",
    ]
    assert result["prefixes"] == ["3 pA", "3 nA", "3 µA", "3 mA", "3 A", "3 kA", "3 MA"]
    assert result["padded"][0] == [0, 1]  # nothing in view
    assert result["padded"][1] == [-1, 1]  # a flat zero
    assert result["padded"][2] == pytest.approx([-5.05, -4.95])
    flat, backwards, endless, nothing, *steps = result["ticks"]
    assert flat == {"step": 0, "ticks": [3]}
    assert backwards == {"step": 0, "ticks": [5]}
    assert endless == {"step": 0, "ticks": [0]}
    assert nothing == {"step": 0, "ticks": []}
    assert [t["step"] for t in steps] == [1, 2, 5, 10]  # every factor, and never less than one
    assert result["labels"] == [
        [],
        ["5 A"],
        ["20.0 °C", "20.5 °C"],
        ["0.0 mA", "0.5 mA", "1.0 mA"],
        ["0 V", "1 V"],  # a tick a rounding left beside zero is written as zero
    ]


def test_a_pin_with_no_gpio_is_named_by_its_silk(watched: Watched, served: Served) -> None:
    board = served.get("/api/pins").body["board"]
    live = served.get("/api/pins").body
    named = _pins_math(
        watched.page,
        """
        const map = m.mapFrom(args.map);
        return args.board.rows.filter((row) => row.gpio === null)
            .map((row) => m.pinState(row, map, map, args.board).name);
        """,
        {"board": board, "map": live["live"]},
    )
    rows = [row for row in board["rows"] if row["gpio"] is None]
    assert len(named) == len(rows) > 0
    for row, name in zip(rows, named, strict=True):
        assert name.startswith(f"{row['rail']} {row['terminal']}, {row['silk']}, ")


def test_the_marks_are_bounded_and_given_by_their_window(watched: Watched) -> None:
    result = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            const marks = new m.MarkList(3);
            [10, 20, 30, 40, 50].forEach((t) => marks.add(t, `at ${t}`));
            const kept = marks.items.map((mark) => mark.t);
            const inside = [marks.between(30, 40), marks.between(31, 49), marks.between(60, 70)];
            marks.clear();
            return {kept, inside, cleared: marks.between(0, 100)};
        }"""
    )
    assert result["kept"] == [30, 40, 50]  # the oldest forgotten first
    assert result["inside"] == [  # both edges of the window are in it
        [{"t": 30, "label": "at 30"}, {"t": 40, "label": "at 40"}],
        [{"t": 40, "label": "at 40"}],
        [],
    ]
    assert result["cleared"] == []


def test_the_time_view_zooms_about_its_anchor_and_pans(watched: Watched) -> None:
    result = watched.page.evaluate(
        """async () => {
            const m = await import('/static/js/chart-math.js');
            const of = (act) => {
                const view = new m.TimeView(1000);
                view.setWindow(1000, 5000);
                act(view);
                return [view.t0, view.t1, view.follow];
            };
            return {
                left: of((v) => v.zoom(0.5, 0, 1)), middle: of((v) => v.zoom(0.5, 0.5, 1)),
                right: of((v) => v.zoom(2, 1, 1)), beyond: of((v) => v.zoom(0.5, 7, 1)),
                before: of((v) => v.zoom(0.5, -7, 1)), least: of((v) => v.zoom(0.001, 1)),
                later: of((v) => v.pan(0.25)), earlier: of((v) => v.pan(-0.5)),
                x: of((v) => { v.x = v.xOf(4250, 800); v.t0 = v.x; }),
                empty: of((v) => v.setWindow(3000, NaN)),
                fixed: of((v) => { v.pan(1); v.track(9000); }),
                tracked: of((v) => { v.track(9000); v.track(NaN); }),
            };
        }"""
    )
    assert result["left"] == [4000, 4500, False]
    assert result["middle"] == [4250, 4750, False]
    assert result["right"] == [3000, 5000, False]
    assert result["beyond"] == [4500, 5000, False]  # an anchor outside is the nearer edge
    assert result["before"] == [4000, 4500, False]
    assert result["least"] == [4000, 5000, False]  # 1 ms, the shortest window by default
    assert result["later"] == [4250, 5250, False]
    assert result["earlier"] == [3500, 4500, False]
    assert result["x"][0] == 200  # a quarter of the window, a quarter of the plot
    assert result["empty"] == [0, 3000, True]
    assert result["fixed"] == [5000, 6000, False]  # a window that was moved follows nothing
    assert result["tracked"] == [8000, 9000, True]


# -- the line: column means, the moving average and the window's figures -------------------


def _math(page: Any, body: str, args: Any) -> Any:
    """Run *body* in the page with its module as ``m`` and the arguments as ``args``."""
    return page.evaluate(
        "async (args) => { const m = await import('/static/js/chart-math.js');\n" + body + "\n}",
        args,
    )


#: The page-side half of the moving-mean checks: a ring of the samples, the
#: columns reduced with a margin of half a window, the line, and the buffers.
_LINE = """
    const [times, values, t0, t1, columns, span, margin] = args;
    const ring = new m.SampleRing(times.length + 1, ['i_na']);
    times.forEach((t, i) => ring.push(t, () => (values[i] === null ? NaN : values[i])));
    const ext = new m.ColumnExtrema().reduce(ring, 'i_na', t0, t1, columns, margin);
    const line = m.movingMean(ext, span);
    const plain = (array, n) => Array.from(array.slice(0, n), (v) => (Number.isNaN(v) ? null : v));
    return {
        offset: ext.offset, total: ext.total, columns: ext.columns,
        count: plain(ext.count, ext.total), sum: plain(ext.sum, ext.total),
        square: plain(ext.square, ext.total),
        mean: plain(line.mean, columns), weight: plain(line.count, columns),
        half: line.half, span: line.span, range: ext.range(),
    };
"""


def _column_of(t: float, t0: float, t1: float, columns: int, half: int) -> int | None:
    """The buffer column a sample at *t* falls in, None outside the buffers: the
    view's columns hold the samples of ``[t0, t1]``, its newest included, and the
    margins the rest of ``[lo, hi)``, each stretch clamped to its own columns."""
    width = (t1 - t0) / columns
    lo, hi = t0 - half * width, t1 + half * width
    total = columns + 2 * half
    if not (lo <= t < hi or t == t1):
        return None
    if t0 <= t <= t1:
        first, last = half, half + columns - 1
    elif t < t0:
        first, last = 0, half - 1
    else:
        first, last = half + columns, total - 1
    return min(last, max(first, math.floor((t - lo) * (total / (hi - lo)))))


def _buffers(
    times: Sequence[float],
    values: Sequence[float | None],
    t0: float,
    t1: float,
    columns: int,
    half: int,
) -> tuple[list[int], list[float], list[float]]:
    """Each buffer column's sample count, sum and sum of squares, over the view
    widened by *half* columns (:func:`_column_of`)."""
    total = columns + 2 * half
    count, sums, squares = [0] * total, [0.0] * total, [0.0] * total
    for t, v in zip(times, values, strict=True):
        column = _column_of(t, t0, t1, columns, half)
        if v is not None and column is not None:
            count[column] += 1
            sums[column] += v
            squares[column] += v * v
    return count, sums, squares


def _moving(
    count: list[int], sums: list[float], offset: int, columns: int, span: int
) -> list[float | None]:
    """The line as decision 11 defines it: the count-weighted mean of the whole
    columns within half a window of each, defined on a column that holds data or
    lies in a gap no wider than the window, and nowhere else."""
    half = (span - 1) // 2
    data = [i for i, n in enumerate(count) if n > 0]
    out: list[float | None] = []
    for c in range(columns):
        i = c + offset
        before = [d for d in data if d <= i]
        after = [d for d in data if d >= i]
        if count[i] == 0 and (not before or not after or after[0] - before[-1] > span):
            out.append(None)
            continue
        window = range(max(0, i - half), min(len(count), i + half + 1))
        out.append(sum(sums[j] for j in window) / sum(count[j] for j in window))
    return out


def _line(
    page: Any,
    times: Sequence[float],
    values: Sequence[float | None],
    t0: float,
    t1: float,
    n: int,
    span: int,
) -> tuple[dict[str, Any], list[float | None]]:
    """The page's line and the reference's, for the same samples."""
    half = (span - 1) // 2
    got: dict[str, Any] = _math(page, _LINE, [times, values, t0, t1, n, span, half])
    count, sums, squares = _buffers(times, values, t0, t1, n, half)
    assert got["count"] == count
    assert got["sum"] == pytest.approx(sums, rel=1e-12)
    assert got["square"] == pytest.approx(squares, rel=1e-12)
    # The axis's range is the view's alone, whatever the margins hold.
    seen = [v for t, v in zip(times, values, strict=True) if v is not None and t0 <= t <= t1]
    assert got["range"] == ([min(seen), max(seen)] if seen else None)
    return got, _moving(count, sums, got["offset"], n, span)


def _same(got: list[float | None], expected: list[float | None]) -> None:
    assert [v is None for v in got] == [v is None for v in expected]
    for g, e in zip(got, expected, strict=True):
        if e is not None:
            assert g == pytest.approx(e, rel=1e-12, abs=1e-9)


def test_a_step_moves_the_line_over_exactly_one_window_centered_on_it(watched: Watched) -> None:
    # 10 µs samples, 100 µs columns over [2 ms, 18 ms): 160 columns, the step at
    # 10 ms, which is column 80's left edge; an 11-column window.
    times = [float(t) for t in range(0, 20_000, 10)]
    values = [0.0 if t < 10_000 else 100.0 for t in times]
    got, expected = _line(watched.page, times, values, 2_000, 18_000, 160, 11)
    _same(got["mean"], expected)
    line = got["mean"]
    assert (got["half"], got["span"], got["offset"]) == (5, 11, 5)
    assert all(v == 0 for v in line[:75]) and all(v == 100 for v in line[85:])
    assert all(0 < v < 100 for v in line[75:85])
    assert line[75:85] == sorted(line[75:85])
    # The last column at the old level is 74 (center 74.5), the first at the new
    # one 85 (center 85.5): one window, 11 columns, half before the step, half after.
    last_old, first_new = 74 + 0.5, 85 + 0.5
    assert first_new - last_old == got["span"]
    assert 80 - last_old == first_new - 80 == got["span"] / 2


def test_a_symmetric_pulse_keeps_its_peak_at_its_time(watched: Watched) -> None:
    peak = 9_730.0
    times = [float(t) for t in range(0, 20_000, 10)]
    values = [1000.0 * math.exp(-(((t - peak) / 400.0) ** 2)) for t in times]
    for span in (1, 5, 21, 41):
        got, expected = _line(watched.page, times, values, 2_000, 18_000, 160, span)
        _same(got["mean"], expected)
        line = got["mean"]
        top = max(range(160), key=lambda c: line[c])
        center = 2_000 + (top + 0.5) * 100
        assert abs(center - peak) <= 100, span  # within one column


def test_the_line_holds_what_exists_at_the_data_edges_and_extrapolates_nothing(
    watched: Watched,
) -> None:
    times = [float(t) for t in range(5_000, 12_000, 10)]
    values = [float((i * 37) % 101) for i in range(len(times))]
    got, expected = _line(watched.page, times, values, 2_000, 18_000, 160, 11)
    _same(got["mean"], expected)
    first, last = (5_000 - 2_000) // 100, (12_000 - 10 - 2_000) // 100
    line = got["mean"]
    assert all(v is None for v in line[:first]) and all(v is None for v in line[last + 1 :])
    assert all(v is not None for v in line[first : last + 1])
    # At the first data column the window holds only what is there: columns first..first+5.
    inside = [v for t, v in zip(times, values, strict=True) if t < 2_000 + (first + 6) * 100]
    assert line[first] == pytest.approx(sum(inside) / len(inside), rel=1e-12)


@pytest.mark.parametrize(
    ("empty", "broken"),
    [(3, False), (10, False), (11, True), (30, True)],
    ids=["narrow", "one-less-than-the-window", "as-wide-as-the-window", "wide"],
)
def test_a_gap_wider_than_the_window_breaks_the_line_and_a_narrower_one_is_bridged(
    watched: Watched, empty: int, broken: bool
) -> None:
    # 100 µs columns; data in every column but `empty` columns from column 60.
    times = [float(t) for t in range(0, 20_000, 10) if not 8_000 <= t < 8_000 + empty * 100]
    values = [float(1 + (i % 7)) for i in range(len(times))]
    got, expected = _line(watched.page, times, values, 2_000, 18_000, 160, 11)
    _same(got["mean"], expected)
    gap = got["mean"][60 : 60 + empty]
    if broken:
        assert all(v is None for v in gap)
    else:
        assert all(v is not None for v in gap)
    assert got["mean"][59] is not None and got["mean"][60 + empty] is not None


@pytest.mark.parametrize(
    ("hole", "defined"),
    [((2_000, 2_500), True), ((17_500, 18_000), True), ((1_000, 2_600), False)],
    ids=["at-the-start", "at-the-end", "wider-at-the-start"],
)
def test_a_gap_at_a_view_edge_is_judged_with_the_data_beyond_it(
    watched: Watched, hole: tuple[int, int], defined: bool
) -> None:
    # The margins hold the data beyond the view's edges: a gap of five columns
    # there is bridged by the window of eleven, as inside the view.
    times = [float(t) for t in range(0, 20_000, 10) if not hole[0] <= t < hole[1]]
    values = [1000.0 if t < 2_000 else float(3 + (i % 5)) for i, t in enumerate(times)]
    got, expected = _line(watched.page, times, values, 2_000, 18_000, 160, 11)
    _same(got["mean"], expected)
    edge = got["mean"][:5] if hole[0] <= 2_000 else got["mean"][-5:]
    assert all((v is not None) == defined for v in edge)


@pytest.mark.parametrize(
    ("held", "defined"),
    [((2_000, 17_700), range(157)), ((2_300, 18_000), range(3, 160))],
    ids=["ends-near-the-views-end", "starts-near-the-views-start"],
)
def test_the_line_stops_with_the_data_however_near_the_views_edge(
    watched: Watched, held: tuple[int, int], defined: range
) -> None:
    # Three empty columns between the data and the edge, and nothing beyond it:
    # nearer than the window of eleven, and still no gap, so nothing to bridge.
    times = [float(t) for t in range(*held, 10)]
    values = [float(3 + (i % 5)) for i in range(len(times))]
    got, expected = _line(watched.page, times, values, 2_000, 18_000, 160, 11)
    _same(got["mean"], expected)
    assert [c for c, v in enumerate(got["mean"]) if v is not None] == list(defined)


def test_nan_cells_and_uneven_timestamps_with_dropped_samples_are_means_of_what_is_there(
    watched: Watched,
) -> None:
    times: list[float] = []
    t = 0.0
    for i in range(3_000):
        t += 7 + (i * 13) % 47  # uneven steps, 7 to 53 µs
        if i % 97 < 9:
            continue  # dropped samples
        times.append(t)
    values: list[float | None] = [
        None if i % 11 == 3 else float((i * 7919) % 211 - 100) for i in range(len(times))
    ]
    for span in (1, 3, 9, 31):
        got, expected = _line(watched.page, times, values, 4_000, 80_000, 190, span)
        _same(got["mean"], expected)


def test_the_line_keeps_the_mean_and_a_window_under_a_column_is_its_mean(
    watched: Watched,
) -> None:
    times = [float(t) for t in range(0, 30_000, 13)]
    values = [((i * 7919) % 1_000_003) / 7 for i in range(len(times))]  # inexact sums
    span, columns = 9, 9 * 20
    got = _math(watched.page, _LINE, [times, values, 0, 30_000, columns, 1, 0])
    # The reduction alone (no margin) holds every sample once.
    assert sum(got["count"]) == len(times)
    assert sum(got["sum"]) == pytest.approx(sum(values), rel=1e-12)
    tiles = _math(watched.page, _LINE, [times, values, 0, 30_000, columns, span, 0])
    # Whole windows tile the columns: their centers' count-weighted line is the raw sum.
    centers = range((span - 1) // 2, columns, span)
    total = sum(tiles["mean"][c] * tiles["weight"][c] for c in centers)
    assert total == pytest.approx(sum(values), rel=1e-9)
    assert sum(tiles["weight"][c] for c in centers) == len(times)
    under = _math(
        watched.page,
        """
        const [times, values] = args;
        const ring = new m.SampleRing(times.length, ['i_na']);
        times.forEach((t, i) => ring.push(t, () => values[i]));
        const ext = new m.ColumnExtrema().reduce(ring, 'i_na', 0, 30000, 180);
        const means = [];
        for (const w of [0, 0.4, 1, 1.9]) {
            const line = m.movingMean(ext, w);
            means.push(Array.from(line.mean.slice(0, 180)));
        }
        const column = Array.from(ext.sum.slice(0, 180), (s, c) => s / ext.count[c]);
        return {means, column};
        """,
        [times, values],
    )
    for line in under["means"]:
        assert line == under["column"]  # exactly the column's mean


def test_the_windows_figures_of_a_sine_and_a_constant(watched: Watched) -> None:
    result = _math(
        watched.page,
        """
        const ring = (f) => {
            const r = new m.SampleRing(10000, ['i_na']);
            for (let i = 0; i < 10000; i += 1) { r.push(i * 10, () => f(i)); }
            return r;
        };
        const sine = ring((i) => 1000 * Math.sin((2 * Math.PI * i) / 50));
        const lifted = ring((i) => 300 + 400 * Math.sin((2 * Math.PI * i) / 40));
        const constant = ring(() => -2500);
        const holed = ring((i) => (i % 2 === 0 ? 7 : NaN));
        return {
            sine: m.windowStats(sine, 'i_na', 0, 99990),
            part: m.windowStats(sine, 'i_na', 1000, 1490),
            lifted: m.windowStats(lifted, 'i_na', 0, 99990),
            constant: m.windowStats(constant, 'i_na', 0, 99990),
            holed: m.windowStats(holed, 'i_na', 0, 99990),
            empty: m.windowStats(constant, 'i_na', 200000, 300000),
            absent: m.windowStats(constant, 'v_uv', 0, 99990),
        };
        """,
        None,
    )
    sine = result["sine"]
    assert sine["n"] == 10_000
    assert sine["mean"] == pytest.approx(0, abs=1e-9)
    assert sine["rms"] == pytest.approx(1000 / math.sqrt(2), rel=1e-12)
    wave = [1000 * math.sin(2 * math.pi * i / 50) for i in range(50)]  # 50 samples a period
    assert sine["min"] == pytest.approx(min(wave), rel=1e-12)
    assert sine["max"] == pytest.approx(max(wave), rel=1e-12)
    assert result["part"]["n"] == 50  # 1000 to 1490 µs inclusive: one whole period
    assert result["part"]["rms"] == pytest.approx(1000 / math.sqrt(2), rel=1e-12)
    lifted = result["lifted"]
    assert lifted["mean"] == pytest.approx(300, rel=1e-12)
    assert lifted["rms"] == pytest.approx(math.sqrt(300**2 + 400**2 / 2), rel=1e-12)
    assert result["constant"] == {
        "n": 10_000,
        "mean": -2500,
        "rms": 2500,
        "min": -2500,
        "max": -2500,
    }
    assert result["holed"] == {"n": 5_000, "mean": 7, "rms": 7, "min": 7, "max": 7}
    for none in (result["empty"], result["absent"]):
        assert none["n"] == 0 and all(math.isnan(none[k]) for k in ("mean", "rms", "min", "max"))


def test_a_runs_buckets_reach_the_chart_with_their_weights(watched: Watched) -> None:
    result = _math(
        watched.page,
        """
        const merged = m.mergeBuckets([
            {column: 'i_na', t0_us: 0, t1_us: 349, bucket_us: 100, t_us: [0, 100, 300],
             min: [1, 5, -4], max: [3, 9, 8], mean: [2, 7, 2], rms: [2.5, 7.5, 6], n: [10, 30, 20]},
            {column: 'v_uv', t0_us: 0, t1_us: 349, bucket_us: 100, t_us: [100],
             min: [5], max: [6], mean: [5.5], rms: [5.6], n: [4]},
        ]);
        const ring = new m.SampleRing(64, ['i_na', 'v_uv'], 8);
        for (let i = 0; i < merged.t.length; i += 1) {
            ring.pushBucket(merged.t[i], merged.end[i], (key) => m.bucketOf(merged, key, i));
        }
        const ext = new m.ColumnExtrema().reduce(ring, 'i_na', 0, 400, 8);
        const inside = new m.ColumnExtrema().reduce(ring, 'i_na', 50, 350, 6);
        const across = m.windowStats(ring, 'i_na', 50, 150);
        const plain = (a, n) => Array.from(a.slice(0, n), (v) => (Number.isNaN(v) ? null : v));
        let full = null;
        const stats = m.windowStats(ring, 'i_na', 0, 400);
        const half = m.windowStats(ring, 'i_na', 0, 50);
        const volts = m.windowStats(ring, 'v_uv', 0, 400);
        try {
            for (let k = 5; k < 15; k += 1) { ring.pushBucket(100 * k, 100 * k + 100, () => null); }
        } catch (e) { full = [String(e), ring.size]; }
        const after = new m.SampleRing(4, ['i_na'], 2);
        after.pushBucket(0, 10, () => ({n: 2, mean: 1, rms: 1, min: 1, max: 1}));
        after.push(20, () => 5);
        const sampled = [after.bucketed, after.size, after.first];
        after.pushBucket(30, 40, () => ({n: 1, mean: 3, rms: 3, min: 3, max: 3}));
        return {
            merged: {t: merged.t, end: merged.end, i: merged.columns.i_na, v: merged.columns.v_uv},
            count: plain(ext.count, 8), sum: plain(ext.sum, 8), square: plain(ext.square, 8),
            min: plain(ext.min, 8), max: plain(ext.max, 8),
            stats, half, volts, mean: ring.valueAt('i_na', 1), full, across,
            inside: plain(inside.count, 6),
            afterSample: sampled,
            afterBucket: [after.bucketed, after.size, after.first],
        };
        """,
        None,
    )
    merged = result["merged"]
    assert merged["t"] == [0, 100, 300] and merged["end"] == [100, 200, 350]  # the last ends at t1
    assert merged["i"]["n"] == [10, 30, 20] and merged["i"]["mean"] == [2, 7, 2]
    assert merged["v"]["n"] == [0, 4, 0] and merged["v"]["mean"][1] == 5.5
    # Each 100 µs bucket spreads over the two 50 µs columns it covers, half its
    # weight in each; the last, cut at the window's end, covers one column.
    assert result["count"] == [5, 5, 15, 15, 0, 0, 20, 0]
    assert result["sum"] == [10, 10, 105, 105, 0, 0, 40, 0]
    assert result["square"] == pytest.approx([31.25, 31.25, 843.75, 843.75, 0, 0, 720, 0])
    assert result["min"][:4] == [1, 1, 5, 5] and result["max"][6] == 8
    stats = result["stats"]
    assert stats["n"] == 60 and stats["mean"] == pytest.approx((20 + 210 + 40) / 60)
    assert stats["rms"] == pytest.approx(math.sqrt((10 * 6.25 + 30 * 56.25 + 20 * 36) / 60))
    assert (stats["min"], stats["max"]) == (-4, 9)
    assert result["half"]["n"] == 5 and result["half"]["mean"] == 2
    assert result["volts"]["n"] == 4 and result["volts"]["mean"] == 5.5
    assert result["mean"] == 7
    # A bucket that starts before the view still gives the view its share.
    assert result["inside"] == [5, 15, 15, 0, 0, 20]
    assert result["across"]["n"] == 20 and result["across"]["mean"] == (5 * 2 + 15 * 7) / 20
    assert "at most 8 buckets" in result["full"][0] and result["full"][1] == 8
    assert result["afterSample"] == [False, 1, 20]  # a sample after buckets starts again
    assert result["afterBucket"] == [True, 1, 30]  # and a bucket after samples


def test_an_opened_run_is_drawn_from_its_weighted_buckets(watched: Watched, served: Served) -> None:
    """Open, in the Runs view, puts the run's buckets in the chart: the readout's
    newest value is the last bucket's mean, and its range in view the buckets' extremes."""
    started = served.post(
        "/api/capture/start",
        {"profile": "STATE", "channels": ["CURR"], "seconds": 1, "label": "buckets"},
    )
    assert started.status == 200, started.body
    run = started.body["run"]
    served.capture_idle()
    body = served.get(f"/api/runs/{run}/decimated?column=i_na&buckets=4000").body
    page = watched.page
    show(page, "runs")
    page.click("#runs-refresh")
    page.locator(f"tr[data-run='{run}'] button", has_text="Open").click()
    until(
        lambda: "buckets of mean, minimum and maximum" in page.text_content("#capture-note"),
        "the run's buckets",
    )
    assert f"as {len(body['t_us'])} buckets" in page.text_content("#capture-note")
    expected = page.evaluate(
        """async ([last, lo, hi]) => {
            const m = await import('/static/js/chart-math.js');
            const c = m.channelOf('i_na');
            return [m.formatRaw(last, c), m.formatRaw(lo, c), m.formatRaw(hi, c)];
        }""",
        [body["mean"][-1], min(body["min"]), max(body["max"])],
    )
    until(lambda: page.text_content("#chart-readout").startswith("Newest at"), "the readout")
    readout = page.text_content("#chart-readout")
    assert f"Current {expected[0]} (in view mean " in readout
    assert f", min {expected[1]}, max {expected[2]})" in readout
    # The decimation is named, and a zoom stops at one bucket per pixel column.
    width = page.evaluate(
        "async (us) => (await import('/static/js/chart-math.js')).formatDuration(us)",
        body["bucket_us"],
    )
    # The caption is the run's once the chart has drawn it, on an animation
    # frame the gate lets through after the note; until then it is the live view's.
    until(
        lambda: f"run buckets {width}" in (page.get_attribute("#chart", "data-caption") or ""),
        "the run's buckets in the caption",
    )
    page.focus("#chart")
    for _ in range(40):
        page.keyboard.press("+")
    page.wait_for_timeout(200)
    assert f"run buckets {width}" in (page.get_attribute("#chart", "data-caption") or "")


def test_the_smoothing_choices_and_the_caption_that_names_the_window(watched: Watched) -> None:
    result = _math(
        watched.page,
        """
        const cases = [[0, 4600], [1000, 4600], [6000, 4600], [100000, 1000], [100000, 4600],
                       [1000000, 4000], [10000, 3000], [0, 0]];
        return {
            choices: m.SMOOTHING.map((s) => [s.label, s.us]),
            windows: cases.map(([choice, column]) => m.smoothingWindow(choice, column)),
            durations: [4600, 100000, 1000000, 10, 1234567, 0.5].map(m.formatDuration),
        };
        """,
        None,
    )
    assert result["choices"] == [
        ["Off", 0],
        ["1 ms", 1_000],
        ["10 ms", 10_000],
        ["100 ms", 100_000],
        ["1 s", 1_000_000],
    ]
    w = result["windows"]
    assert (w[0]["span"], w[0]["caption"]) == (1, "mean per pixel, 4.6 ms")
    assert (w[1]["span"], w[1]["caption"]) == (1, "mean per pixel, 4.6 ms (1 ms asked)")
    assert (w[2]["span"], w[2]["caption"]) == (1, "mean per pixel, 4.6 ms (6 ms asked)")
    assert (w[3]["half"], w[3]["span"], w[3]["us"]) == (50, 101, 101_000)
    assert w[3]["caption"] == "moving average 101 ms (100 ms asked)"
    assert (w[4]["span"], w[4]["us"]) == (21, pytest.approx(96_600))
    assert w[4]["caption"] == "moving average 96.6 ms (100 ms asked)"
    assert (w[5]["span"], w[5]["caption"]) == (251, "moving average 1 s")  # 1.004 s: within 0.5 %
    assert (w[6]["span"], w[6]["caption"]) == (3, "moving average 9 ms (10 ms asked)")
    assert (w[7]["span"], w[7]["caption"]) == (1, "mean per pixel")
    assert result["durations"] == ["4.6 ms", "100 ms", "1 s", "10 µs", "1.23 s", "500 ns"]


def test_the_views_columns_hold_every_sample_of_the_window_and_give_its_figures(
    watched: Watched,
) -> None:
    """The view's columns hold the samples of ``[t0, t1]``, both edges included
    (the newest sample of a following view is in it), whatever the margin, and
    their figures are ``windowStats()``'s over the samples and over buckets."""
    result = _math(
        watched.page,
        """
        const ring = new m.SampleRing(100, ['i_na']);
        for (let i = 0; i < 100; i += 1) { ring.push(i * 100, () => (i % 9) * 11 - 30); }
        const views = [0, 1, 7].map((margin) => {
            const ext = new m.ColumnExtrema().reduce(ring, 'i_na', 2000, 7000, 10, margin);
            let held = 0;
            for (let c = ext.offset; c < ext.offset + ext.columns; c += 1) { held += ext.count[c]; }
            return {held, stats: ext.viewStats(), first: ext.count[ext.offset],
                    last: ext.count[ext.offset + ext.columns - 1]};
        });
        const buckets = new m.SampleRing(8, ['i_na'], 8);
        [[0, 100, 4, 1, 1.5, 0, 2], [100, 250, 6, 3, 3.2, 2, 4], [250, 400, 2, -1, 1, -2, 0]]
            .forEach(([t, end, n, mean, rms, min, max]) =>
                buckets.pushBucket(t, end, () => ({n, mean, rms, min, max})));
        const bext = new m.ColumnExtrema().reduce(buckets, 'i_na', 50, 300, 7, 2);
        const empty = new m.ColumnExtrema().reduce(ring, 'i_na', 50000, 60000, 10, 3);
        return {
            views, window: m.windowStats(ring, 'i_na', 2000, 7000),
            upper: [2000, 2050, -1, 1e9].map((t) => ring.upperBound(t)),
            buckets: bext.viewStats(), bucketWindow: m.windowStats(buckets, 'i_na', 50, 300),
            empty: empty.viewStats(),
        };
        """,
        None,
    )
    assert result["upper"] == [21, 21, 0, 100]
    window = result["window"]
    assert window["n"] == 51  # 2000 to 7000 µs inclusive, every 100 µs
    for view in result["views"]:
        assert view["held"] == 51
        assert view["first"] == 5 and view["last"] == 6  # 2000 starts the first; 7000 ends the last
        assert view["stats"]["n"] == window["n"]
        for key in ("mean", "rms", "min", "max"):
            assert view["stats"][key] == pytest.approx(window[key], rel=1e-12)
    for key in ("n", "mean", "rms", "min", "max"):
        assert result["buckets"][key] == pytest.approx(result["bucketWindow"][key], rel=1e-12)
    assert result["empty"]["n"] == 0 and math.isnan(result["empty"]["mean"])


# -- the chart as drawn: the line, the band, the smoothing and the readout ---------------------

#: The injected stream: 12 s of 500 µs samples of current, a square wave of
#: 100 ms between 2 and 6 mA with every other sample 1 mA above it and the next
#: 1 mA below. Each pixel column holds about eighteen samples, so its band is a
#: plateau's 1 to 3 mA or 5 to 7 mA and its mean the plateau's level; a window
#: of 100 ms holds a whole period, so the smoothed line is near 4 mA everywhere.
SIGNAL_T = [1_000_000 + 500 * k for k in range(24_000)]
SIGNAL_V = [
    (6_000_000 if ((t - 1_000_000) // 50_000) % 2 else 2_000_000)
    + (1_000_000 if k % 2 else -1_000_000)
    for k, t in enumerate(SIGNAL_T)
]
#: The live view's default window, microseconds (live.js WINDOWS[1]).
WINDOW_US = 10_000_000


@contextmanager
def injected(
    browser: Any,
    served: Served,
    scheme: str = "dark",
    times: list[int] = SIGNAL_T,
    values: list[int] = SIGNAL_V,
) -> Iterator[Watched]:
    """A page whose live events carry one ``samples`` batch of *times* and
    *values* (current, nA) and nothing else, in the color *scheme*; it must end
    with no error and no foreign request."""
    context = browser.new_context(color_scheme=scheme, viewport={"width": 1280, "height": 900})
    page = context.new_page()
    seen = watch(page, served)
    batch = {"n0": 0, "count": len(times), "t_us": times, "i_na": values}
    body = f"retry: 3600000\n\nevent: samples\ndata: {json.dumps(batch)}\n\n"
    page.route(
        "**/api/events*",
        lambda route: route.fulfill(
            status=200, headers={"Content-Type": "text/event-stream"}, body=body
        ),
    )
    try:
        page.goto(served.url)
        page.wait_for_function(
            "() => document.querySelector('#chart-readout').textContent.startsWith('Newest at')"
        )
        yield seen
    finally:
        COVERAGE.close(context)
    assert seen.unexpected() == []
    assert seen.foreign() == []


#: The page-side half of the pixel checks: one device column of the canvas,
#: each row's color and opacity, the lane's box and the tokens of the theme.
_PIXELS = """
async (x) => {
    const chart = await import('/static/js/chart.js');
    const canvas = document.querySelector('#chart');
    const ratio = window.devicePixelRatio || 1;
    const data = canvas.getContext('2d')
        .getImageData(Math.floor(x * ratio), 0, 1, canvas.height).data;
    const rows = [];
    for (let i = 0; i < data.length; i += 4) { rows.push(Array.from(data.slice(i, i + 4))); }
    const style = getComputedStyle(canvas);
    const token = (name) => style.getPropertyValue(name).trim();
    return {rows, ratio, lane: chart.laneBox(canvas.clientHeight, 1, 0), plotX: chart.AXIS_WIDTH,
            width: canvas.clientWidth, caption: canvas.dataset.caption,
            ink: token('--ink'), band: token('--trace-band'), bg: token('--bg'),
            accent: token('--accent')};
}
"""


def _rgba(text: str) -> tuple[float, float, float, float]:
    """A token's color: ``#rrggbb``, ``rgb(r g b / a%)`` or, computed, ``rgb(r, g, b)``."""
    if text.startswith("#"):
        return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), 1.0)
    parts = text.removeprefix("rgb(").removesuffix(")").replace("/", " ").replace(",", " ").split()
    alpha = float(parts[3].removesuffix("%")) / 100 if len(parts) > 3 else 1.0
    return (float(parts[0]), float(parts[1]), float(parts[2]), alpha)


def _over(top: tuple[float, ...], ground: tuple[float, ...]) -> tuple[float, float, float]:
    """*top* (with its opacity) laid over the opaque *ground*."""
    a = top[3]
    r, g, b = (a * t + (1 - a) * u for t, u in zip(top[:3], ground[:3], strict=True))
    return (r, g, b)


def _contrast(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """The WCAG contrast ratio of two opaque colors."""

    def luminance(c: tuple[float, ...]) -> float:
        lin = [
            x / 255 / 12.92 if x / 255 <= 0.04045 else ((x / 255 + 0.055) / 1.055) ** 2.4
            for x in c[:3]
        ]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@dataclass
class Column:
    """One device column of the chart: what each row holds, and the value scale."""

    shot: dict[str, Any]
    lo: float
    hi: float

    #: Opacity at or above which a pixel is the line, and at or above which it is drawn.
    LINE = 160
    DRAWN = 30

    def row(self, value: float) -> int:
        """The device row of a value."""
        lane = self.shot["lane"]
        y: float = lane["top"] + lane["height"] * (1 - (value - self.lo) / (self.hi - self.lo))
        ratio: float = self.shot["ratio"]
        return math.floor(y * ratio)

    def value(self, row: float) -> float:
        """The value at a device row's center."""
        lane = self.shot["lane"]
        share: float = ((row + 0.5) / self.shot["ratio"] - lane["top"]) / lane["height"]
        return self.lo + (1 - share) * (self.hi - self.lo)

    def inside(self) -> range:
        """The rows inside the lane, its outline left out."""
        lane, ratio = self.shot["lane"], self.shot["ratio"]
        return range(
            math.ceil(lane["top"] * ratio) + 2,
            math.floor((lane["top"] + lane["height"]) * ratio) - 2,
        )

    def line(self) -> list[int]:
        return [r for r in self.inside() if self.shot["rows"][r][3] >= self.LINE]

    def drawn(self) -> list[int]:
        return [r for r in self.inside() if self.shot["rows"][r][3] >= self.DRAWN]


def _plateau_column(shot: dict[str, Any], level: int) -> tuple[float, int]:
    """A pixel column well inside a plateau at *level*, near 40 % of the plot:
    its CSS x, and the plot's columns."""
    ratio = shot["ratio"]
    columns = math.floor((shot["width"] - shot["plotX"] - 8) * ratio)
    t1 = SIGNAL_T[-1]
    t0 = t1 - WINDOW_US
    width = WINDOW_US / columns
    high = level > 4_000_000
    for c in range(int(0.4 * columns), columns):
        start, end = t0 + (c - 1) * width, t0 + (c + 2) * width
        plateau = {((t - 1_000_000) // 50_000) % 2 == 1 for t in (start, end)}
        if plateau == {high} and (start - 1_000_000) // 50_000 == (end - 1_000_000) // 50_000:
            return shot["plotX"] + (c + 0.5) / ratio, columns
    raise AssertionError("no plateau column")


def _choose_smoothing(page: Any, label: str) -> None:
    before = page.get_attribute("#chart", "data-caption")
    page.click(f"#chart-smoothing label:has-text('{label}')")
    until(lambda: page.get_attribute("#chart", "data-caption") != before, "a new caption")


def _caption(page: Any, choice_us: int, columns: int) -> str:
    caption: str = _math(
        page,
        "return m.smoothingWindow(args[0], args[1]).caption;",
        [choice_us, WINDOW_US / columns],
    )
    return caption


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_chart_draws_the_mean_over_a_faint_band_and_smoothing_moves_only_the_line(
    browser: Any, served: Served, scheme: str
) -> None:
    """By default the line is each pixel column's mean over the band of its
    minimum to maximum; 100 ms of smoothing moves the line and leaves the band
    pixel for pixel, and the caption names the window. In both themes the line
    stands out of the band and the band, faint, out of the ground (measured on
    the canvas's own pixels)."""
    with injected(browser, served, scheme) as seen:
        page = seen.page
        shot = page.evaluate(_PIXELS, 0)
        x, columns = _plateau_column(shot, 6_000_000)
        off = Column(
            page.evaluate(_PIXELS, x), 700_000, 7_300_000
        )  # 1 to 7 mA padded by a twentieth
        assert page.get_attribute("#chart", "data-caption") == _caption(page, 0, columns)
        assert off.shot["caption"].startswith("mean per pixel, ")
        assert f"line {off.shot['caption']}" in page.text_content("#chart-readout")

        line = off.line()
        assert line, "no line in the column"
        assert sum(map(off.value, line)) / len(line) == pytest.approx(6_000_000, abs=200_000)
        drawn = off.drawn()
        assert (
            abs(min(drawn) - off.row(7_000_000)) <= 2 and abs(max(drawn) - off.row(5_000_000)) <= 2
        )
        assert all(
            off.shot["rows"][r][3] < Column.DRAWN
            for r in range(off.row(4_500_000), max(off.inside()))
        )

        ground = _rgba(off.shot["bg"])
        band_px = off.shot["rows"][off.row(5_400_000)]
        band = _over((*band_px[:3], band_px[3] / 255), ground)
        ink = tuple(off.shot["rows"][max(line, key=lambda r: off.shot["rows"][r][3])][:3])
        assert _contrast(ink, band) >= 7, (scheme, ink, band)
        assert 1.4 <= _contrast(band, ground) <= 2.0, (scheme, band, ground)
        assert _contrast(_rgba(off.shot["accent"]), band) >= 3  # the crosshair's dot on the band
        assert band == pytest.approx(_over(_rgba(off.shot["band"]), ground), abs=2)

        _choose_smoothing(page, "100 ms")
        smoothed = Column(page.evaluate(_PIXELS, x), 700_000, 7_300_000)
        assert smoothed.shot["caption"] == _caption(page, 100_000, columns)
        assert smoothed.shot["caption"].startswith("moving average ")
        moved = smoothed.line()
        assert moved, "no line in the column"
        assert sum(map(smoothed.value, moved)) / len(moved) == pytest.approx(4_000_000, abs=350_000)
        near = {r + d for r in line + moved for d in range(-3, 4)}
        band_rows = [r for r in off.inside() if r not in near]
        assert [off.shot["rows"][r] for r in band_rows] == [
            smoothed.shot["rows"][r] for r in band_rows
        ]
        assert all(smoothed.shot["rows"][r][3] < Column.LINE for r in line)


def _figures(page: Any, values: Sequence[float], newest: float | None = None) -> dict[str, str]:
    """The readout's text of a window's figures, from the samples themselves."""
    n = len(values)
    mean = sum(values) / n
    rms = math.sqrt(sum(v * v for v in values) / n)
    figures: dict[str, str] = _math(
        page,
        """
        const c = m.channelOf('i_na');
        const f = (v) => (v === null ? null : m.formatRaw(v, c));
        const [mean, rms, min, max, newest] = args.map(f);
        return {mean, rms, min, max, newest};
        """,
        [mean, rms, min(values), max(values), newest],
    )
    return figures


def test_the_readout_gives_the_windows_figures_of_the_samples_and_the_line_at_the_cursor(
    browser: Any, served: Served
) -> None:
    """The readout's mean, RMS, minimum and maximum are those of the samples in
    view, computed here from the samples injected, whatever the smoothing; at
    the crosshair it gives the line's value, the window it averages, and that
    column's minimum and maximum."""
    with injected(browser, served) as seen:
        page = seen.page
        t1 = SIGNAL_T[-1]
        t0 = t1 - WINDOW_US
        inside = [v for t, v in zip(SIGNAL_T, SIGNAL_V, strict=True) if t0 <= t <= t1]
        assert (
            0 < len(inside) < len(SIGNAL_V) and SIGNAL_T.count(t0) == 1
        )  # one sample on each edge
        want = _figures(page, inside, SIGNAL_V[-1])
        expected = (
            f"Current {want['newest']} (in view mean {want['mean']}, RMS {want['rms']},"
            f" min {want['min']}, max {want['max']})"
        )
        assert expected in page.text_content("#chart-readout")
        _choose_smoothing(page, "100 ms")
        until(
            lambda: "moving average" in page.text_content("#chart-readout"), "the smoothed readout"
        )
        assert expected in page.text_content("#chart-readout")

        _choose_smoothing(page, "Off")
        shot = page.evaluate(_PIXELS, 0)
        columns = math.floor((shot["width"] - shot["plotX"] - 8) * shot["ratio"])
        column = 400
        held = [
            v
            for t, v in zip(SIGNAL_T, SIGNAL_V, strict=True)
            if _column_of(t, t0, t1, columns, 0) == column
        ]
        assert len(held) > 10
        at = _figures(page, held)
        box = page.locator("#chart").bounding_box()
        page.mouse.move(box["x"] + shot["plotX"] + column / shot["ratio"] + 0.25, box["y"] + 100)
        until(
            lambda: page.text_content("#chart-readout").startswith("Crosshair at"),
            "the cursor's readout",
        )
        readout = page.text_content("#chart-readout")
        assert f"line {_caption(page, 0, columns)}:" in readout
        assert (
            f"Current {at['mean']} (column {at['min']} to {at['max']}, in view mean {want['mean']},"
            in readout
        )


def test_each_chart_remembers_its_own_smoothing_in_this_browser(
    watched: Watched, served: Served
) -> None:
    """Off by default; the live chart's choice and a run's history chart's are
    each their own, and both survive a reload."""
    page = watched.page
    checked = "() => document.querySelector('#chart-smoothing input:checked')?.value"
    assert page.evaluate(checked) == "0"
    assert (page.get_attribute("#chart", "data-caption") or "").startswith("mean per pixel")
    page.click("#chart-smoothing label:has-text('100 ms')")
    page.reload()
    page.wait_for_selector("#chart-smoothing input:checked")
    assert page.evaluate(checked) == "100000"

    started = served.post(
        "/api/capture/start",
        {"profile": "STATE", "channels": ["CURR"], "seconds": 1, "label": "smoothing"},
    )
    assert started.status == 200, started.body
    run = started.body["run"]
    served.capture_idle()
    show(page, "runs")
    page.click("#runs-refresh")
    page.locator(f"tr[data-run='{run}'] button", has_text="Open").click()
    until(lambda: "buckets of mean" in page.text_content("#capture-note"), "the run's buckets")
    assert page.evaluate(checked) == "0"  # the history chart's own, off by default
    page.click("#chart-smoothing label:has-text('1 s')")
    until(
        lambda: "1 s" in (page.get_attribute("#chart", "data-caption") or ""),
        "the history's caption",
    )
    page.reload()
    page.wait_for_selector("#chart-smoothing input:checked")
    assert page.evaluate(checked) == "100000"  # the live chart's, untouched
    show(page, "runs")
    page.click("#runs-refresh")
    page.locator(f"tr[data-run='{run}'] button", has_text="Open").click()
    until(lambda: page.evaluate(checked) == "1000000", "the history's own choice again")
    stored = page.evaluate("() => Object.entries(localStorage).sort()")
    assert stored == [
        ["kleidos-meter.smoothing.history", "1000000"],
        ["kleidos-meter.smoothing.live", "100000"],
    ]


def test_the_live_readout_is_the_newest_sample_of_the_run(watched: Watched, served: Served) -> None:
    page = watched.page
    page.select_option("#capture-profile", "STATe")
    page.fill("#capture-seconds", "1")
    page.click("#capture-start")
    page.wait_for_selector("#statistics:not([hidden])")
    served.capture_idle()
    run = served.get("/api/capture").body["last"]["id"]
    rows = served.get(f"/api/runs/{run}/export?format=csv").raw.decode().splitlines()
    newest = [r for r in rows if r.startswith("S,")][-1].split(",")
    expected = page.evaluate(
        """async (milli) => {
            const m = await import('/static/js/chart-math.js');
            return m.formatRaw(milli, m.channelOf('i_na'));
        }""",
        round(float(newest[2]) * 1000),
    )
    until(lambda: page.text_content("#chart-readout").startswith("Newest at"), "the readout")
    assert f"Current {expected}" in page.text_content("#chart-readout")


def test_the_watch_tells_a_refusal_asked_for_from_a_fault_and_a_foreign_request() -> None:
    seen = Watched(page=None, origin="http://127.0.0.1:1/")
    refused = "console: Failed to load resource: the server responded with a status of 409 (x)"
    seen.errors += [refused, "exception: boom"]
    assert seen.unexpected() == [refused, "exception: boom"]
    seen.refusals.add(409)
    assert seen.unexpected() == ["exception: boom"]
    seen.requests += [
        "http://127.0.0.1:1/api/status",
        "data:image/svg+xml,x",
        "https://cdn.example/x.js",
    ]
    assert seen.foreign() == ["https://cdn.example/x.js"]


# -- the events stream after a refusal ------------------------------------------------------


def test_the_page_opens_the_events_again_after_serve_refuses_them(
    browser: Any, served: Served
) -> None:
    """A 503 (serve already serves as many streams as it may) closes an
    EventSource for good; the page opens a new one after a wait and runs again."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(PAGE_WAIT)
    refused: list[str] = []

    def refuse_twice(route: Any) -> None:
        if len(refused) < 2:
            refused.append(route.request.url)
            route.fulfill(
                status=503,
                headers={"Retry-After": "5", "Content-Type": "application/json"},
                body='{"error": "busy", "detail": "8 streams are open already"}',
            )
        else:
            route.continue_()

    asked: list[str] = []
    page.on("request", lambda request: asked.append(request.url))
    page.route("**/api/events*", refuse_twice)
    try:
        page.goto(served.url)
        deadline = time.monotonic() + PAGE_WAIT / 1000
        # The route's handler runs while Playwright waits, not while this sleeps.
        while sum("/api/events" in url for url in asked) < 3 and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert len(refused) == 2
        assert sum("/api/events" in url for url in asked) >= 3, asked
        page.wait_for_function(
            "() => document.querySelector('[data-status=link]').textContent === 'up'"
        )
        # The new stream carries the live samples: a capture reaches the readout.
        page.select_option("#capture-profile", "STATe")
        page.fill("#capture-seconds", "1")
        page.click("#capture-start")
        page.wait_for_function(
            "() => document.querySelector('#chart-readout').textContent.startsWith('Newest at')"
        )
        served.capture_idle()
    finally:
        COVERAGE.close(context)


# -- the readout is announced on request -----------------------------------------------------


def test_the_readout_is_announced_only_when_a_person_asks(watched: Watched, served: Served) -> None:
    """The readout changes once a second while the chart follows a capture: a
    live region would announce every change for the whole capture (WCAG 2.2.2),
    so it is silent then and polite only for the update an interaction asks for."""
    page = watched.page
    readout = "#chart-readout"
    assert page.get_attribute(readout, "aria-live") == "off"
    page.select_option("#capture-profile", "STATe")
    page.fill("#capture-seconds", "2")
    page.click("#capture-start")
    page.wait_for_function(
        "() => document.querySelector('#chart-readout').textContent.startsWith('Newest at')"
    )
    first = page.text_content(readout)
    until(lambda: page.text_content(readout) != first, "a second periodic readout")
    assert page.get_attribute(readout, "aria-live") == "off"
    served.capture_idle()
    page.focus("#chart")
    page.keyboard.press("ArrowLeft")  # a pan: a person asked for the update
    until(lambda: page.get_attribute(readout, "aria-live") == "polite", "an announced readout")
    page.keyboard.press("f")


# -- the labels the page takes are the labels serve takes ------------------------------------


def _route_argument(served: Served, path: str, name: str) -> dict[str, Any]:
    route = next(r for r in served.get("/api/schema").body["routes"] if r["path"] == path)
    return next(a for a in route["takes"] if a["name"] == name)


#: Labels on either side of the limits: lengths, a space, each excluded character,
#: a character beyond ASCII.
LABELS = (
    "a",
    "run-1",
    "A" * 15,
    "A" * 16,
    "A" * 80,
    "A" * 81,
    "two words",
    "x,y",
    "x;y",
    "x=y",
    'x"y',
    "x'y",
    "café",
    "~!#$%&()*+-./:<>?@[]^_`{|}",
)


def test_the_label_fields_take_what_serve_takes(watched: Watched, served: Served) -> None:
    """The page's fields hold the limits /api/schema declares, and judge every
    sample label as the recorder does, so a mark is never refused after the click."""
    from kleidos_meter.panel.recorder import mark_label_ok, run_label_ok

    page = watched.page
    fields = (
        ("#capture-label", "/api/capture/start", run_label_ok),
        ("#mark-label", "/api/capture/mark", mark_label_ok),
    )
    for selector, path, server_ok in fields:
        argument = _route_argument(served, path, "label")
        assert page.eval_on_selector(selector, "e => e.maxLength") == argument["max_length"]
        assert page.get_attribute(selector, "pattern") == argument["pattern"]
        for label in LABELS:
            fits = page.eval_on_selector(
                selector,
                """(e, value) => {
                    e.value = value;
                    const ok = value.length <= e.maxLength && !e.validity.patternMismatch;
                    e.value = "";
                    return ok;
                }""",
                label,
            )
            assert fits == server_ok(label), (selector, label)


# -- the view's exports: a PNG of the canvas and a CSV of what it plots ----------------------


def _comments_and_rows(text: str) -> tuple[dict[str, str], list[str], list[list[str]]]:
    """A view CSV's ``# key=value`` lines, its header and its rows."""
    lines = text.splitlines()
    comments = [line for line in lines if line.startswith("#")]
    assert lines[: len(comments)] == comments, "the comment lines come first"
    pairs = dict(line.removeprefix("# ").split("=", 1) for line in comments)
    assert len(pairs) == len(comments), "each key once"
    table = list(csv.reader(lines[len(comments) :]))
    return pairs, table[0], table[1:]


def _download(page: Any, selector: str, tmp_path: Path) -> tuple[str, bytes]:
    """Click *selector* and return the file it downloads: its name and bytes."""
    with page.expect_download() as info:
        page.click(selector)
    download = info.value
    target = tmp_path / download.suggested_filename
    download.save_as(target)
    return download.suggested_filename, target.read_bytes()


def _asked_since(seen: Watched, before: int) -> list[str]:
    """What the page asked for after its *before*-th request, its own ``data:`` URLs left out."""
    return [url for url in seen.requests[before:] if not url.startswith("data:")]


def _cell(text: str) -> float | None:
    return None if text == "" else float(text)


def _wait_serial(page: Any) -> None:
    page.wait_for_function(
        "serial => document.querySelector('[data-status=serial]').textContent.includes(serial)",
        arg=SIM_SERIAL,
    )


@pytest.mark.parametrize(("label", "choice_us"), [("Off", 0), ("100 ms", 100_000)])
def test_the_view_csv_is_the_line_drawn_and_names_its_filter(
    browser: Any, served: Served, tmp_path: Path, label: str, choice_us: int
) -> None:
    """The view's CSV holds, per pixel column, the line the chart draws (the
    column's mean, or the moving average), the band's minimum and maximum and
    the samples the line averages, all computed here again from the samples
    injected; its header names the filter, the window, the column and the
    source; and making it asks serve for nothing."""
    with injected(browser, served) as seen:
        page = seen.page
        _wait_serial(page)
        if choice_us:
            _choose_smoothing(page, label)
        shot = page.evaluate(_PIXELS, 0)
        columns = math.floor((shot["width"] - shot["plotX"] - 8) * shot["ratio"])
        width = WINDOW_US / columns
        window = _math(page, "return m.smoothingWindow(args[0], args[1]);", [choice_us, width])
        asked = len(seen.requests)
        name, raw = _download(page, "#chart-export-csv", tmp_path)
        assert _asked_since(seen, asked) == [], "the export asked serve for something"
    assert name == "live-view.csv"
    pairs, header, rows = _comments_and_rows(raw.decode("utf-8"))
    assert header == ["t_us", "mean", "min", "max", "n", "channel"]
    assert pairs["filter"] == ("moving average" if choice_us else "mean per pixel column")
    assert pairs["caption"] == window["caption"] == shot["caption"]
    assert float(pairs["asked_ms"]) == choice_us / 1000
    assert float(pairs["window_ms"]) == pytest.approx(window["span"] * width / 1000, rel=1e-12)
    assert float(pairs["column_ms"]) == pytest.approx(width / 1000, rel=1e-12)
    assert (pairs["source"], pairs["instrument"]) == ("live", SIM_SERIAL)
    assert pairs["channels"] == "i_na" and pairs["unit_i_na"] == "nA"
    t1 = SIGNAL_T[-1]
    t0 = t1 - WINDOW_US
    assert (float(pairs["t0_us"]), float(pairs["t1_us"])) == (t0, t1)
    assert int(pairs["columns"]) == columns == len(rows)

    half = window["half"]
    count, sums, _ = _buffers(SIGNAL_T, SIGNAL_V, t0, t1, columns, half)
    line = _moving(count, sums, half, columns, window["span"])
    extremes: list[list[float]] = [[math.inf, -math.inf] for _ in range(columns)]
    for t, v in zip(SIGNAL_T, SIGNAL_V, strict=True):
        column = _column_of(t, t0, t1, columns, half)
        if column is not None and half <= column < half + columns:
            low, high = extremes[column - half]
            extremes[column - half] = [min(low, v), max(high, v)]
    _same([_cell(r[1]) for r in rows], line)
    for c, row in enumerate(rows):
        assert row[5] == "i_na"
        assert float(row[0]) == pytest.approx(t0 + (c + 0.5) * width, rel=1e-12)
        low, high = extremes[c]
        if count[c + half] == 0:
            assert (row[2], row[3]) == ("", "")
        else:
            assert (float(row[2]), float(row[3])) == (low, high)
        weight = sum(count[c : c + 2 * half + 1])  # buffer columns c + half ± half
        assert float(row[4]) == (0 if line[c] is None else weight)


def _png_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """A PNG's chunks, each CRC checked."""
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, at = [], 8
    while at < len(raw):
        (length,) = struct.unpack(">I", raw[at : at + 4])
        kind, data = raw[at + 4 : at + 8], raw[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", raw[at + 8 + length : at + 12 + length])
        assert zlib.crc32(kind + data) == crc, kind
        chunks.append((kind, data))
        at += 12 + length
    assert chunks[0][0] == b"IHDR" and chunks[-1] == (b"IEND", b"")
    return chunks


def _png_pixels(chunks: list[tuple[bytes, bytes]]) -> tuple[int, int, list[bytes]]:
    """Decode an 8-bit RGBA PNG: its width, height and rows (filters undone)."""
    width, height, depth, kind, _, _, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, kind, interlace) == (8, 6, 0)
    data = zlib.decompress(b"".join(d for k, d in chunks if k == b"IDAT"))
    stride = 4 * width
    assert len(data) == height * (1 + stride)
    rows: list[bytes] = []
    previous = bytearray(stride)
    for y in range(height):
        kind, line = (
            data[y * (1 + stride)],
            bytearray(data[y * (1 + stride) + 1 : (y + 1) * (1 + stride)]),
        )
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = previous[i]
            c = previous[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + a) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + b) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
            else:
                assert kind == 0, kind
        rows.append(bytes(line))
        previous = line
    return width, height, rows


def test_the_view_png_is_the_canvas_with_what_it_shows_named(
    browser: Any, served: Served, tmp_path: Path
) -> None:
    """The PNG decodes at the canvas's size, holds the canvas's pixels below
    the strip over the lanes, writes in that strip what the view is of, and
    carries the CSV's header as text chunks: the filter, the window, the
    source and the instrument, and no path."""
    with injected(browser, served) as seen:
        page = seen.page
        _wait_serial(page)
        _choose_smoothing(page, "100 ms")
        asked = len(seen.requests)
        name, raw = _download(page, "#chart-export-png", tmp_path)
        assert _asked_since(seen, asked) == [], "the export asked serve for something"
        canvas = page.evaluate(
            """() => {
                const canvas = document.querySelector('#chart');
                const {width, height} = canvas;
                const data = canvas.getContext('2d').getImageData(0, 0, width, height).data;
                const rows = [];
                for (let y = 0; y < height; y += 1) {
                    const row = data.subarray(y * width * 4, (y + 1) * width * 4);
                    rows.push(btoa(String.fromCharCode(...row)));
                }
                const bg = getComputedStyle(canvas).getPropertyValue('--bg').trim();
                return {width, height, rows, bg,
                        ratio: window.devicePixelRatio || 1, caption: canvas.dataset.caption};
            }"""
        )
        _, csv_raw = _download(page, "#chart-export-csv", tmp_path)
    assert name == "live-view.png"
    chunks = _png_chunks(raw)
    width, height, rows = _png_pixels(chunks)
    assert (width, height) == (canvas["width"], canvas["height"])
    # Below the strip over the lanes, the canvas laid over the theme's ground;
    # in it, the line naming the view.
    ground = _rgba(canvas["bg"])
    strip = math.ceil(18 * canvas["ratio"])  # chart.js TOP_PAD
    expected = [base64.b64decode(r) for r in canvas["rows"]]
    assert all(row[3::4] == b"\xff" * width for row in rows), "the image is opaque"
    for y in range(strip, height, 3):
        for x in range(0, width, 2):
            pixel = expected[y][4 * x : 4 * x + 4]
            want = _over((*pixel[:3], pixel[3] / 255), ground)
            got = rows[y][4 * x : 4 * x + 3]
            assert all(abs(g - w) <= 2 for g, w in zip(got, want, strict=True)), (x, y, got, want)
    plain = bytes(round(c) for c in ground) + b"\xff"
    assert any(row[4 * x : 4 * x + 4] != plain for row in rows[:strip] for x in range(width))
    text = dict(d.decode("latin-1").split("\0", 1) for k, d in chunks if k == b"tEXt")
    pairs, _, _ = _comments_and_rows(csv_raw.decode("utf-8"))
    assert text["Title"] == f"Kleidos meter view: live, {SIM_SERIAL}"
    assert text["Description"].startswith("live · ") and text["Description"].endswith(
        f" s (10.0 s) · {SIM_SERIAL}"
    )
    for key, value in pairs.items():
        assert text[key] == value, key
    assert text["filter"] == "moving average" and text["caption"] == canvas["caption"]
    home = str(Path.home())
    for value in text.values():
        assert home not in value and not value.startswith("/")


def test_a_runs_csv_export_is_the_same_whatever_the_smoothing(
    watched: Watched, served: Served, tmp_path: Path
) -> None:
    """Smoothing changes only the drawing: the run's own CSV is byte for byte
    the same with each choice, and the view's CSV of the run's history chart
    names the run and the run's instrument."""
    started = served.post(
        "/api/capture/start",
        {"profile": "STATE", "channels": ["CURR"], "seconds": 1, "label": "same csv"},
    )
    assert started.status == 200, started.body
    run = started.body["run"]
    served.capture_idle()
    page = watched.page
    link = f"tr[data-run='{run}'] a.export:has-text('CSV')"
    exports: list[bytes] = []
    views: list[dict[str, str]] = []
    for choice in ("Off", "1 s", "10 ms"):
        show(page, "runs")
        page.click("#runs-refresh")
        page.locator(f"tr[data-run='{run}'] button", has_text="Open").click()
        until(lambda: "buckets of mean" in page.text_content("#capture-note"), "the run's buckets")
        if choice != "Off":
            _choose_smoothing(page, choice)
        _, view = _download(page, "#chart-export-csv", tmp_path)
        views.append(_comments_and_rows(view.decode("utf-8"))[0])
        show(page, "runs")
        name, raw = _download(page, link, tmp_path)
        assert name == f"{run}.csv"
        exports.append(raw)
    assert exports[0] == exports[1] == exports[2]
    assert exports[0] == served.get(f"/api/runs/{run}/export?format=csv").raw
    assert [v["filter"] for v in views] == [
        "mean per pixel column",
        "moving average",
        "moving average",
    ]
    assert {(v["source"], v["instrument"]) for v in views} == {(f"run {run}", SIM_SERIAL)}


def test_the_view_files_are_made_by_the_pages_module(watched: Watched) -> None:
    """The CSV and the PNG text through the page's own module: a line that
    breaks writes empty cells, a weight keeps its fraction, a name keeps no
    character a file system refuses, and the CRC is the standard one."""
    result = _math(
        watched.page,
        """
        const f = (...values) => Float64Array.of(...values);
        const lane = {key: 'i_na', mean: f(1.5, NaN, 3), n: f(2, 0, 0.25),
                      min: f(1, 0, 3), max: f(2, 0, 3), held: f(2, 0, 1)};
        const frame = {t0: 1000, t1: 1300, columns: 3, columnUs: 100, choiceUs: 0,
                       window: m.smoothingWindow(0, 100), lanes: [lane]};
        const subject = {source: 'live', instrument: 'KM-1'};
        const enc = new TextEncoder();
        const url = document.createElement('canvas').toDataURL('image/png');
        const png = Uint8Array.from(atob(url.split(',')[1]), (c) => c.charCodeAt(0));
        const texted = m.pngWithText(png, [['Title', 'x µ'], ['filter', 'ü·€']]);
        return {
            csv: m.viewCsv(frame, subject),
            crc: m.crc32(enc.encode('123456789')),
            names: [m.viewFileName('live', 'csv'),
                    m.viewFileName('run 2026-11-25T10:00 a/b', 'png')],
            png: btoa(String.fromCharCode(...texted)),
            empty: m.viewCsv({...frame, columns: 0, lanes: []}, subject)
                .split('\\n').filter((l) => !l.startsWith('#')),
            unnamed: m.viewFileName('///', 'csv'),
            unknown: m.viewHeader({...frame, lanes: [{...lane, key: 'q_nc'}]}, subject)
                .find(([key]) => key === 'unit_q_nc'),
            refused: [new Uint8Array(8), new Uint8Array(64)].map((bytes) => {
                try { m.pngWithText(bytes, []); return 'taken'; } catch (e) { return e.message; }
            }),
        };
        """,
        None,
    )
    pairs, header, rows = _comments_and_rows(result["csv"])
    assert rows == [
        ["1050", "1.5", "1", "2", "2", "i_na"],
        ["1150", "", "", "", "0", "i_na"],
        ["1250", "3", "3", "3", "0.25", "i_na"],
    ]
    assert (
        pairs["filter"] == "mean per pixel column" and pairs["caption"] == "mean per pixel, 100 µs"
    )
    assert result["crc"] == 0xCBF43926
    assert result["names"] == ["live-view.csv", "run-2026-11-25T10-00-a-b-view.png"]
    assert result["empty"] == ["t_us,mean,min,max,n,channel", ""]
    assert result["unnamed"] == "chart-view.csv"
    assert result["unknown"] == ["unit_q_nc", "?"]
    assert result["refused"] == ["not a PNG", "not a PNG"]
    chunks = _png_chunks(base64.b64decode(result["png"]))
    assert [k for k, _ in chunks[:3]] == [b"IHDR", b"tEXt", b"tEXt"]
    assert [d for k, d in chunks if k == b"tEXt"] == [b"Title\0x \xb5", b"filter\0\xfc\xb7?"]


# -- noise reduction in the INA228 --------------------------------------------------------

#: A profile's card in Settings.
NOISE = "article[data-noise='{}']"

#: What a person reads of every profile's card, before anything was written.
NOISE_CARDS = """() => Object.fromEntries(
    [...document.querySelectorAll('[data-noise]')].map((card) => [card.dataset.noise, {
        state: card.querySelector('[data-noise-setting=average]').value,
        count: card.querySelector('[data-noise-setting=average_count]').value,
        time: card.querySelector('[data-noise-setting=average_aperture]').value,
        locked: card.querySelector('[data-noise-setting=average_count]').disabled,
        current: card.querySelector('[data-current]').textContent,
        period: card.querySelector('[data-period]').textContent,
        refusal: card.querySelector('[data-refusal]').textContent,
    }]))"""


@contextmanager
def noise_page(
    browser: Any, base: Path, *options: str, refusals: tuple[int, ...] = (), view: str = "settings"
) -> Iterator[tuple[Watched, Served]]:
    """A page on a ``serve --sim`` of its own, whose instrument a test may change."""
    with serving(["--sim", "--data-dir", str(base / "runs"), *options]) as running:
        context = browser.new_context(
            accept_downloads=True, viewport={"width": 1280, "height": 1200}
        )
        page = context.new_page()
        seen = watch(page, running)
        seen.refusals.update(refusals)
        page.goto(running.url)
        _wait_serial(page)
        show(page, view)
        try:
            yield seen, running
        finally:
            COVERAGE.close(context)
        assert seen.unexpected() == []
        assert seen.foreign() == []


def _read_noise(
    page: Any, profile: str, starts: str, settled: bool = True
) -> dict[str, dict[str, Any]]:
    """Wait for a profile's card to read *starts*, and to take a choice if *settled*; every card."""
    page.wait_for_function(
        "([profile, starts]) => document.querySelector(`[data-noise=${profile}] [data-current]`)"
        ".textContent.startsWith(starts)",
        arg=[profile, starts],
    )
    if settled:
        page.wait_for_selector(NOISE.format(profile) + " [data-action=set]:enabled")
    cards: dict[str, dict[str, Any]] = page.evaluate(NOISE_CARDS)
    return cards


def _choose_noise(
    page: Any, profile: str, state: str, count: str = "", time: str = "", press: bool = True
) -> str:
    """Choose a profile's noise reduction, press Set; what the card said it would give."""
    card = page.locator(NOISE.format(profile))
    card.locator("[data-noise-setting=average]").select_option(state)
    if count:
        card.locator("[data-noise-setting=average_count]").select_option(count)
    if time:
        card.locator("[data-noise-setting=average_aperture]").select_option(time)
    said = str(card.locator("[data-period]").text_content())
    if press:
        card.locator("[data-action=set]").click()
    return said


def _qualifier(page: Any, text: str) -> None:
    """Wait for the status bar's Samples item to say *text* of the averaging."""
    page.wait_for_function(
        "text => document.querySelector('[data-qualifier=loss]').textContent === text", arg=text
    )


def _refused(page: Any, profile: str) -> str:
    """Wait for a profile's card to show a refusal; the refusal."""
    selector = NOISE.format(profile) + " [data-refusal]"
    page.wait_for_function("s => document.querySelector(s).textContent !== ''", arg=selector)
    return str(page.text_content(selector))


def _until_asked(seen: Watched, before: int, times: int) -> None:
    """Wait until the page read the averaging *times* times after its *before*-th request."""
    deadline = time.monotonic() + PAGE_WAIT / 1000
    asked = 0
    while asked < times and time.monotonic() < deadline:
        seen.page.wait_for_timeout(50)  # lets the page's requests reach this process
        asked = sum("q=SENS" in url for url in seen.requests[before:])
    assert asked >= times


def _sends(seen: Watched) -> list[tuple[str, Any]]:
    """Every change the page asks for from now on: the path and the body."""
    sent: list[tuple[str, Any]] = []
    seen.page.on(
        "request",
        lambda request: (
            sent.append((request.url.removeprefix(seen.origin), request.post_data_json))
            if request.method == "POST"
            else None
        ),
    )
    return sent


def test_a_fresh_page_shows_noise_reduction_off_on_every_profile(
    browser: Any, tmp_path: Path
) -> None:
    with noise_page(browser, tmp_path) as (seen, running):
        page, sent = seen.page, _sends(seen)
        title = page.text_content("#noise-title")
        assert title == "Noise reduction in the INA228 (changes the measurement)"
        cards = _read_noise(page, "TRACe", "Off: ")
        assert list(cards) == [p["name"] for p in running.get("/api/schema").body["profiles"]]
        assert {card["state"] for card in cards.values()} == {"OFF"}
        assert all(card["locked"] for card in cards.values())  # nothing to choose while off
        assert {name: (card["count"], card["time"]) for name, card in cards.items()} == {
            "TRACe": ("1", "0.00028"),
            "STATe": ("1", "0.001052"),
            "SLEep": ("16", "0.00412"),
            "BURSt": ("1", "0.00005"),
        }
        assert cards["TRACe"]["current"] == (
            "Off: AVG 1 × 280 µs, a sample every 280 µs (the live profile)."
        )
        for name in ("STATe", "SLEep", "BURSt"):
            assert cards[name]["current"] == (
                "Off by default; not read: only the live profile's can be."
            )
        assert cards["SLEep"]["period"] == (
            "Off gives the profile's own: AVG 16 × 4120 µs, a sample every 67520 µs (67.5 ms)."
        )
        assert cards["BURSt"]["period"] == (  # paced by the timer, every second conversion
            "Off gives the profile's own: AVG 1 × 50 µs, a sample every 100 µs."
        )
        # The period is shown before anything is written, and nothing is.
        said = _choose_noise(page, "STATe", "ON", "16", press=False)
        assert said == (
            "On gives AVG 16 × 1052 µs, a sample every 18432 µs (18.4 ms), instead of 1.15 ms."
        )
        said = _choose_noise(page, "SLEep", "ON", "1024", "0.00005", press=False)
        assert "AVG 1024 × 50 µs, a sample every 153600 µs (154 ms)" in said
        # Off again, with nothing on: Set has nothing to write.
        _choose_noise(page, "TRACe", "OFF")
        page.wait_for_timeout(300)
        assert sent == []
        _qualifier(page, "")
        assert page.get_attribute("[data-qualifier=loss]", "data-state") is None


def test_noise_reduction_turned_on_is_named_wherever_its_samples_are(
    browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On for ``STATe``, a capture, a stop: the header, the chart, the exports
    of the view, the run's row and its meta.json name it; serve's exit turns
    it off again, as the simulator's log shows."""
    sims: list[Any] = []

    def simulator(*args: Any, **kwargs: Any) -> Any:
        sims.append(SimulatedMeter(*args, **kwargs))
        return sims[-1]

    monkeypatch.setattr(serve, "SimulatedMeter", simulator)
    with noise_page(browser, tmp_path) as (seen, running):
        page = seen.page
        _read_noise(page, "TRACe", "Off: ")
        sent = _sends(seen)
        height = "() => document.querySelector('.masthead-band').getBoundingClientRect().height"
        before = page.evaluate(height)
        _choose_noise(page, "STATe", "ON", "16")
        cards = _read_noise(page, "STATe", "On: ")
        assert cards["STATe"]["current"] == (
            "On: AVG 16 × 1052 µs, a sample every 18432 µs (18.4 ms) (the live profile)."
        )
        assert re.fullmatch(
            r"Off: AVG 1 × 280 µs, .* \(read at \d\d:\d\d:\d\d, when it was the live profile\)\.",
            cards["TRACe"]["current"],
        )
        assert (cards["STATe"]["refusal"], cards["STATe"]["state"]) == ("", "ON")
        assert sent == [
            ("api/settings", {"profile": "STATe"}),
            (
                "api/settings",
                {"average_count": "16", "average_aperture": "0.001052", "average": True},
            ),
        ]
        _qualifier(page, "AVG 16, 18.4 ms per sample")
        assert page.get_attribute("[data-qualifier=loss]", "data-state") == "pending"
        assert "changes the measurement" in page.get_attribute("[data-qualifier=loss]", "title")
        assert page.evaluate(height) == before
        # Other figures for an averaging that is on: off first, so that no
        # figure is judged beside the other's old one.
        sent.clear()
        _choose_noise(page, "STATe", "ON", "4", "0.00028")
        _read_noise(page, "STATe", "On: AVG 4 × 280 µs, a sample every 1520 µs")
        assert [body for _, body in sent] == [
            {"average": False},
            {"average_count": "4", "average_aperture": "0.00028", "average": True},
        ]
        _choose_noise(page, "STATe", "ON", "16", "0.001052")
        _read_noise(page, "STATe", "On: AVG 16 × 1052 µs")

        show(page, "live")
        page.select_option("#capture-profile", "STATe")
        page.click("#capture-start")
        page.wait_for_function(
            "() => document.querySelector('#chart-readout').textContent.startsWith('Newest at')"
        )
        caption = "INA228 AVG 16, 18.4 ms per sample"
        page.wait_for_function(
            "text => document.querySelector('#chart').dataset.caption.endsWith(` · ${text}`)",
            arg=caption,
        )
        until(lambda: caption in page.text_content("#chart-readout"), "the readout to name it")
        # A card is locked while the capture runs, and says why.
        show(page, "settings")
        locked = page.locator(NOISE.format("STATe"))
        locked.locator("[data-reason]", has_text="Disabled: R").wait_for()
        assert locked.locator("[data-noise-setting=average]").is_disabled()
        show(page, "live")
        # A page opened meanwhile says it too.
        other = page.context.new_page()
        other.goto(running.url)
        other.wait_for_function(
            "text => (document.querySelector('#chart').dataset.caption ?? '').endsWith(text)",
            arg=caption,
        )
        _qualifier(other, "AVG 16, 18.4 ms per sample")
        other.close()
        page.click("#capture-stop")
        page.wait_for_selector("#statistics:not([hidden])")
        assert page.text_content("#statistics-run").endswith(f"ended by stop) · {caption}")
        run = running.capture_idle()["last"]["id"]
        meta = running.get(f"/api/runs/{run}").body
        noise = {"state": "on", "count": 16, "conversion_us": 1052, "period_us": 18432}
        assert meta["noise_reduction"] == noise
        stored = json.loads((tmp_path / "runs" / run / "meta.json").read_text(encoding="utf-8"))
        assert stored["noise_reduction"] == noise
        _, raw = _download(page, "#chart-export-csv", tmp_path)
        pairs, _, _ = _comments_and_rows(raw.decode("utf-8"))
        _, png = _download(page, "#chart-export-png", tmp_path)
        text = dict(d.decode("latin-1").split("\0", 1) for k, d in _png_chunks(png) if k == b"tEXt")
        for view in (pairs, text):
            assert (view["noise_reduction"], view["average_count"]) == ("on", "16")
            assert (view["conversion_us"], view["period_us"]) == ("1052", "18432")
        assert pairs["caption"].endswith(caption) and pairs["source"] == "live"

        # Off, and a run without it: its row says so, and each run opened
        # names its own.
        show(page, "settings")
        _choose_noise(page, "STATe", "OFF")
        _read_noise(page, "STATe", "Off: AVG 1 × 1052 µs")
        _qualifier(page, "")
        plain = running.post("/api/capture/start", {"profile": "STATE", "seconds": 1}).body["run"]
        running.capture_idle()
        show(page, "runs")
        page.click("#runs-refresh")
        page.wait_for_selector(f"tr[data-run='{plain}']")
        head = page.locator("table.runs thead th").all_text_contents()
        at = head.index("Noise reduction")
        for which, said, mark in ((run, "AVG 16, 18.4 ms per sample", "on"), (plain, "off", "off")):
            row = page.locator(f"tr[data-run='{which}']")
            assert row.locator("th, td").nth(at).text_content() == said
            assert row.get_attribute("data-averaging") == mark
        marked = f"tr[data-run='{run}'] td[data-state=pending]"
        assert page.locator(marked).text_content() == "AVG 16, 18.4 ms per sample"
        for which, ends in ((run, True), (plain, False)):
            page.locator(f"tr[data-run='{which}'] button", has_text="Open").click()
            until(lambda: "buckets of mean" in page.text_content("#capture-note"), "its buckets")
            page.wait_for_function(
                "([text, ends]) =>"
                " document.querySelector('#chart').dataset.caption.endsWith(text) === ends",
                arg=[caption, ends],
            )
            _, raw = _download(page, "#chart-export-csv", tmp_path)
            view = _comments_and_rows(raw.decode("utf-8"))[0]
            assert (view["source"], view["noise_reduction"]) == (
                f"run {which}",
                "on" if ends else "off",
            )
            show(page, "runs")
            page.click("#runs-refresh")

        # Left on: serve's exit puts it back.
        show(page, "settings")
        _choose_noise(page, "STATe", "ON", "16")
        _read_noise(page, "STATe", "On: ")
        written = len(sims[0].messages)
    assert len(sims) == 1 and running.status == [0]
    handed_back = sims[0].messages[written:]
    assert any(message.startswith("SENS:AVER OFF") for message in handed_back), handed_back
    assert not sims[0].averaging["STATE"].on and sims[0].profile == "TRACE"
    assert "wrote back average of STATE 0 (it was 1)" in running.out.getvalue()


def test_a_refused_noise_reduction_shows_the_instruments_reason(
    browser: Any, tmp_path: Path
) -> None:
    with noise_page(browser, tmp_path, refusals=(409,)) as (seen, running):
        page = seen.page
        _read_noise(page, "TRACe", "Off: ")
        assert running.post("/api/settings", {"i2c_hz": 1000000}).status == 200
        _choose_noise(page, "BURSt", "ON", "4")
        said = _refused(page, "BURSt")
        assert "-221" in said and "BURSt has no averaging override" in said
        cards = _read_noise(page, "BURSt", "Off: AVG 1 × 50 µs, a sample every 100 µs")
        assert cards["BURSt"]["state"] == "OFF"
        _qualifier(page, "")
        # A period shorter than a read at this bus clock: refused, and left off.
        assert running.post("/api/settings", {"i2c_hz": 50000}).status == 200
        _choose_noise(page, "TRACe", "ON", "1", "0.00005")
        said = _refused(page, "TRACe")
        assert "-221" in said and "under the" in said
        assert _read_noise(page, "TRACe", "Off: ")["TRACe"]["state"] == "OFF"
        assert running.get("/api/settings").body["settings"]["average"] == "0"
        # Turned on at a speed that takes it, then changed to one it refuses:
        # off, the reason shown, never the figures half changed.
        assert running.post("/api/settings", {"i2c_hz": 400000}).status == 200
        _choose_noise(page, "TRACe", "ON", "64", "0.00028")
        _read_noise(page, "TRACe", "On: AVG 64 × 280 µs")
        assert page.text_content(NOISE.format("TRACe") + " [data-refusal]") == ""
        assert running.post("/api/settings", {"i2c_hz": 50000}).status == 200
        _choose_noise(page, "TRACe", "ON", "1", "0.00005")
        assert "under the" in _refused(page, "TRACe")
        _read_noise(page, "TRACe", "Off: ")
        _qualifier(page, "")


def test_a_profile_refused_changes_no_averaging(browser: Any, tmp_path: Path) -> None:
    """The profile could not be selected: nothing else is sent, and the live
    profile's averaging, which nobody asked to change, stays as it was."""
    with noise_page(browser, tmp_path, refusals=(409,)) as (seen, running):
        page = seen.page
        _read_noise(page, "TRACe", "Off: ")
        refusal = {
            "written": [],
            "errors": [{"code": -221, "text": "Settings conflict;not now"}],
            "settings": {},
        }
        page.route(
            "**/api/settings",
            lambda route: (
                route.fulfill(status=409, json=refusal)
                if route.request.method == "POST"
                else route.continue_()
            ),
        )
        sent = _sends(seen)
        _choose_noise(page, "SLEep", "ON", "64")
        assert "-221 Settings conflict;not now" in _refused(page, "SLEep")
        page.wait_for_timeout(200)
        assert [body for _, body in sent] == [{"profile": "SLEep"}]
        settings = running.get("/api/settings").body["settings"]
        assert (settings["profile"], settings["average"]) == ("TRACE", "0")


def test_the_page_follows_a_noise_reduction_another_controller_changes(
    browser: Any, tmp_path: Path
) -> None:
    """The console, another page or a reset changes it: the header and the
    cards follow, and what was read of another profile is forgotten by a
    reset and by a client that had the line."""
    options = ("--scpi-passthrough", "0")
    with noise_page(browser, tmp_path, *options, refusals=(423,)) as (seen, running):
        page = seen.page
        _read_noise(page, "TRACe", "Off: ")
        scpi = "SENS:PROF STAT;:SENS:AVER:COUN 64;:SENS:AVER ON"
        assert running.post("/api/scpi", scpi).status == 200
        _qualifier(page, "AVG 64, 73.7 ms per sample")
        cards = _read_noise(page, "STATe", "On: AVG 64 × 1052 µs")
        assert cards["STATe"]["state"] == "OFF"  # what a person was choosing is theirs
        assert "when it was the live profile" in cards["TRACe"]["current"]
        # Nothing but the averaging moved: the figures are read again all the same.
        assert running.post("/api/scpi", "SENS:AVER:COUN 16").status == 200
        _qualifier(page, "AVG 16, 18.4 ms per sample")
        # A client refused the line changes nothing the page knows.
        started = running.post("/api/capture/start", {"profile": "STATE", "channels": ["CURR"]})
        assert started.status == 200
        caption = "document.querySelector('#chart').dataset.caption.endsWith('per sample')"
        page.wait_for_function(f"() => {caption}")
        assert running.passthrough is not None
        host, port = running.passthrough.removeprefix("socket://").split(":")
        with socket.create_connection((host, int(port)), timeout=30) as refused:
            assert refused.recv(1) == b""
        page.wait_for_function(
            "() => document.querySelector('#notices').textContent.includes('passthrough: refused')"
        )
        assert page.evaluate(caption)
        assert page.evaluate(NOISE_CARDS)["TRACe"]["current"].startswith("Off: AVG 1")
        assert running.post("/api/capture/stop").status == 200
        running.capture_idle()
        assert running.post("/api/scpi", "*RST").status == 200
        _qualifier(page, "")
        cards = _read_noise(page, "STATe", "Off by default; not read")
        assert cards["TRACe"]["current"].startswith("Off: ")
        # A hidden Settings view reads nothing: the header alone follows.
        show(page, "health")
        page.wait_for_timeout(300)
        before = len(seen.requests)
        assert running.post("/api/scpi", "SENS:AVER:COUN 16;:SENS:AVER ON").status == 200
        _qualifier(page, "AVG 16, 4.48 ms per sample")
        asked = [url for url in seen.requests[before:] if "SENS" in url]
        assert len(asked) == 1
        # A passthrough client: while it has the line nothing can be read.
        show(page, "settings")
        _read_noise(page, "TRACe", "On: ")
        with socket.create_connection((host, int(port)), timeout=30) as client:
            _qualifier(page, "averaging not known")
            _read_noise(page, "TRACe", "Off by default; not read", settled=False)
            client.sendall(b"*IDN?\n")
            assert client.recv(4096)
            client.shutdown(socket.SHUT_RDWR)
        _qualifier(page, "")  # serve put its own back before it handed the line over
        page.click("#settings-refresh")
        _read_noise(page, "TRACe", "Off: ")


def test_a_noise_reduction_changed_while_the_events_were_not_read_is_read_again(
    browser: Any, tmp_path: Path
) -> None:
    """No ``settings`` event reaches this page: it reads the averaging again
    each time the events open, so what changed meanwhile is never missed."""
    with noise_page(browser, tmp_path, view="live") as (seen, running):
        page = seen.page
        _qualifier(page, "")
        page.route(
            "**/api/events*",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body="retry: 200\n\n",
            ),
        )
        before = len(seen.requests)
        page.reload()
        _until_asked(seen, before, 2)  # at load, and once the events opened
        page.wait_for_timeout(300)  # answered too: what follows is another reading
        scpi = "SENS:PROF STAT;:SENS:AVER:COUN 4;:SENS:AVER ON"
        assert running.post("/api/scpi", scpi).status == 200
        _qualifier(page, "AVG 4, 4.61 ms per sample")
        # Events the page missed may have been those.
        page.unroute("**/api/events*")
        page.route(
            "**/api/events*",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body='retry: 3600000\n\nevent: lost\ndata: {"events": 3, "samples": null}\n\n',
            ),
        )
        before = len(seen.requests)
        page.reload()
        _until_asked(seen, before, 3)


def test_a_choice_made_while_the_card_is_read_stays(browser: Any, tmp_path: Path) -> None:
    """Read again asks the instrument; what a person chooses before it
    answers is not replaced by the answer."""
    with noise_page(browser, tmp_path) as (seen, _running):
        page = seen.page
        _read_noise(page, "TRACe", "Off: ")
        held: list[Any] = []

        def hold(route: Any) -> None:
            held.append(route)

        page.route("**/api/query?q=SENS*", hold)
        page.click("#settings-refresh")
        deadline = time.monotonic() + PAGE_WAIT / 1000
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        _choose_noise(page, "TRACe", "ON", "64", press=False)
        with page.expect_response("**/api/query?q=SENS*"):
            held[0].continue_()
        page.wait_for_timeout(200)
        card = page.evaluate(NOISE_CARDS)["TRACe"]
        assert (card["state"], card["count"]) == ("ON", "64")


def test_a_noise_reduction_that_cannot_be_read_says_so(browser: Any, tmp_path: Path) -> None:
    """An instrument that answers no report, and an image without the averaging."""
    with noise_page(browser, tmp_path, refusals=(504,), view="health") as (seen, _running):
        page = seen.page
        answers = [
            (504, {"error": "timeout", "detail": "no answer"}),
            (200, {"units": [], "errors": [], "fields": [{"profile": "TRACE", "period_us": 280}]}),
        ]
        current: list[int] = [0]
        page.route(
            "**/api/query?q=SENS*",
            lambda route: route.fulfill(status=answers[current[0]][0], json=answers[current[0]][1]),
        )
        show(page, "settings")
        page.wait_for_function(
            "() => document.querySelector('#noise-refusal').textContent.includes('504')"
        )
        sent = _sends(seen)
        _choose_noise(page, "TRACe", "ON", "4")
        page.wait_for_timeout(200)
        assert sent == []  # what cannot be read is not changed
        current[0] = 1
        page.click("#settings-refresh")
        page.wait_for_function(
            "() => document.querySelector('#noise-refusal').textContent.includes('0.18.0')"
        )
        assert page.evaluate(NOISE_CARDS)["TRACe"]["current"].startswith("Off by default")


def test_the_averagings_words_and_figures(watched: Watched) -> None:
    """The page's own module: the period, the words and an export's lines."""
    result = _math(
        watched.page,
        """
        const on = {state: 'on', count: 16, conversion_us: 1052, period_us: 18432};
        const off = {...on, state: 'off', count: 1, period_us: 1152};
        const w = m.smoothingWindow(0, 4600);
        return {
            period: [m.averagedPeriod({other_us: 100}, 16, 1052),
                     m.averagedPeriod({other_us: 0}, 1024, 4120)],
            text: m.noiseText(on),
            captions: [on, off, null, undefined].map((n) => m.noiseCaption(n)),
            chart: [m.chartCaption(w, on), m.chartCaption(w, off), m.chartCaption(w, null)],
            pairs: [on, off, null, undefined].map((n) => m.noisePairs(n)),
            of: [m.noiseOf({avg_state: 'on', avg: 16, vshct_us: 1052, period_us: 18432}),
                 m.noiseOf({avg: 16}), m.noiseOf(null), m.noiseOf(undefined)],
        };
        """,
        None,
    )
    assert result["period"] == [18432, 4218880]
    assert result["text"] == "AVG 16, 18.4 ms per sample"
    assert result["captions"] == ["INA228 AVG 16, 18.4 ms per sample", "", "", ""]
    assert result["chart"] == [
        "mean per pixel, 4.6 ms · INA228 AVG 16, 18.4 ms per sample",
        "mean per pixel, 4.6 ms",
        "mean per pixel, 4.6 ms",
    ]
    unknown = [["noise_reduction", "unknown"]]
    assert result["pairs"][0] == [
        ["noise_reduction", "on"],
        ["average_count", "16"],
        ["conversion_us", "1052"],
        ["period_us", "18432"],
    ]
    assert result["pairs"][1][0] == ["noise_reduction", "off"]
    assert result["pairs"][2:] == [unknown, unknown]
    noise = {"state": "on", "count": 16, "conversion_us": 1052, "period_us": 18432}
    assert result["of"] == [noise, None, None, None]


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_noise_reduction_is_legible_in_both_themes_and_on_a_phone(
    browser: Any, served: Served, scheme: str
) -> None:
    """The mark of one that is on has a text's contrast in both themes, and
    the cards and the caption fit a phone's width."""
    for width in (1280, 390):
        context = browser.new_context(color_scheme=scheme, viewport={"width": width, "height": 900})
        page = context.new_page()
        seen = watch(page, served)
        page.goto(served.url + "#settings")
        page.wait_for_function(  # whichever profile the tests before it left live
            "() => [...document.querySelectorAll('[data-noise] [data-current]')]"
            ".some((node) => node.textContent.startsWith('Off: '))"
        )
        got = page.evaluate(
            """async () => {
                const card = document.querySelector('[data-noise=STATe]');
                const current = card.querySelector('[data-current]');
                current.dataset.state = 'pending';
                const ground = getComputedStyle(card.closest('.card')).backgroundColor;
                const said = document.querySelector('[data-qualifier=loss]');
                said.textContent = 'AVG 1024, 4.22 s per sample';
                return {ink: getComputedStyle(current).color, ground,
                        bar: said.getBoundingClientRect().right,
                        cell: said.closest('div').getBoundingClientRect().right,
                        wide: document.documentElement.scrollWidth,
                        cards: [...document.querySelectorAll('[data-noise]')]
                            .map((c) => c.getBoundingClientRect().right)};
            }"""
        )
        COVERAGE.close(context)
        assert seen.unexpected() == [] and seen.foreign() == []
        assert got["wide"] <= width and max(got["cards"]) <= width
        assert got["bar"] <= got["cell"] + 0.5 and got["cell"] <= width - 16  # cut, not spilt
        ink = _over(_rgba(got["ink"]), (0, 0, 0))
        assert _contrast(ink, _over(_rgba(got["ground"]), (0, 0, 0))) >= 4.5


# -- pins ---------------------------------------------------------------------------------

#: The firmware's verdicts on 2,989 pin maps, which the page's rules must give too.
VECTORS = (
    Path(__file__).resolve().parents[2]
    / "firmware"
    / "test"
    / "native"
    / "golden"
    / "pinmap.vectors"
)

#: A pin of the drawing by its GPIO.
PIN = ".pin[data-gpio='{}']"


def _pins_math(page: Any, body: str, args: Any) -> Any:
    """Run *body* in the page with ``pins-math.js`` as ``m`` and the arguments as ``args``."""
    return page.evaluate(
        "async (args) => { const m = await import('/static/js/pins-math.js');\n" + body + "\n}",
        args,
    )


@contextmanager
def pins_page(
    browser: Any, base: Path, *options: str, wiring: str = "", **context: Any
) -> Iterator[tuple[Watched, Served]]:
    """A page on the Pins view of a ``serve --sim`` of its own, wired as *wiring* says."""
    argv = ["--sim", "--data-dir", str(base / "runs"), *options]
    if wiring:
        argv += ["--sim-wiring", wiring]
    with serving(argv) as served:
        opened = browser.new_context(**{"viewport": {"width": 1280, "height": 1200}, **context})
        page = opened.new_page()
        seen = watch(page, served)
        page.goto(served.url + "#pins")
        page.wait_for_selector(".pin[data-state]")
        page.wait_for_function(
            "() => document.querySelector('[data-status=pins]').textContent !== '-'"
        )
        try:
            yield seen, served
        finally:
            COVERAGE.close(opened)
        assert seen.unexpected() == []
        assert seen.foreign() == []


def _give(page: Any, gpio: int, use: str, **config: str) -> None:
    """Give a pin a use through its dialog: the use's radio, its configuration, Keep."""
    page.click(PIN.format(gpio))
    page.wait_for_selector("#pin-dialog[open]")
    page.check(f"#pin-uses input[value='{use}']")
    for name, value in config.items():
        control = page.locator(f"#pin-{name}")
        if control.evaluate("node => node.tagName") == "SELECT":
            control.select_option(value)
        else:
            control.fill(value)
    page.click("#pin-keep")
    page.wait_for_selector("#pin-dialog:not([open])", state="attached")


def _apply(page: Any) -> str:
    """Press Apply, confirm, and return what the confirmation said."""
    page.click("#pins-apply")
    page.wait_for_selector("#confirm-dialog[open]")
    said = str(page.text_content("#confirm-lines"))
    page.click("#confirm-go")
    return said


def _result(page: Any, starts: str) -> str:
    """Wait for the last change's sentence, and return it."""
    page.wait_for_function(
        "text => document.querySelector('#pins-result').textContent.startsWith(text)", arg=starts
    )
    return str(page.text_content("#pins-result"))


def test_the_board_is_drawn_from_the_table_terminal_by_terminal(
    watched: Watched, served: Served
) -> None:
    page = watched.page
    show(page, "pins")
    page.wait_for_selector(".pin[data-state]")
    board = served.get("/api/pins").body["board"]
    rows = [row for row in board["rows"] if row["rail"] != "offboard"]
    drawn = page.eval_on_selector_all(
        ".pin",
        """pins => pins.map(p => ({rail: p.dataset.rail, terminal: Number(p.dataset.terminal),
            silk: p.querySelector('.silk').textContent, gpio: p.querySelector('.gpio').textContent,
            number: p.querySelector('.number').textContent, state: p.dataset.state,
            word: p.querySelector('.word').textContent, name: p.getAttribute('aria-label'),
            mark: p.querySelector('.mark').getAttribute('d'), role: p.getAttribute('role')}))""",
    )
    assert len(drawn) == len(rows) == 44
    assert [(d["rail"], d["terminal"], d["silk"], d["gpio"], d["number"]) for d in drawn] == [
        (
            row["rail"],
            row["terminal"],
            row["silk"],
            "" if row["gpio"] is None else f"GPIO{row['gpio']}",
            str(row["terminal"]),
        )
        for row in rows
    ]
    used = {4, 5, 6, 7, 15}
    assert [d["state"] for d in drawn] == [
        "used" if row["gpio"] in used else row["class"] for row in rows
    ]
    assert {d["role"] for d in drawn} == {"button"}
    # A state is a word and a shape as well as a color: one shape a state, named in the legend.
    shapes = {state: {d["mark"] for d in drawn if d["state"] == state} for state in MARKED}
    assert all(len(marks) == 1 for marks in shapes.values())
    assert len(set.union(*shapes.values())) == len(MARKED)
    legend = {
        node["state"]: (node["d"], node["text"])
        for node in page.eval_on_selector_all(
            ".pin-legend .mark",
            """marks => marks.map(m => ({state: m.dataset.state, d: m.getAttribute('d'),
                text: m.closest('li').textContent.trim()}))""",
        )
    }
    assert {s: t for s, (_, t) in legend.items()} == {**MARKED, "conflict": "conflict"}
    for state, marks in shapes.items():
        assert marks == {legend[state][0]}
    names = {d["name"] for d in drawn}
    assert {
        "left 7, GPIO7, Relay 1, in use",
        "left 8, GPIO15, Relay 2, in use",
        "left 11, GPIO18, free",
        "left 1, 3.3, power, reserved by the board",
        "right 15, GPIO45, strapping, reserved by the board",
        "right 9, GPIO39, JTAG, usable with caution",
    } <= names
    assert len(names) == 44
    # The module and its two USB-C ports.
    assert page.locator("#board .module").count() == 1
    assert page.locator("#board .usb").count() == 2


#: The states of a pin, and the word the legend and the names give each.
MARKED = {
    "free": "free",
    "used": "in use",
    "reserved": "reserved by the board",
    "caution": "usable with caution",
}


def test_the_pages_rules_read_a_roles_limit_as_the_first_gpio_it_cannot_take(
    watched: Watched, served: Served
) -> None:
    given = _pins_math(
        watched.page,
        """
        const [board, live] = args;
        const map = m.mapFrom(live);
        const limited = (below) => ({...board, roles: board.roles.map(
            (role) => (role.key === 'alert' ? {...role, below} : role))});
        return [1, 0].map((more) => m.validateMap(map, limited(map.alert + more)));
        """,
        [served.get("/api/pins").body["board"], served.get("/api/pins").body["live"]],
    )
    assert given == [None, {"problem": "too_high", "at": "alert"}]


def test_the_pages_rules_give_every_vector_the_firmwares_verdict(
    watched: Watched, served: Served
) -> None:
    keys = [
        "sda",
        "scl",
        "alert",
        "addr",
        "scl_hz",
        "pull_sda",
        "pull_scl",
        "pull_alert",
        "relay1",
        "relay1_active",
        "relay2",
        "relay2_active",
    ]
    vectors = []
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            words, verdict = line.split(" | ")
            vectors.append((dict(zip(keys, words.split(), strict=True)), verdict))
    assert len(vectors) > 2500
    body = served.get("/api/pins").body
    given = _pins_math(
        watched.page,
        """
        const [board, maps] = args;
        return [m.KEYS, maps.map((words) => {
            const found = m.validateMap(m.mapFrom(words), board);
            return found === null ? '1 - -' : `0 ${found.problem} ${found.at}`;
        })];
        """,
        [body["board"], [words for words, _ in vectors]],
    )
    assert given[0] == keys
    wrong = [
        (words, verdict, said)
        for (words, verdict), said in zip(vectors, given[1], strict=True)
        if verdict != said
    ]
    assert wrong == []
    assert {verdict.split()[1] for _, verdict in vectors} == {
        "-",
        "missing",
        "no_pin",
        "reserved",
        "too_high",
        "shared",
        "address",
        "clock",
    }


def test_the_pages_words_for_maps_pins_and_results(watched: Watched, served: Served) -> None:
    """Every sentence the Pins view makes, through the page's own module."""
    body = served.get("/api/pins").body
    said = _pins_math(
        watched.page,
        """
        const [board, live] = args;
        const base = m.mapFrom(live);
        const at = (gpio) => board.rows.find((row) => row.gpio === gpio);
        const problems = (changes) => {
            const map = {...base, ...changes};
            return m.problemsOf(map, board).map((p) => m.problemText(p, map, board));
        };
        const planted = {...board, groups: {}, rows: board.rows.map((row) =>
            row.gpio === 45 ? {...row, group: 'unheard'} : row)};
        return {
            problems: [
                problems({sda: null, scl: 22, alert: 41}),
                problems({relay1: 45, relay2: 4}),
                problems({addr: 0x50, scl_hz: NaN}),
            ],
            unheard: m.roleReason(board.roles[0], 45, 'reserved', planted),
            unheardName: m.pinState(
                planted.rows.find((r) => r.gpio === 45), base, base, planted).name,
            unknownRole: m.roleReason(board.roles[0], 99, 'reserved', board),
            states: [
                m.pinState(at(4), {...base, relay1: 4}, base, board),
                m.pinState(at(7), {...base, relay1: 4}, base, board),
                m.pinState(at(39), {...base, relay1: 39}, base, board),
                m.pinState(at(7), m.withUse(base, 7, 'sda', board), base, board),
                m.pinState(at(4), m.withUse(base, 4, null, board), base, board),
            ],
            differs: m.differences(base, {...base, sda: null, addr: 0x41, scl_hz: NaN,
                pull_alert: 'off', relay1_active: 'high'}, board),
            order: m.stageOrder(base, {...base, sda: 18, relay1_active: 'high'}, board),
            roleless: m.differences(base, {...base, sda: 18}, {...board, roles: []}),
            assigned: m.assignments({...base, relay2: null}, board),
            results: [
                {result: 'none', reason: null},
                {result: 'ok', reason: null},
                {result: 'ok', reason: 'no_chip'},
                {result: 'reverted', reason: 'alert'},
                {result: 'reverted', reason: 'relays'},
                {result: 'refused', reason: 'relays'},
                {result: 'refused', reason: 'invalid'},
                {result: 'reverted', reason: 'a_new_word'},
                {result: 'busy', reason: undefined},
            ].map((result) => m.resultText(result)),
            limit: [17, 18].map((gpio) =>
                m.roleProblem({...board.roles[2], below: 18}, gpio, board)),
            none: m.rolesAt(base, null, board),
        };
        """,
        [body["board"], body["live"]],
    )
    assert said["problems"] == [
        [
            "INA228 SDA has no pin.",
            "INA228 SCL: the chip has no GPIO22",
            "INA228 ALERT: needs a pin below GPIO32",
        ],
        [
            "Relay 1: GPIO45 is reserved by the board. A strapping pin: its level at reset"
            " selects the boot mode, the JTAG source or the VDD_SPI voltage.",
            "Relay 2 is on GPIO4, which INA228 SDA holds.",
        ],
        ["The INA228's address is auto or 0x40 to 0x4F.", "The I2C clock is 50000 to 1000000 Hz."],
    ]
    assert said["unheard"] == said["unknownRole"].replace("99", "45")
    assert said["unheardName"] == "right 15, GPIO45, unheard, reserved by the board"
    names = [state["name"] for state in said["states"]]
    assert names == [
        "left 4, GPIO4, INA228 SDA and Relay 1, in use,"
        " conflict: Relay 1 is on GPIO4, which INA228 SDA holds.",
        "left 7, GPIO7, free",
        "right 9, GPIO39, Relay 1, in use, with caution",
        "left 7, GPIO7, INA228 SDA, in use, conflict: Relay 1 has no pin.",
        "left 4, GPIO4, free, conflict: INA228 SDA has no pin.",
    ]
    assert [state["changed"] for state in said["states"]] == [True] * 5
    assert [(d["label"], d["from"], d["to"]) for d in said["differs"]] == [
        ("INA228 SDA", "GPIO4", "no pin"),
        ("INA228 address", "auto", "0x41"),
        ("I2C clock at boot", "400000 Hz", "no number"),
        ("INA228 ALERT", "pull up", "pull off"),
        ("Relay 1", "active low", "active high"),
    ]
    assert said["order"] == {"clear": True, "sda": 18, "relay1_active": "high"}
    assert [d["label"] for d in said["roleless"]] == ["sda"]
    assert said["assigned"] == [
        ["INA228 SDA", "GPIO4, pull up"],
        ["INA228 SCL", "GPIO5, pull up"],
        ["INA228 ALERT", "GPIO6, pull up"],
        ["Relay 1", "GPIO7, active low"],
        ["Relay 2", "no pin, active high"],
        ["INA228 address", "auto"],
        ["I2C clock at boot", "400000 Hz"],
    ]
    assert said["results"] == [
        "No change was applied since boot.",
        "Applied: the INA228 answered on the new map and the map is saved.",
        "Applied, though the INA228 did not answer on the map's SDA, SCL and address:"
        " the map is saved and the console is the way back.",
        "Reverted: the INA228 answered, and its ALERT did not reach the map's ALERT pin."
        " The last good map is back.",
        "Reverted: a relay pad did not read back the level it was given."
        " The last good map is back.",
        "Refused: a relay is not at rest. Nothing changed.",
        "Refused: the map breaks a rule of the board. Nothing changed.",
        "Reverted: a_new_word. The last good map is back.",
        "The instrument answered busy: no reason given.",
    ]
    assert said["limit"] == [None, "too_high"]  # the limit is the first GPIO a role cannot take
    assert said["none"] == []


def test_a_wrong_sda_is_reverted_with_its_reason_and_the_live_map_stays(
    browser: Any, tmp_path: Path
) -> None:
    with pins_page(browser, tmp_path) as (seen, served):
        page = seen.page
        seen.refusals.add(409)
        assert page.is_disabled("#pins-apply")
        assert "nothing differs" in page.text_content("#pins-reason")
        _give(page, 18, "sda")
        # Drawn, listed, and not sent.
        assert page.get_attribute(PIN.format(18), "data-state") == "used"
        assert page.get_attribute(PIN.format(18), "data-changed") == "true"
        assert page.text_content("#pins-diff") == "INA228 SDA: GPIO4 → GPIO18"
        assert served.get("/api/pins").body["differs"] == []
        assert "Ready to apply" in page.text_content("#pins-reason")
        # The confirmation says it reverts by itself; declined, nothing is sent.
        page.click("#pins-apply")
        page.wait_for_selector("#confirm-dialog[open]")
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.querySelector('#pins-refusal').textContent.includes('not confirmed')"
        )
        assert not any(url.startswith("/api/pins/") for url in seen.asked())
        said = _apply(page)
        assert "A bus that does not answer reverts by itself" in said
        assert "INA228 SDA: GPIO4 → GPIO18" in said and "null-path" not in said
        text = _result(page, "Reverted")
        assert text == (
            "Reverted: the INA228 did not answer on the map's SDA, SCL and address."
            " The last good map is back."
        )
        assert "117" in page.text_content("#pins-refusal")
        listed = page.inner_text("#pins-result-list").upper()
        assert "REVERTED" in listed and "NO_CHIP" in listed
        body = served.get("/api/pins").body
        assert (body["live"]["sda"], body["live"]["src"]) == (4, "default")
        assert page.text_content("[data-status=pins]") == "default"
        # The draft stays to be corrected; read again keeps it, discarding drops it on both sides.
        page.click("#pins-refresh")
        page.wait_for_timeout(200)
        assert page.text_content("#pins-diff") == "INA228 SDA: GPIO4 → GPIO18"
        page.click("#pins-discard")
        page.wait_for_function("() => document.querySelector('#pins-diff').textContent === ''")
        until(lambda: served.get("/api/pins").body["differs"] == [], "the staged map dropped")
        assert page.get_attribute(PIN.format(4), "data-state") == "used"
        assert page.is_disabled("#pins-discard")


def test_a_relay_on_a_free_pin_is_applied_checked_and_ends_verified(
    browser: Any, tmp_path: Path
) -> None:
    with pins_page(browser, tmp_path, wiring="relay1=16,relay1_active=high") as (seen, served):
        page = seen.page
        _give(page, 16, "relay1", option="high")
        assert page.eval_on_selector_all("#pins-diff li", "n => n.map(i => i.textContent)") == [
            "Relay 1: GPIO7 → GPIO16",
            "Relay 1: active low → active high",
        ]
        said = _apply(page)
        assert "the null-path check runs after it" in said
        text = _result(page, "Applied")
        assert text.endswith("The null-path check passed: the relays are verified.")
        live = served.get("/api/pins").body["live"]
        assert (live["relay1"], live["src"], live["verified"]) == (16, "stored", 1)
        assert page.text_content("[data-status=pins]") == "stored"
        assert page.text_content("[data-qualifier=pins]") == "verified"
        assert page.text_content("#pins-diff") == ""
        assert page.get_attribute(PIN.format(7), "data-state") == "free"
        # The Relays view shows each relay's pin and level, and leads here; Settings lists them.
        show(page, "relays")
        page.wait_for_function(
            "() => document.querySelector('#k1-pin').textContent === 'GPIO16, active high'"
        )
        assert page.text_content("#k2-pin") == "GPIO15, active high"
        assert "Relay 1 (GPIO16, active high) closed" in page.text_content("#circuit-text")
        page.click("#relays a[href='#pins']")
        page.wait_for_selector("section#pins:not([hidden])")
        show(page, "settings")
        page.wait_for_function(
            "() => document.querySelector('#settings-pins-list')"
            ".textContent.includes('GPIO16, active high')"
        )
        page.click("#settings-pins a[href='#pins']")
        page.wait_for_selector("section#pins:not([hidden])")
        # Defaults go back, the check fails on this wiring, and the panel says so.
        seen.refusals.add(409)
        page.click("#pins-defaults")
        page.wait_for_selector("#confirm-dialog[open]")
        assert "needs no INA228" in page.text_content("#confirm-lines")
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.querySelector('#pins-refusal').textContent.includes('not confirmed')"
        )
        page.click("#pins-defaults")
        page.click("#confirm-go")
        page.wait_for_function(
            "() => document.querySelector('#pins-result').textContent.includes('check failed')"
        )
        assert "104" in page.text_content("#pins-result")
        assert served.get("/api/pins").body["live"]["relay1"] == 7
        assert page.text_content("[data-status=pins]") == "default"


def test_a_relay_nothing_answers_to_ends_unverified_until_a_null_path_passes(
    browser: Any, tmp_path: Path
) -> None:
    with pins_page(browser, tmp_path) as (seen, served):
        page = seen.page
        seen.refusals.add(409)
        _give(page, 16, "relay2")
        _apply(page)
        text = _result(page, "Applied")
        assert "the relays are unverified: 103" in text
        assert page.get_attribute("#pins-result", "data-state") == "pending"
        assert page.text_content("[data-qualifier=pins]") == "unverified"
        assert served.get("/api/pins").body["live"]["verified"] == 0
        # A relay order reads the map again: the header follows what it verified.
        page.evaluate("() => document.querySelector('[data-qualifier=pins]').textContent = 'old'")
        show(page, "relays")
        page.click("article[data-relay=nullpath] button[data-choice=OFF]")
        page.wait_for_function(
            "() => document.querySelector('[data-qualifier=pins]').textContent === 'unverified'"
        )


def test_a_pin_that_cannot_be_assigned_says_why_before_anything_is_sent(
    browser: Any, tmp_path: Path
) -> None:
    with pins_page(browser, tmp_path) as (seen, _):
        page = seen.page

        def dialog(selector: str) -> list[tuple[str, bool]]:
            page.click(selector)
            page.wait_for_selector("#pin-dialog[open]")
            return [
                (str(label["text"]).strip(), bool(label["off"]))
                for label in page.eval_on_selector_all(
                    "#pin-uses label",
                    """n => n.map(l => ({text: l.textContent,
                        off: l.querySelector('input').disabled}))""",
                )
            ]

        page.hover(PIN.format(45))
        about = page.text_content("#pin-about")
        assert "right 15, GPIO45: reserved by the board" in about and "boot mode" in about
        uses = dialog(PIN.format(45))
        assert [off for _, off in uses] == [True] * 5 + [False] and uses[5][0] == "unused"
        assert all(
            "GPIO45 is reserved by the board. A strapping pin" in text for text, _ in uses[:5]
        )
        assert page.text_content("#pin-problem") == "This pin cannot be assigned."
        assert page.is_disabled("#pin-keep")
        page.keyboard.press("Escape")
        page.wait_for_selector("#pin-dialog:not([open])", state="attached")
        # A terminal that is no GPIO.
        uses = dialog(".pin[data-rail=left][data-terminal='21']")
        assert all(text.endswith("not a GPIO") and off for text, off in uses[:5])
        assert page.text_content("#pin-title") == "left 21, 5V"
        page.keyboard.press("Escape")
        # ALERT needs an interrupt of the first bank; the other roles may have it, with caution.
        uses = dialog(PIN.format(41))
        assert [text for text, off in uses if off] == ["INA228 ALERT: needs a pin below GPIO32"]
        page.check("#pin-uses input[value=relay1]")
        page.click("#pin-keep")
        assert page.text_content(f"{PIN.format(41)} .word") == "Relay 1 · in use, with caution"
        # A pin taken from a role is a conflict drawn on it, named in the list and in its name.
        page.click(PIN.format(5))
        assert page.is_checked("#pin-uses input[value=scl]")
        page.check("#pin-uses input[value=alert]")
        assert page.locator("#pin-address").count() == 0  # ALERT has a pull and nothing else
        assert page.text_content("#pin-problem") == "INA228 SCL has no pin."
        page.click("#pin-keep")
        assert page.get_attribute(PIN.format(5), "data-conflict") == "true"
        assert "INA228 SCL has no pin" in page.get_attribute(PIN.format(5), "aria-label")
        assert page.text_content(f"{PIN.format(5)} .word") == "conflict · INA228 ALERT"
        assert page.text_content("#pins-problems") == "INA228 SCL has no pin."
        assert page.is_disabled("#pins-apply")
        assert "breaks a rule" in page.text_content("#pins-reason")
        # Escape in the dialog keeps nothing.
        page.click(PIN.format(8))
        page.check("#pin-uses input[value=scl]")
        page.keyboard.press("Escape")
        assert page.get_attribute(PIN.format(8), "data-state") == "free"
        # And so does Cancel.
        page.click(PIN.format(8))
        page.check("#pin-uses input[value=scl]")
        page.click("#pin-form button[value=cancel]")
        page.wait_for_selector("#pin-dialog:not([open])", state="attached")
        assert page.get_attribute(PIN.format(8), "data-state") == "free"
        # A pin opened before the last dialog's close event came is edited all the same.
        page.evaluate(
            """async ([first, second]) => {
                const press = (pin) => document.querySelector(pin)
                    .dispatchEvent(new MouseEvent('click', {bubbles: true}));
                const closed = new Promise((done) => document.querySelector('#pin-dialog')
                    .addEventListener('close', done, {once: true}));
                press(first);
                document.querySelector('#pin-dialog').close();
                press(second);
                await closed;
            }""",
            [PIN.format(8), PIN.format(9)],
        )
        page.check("#pin-uses input[value=relay1]")
        assert page.locator("#pin-option").count() == 1 and page.is_enabled("#pin-keep")
        page.keyboard.press("Escape")
        # The bus's clock and address are judged too, and an unused pin is free again.
        _give(page, 8, "scl", clock="20000", address="65", option="off")
        assert page.text_content("#pins-problems") == "The I2C clock is 50000 to 1000000 Hz."
        page.click(PIN.format(8))
        assert page.input_value("#pin-clock") == "20000"
        page.fill("#pin-clock", "")
        assert "I2C clock is" in page.text_content("#pin-problem")
        page.click("#pin-keep")
        page.click(PIN.format(8))
        assert page.input_value("#pin-clock") == ""
        page.fill("#pin-clock", "100000")
        page.click("#pin-keep")
        assert page.text_content("#pins-problems") == ""
        assert "INA228 address: auto → 0x41" in page.text_content("#pins-diff")
        page.click(PIN.format(6))
        page.check("#pin-uses input[value='']")
        assert page.is_disabled("#pin-keep")  # nothing is there: ALERT is on GPIO5
        page.keyboard.press("Escape")
        assert not any(url.startswith("/api/pins/") for url in seen.asked())


def test_two_roles_staged_on_one_pin_are_drawn_as_a_conflict_on_it(
    browser: Any, tmp_path: Path
) -> None:
    with pins_page(browser, tmp_path) as (seen, served):
        page = seen.page
        assert served.post("/api/pins/stage", {"relay1": 4, "alert": 45}).status == 200
        page.click("#pins-refresh")
        page.wait_for_selector(f"{PIN.format(4)}[data-conflict=true]")
        assert page.text_content(f"{PIN.format(4)} .word") == "conflict · INA228 SDA and Relay 1"
        assert page.get_attribute(PIN.format(45), "data-conflict") == "true"
        marks = {page.get_attribute(f"{PIN.format(gpio)} .mark", "d") for gpio in (4, 45)}
        assert marks == {page.get_attribute(".pin-legend .mark[data-state=conflict]", "d")}
        page.hover(PIN.format(45))
        about = page.text_content("#pin-about")
        assert "Conflict: INA228 ALERT: GPIO45 is reserved by the board." in about
        page.click(PIN.format(4))
        assert page.locator("#pin-uses input:checked").count() == 0
        assert page.is_disabled("#pin-keep")
        page.check("#pin-uses input[value=sda]")  # only SDA is left here
        page.click("#pin-keep")
        assert "Relay 1 has no pin." in page.text_content("#pins-problems")
        page.mouse.move(5, 5)
        assert page.text_content("#pin-about h3").startswith("left 4, GPIO4")


def test_the_pins_are_set_with_the_keyboard_alone(browser: Any, tmp_path: Path) -> None:
    """From the top of the page to an applied map with no pointer: the board is
    one tab stop, the arrows move on it, Enter opens a pin, Escape closes it."""
    with pins_page(browser, tmp_path, wiring="relay1=8") as (seen, served):
        page = seen.page

        def focused() -> str:
            return str(
                page.evaluate(
                    "() => document.activeElement.getAttribute('aria-label')"
                    " ?? document.activeElement.id"
                )
            )

        def tab_to(name: str) -> None:
            for _ in range(60):
                page.keyboard.press("Tab")
                if focused() == name:
                    return
            raise AssertionError(f"no tab stop {name!r}: at {focused()!r}")

        assert page.locator(".pin[tabindex='0']").count() == 1
        tab_to("left 1, 3.3, power, reserved by the board")
        stops = []
        for key, gpio in (
            ("ArrowUp", "left 1, 3.3, power, reserved by the board"),
            ("ArrowDown", "left 2, 3.3, power, reserved by the board"),
            ("ArrowLeft", "left 2, 3.3, power, reserved by the board"),
            ("ArrowRight", "right 2, GPIO43, console UART, reserved by the board"),
            ("ArrowRight", "right 2, GPIO43, console UART, reserved by the board"),
            ("End", "right 22, GND, ground, reserved by the board"),
            ("ArrowDown", "right 22, GND, ground, reserved by the board"),
            ("ArrowLeft", "left 22, G, ground, reserved by the board"),
            ("Home", "left 1, 3.3, power, reserved by the board"),
            ("x", "left 1, 3.3, power, reserved by the board"),
            ("Control+ArrowDown", "left 1, 3.3, power, reserved by the board"),
        ):
            page.keyboard.press(key)
            stops.append((focused(), gpio))
        assert [got for got, _ in stops] == [want for _, want in stops]
        for _ in range(11):
            page.keyboard.press("ArrowDown")
        assert focused() == "left 12, GPIO8, free"
        assert "left 12, GPIO8: free" in page.text_content("#pin-about")
        described = page.evaluate("() => document.activeElement.getAttribute('aria-describedby')")
        assert described == "pin-about"
        assert page.locator(".pin[tabindex='0']").count() == 1
        # Space opens it, Escape closes it and the focus is back on the pin.
        page.keyboard.press("Space")
        page.wait_for_selector("#pin-dialog[open]")
        page.keyboard.press("Escape")
        page.wait_for_selector("#pin-dialog:not([open])", state="attached")
        assert focused() == "left 12, GPIO8, free"
        # Enter opens it with the focus on the uses; the arrows choose, Enter keeps.
        page.keyboard.press("Enter")
        page.wait_for_selector("#pin-dialog[open]")
        for _ in range(2):
            page.keyboard.press("ArrowUp")  # from unused, past Relay 2, to Relay 1
        assert page.is_checked("#pin-uses input[value=relay1]")
        page.keyboard.press("Tab")
        assert focused() == "pin-option"
        page.keyboard.press("Tab")
        assert focused() == "pin-keep"
        page.keyboard.press("Enter")
        page.wait_for_selector("#pin-dialog:not([open])", state="attached")
        assert focused() == "left 12, GPIO8, Relay 1, in use"
        tab_to("pins-apply")
        page.keyboard.press("Enter")
        page.wait_for_selector("#confirm-dialog[open]")
        page.keyboard.press("Tab")
        assert focused() == "confirm-go"
        page.keyboard.press("Enter")
        _result(page, "Applied")
        assert served.get("/api/pins").body["live"]["relay1"] == 8
        assert page.text_content("[data-qualifier=pins]") == "verified"


def test_apply_waits_for_what_is_running_and_for_the_instruments_own_table(
    browser: Any, tmp_path: Path
) -> None:
    with pins_page(browser, tmp_path) as (seen, served):
        page = seen.page
        _give(page, 18, "sda")
        assert page.is_enabled("#pins-apply")
        assert served.post("/api/capture/start", {"profile": "STATe"}).status == 200
        page.wait_for_selector("#pins-apply:disabled")
        assert "capture runs" in page.text_content("#pins-reason")
        assert page.is_disabled("#pins-defaults")
        assert page.is_enabled("#pins-discard")  # the draft is the page's own
        served.post("/api/capture/stop")
        served.capture_idle()
        page.wait_for_selector("#pins-apply:enabled")
        # A relay away from rest is serve's to refuse, beside the buttons.
        seen.refusals.add(409)
        assert served.post("/api/relays/bypass", {"state": "ON", "force": True}).status == 200
        _apply(page)
        page.wait_for_function(
            "() => document.querySelector('#pins-refusal').textContent.includes('not at rest')"
        )
        assert served.post("/api/relays/nullpath", {"state": "OFF"}).status == 200
        # An order that cannot be staged, or a map that cannot be read, is said.
        page.route("**/api/pins/stage", lambda route: route.fulfill(status=423, json={}))
        seen.refusals.add(423)
        refused = "() => document.querySelector('#pins-refusal').textContent.startsWith('423')"
        _apply(page)
        page.wait_for_function(refused)
        page.evaluate("() => document.querySelector('#pins-refusal').textContent = ''")
        page.click("#pins-discard")
        page.wait_for_function(refused)
        page.unroute("**/api/pins/stage")
        # Another table: the view only shows, but for the defaults.
        table = {"package": "0x1", "instrument": "0x2", "matches": False}
        body = served.get("/api/pins").body
        page.route(
            "**/api/pins", lambda route: route.fulfill(status=200, json={**body, "table": table})
        )
        page.click("#pins-refresh")
        page.wait_for_selector("#pins-notice:not([hidden])")
        assert "0x2" in page.text_content("#pins-notice")
        assert "pin table is another" in page.text_content("#pins-reason")
        page.click(PIN.format(8))
        page.check("#pin-uses input[value=scl]")
        assert page.is_disabled("#pin-keep")
        assert "Read only" in page.text_content("#pin-problem")
        page.keyboard.press("Escape")
        assert page.is_disabled("#pins-apply") and page.is_enabled("#pins-defaults")
        page.unroute("**/api/pins")
        page.route("**/api/pins", lambda route: route.fulfill(status=423, json={"detail": "held"}))
        page.click("#pins-refresh")
        page.wait_for_function(
            "() => document.querySelector('#pins-refusal').textContent === '423 · held'"
        )
        assert page.text_content("[data-status=pins]") == "not known (423)"
        show(page, "settings")
        page.wait_for_function(
            "() => document.querySelector('#settings-pins-refusal').textContent === '423 · held'"
        )


def test_no_text_of_the_page_names_a_relays_part_but_the_recommendation(
    watched: Watched, served: Served
) -> None:
    page = watched.page
    named = re.compile(r"\bK[1-4]\b|HW-?316|\bClick\b|relay2click|LCA717|PhotoMOS")
    visible = """() => {
        const copy = document.body.cloneNode(true);
        copy.querySelectorAll('[data-recommendation], script').forEach(n => n.remove());
        const words = [copy.textContent];
        for (const node of copy.querySelectorAll('*')) {
            for (const name of ['title', 'aria-label', 'placeholder', 'alt']) {
                words.push(node.getAttribute(name) ?? '');
            }
        }
        return words.join('\\n');
    }"""
    for view in VIEWS:
        show(page, view)
        if view == "sequences":  # its results are reports, the image's among them
            page.click("article[data-sequence=HEALTH] button[data-action=run]")
            page.wait_for_function(
                "() => document.querySelector('#sequence-state').textContent.includes('done')"
            )
            assert "model=kleidos-meter" in page.text_content("#sequence-results")
        if view == "relays":
            page.wait_for_function("() => document.querySelector('#k1-pin').textContent !== ''")
        if view == "pins":
            page.wait_for_selector(".pin[data-state]")
            for terminal in range(page.locator(".pin").count()):
                page.locator(".pin").nth(terminal).hover()
                assert named.findall(str(page.text_content("#pin-about"))) == []
        if view == "health":
            page.wait_for_function("() => document.querySelector('#health-elf').textContent !== ''")
        text = str(page.evaluate(visible))
        assert [text[max(0, m.start() - 120) : m.end()] for m in named.finditer(text)] == [], view
    note = str(page.text_content("[data-recommendation]"))
    assert "LCA717" in note and "HW-316" in note


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_board_is_legible_in_both_themes_and_scrolls_on_a_phone(
    browser: Any, tmp_path: Path, scheme: str
) -> None:
    viewport = {"width": 390, "height": 844}
    with pins_page(browser, tmp_path, viewport=viewport, color_scheme=scheme, has_touch=True) as (
        seen,
        served,
    ):
        page = seen.page
        assert served.post("/api/pins/stage", {"relay1": 4}).status == 200  # a conflict drawn too
        page.click("#pins-refresh")
        page.wait_for_selector(f"{PIN.format(4)}[data-conflict=true]")
        box = page.evaluate(
            """() => {
                const board = document.querySelector('#board').getBoundingClientRect();
                const frame = document.querySelector('.board-frame');
                return {board: board.width, shown: frame.clientWidth, whole: frame.scrollWidth,
                    page: document.documentElement.scrollWidth,
                    size: getComputedStyle(document.querySelector('.pin .silk')).fontSize};
            }"""
        )
        assert box["board"] == 860 and box["size"] == "13px"  # its own size, not shrunk
        assert box["whole"] >= 860 > box["shown"] and box["page"] == 390
        # A pin of the far rail comes into view with the focus, and a tap opens it.
        page.focus(PIN.format(21))
        left, right = page.evaluate(
            """() => { const r = document.activeElement.querySelector('.terminal')
                .getBoundingClientRect(); return [r.left, r.right]; }"""
        )
        assert left >= 0 and right <= 390
        page.tap(PIN.format(21))
        page.wait_for_selector("#pin-dialog[open]")
        assert "GPIO21" in page.text_content("#pin-what")
        page.keyboard.press("Escape")
        # Every text of the drawing reads at 4.5:1 and every mark at 3:1 on its ground.
        colors = page.evaluate(
            """() => {
                const resolve = (text) => { const c = document.createElement('canvas');
                    const g = c.getContext('2d'); g.fillStyle = text; g.fillRect(0, 0, 1, 1);
                    return [...g.getImageData(0, 0, 1, 1).data].slice(0, 3); };
                const token = (name) => resolve(getComputedStyle(document.documentElement)
                    .getPropertyValue(name).trim());
                const grounds = [token('--surface'), token('--bg-2')];
                return {grounds, texts: [...document.querySelectorAll('#board text')].map(t =>
                        resolve(getComputedStyle(t).fill)),
                    marks: [...document.querySelectorAll('.pin .mark')].map(m =>
                        resolve(getComputedStyle(m).stroke))};
            }"""
        )
        for ground in colors["grounds"]:
            assert min(_contrast(text, ground) for text in colors["texts"]) >= 4.5
            assert min(_contrast(mark, ground) for mark in colors["marks"]) >= 3.0


# -- the panel's bench scenario, on the simulator --------------------------------------------


def test_the_page_scenario_of_the_bench_runs_on_the_simulator(browser: Any, tmp_path: Path) -> None:
    """``panel_bench.run_page_scenario``, which the bench runs on the meter, on
    ``serve --sim`` with short spans: the live map drawn, a wrong SDA put back
    with its sentence, the readout's mean the capture's, a run with the
    averaging on named in its meta.json, and the three views' images."""
    data_dir = tmp_path / "runs"
    argv = ["--sim", "--data-dir", str(data_dir), "--scpi-passthrough", "0"]
    with serving(argv) as running:
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        seen = watch(page, running)
        seen.refusals.add(409)  # the apply that is put back
        shots = Shots(tmp_path / "shots", "2026-11-26-elf-0123456789abcdef")
        try:
            report = run_page_scenario(
                page, running, data_dir, Spans(state=2, null=1, trace=1, chart=2, averaged=1), shots
            )
        finally:
            COVERAGE.close(context)
        assert seen.unexpected() == []
        assert seen.foreign() == []
    assert running.status == [0], running.err.getvalue()
    assert (report["pins"]["live"], report["pins"]["reverted"]) == ("default", REVERTED)
    assert report["pins"]["refusal"].startswith("409 · 117 Pin map reverted;")
    assert report["averaged"]["noise_reduction"] == AVERAGING
    assert report["selftest"] == "0: every part passes."
    assert report["heap"] == dict.fromkeys(HEAP_KEYS, 0)
    mean, stats = report["state"]["readout_mean_na"], report["state"]["statistics_mean_na"]
    assert mean == pytest.approx(stats, rel=1e-3)
    assert sorted(report["shots"]) == ["health", "live", "pins", "pins-reverted"]
    for name in report["shots"].values():
        raw = (tmp_path / "shots" / name).read_bytes()
        assert raw.startswith(b"\x89PNG\r\n\x1a\n") and name.endswith(
            "-2026-11-26-elf-0123456789abcdef.png"
        )


@pytest.mark.parametrize(
    ("text", "nanoamps"),
    [
        ("Current 42.31 µA (in view mean 42.30 µA, RMS 43 µA", 42_300.0),
        ("Newest at 1 s: Current 1.000 mA (in view mean -1.500 mA, RMS", -1_500_000.0),
        ("Current 900 nA (in view mean 899.5 nA, RMS", 899.5),
        ("Current 2 A (in view mean 2.000 A, RMS", 2e9),
    ],
)
def test_the_readouts_mean_is_read_in_nanoamps(text: str, nanoamps: float) -> None:
    assert readout_mean(text) == pytest.approx(nanoamps)


@pytest.mark.parametrize(
    ("text", "step"),
    [
        ("Current 42.31 µA (in view mean 42.30 µA, RMS", 10.0),
        ("Current 1 mA (in view mean -1.500 mA, RMS", 1_000.0),
        ("Current 2 A (in view mean 2 A, RMS", 1e9),
    ],
)
def test_the_readouts_last_digit_is_its_step(text: str, step: float) -> None:
    assert readout_step(text) == pytest.approx(step)


def test_a_readout_without_a_mean_is_a_failure() -> None:
    with pytest.raises(AssertionError):
        readout_mean("Current 1.000 mA (nothing in view)")
    with pytest.raises(AssertionError):
        readout_step("Current 1.000 mA (nothing in view)")


# -- the status band never hides what the page scrolls to -----------------------------------

#: The focused element's box and the band's bottom, in the viewport, as one call.
_UNDER_BAND = """() => {
    const band = document.querySelector('.masthead-band').getBoundingClientRect();
    const box = document.activeElement.getBoundingClientRect();
    return [box.top, box.bottom, Math.max(0, band.bottom), innerHeight];
}"""


@pytest.mark.parametrize(("width", "height"), [(1280, 800), (390, 844)])
def test_a_pin_focused_with_the_arrows_is_never_under_the_status_band(
    browser: Any, tmp_path: Path, width: int, height: int
) -> None:
    """WCAG 2.2 SC 2.4.11: down the left rail and back up, every focused pin is
    whole below the band (sticky from 720 px, scrolled away on a phone)."""
    with pins_page(browser, tmp_path, viewport={"width": width, "height": height}) as (seen, _):
        page = seen.page
        page.locator(".pin[tabindex='0']").focus()
        page.keyboard.press("Home")
        hidden = []
        for key in ["ArrowDown"] * 21 + ["ArrowUp"] * 21:
            page.keyboard.press(key)
            top, bottom, band, view = page.evaluate(_UNDER_BAND)
            if top < band - 0.5 or bottom > view + 0.5:
                hidden.append(
                    (page.evaluate("() => document.activeElement.dataset.terminal"), top, band)
                )
        assert hidden == []


@pytest.mark.parametrize(("width", "height"), [(1280, 800), (1280, 900), (1440, 900)])
def test_the_pin_box_stays_in_view_beside_a_low_pin(
    browser: Any, tmp_path: Path, width: int, height: int
) -> None:
    """Pointing at the last terminal of either rail, its description is on screen."""
    with pins_page(browser, tmp_path, viewport={"width": width, "height": height}) as (seen, _):
        page = seen.page
        for rail in ("left", "right"):
            pin = page.locator(f".pin[data-rail='{rail}'][data-terminal='22']")
            pin.scroll_into_view_if_needed()
            pin.hover()
            page.wait_for_function(
                "rail => document.querySelector('#pin-about h3')"
                "?.textContent.startsWith(rail + ' 22')",
                arg=rail,
            )
            top, bottom, band, view = page.evaluate(
                """() => {
                    const band = document.querySelector('.masthead-band').getBoundingClientRect();
                    const box = document.querySelector('#pin-about').getBoundingClientRect();
                    return [box.top, box.bottom, Math.max(0, band.bottom), innerHeight];
                }"""
            )
            assert band - 0.5 <= top and bottom <= view + 0.5, (rail, top, bottom, band, view)


@pytest.mark.parametrize(("width", "height"), [(1280, 900), (390, 844)])
def test_the_link_of_the_view_shown_leaves_its_title_in_sight(
    browser: Any, served: Served, width: int, height: int
) -> None:
    """Clicking the view already shown, or loading a view's fragment, scrolls its
    title no higher than the band's bottom."""
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    seen = watch(page, served)
    page.goto(served.url + "#pins")
    _wait_serial(page)
    for view in ("pins", "live", "live", "pins", "pins"):
        page.click(f".views a[href='#{view}']")
        page.wait_for_timeout(100)
        top, band = page.evaluate(
            """view => [
                document.querySelector(`#${view}-title`).getBoundingClientRect().top,
                Math.max(0,
                    document.querySelector('.masthead-band').getBoundingClientRect().bottom),
            ]""",
            view,
        )
        assert top >= band - 0.5, (view, top, band)
    page.goto(served.url + "#live")
    page.reload()
    _wait_serial(page)
    top, band = page.evaluate(
        """() => [
            document.querySelector('#live-title').getBoundingClientRect().top,
            Math.max(0, document.querySelector('.masthead-band').getBoundingClientRect().bottom),
        ]"""
    )
    assert top >= band - 0.5
    COVERAGE.close(context)
    assert seen.unexpected() == [] and seen.foreign() == []


def test_on_a_phone_the_status_band_scrolls_away_and_on_a_desktop_it_stays(
    browser: Any, served: Served
) -> None:
    """At 390 px the band would cover a third of the screen: it is not sticky there."""
    for width, stays in ((390, False), (719, False), (720, True), (1280, True)):
        context = browser.new_context(viewport={"width": width, "height": 844})
        page = context.new_page()
        seen = watch(page, served)
        page.goto(served.url + "#pins")
        page.wait_for_selector(".pin[data-state]")
        page.evaluate("() => window.scrollTo(0, 1500)")
        page.wait_for_timeout(50)
        top = page.evaluate(
            "() => document.querySelector('.masthead-band').getBoundingClientRect().top"
        )
        assert (top == 0) is stays, (width, top)
        COVERAGE.close(context)
        assert seen.unexpected() == [] and seen.foreign() == []


# -- the chart's cost is bounded by the view, not by the smoothing ---------------------------


def test_a_long_smoothing_on_a_short_view_keeps_the_buffers_the_views_size(
    watched: Watched,
) -> None:
    """1 s of smoothing on the shortest view (1 ms over 2400 columns): the window
    is capped at twice the view, named with the choice, and the reduction's
    buffers stay a few times the view's columns; they shrink again after."""
    result = _math(
        watched.page,
        """
        const columns = 2400, columnUs = 1000 / columns;
        const w = m.smoothingWindow(1000000, columnUs, columns);
        const ring = new m.SampleRing(4096, ['i_na']);
        for (let k = 0; k < 4000; k += 1) { ring.push(1000000 + k, () => 5); }
        const ext = new m.ColumnExtrema();
        ext.reduce(ring, 'i_na', 1002000, 1003000, columns, w.half);
        const grown = ext.min.length;
        const big = new m.ColumnExtrema().reduce(ring, 'i_na', 1002000, 1003000, columns, 100000);
        const before = big.min.length;
        big.reduce(ring, 'i_na', 1002000, 1003000, columns, 0);
        const line = m.movingMean(ext, w.span);
        return {w, grown, before, after: big.min.length, mean: line.mean[1200],
                open: m.smoothingWindow(1000000, columnUs).half};
        """,
        None,
    )
    w = result["w"]
    assert w["half"] == 2400 and w["span"] == 4801
    assert w["caption"] == "moving average 2 ms (1 s asked)"
    assert result["open"] == 1_200_000  # what it was uncapped
    assert result["grown"] == 2400 + 2 * 2400
    assert result["before"] == 202_400 and result["after"] == 2400
    assert result["mean"] == 5


def test_four_lanes_of_a_full_ring_reduce_in_one_pass(watched: Watched) -> None:
    """The 5 min view with STATE's four channels and a full ring (262,144
    samples): one pass reduces every lane, as each would alone, faster than the
    four apart, and the frame gate keeps the drawing under half the page's time
    whatever it costs (below)."""
    result = _math(
        watched.page,
        """
        const keys = ['i_na', 'v_mv', 'p_uw', 'die_mc'];
        const n = 262144, ring = new m.SampleRing(n, keys);
        for (let k = 0; k < n; k += 1) {
            const t = k * 1144;
            ring.push(t, (key) =>
                (key === 'i_na' ? (k % 97) : key === 'v_mv' ? 4000 + (k % 5) : k % 13));
        }
        const columns = 2400, t0 = ring.last - 300e6, t1 = ring.last;
        const lanes = keys.map(() => new m.ColumnExtrema());
        m.reduceLanes(ring, keys, t0, t1, columns, 0, lanes);
        let best = Infinity;
        for (let r = 0; r < 5; r += 1) {
            const began = performance.now();
            m.reduceLanes(ring, keys, t0, t1, columns, 0, lanes);
            best = Math.min(best, performance.now() - began);
        }
        let apart = Infinity;
        const each = keys.map(() => new m.ColumnExtrema());
        for (let r = 0; r < 5; r += 1) {
            const began = performance.now();
            keys.forEach((key, lane) => each[lane].reduce(ring, key, t0, t1, columns, 0));
            apart = Math.min(apart, performance.now() - began);
        }
        const alone = new m.ColumnExtrema().reduce(ring, 'v_mv', t0, t1, columns, 0);
        const same = [0, 1200, 2399].every((c) =>
            alone.count[c] === lanes[1].count[c] && alone.sum[c] === lanes[1].sum[c]
            && alone.min[c] === lanes[1].min[c] && alone.max[c] === lanes[1].max[c]);
        return {best, apart, same, held: lanes[0].viewStats().n};
        """,
        None,
    )
    print(
        f"four lanes of 262,144 samples: {result['best']:.1f} ms in one pass,"
        f" {result['apart']:.1f} ms apart"
    )
    assert result["same"]
    assert result["held"] > 260_000
    # The browser tests run with precise block coverage, which keeps the JIT
    # from optimizing, so only the ratio is held here; without it, in Node
    # 26 on the bench Mac, one pass took 28 ms and the four apart 38 ms.
    assert result["best"] < result["apart"]


def test_the_frame_gate_gives_drawing_at_most_half_the_time(watched: Watched) -> None:
    result = _math(
        watched.page,
        """
        const gate = new m.FrameGate(1000 / 30);
        const seen = [gate.ready(0)];
        gate.spent(50);  // a 50 ms frame: the next waits 100 ms
        seen.push(gate.ready(40), gate.ready(99), gate.ready(100));
        gate.spent(5);   // a cheap one: back to the 30 fps cap
        seen.push(gate.ready(120), gate.ready(133.4));
        const held = {follow: false, t0: 1000, t1: 2000};
        return {seen, changes: [
            m.batchChangesView({follow: true, t0: 0, t1: 1}, 5, 0, NaN),
            m.batchChangesView(held, 2500, 0, 100),   // past the view and its margin
            m.batchChangesView(held, 2050, 0, 100),   // inside the margin
            m.batchChangesView(held, 2500, 950, 100), // the ring forgot what the margin holds
            m.batchChangesView(held, 2500, 900, NaN), // no window yet: no margin
        ]};
        """,
        None,
    )
    assert result["seen"] == [True, False, False, True, False, True]
    assert result["changes"] == [True, False, True, True, False]


def test_a_runs_view_names_its_buckets_in_the_caption_and_the_export_header(
    watched: Watched,
) -> None:
    result = _math(
        watched.page,
        """
        const ring = new m.SampleRing(16, ['i_na'], 8);
        ring.pushBucket(0, 900000, () => ({n: 10, mean: 1, rms: 1, min: 0, max: 2}));
        ring.pushBucket(900000, 1500000, () => ({n: 5, mean: 2, rms: 2, min: 1, max: 3}));
        const w = m.smoothingWindow(0, 1000000);
        const frame = {t0: 0, t1: 1500000, columns: 1, columnUs: 1500000, choiceUs: 0,
                       window: w, bucketUs: ring.bucketUs, lanes: []};
        const live = {...frame, bucketUs: 0};
        const subject = {source: 'history', instrument: 'KM-0', noise: null};
        ring.clear();
        return {bucketUs: frame.bucketUs, cleared: ring.bucketUs,
                caption: m.chartCaption(w, null, frame.bucketUs),
                header: Object.fromEntries(m.viewHeader(frame, subject)),
                liveHeader: Object.fromEntries(m.viewHeader(live, subject))};
        """,
        None,
    )
    assert result["bucketUs"] == 900_000 and result["cleared"] == 0
    assert result["caption"] == "mean per pixel, 1 s · run buckets 900 ms"
    header = result["header"]
    assert header["caption"] == result["caption"]
    assert header["bucket_us"] == "900000" and header["source_resolution"].startswith("run buckets")
    assert result["liveHeader"]["bucket_us"] == ""
    assert result["liveHeader"]["source_resolution"] == "every sample"


# -- what of the page's modules ran --------------------------------------------------------


def _run_alone(browser: Any, served: Served, text: str, calls: str) -> js_coverage.Module:
    """What *calls* runs of *text*, served as ``planted.js`` to a page with nothing else."""
    recorder = Recorder({"planted.js": text})
    context = browser.new_context()
    page = context.new_page()
    recorder.start(page)
    page.route("**/blank", lambda route: route.fulfill(content_type="text/html", body="<title>"))
    page.route(
        f"**{js_coverage.SERVED}planted.js",
        lambda route: route.fulfill(content_type="text/javascript", body=text),
    )
    page.goto(served.url + "blank")
    page.evaluate(
        f"async () => {{ const m = await import('{js_coverage.SERVED}planted.js'); {calls} }}"
    )
    recorder.close(context)
    return recorder.modules["planted.js"]


@pytest.mark.parametrize(
    ("module", "function", "calls", "before", "planted", "kind", "left"),
    [
        pytest.param(
            "pins-math.js",
            "gpioText",
            "m.gpioText(null); m.gpioText(7);",
            "    return gpio === null",
            '    if (gpio === -1) {\n        return "planted";\n    }\n',
            "blocks",
            '{ return "planted"; }',
            id="a-branch",
        ),
        pytest.param(
            "chart-math.js",
            "rowsMissed",
            "m.rowsMissed(null, 5); m.rowsMissed(10, 17); m.rowsMissed(10, 3);",
            "    if (expected === null",
            "    const at = n0 < 0 ? -n0 : n0;\n",
            "blocks",
            "? -n0",
            id="an-arm",
        ),
        pytest.param(
            "chart-math.js",
            "rowsMissed",
            "m.rowsMissed(null, 5); m.rowsMissed(10, 17); m.rowsMissed(10, 3);",
            "export function rowsMissed",
            "function planted() {\n    return 1;\n}\n\n",
            "functions",
            "function planted() { return 1; }",
            id="a-function",
        ),
    ],
)
def test_a_planted_dead_branch_lowers_the_figure_and_fails_the_gate(
    browser: Any,
    served: Served,
    module: str,
    function: str,
    calls: str,
    before: str,
    planted: str,
    kind: str,
    left: str,
) -> None:
    """The gate's self-test: a function of the page that its calls run whole, as
    it is and with a dead piece planted in it, under the floors of 100."""
    source = (js_coverage.STATIC / module).read_text(encoding="utf-8")
    start = source.index(f"export function {function}(")
    text = source[start : source.index("\n}\n", start) + 3]
    floors = {"planted.js": js_coverage.Floor(functions=100.0, blocks=100.0)}
    clean = _run_alone(browser, served, text, calls)
    assert (clean.percent("functions"), clean.percent("blocks")) == (100.0, 100.0)
    assert js_coverage.verdict({"planted.js": clean}, floors, [], pure=("planted.js",)) == ([], [])
    assert text.count(before) == 1
    dead = _run_alone(browser, served, text.replace(before, planted + before), calls)
    assert dead.percent(kind) < 100.0
    problems, _ = js_coverage.verdict({"planted.js": dead}, floors, [], pure=("planted.js",))
    assert any(p.startswith(f"planted.js: {kind} ") and "under its floor" in p for p in problems)
    assert [p.split(": ", 2)[2] for p in problems if "no reason is given" in p] == [left]


# -- the documented images (docs/images/panel/, scripts/take_panel_shots.py) ---------------

#: ``scripts/panel_images.py``: which images there are and how a view is signed.
IMAGES = panel_shots.panel_images()

#: ``scripts/take_panel_shots.py``, loaded by its path as ``scripts/`` is not a package.
TAKE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "take_panel_shots.py"


def _retakes(signed: dict[str, str], manifest: dict[str, Any]) -> list[str]:
    """The images whose view renders otherwise than when they were taken, each named."""
    return [
        f"{IMAGES.FOLDER}/{name}: what its view renders changed since it was taken;"
        " retake it with scripts/take_panel_shots.py"
        for name, signature in sorted(signed.items())
        if manifest["images"].get(name, {}).get("signature") != signature
    ]


def _render(
    browser: Any, served: Served, names: set[str] | None = None, before: Any = None
) -> dict[str, str]:
    """Every image's view of the scenario rendered and signed; *before* runs on its page first.

    Each page must end with no error and no foreign request, and no text of a
    view may hold an absolute path.
    """
    signed: dict[str, str] = {}

    def keep(image: Any, page: Any) -> None:
        if before is not None:
            before(page)
        assert IMAGES.path_hits(panel_shots.texts(page)) == [], image.name
        signed[image.name] = panel_shots.signed(page)

    def watching(page: Any) -> Any:
        seen = watch(page, served)
        seen.refusals.add(409)  # the reverted apply's refusal, which the scenario asks for

        def done() -> None:
            COVERAGE.close(page.context)
            assert seen.unexpected() == [] and seen.foreign() == []

        return done

    panel_shots.run(browser, served, keep, watch=watching, names=names)
    return signed


@pytest.fixture
def scene(tmp_path: Path) -> Iterator[Served]:
    """The scenario the images are taken in (``panel_shots.scene``), fresh for each test:
    its states leave their trace on the instrument (the Pins view's last change)."""
    with panel_shots.scene(tmp_path / "shots") as served:
        yield served


def test_every_image_shows_the_view_its_manifest_signed(browser: Any, scene: Served) -> None:
    """The browser half of the images' guard (``tests/firmware/test_panel_images.py``
    holds the files that shape each view): every view rendered again, its
    text masked and its landmarks' boxes, against the signature the manifest
    took. A difference names the image to retake."""
    signed = _render(browser, scene)
    assert sorted(signed) == sorted(image.name for image in IMAGES.IMAGES)
    assert _retakes(signed, IMAGES.load()) == []


def test_a_view_that_renders_otherwise_names_its_image(browser: Any, scene: Served) -> None:
    """A word or a box the view renders otherwise fails the image, and only it."""
    manifest = IMAGES.load()
    name = "console-dark-1280.png"
    retake = [
        f"{IMAGES.FOLDER}/{name}: what its view renders changed since it was taken;"
        " retake it with scripts/take_panel_shots.py"
    ]
    worded = _render(
        browser,
        scene,
        {name},
        lambda page: page.evaluate(
            "() => { document.querySelector('#console-title').textContent = 'Line'; }"
        ),
    )
    assert _retakes(worded, manifest) == retake
    narrowed = _render(
        browser,
        scene,
        {name},
        lambda page: page.evaluate(
            "() => { document.querySelector('#console').style.width = '50%'; }"
        ),
    )
    assert _retakes(narrowed, manifest) == retake
    assert _retakes(_render(browser, scene, {name}), manifest) == []


def test_the_signature_does_not_see_the_font(browser: Any, scene: Served) -> None:
    """Another font, of other widths, signs every view at a desktop's width as the
    manifest did: CI's fonts are not the Mac's the images were taken on."""
    names = {i.name for i in IMAGES.IMAGES if i.width == 1280 and i.scheme == "dark"}

    def other_font(page: Any) -> None:
        page.evaluate(panel_shots.OTHER_FONT)
        page.wait_for_timeout(300)

    signed = _render(browser, scene, names, other_font)
    assert sorted(signed) == sorted(names)
    assert _retakes(signed, IMAGES.load()) == []


def test_the_script_takes_an_image_as_the_manifest_lists_it(browser: Any) -> None:
    """``take_panel_shots.take`` end to end on one image: a stamped PNG of the page,
    and the entry the manifest holds for it."""
    spec = importlib.util.spec_from_file_location("take_panel_shots", TAKE_SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    name = "runs-dark-390.png"
    pngs, entries = script.take({name}, browser)
    assert list(pngs) == list(entries) == [name]
    assert entries[name] == IMAGES.load()["images"][name]
    meta = script.media_meta.record(f"{IMAGES.FOLDER}/{name}")
    assert script.media_meta.read_png_text(pngs[name]) == script.media_meta.png_text(meta)
    assert struct.unpack(">I", pngs[name][16:20])[0] == 390  # the IHDR's width


def test_the_modules_are_run_as_far_as_their_floors_by_the_tests_above(
    request: pytest.FixtureRequest, record_testsuite_property: Any
) -> None:
    """The last test: what the module's pages ran, against ``host/js_floors.toml``.

    Every block and function of the page's pure modules, as Chromium counts
    them, or the reason in the floor file of the ones no test can run; the
    drawn modules at their floors, the blocks they leave printed.
    """
    left_out = request.config.stash.get(DESELECTED, [])
    if not js_coverage.whole_run(request.module, request.session.items, left_out):
        pytest.skip("a selection of the browser tests: the coverage gate judges all of them")
    floors, exclusions = js_coverage.load()
    for name, module in COVERAGE.modules.items():
        figures = ", ".join(
            "{} {:.2f} % ({} of {})".format(kind, module.percent(kind), *getattr(module, kind))
            for kind in ("functions", "blocks")
        )
        record_testsuite_property(name, figures)
        print(f"{name}: {figures}")
        if name not in js_coverage.PURE:
            for block in module.uncovered():
                print(f"    {name}:{block.line}: never ran: {block.text}")
    problems, notes = js_coverage.verdict(COVERAGE.modules, floors, exclusions)
    assert problems == [] and notes == [], "\n".join([*problems, *notes])
