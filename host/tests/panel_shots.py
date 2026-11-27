# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The scenario the panel's documented images are taken in, on ``serve --sim``.

``scripts/take_panel_shots.py`` runs it to take ``docs/images/panel/``, and
``test_panel_browser.py`` runs it again to render every image's view and hold
its signature to the manifest (``scripts/panel_images.py`` says which images
there are and how a view is signed). The scenario is fixed:

- one ``serve --sim``: the clock-driven simulator, whose noise its sample
  count alone decides, wired as the rig is, with a registry made for the
  purpose (the simulator filed as built, then today's calibration accepted
  and restored), its DUT drawing a stepped current (:data:`WAVEFORM`) under a
  noise its sample count alone decides (:data:`NOISE_A`), a recorded run of
  five seconds of ``STATE`` current labeled ``docs``, the HEALTH sequence
  done, and a local build of the instrument's firmware the page compares
  with (``build/firmware.elf``); the data
  directory and the registry are under folders named for them, so the page
  shows ``meter/runs``, never a temporary folder's made-up name;
- on each page (one per scheme and width): the self-test and the bus scan
  run, ``*IDN?`` asked in the console, and the run opened in the chart;
- then every view, and on the dark desktop page the states: smoothing off
  and at 100 ms, a pin's menu open (GPIO18, free), Relay 1 staged on SDA's
  pin (the conflict drawn on GPIO4), and SDA moved to GPIO18 and applied
  (the rig leaves it unwired, so the instrument reverts).

Times, currents and digests differ from one session to the next and the
signature masks them; the clock is the real one, at the simulator's stated
rate (it runs on the host's monotonic clock, and the run's time is the
host's).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import itertools
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

from conftest import SIM_SERIAL, build_registry
from kleidos_meter.panel import recorder, serve
from kleidos_meter.sim import ScriptedWaveform, SimulatedMeter
from panel_harness import Served, serving
from test_registry import accepted, add_events

#: ``scripts/panel_images.py``, loaded by its path: ``scripts/`` is not a package.
IMAGES_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "panel_images.py"

#: Milliseconds the page may take for anything the scenario waits for.
PAGE_WAIT = 20_000

#: What the simulated DUT draws, amperes: 4 mA at rest with a 40 ms burst of
#: 24 mA every 250 ms, so the line, the band and the smoothing each show.
WAVEFORM = ScriptedWaveform([(0.040, 0.024), (0.210, 0.004)])

#: The RMS noise of each sample, amperes; its sample count alone decides it.
NOISE_A = 0.0004

#: Seconds of ``STATE`` current the recorded run holds.
RUN_SECONDS = 5


class DocsMeter(SimulatedMeter):
    """The clock-driven simulator ``serve --sim`` builds, its DUT drawing :data:`WAVEFORM`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **{"waveform": WAVEFORM, "noise_a": NOISE_A, **kwargs})


#: The page's height: the images are of the whole page, so this only starts the layout.
HEIGHT = 900


def panel_images() -> ModuleType:
    """``scripts/panel_images.py`` as a module, loaded once and from its file."""
    name = "meter_panel_images"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, IMAGES_SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # a dataclass looks its own module up
        spec.loader.exec_module(module)
    return sys.modules[name]


#: What a view shows once what it reads of the instrument is in.
READY = {
    "live": "document.querySelector('#chart-readout').textContent !== ''"
    " && document.querySelector('#statistics:not([hidden])') !== null",
    "sequences": "document.querySelector('#sequence-state').textContent.startsWith('HEALTH done')",
    "runs": "document.querySelectorAll('#runs-body tr').length === 1",
    "relays": "document.querySelector('#relay-state').children.length > 0",
    "pins": "document.querySelectorAll('.pin[data-state]').length === 44"
    " && document.querySelector('[data-status=pins]').textContent !== '-'",
    "settings": "document.querySelector('#shunt-verdict').textContent !== ''"
    " && document.querySelector('#settings-pins-list').children.length > 0"
    " && [...document.querySelectorAll('[data-noise] [data-current]')]"
    ".every((cell) => cell.textContent !== '')",
    "health": "document.querySelector('#health-elf').textContent.endsWith('build/firmware.elf')"
    " && document.querySelector('#health-identity').children.length > 0"
    " && document.querySelector('#health-calibration').children.length > 0"
    " && document.querySelector('#health-scan-result').dataset.state === 'done'",
    "console": "document.querySelector('#console-log li .verdict') !== null",
}

#: What the page renders, for the signature: its visible text, and its
#: landmarks. A landmark whose width is the layout's (a section, an article, a
#: canvas, an open dialog, a card) is given as its tag and id with its left
#: edge and width in sixteenths of the page's width; one whose width is its
#: text's (a fieldset, a table, a form, the board's pins) by its tag, id and
#: state alone. No height and no top: a line's height is the font's.
RENDERED = """() => {
    const width = document.documentElement.clientWidth;
    const at = (value) => Math.round((value / width) * 16);
    const laid = "header, nav, main, section, article, aside, footer, canvas, dialog, .card";
    const sized = "fieldset, table, form, .pin";
    const boxes = [];
    for (const node of document.querySelectorAll(`${laid}, ${sized}`)) {
        if (!node.checkVisibility({ visibilityProperty: true })) continue;
        const box = node.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        const id = node.id ? `#${node.id}` : "";
        const gpio = node.dataset.gpio === undefined ? "" : `[gpio=${node.dataset.gpio}]`;
        const state = ["state", "conflict", "changed"]
            .filter((key) => node.dataset[key] !== undefined)
            .map((key) => `[${key}=${node.dataset[key]}]`).join("");
        const left = at(box.left + window.scrollX);
        const where = node.matches(laid) ? ` ${left} ${at(box.width)}` : "";
        boxes.push(`${node.tagName.toLowerCase()}${id}${gpio}${state}${where}`);
    }
    return { text: document.body.innerText, boxes };
}"""

#: Every text of the page: its text nodes, the hidden ones included (a dialog,
#: a hidden view), and the attributes and values a person is shown or read.
TEXTS = """() => [...document.querySelectorAll('body, body *:not(noscript)')].flatMap((node) => [
    ...[...node.childNodes].filter((child) => child.nodeType === Node.TEXT_NODE)
        .map((child) => child.data),
    ...[...node.attributes].filter(({name}) => /^(title|alt|placeholder|value|aria-.*|data-.*)$/
        .test(name)).map(({value}) => value),
    typeof node.value === 'string' ? node.value : '',
]).filter((text) => text.trim() !== '')"""

#: Fonts no page of the panel names, set on the page's root (its policy refuses
#: an inline stylesheet, not the object model): the signature must not see them.
OTHER_FONT = """() => {
    const root = document.documentElement.style;
    root.setProperty("--font-sans", "'Courier New', monospace", "important");
    root.setProperty("--font-mono", "'Times New Roman', serif", "important");
}"""


def signed(page: Any) -> str:
    """The signature of what *page* renders now (``panel_images.signature``)."""
    rendered = page.evaluate(RENDERED)
    signature: str = panel_images().signature(rendered["text"], rendered["boxes"])
    return signature


def texts(page: Any) -> list[str]:
    """Every text of *page*, hidden ones, titles, names and values included."""
    found: list[str] = page.evaluate(TEXTS)
    return found


@contextmanager
def scene(base: Path) -> Iterator[Served]:
    """``serve --sim`` set as the scenario says, its run recorded and its sequence done.

    :param base: An empty directory for the registry, the runs and the build.
    """
    meter = base / "meter"
    registry = build_registry(meter)
    today = dt.datetime.now().astimezone().date().isoformat()
    add_events(registry, accepted(registry, f"{today}-01"))
    elf = base / "build" / "firmware.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"a build of the meter's firmware")
    argv = ["--sim", "--data-dir", str(meter / "runs"), "--registry", str(registry)]
    # A run's name ends in six random hex digits, which the signature masks as
    # a digest when a letter is among them and as a figure when none is: about
    # one name in seventeen. The scene's runs take a letter and digits.
    suffixes = (f"a{count:05x}" for count in itertools.count(1))
    with (
        mock.patch.object(serve, "SimulatedMeter", DocsMeter),
        mock.patch.object(recorder, "secrets", SimpleNamespace(token_hex=lambda _: next(suffixes))),
        serving([*argv, "--elf", str(elf), "--scpi-passthrough", "0"]) as served,
    ):
        assert served.post("/api/calibration/restore").status == 200
        order = {"profile": "STATE", "channels": ["CURR"], "seconds": RUN_SECONDS, "label": "docs"}
        assert served.post("/api/capture/start", order).status == 200
        served.capture_idle()
        served.sequence("HEALTH")
        yield served


def show(page: Any, view: str) -> None:
    """Show a view by its link, as a person does."""
    page.click(f"nav.views a[href='#{view}']")
    page.wait_for_selector(f"section#{view}:not([hidden])")


def _ready(page: Any, view: str) -> None:
    page.wait_for_function(f"() => {READY[view]}")
    page.wait_for_timeout(300)  # what the view draws after its last read
    page.evaluate("() => window.scrollTo(0, 0)")


def _act_out(page: Any) -> None:
    """The self-test, the bus scan, ``*IDN?`` in the console, and the run opened in the chart."""
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
    page.wait_for_selector("#console-log li .verdict")
    show(page, "runs")
    page.click("#runs-body button")
    page.wait_for_selector("section#live:not([hidden])")


def _smoothing(page: Any, label: str) -> None:
    page.click(f"#chart-smoothing label:has-text('{label}')")
    page.wait_for_function(
        "label => [...document.querySelectorAll('#chart-smoothing input')]"
        ".some((input) => input.checked && input.closest('label').textContent.trim() === label)",
        arg=label,
    )
    page.wait_for_timeout(300)


PIN = ".pin[data-gpio='{}']"


def _states(page: Any, served: Served, keep: Keep, images: dict[str, Any]) -> None:
    """The states the documents explain, each kept as its image once it shows."""
    show(page, "live")
    _ready(page, "live")
    _smoothing(page, "Off")
    keep(images["live-smoothing-off-dark-1280.png"], page)
    _smoothing(page, "100 ms")
    keep(images["live-smoothing-100ms-dark-1280.png"], page)
    _smoothing(page, "Off")

    show(page, "pins")
    _ready(page, "pins")
    page.click(PIN.format(18))
    page.wait_for_selector("#pin-dialog[open]")
    page.check("#pin-uses input[value='sda']")
    keep(images["pins-menu-dark-1280.png"], page)
    page.click("#pin-form button[value=cancel]")
    page.wait_for_selector("#pin-dialog:not([open])", state="attached")

    assert served.post("/api/pins/stage", {"relay1": 4}).status == 200
    page.click("#pins-refresh")
    page.wait_for_selector(f"{PIN.format(4)}[data-conflict=true]")
    page.hover(PIN.format(4))
    keep(images["pins-conflict-dark-1280.png"], page)
    _discard(page)

    page.click(PIN.format(18))
    page.wait_for_selector("#pin-dialog[open]")
    page.check("#pin-uses input[value='sda']")
    page.click("#pin-keep")
    page.wait_for_selector("#pin-dialog:not([open])", state="attached")
    page.click("#pins-apply")
    page.wait_for_selector("#confirm-dialog[open]")
    page.click("#confirm-go")
    page.wait_for_function(
        "() => document.querySelector('#pins-result').textContent.startsWith('Reverted')"
    )
    page.mouse.move(5, 5)
    page.wait_for_timeout(300)
    keep(images["pins-reverted-dark-1280.png"], page)
    _discard(page)


def _discard(page: Any) -> None:
    """Discard the draft, and wait until the board is drawn again from the live map."""
    page.click("#pins-discard")
    page.wait_for_function("() => document.querySelector('#pins-diff').textContent === ''")
    page.wait_for_selector("#pins-discard:disabled")
    page.wait_for_timeout(300)


#: What is done with an image's view once it shows: the page is passed as it is.
Keep = Callable[[Any, Any], None]


def run(
    browser: Any,
    served: Served,
    keep: Keep,
    *,
    watch: Callable[[Any], Callable[[], None]] | None = None,
    names: set[str] | None = None,
) -> None:
    """Show every image's view on *served* and hand each to *keep* with its page.

    :param browser: A Playwright browser.
    :param served: The scenario's serve (:func:`scene`).
    :param keep: Called with the image (``panel_images.Image``) and the page.
    :param watch: Given each new page before it loads; returns what to call
        when the page is done (a test checks there what the page wrote).
    :param names: Only these images (all by default).
    """
    module = panel_images()
    images = {image.name: image for image in module.IMAGES}
    wanted = set(images) if names is None else names
    for scheme, width in module.TAKES:
        take = [
            image
            for image in module.IMAGES
            if (image.scheme, image.width) == (scheme, width) and image.name in wanted
        ]
        if not take:
            continue
        context = browser.new_context(
            color_scheme=scheme, viewport={"width": width, "height": HEIGHT}
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_WAIT)
        done = watch(page) if watch is not None else None
        page.goto(served.url)
        page.wait_for_function(
            "serial => document.querySelector('[data-status=serial]').textContent.includes(serial)",
            arg=SIM_SERIAL,
        )
        _act_out(page)
        show(page, "settings")
        _ready(page, "settings")
        for image in take:
            if image.state is None:
                show(page, image.view)
                _ready(page, image.view)
                keep(image, page)
        if any(image.state is not None for image in take):
            _states(
                page, served, lambda image, shown: _keep_wanted(keep, wanted, image, shown), images
            )
        if done is not None:
            done()
        context.close()


def _keep_wanted(keep: Keep, wanted: set[str], image: Any, page: Any) -> None:
    if image.name in wanted:
        keep(image, page)
