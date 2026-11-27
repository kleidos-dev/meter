# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's images in ``docs/images/panel/``: which there are, and what holds each to its view.

Every view of the panel's page (:data:`VIEWS`) is taken dark and light at a
desktop's width and dark at a phone's, and a few states the documents explain
(:data:`STATES`) dark at the desktop's, by ``scripts/take_panel_shots.py`` on
``kleidos-meter serve --sim``. The pixels are not what holds an image to its
view: fonts differ between the Mac the images are taken on and CI, so a pixel
comparison would fail for no change. ``manifest.json`` holds two things per
image instead:

- ``sources``: the files that shape its view, each by its SHA-256, and their
  digest together (:func:`source_digests`): the page's skeleton and the view's
  own section of ``index.html``, the stylesheet, the page's shell and the
  view's ES modules with every module they import, the schema the page is
  drawn from, and the board's pin table for the views that draw it. A
  host-only test recomputes them (``tests/firmware/test_panel_images.py``);
- ``signature``: the SHA-256 of what the view rendered (:func:`signature`):
  its visible text with every figure and digest masked, and its landmarks:
  the left edge and width, in sixteenths of the page's width, of those the
  layout sizes (sections, articles, canvases, an open dialog, cards), which
  no font moves, and only the presence and state of those their text sizes
  (fieldsets, tables, forms, the board's pins). The browser test renders
  every image's view again and recomputes it, and holds it unchanged under
  another font (``host/tests/test_panel_browser.py``).

Either differing names the image: retake it. Bench images (``kind`` ``bench``,
DO-05) are screenshots of the panel over the cable to the meter, filed under
``bench/`` by ``scripts/file_bench_shots.py``: outside the retake check, each
carries its date, the firmware and the image's ``elf=`` it ran, the
instrument, the rig, who checked it by eye for private bench data and the
SHA-256 of the file filed (:func:`bench_findings`). No image is saved whose
view's text holds an absolute path (:func:`path_hits`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "docs/images/panel"
MANIFEST = f"{FOLDER}/manifest.json"
#: The bench's images, under :data:`FOLDER` (their manifest keys start with it).
BENCH = "bench"
SCHEMA = "kleidos.meter.panel-images/1"
PANEL = "host/kleidos_meter/panel"
STATIC = f"{PANEL}/static"
INDEX = f"{STATIC}/index.html"

#: The views of the page, in its navigation's order, with the names the documents give them.
VIEWS: dict[str, str] = {
    "live": "Live",
    "sequences": "Sequences",
    "runs": "Runs",
    "relays": "Relays",
    "pins": "Pins",
    "settings": "Settings",
    "health": "Calibration and health",
    "console": "SCPI console",
}

#: The ES module that draws each view; what it imports is followed.
VIEW_MODULES: dict[str, str] = {
    "live": "live.js",
    "sequences": "sequences.js",
    "runs": "runs.js",
    "relays": "relays.js",
    "pins": "pins.js",
    "settings": "settings.js",
    "health": "health.js",
    "console": "console.js",
}

#: The page's shell, around every view: what it imports is followed, the views' modules excepted.
SHELL_MODULES = ("main.js", "status.js", "session.js", "views.js")

#: Files beyond the page's that shape every view: the schema it is drawn from.
COMMON_FILES = (f"{PANEL}/schema.py",)

#: Files beyond the page's that shape one view: the board the pin views draw.
VIEW_FILES: dict[str, tuple[str, ...]] = {
    "pins": ("host/kleidos_meter/board_pins.json",),
    "settings": ("host/kleidos_meter/board_pins.json",),
}

#: The schemes and widths every view is taken in: dark and light on a desktop, dark on a phone.
TAKES: tuple[tuple[str, int], ...] = (("dark", 1280), ("light", 1280), ("dark", 390))

#: The states the documents explain, each on its view, dark on a desktop: name, view, what it shows.
STATES: tuple[tuple[str, str, str], ...] = (
    ("live-smoothing-off", "live", "the chart with smoothing off"),
    ("live-smoothing-100ms", "live", "the chart smoothed over 100 ms"),
    ("pins-menu", "pins", "a pin's menu open"),
    ("pins-conflict", "pins", "a conflict drawn on a pin"),
    ("pins-reverted", "pins", "an apply the instrument reverted"),
)


@dataclass(frozen=True)
class Image:
    """One image of the panel.

    :ivar name: Its file's name in :data:`FOLDER`.
    :ivar view: The view it shows (a key of :data:`VIEWS`).
    :ivar scheme: ``dark`` or ``light``.
    :ivar width: The page's width, CSS pixels.
    :ivar state: The state of :data:`STATES` it shows, None for the view as it opens.
    :ivar shows: What it shows, in words.
    """

    name: str
    view: str
    scheme: str
    width: int
    state: str | None
    shows: str

    @property
    def path(self) -> str:
        """Its path from the repository's root."""
        return f"{FOLDER}/{self.name}"


def _images() -> tuple[Image, ...]:
    out = [
        Image(
            f"{view}-{scheme}-{width}.png",
            view,
            scheme,
            width,
            None,
            f"the {title} view, {scheme}, {width} px wide",
        )
        for scheme, width in TAKES
        for view, title in VIEWS.items()
    ]
    out += [
        Image(f"{state}-dark-1280.png", view, "dark", 1280, state, f"{shows}, dark, 1280 px wide")
        for state, view, shows in STATES
    ]
    return tuple(out)


#: Every image the script takes, in the order it takes them.
IMAGES: tuple[Image, ...] = _images()
BY_NAME: dict[str, Image] = {image.name: image for image in IMAGES}


# ---------------------------------------------------------------- sources

Reader = Callable[[str], bytes]


def read_file(path: str) -> bytes:
    """The bytes of *path*, from the repository's root."""
    return (ROOT / path).read_bytes()


_IMPORT = re.compile(r"""\bfrom\s+["']\./([\w-]+\.js)["']""")
_SECTION_OPEN = re.compile(r'<section id="([\w-]+)" class="card" data-view="[\w-]+"')


def imports(text: str) -> list[str]:
    """The sibling modules an ES module imports, in its order."""
    return _IMPORT.findall(text)


def closure(entries: Iterable[str], read: Reader, stop: Iterable[str] = ()) -> list[str]:
    """The modules *entries* are and import, transitively, sorted; *stop* is never followed."""
    stopped = set(stop)
    seen: set[str] = set()
    todo = list(entries)
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        text = read(f"{STATIC}/js/{module}").decode("utf-8")
        todo += [name for name in imports(text) if name not in stopped]
    return sorted(seen)


def view_sections(html: str) -> tuple[str, dict[str, str]]:
    """``index.html`` split: its skeleton with every view's section cut, and each view's section.

    A view's section is a ``<section … data-view>`` of the page's ``main``, from
    its opening tag to the ``</section>`` that closes it (nested sections counted).

    :raises ValueError: When a view's section does not close.
    """
    sections: dict[str, str] = {}
    skeleton = html
    for found in _SECTION_OPEN.finditer(html):
        depth = 0
        for tag in re.finditer(r"<section\b|</section>", html[found.start() :]):
            depth += 1 if tag.group(0) == "<section" else -1
            if depth == 0:
                end = found.start() + tag.end()
                break
        else:
            raise ValueError(f"{INDEX}: the section of {found.group(1)} does not close")
        sections[found.group(1)] = html[found.start() : end]
    for text in sections.values():
        skeleton = skeleton.replace(text, "")
    return skeleton, sections


def _sha(data: bytes) -> str:
    """``sha256:`` and the hex digest of *data*: tagged, so no scanner reads it as a key."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def source_digests(view: str, read: Reader = read_file) -> dict[str, str]:
    """What shapes *view*, each by its SHA-256: the skeleton, the view's section, the files.

    :raises KeyError: When *view* is not a view of the page.
    """
    html = read(INDEX).decode("utf-8")
    skeleton, sections = view_sections(html)
    if view not in sections:
        raise KeyError(f"{INDEX} has no section for the view {view}")
    shell = closure(SHELL_MODULES, read, stop=VIEW_MODULES.values())
    own = closure([VIEW_MODULES[view]], read)
    modules = sorted(set(shell) | set(own))
    out = {
        f"{INDEX}#skeleton": _sha(skeleton.encode("utf-8")),
        f"{INDEX}#{view}": _sha(sections[view].encode("utf-8")),
        f"{STATIC}/panel.css": _sha(read(f"{STATIC}/panel.css")),
    }
    out |= {f"{STATIC}/js/{module}": _sha(read(f"{STATIC}/js/{module}")) for module in modules}
    out |= {path: _sha(read(path)) for path in (*COMMON_FILES, *VIEW_FILES.get(view, ()))}
    return dict(sorted(out.items()))


def combined(digests: dict[str, str]) -> str:
    """One digest of every source's: of ``name sha256`` lines, sorted."""
    return _sha("".join(f"{name} {sha}\n" for name, sha in sorted(digests.items())).encode())


# ---------------------------------------------------------------- the rendered view

#: A hexadecimal digest of six digits or more, letters and digits both (an
#: image's ``elf=``, a table's digest); a run of decimal digits is a figure.
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,}\b")
#: A figure, with its sign, its decimals and the thousands it groups.
_FIGURE = re.compile(r"[-+−]?\d+(?:,\d{3})*(?:\.\d+)?")
#: A figure's unit, which a figure's size picks (nA or µA, ms or s): masked with it.
_UNIT = re.compile(r"#\s?(?:[pnµumkMG]?(?:A|V|W|Ω|Hz|s|°C)|min|h|d|%|ppm|B|KiB|MiB)(?!\w)")


def mask(text: str) -> str:
    """*text* with what changes from one session to the next masked.

    Digests become ``<hex>``, figures ``#`` and a figure's unit goes with it,
    so a time or a current read at another moment reads the same; the words
    stay, and they are what a view's change changes.
    """
    text = _HEX.sub("<hex>", text)
    text = _FIGURE.sub("#", text)
    return _UNIT.sub("#", text)


def lines(text: str) -> list[str]:
    """The non-empty lines of a view's text, masked and with their spaces collapsed."""
    out = []
    for line in text.splitlines():
        squeezed = " ".join(mask(line).split())
        if squeezed:
            out.append(squeezed)
    return out


def signature(text: str, boxes: list[str]) -> str:
    """The SHA-256 of a rendered view: its masked text and its landmarks' boxes."""
    body = json.dumps({"text": lines(text), "boxes": boxes}, ensure_ascii=False, sort_keys=True)
    return _sha(body.encode("utf-8"))


#: An absolute path, a home's shorthand or a file's URL in a text. A route of
#: the API (``/api/...``) is no path, nor is the slash of ``n/a`` or of a URL.
#: The same rule as the browser test's of every view (``_PATH`` there).
PATH = re.compile(
    r"(?<![\w./:~-])/(?!api/)[\w.@~-]+"  # /Users, /dev/cu.x
    r"|(?<!\w)~/|file://|(?<!\w)[A-Za-z]:\\"
)


def path_hits(texts: Iterable[str]) -> list[str]:
    """The texts of a view that hold an absolute path (none may, in an image)."""
    return [text for text in texts if PATH.search(text)]


class PathInView(ValueError):
    """A view's text holds an absolute path: its image is not saved."""


def refuse_paths(image: Image, texts: Iterable[str]) -> None:
    """Refuse *image* when a text of its view holds an absolute path.

    :raises PathInView: Naming the image and the texts.
    """
    hits = path_hits(texts)
    if hits:
        raise PathInView(f"{image.path}: its view's text holds an absolute path: {hits}")


# ---------------------------------------------------------------- the manifest


def entry(image: Image, signed: str, read: Reader = read_file) -> dict[str, Any]:
    """The manifest's entry for *image*, its view rendered to *signed*."""
    digests = source_digests(image.view, read)
    return {
        "kind": "sim",
        "view": image.view,
        "scheme": image.scheme,
        "width": image.width,
        "state": image.state,
        "sources": sorted(digests),
        "digest": combined(digests),
        "signature": signed,
    }


def manifest(entries: dict[str, dict[str, Any]], read: Reader = read_file) -> dict[str, Any]:
    """The manifest of *entries* (name to entry), with every source's SHA-256 once."""
    files: dict[str, str] = {}
    for image in IMAGES:
        if image.name in entries:
            files |= source_digests(image.view, read)
    return {
        "schema": SCHEMA,
        "taken_by": "scripts/take_panel_shots.py",
        "sources": dict(sorted(files.items())),
        "images": {name: entries[name] for name in sorted(entries)},
    }


def dumps(value: dict[str, Any]) -> str:
    """The manifest's text: two-space JSON, keys in order, one newline at the end."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def load(read: Reader = read_file) -> dict[str, Any]:
    """The committed manifest."""
    loaded: dict[str, Any] = json.loads(read(MANIFEST).decode("utf-8"))
    return loaded


def stale(found: dict[str, Any], read: Reader = read_file) -> list[str]:
    """The images whose view's sources changed since they were taken, each with what changed."""
    out = []
    for name, item in sorted(found.get("images", {}).items()):
        if item.get("kind") != "sim":
            continue
        if name not in BY_NAME:
            out.append(f"{FOLDER}/{name}: the manifest lists an image the script does not take")
            continue
        digests = source_digests(BY_NAME[name].view, read)
        if combined(digests) == item["digest"] and sorted(digests) == item["sources"]:
            continue
        was = found.get("sources", {})
        changed = sorted(
            {source for source in digests if was.get(source) != digests[source]}
            | (set(item["sources"]) ^ set(digests))
        )
        out.append(
            f"{FOLDER}/{name}: {', '.join(changed) or 'its sources'} changed since it was taken;"
            " retake it with scripts/take_panel_shots.py"
        )
    return out


def listing(found: dict[str, Any], files: Iterable[str]) -> list[str]:
    """Where the folder and the manifest disagree: an image unlisted, missing or not listed."""
    on_disk = {name for name in files if name.endswith(".png")}
    listed = set(found.get("images", {}))
    out = [f"{FOLDER}/{name}: an image the manifest does not list" for name in on_disk - listed]
    out += [f"{FOLDER}/{name}: listed and not in the folder" for name in listed - on_disk]
    out += [
        f"{FOLDER}/{image.name}: an image the script takes and the manifest does not list"
        for image in IMAGES
        if image.name not in listed
    ]
    return sorted(out)


# ---------------------------------------------------------------- the bench's images

#: What every bench entry holds besides its kind.
BENCH_KEYS = (
    "view",
    "state",
    "scheme",
    "width",
    "date",
    "fw",
    "elf",
    "elf_sha256",
    "instrument",
    "rig",
    "source_commit",
    "taken_by",
    "checked",
    "sha256",
)
_DATE = re.compile(r"\A\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\Z")
_ELF = re.compile(r"\A[0-9a-f]{16}\Z")
_SHA = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\A\d+\.\d+\.\d+\Z")
_SERIAL = re.compile(r"\AKM-[0-9A-F]{10}\Z")


def bench_name(shows: str, date: str, elf: str) -> str:
    """The manifest key of a bench image of *shows* (a view or a state), its date and elf=."""
    return f"{BENCH}/bench-{shows}-{date}-elf-{elf}.png"


def sha256_tag(data: bytes) -> str:
    """*data*'s SHA-256, tagged as every digest of the manifest is."""
    return _sha(data)


def _bench_faults(name: str, item: dict[str, Any], read: Reader) -> list[str]:
    missing = [key for key in BENCH_KEYS if key not in item]
    if missing:
        return [f"it lacks {', '.join(missing)}"]
    out = []
    if not _DATE.match(str(item["date"])):
        out.append("its date is not a date")
    if not _ELF.match(str(item["elf"])):
        out.append("its elf= is not 16 hex digits")
    elif not _SHA.match(str(item["elf_sha256"])) or not str(item["elf_sha256"]).startswith(
        f"sha256:{item['elf']}"
    ):
        out.append("its ELF SHA-256 does not begin with its elf=")
    if not _VERSION.match(str(item["fw"])):
        out.append("its firmware is not a version")
    if not _SERIAL.match(str(item["instrument"])):
        out.append("its instrument is not a serial")
    if item["view"] not in VIEWS:
        out.append("its view is not a view of the page")
    elif item["state"] is not None and (item["state"], item["view"]) not in {
        (state, view) for state, view, _ in STATES
    }:
        out.append("its state is not a state of its view")
    if not str(item["checked"]).strip():
        out.append("it was not checked by eye")
    shows = item["state"] or item["view"]
    if not out and name != bench_name(str(shows), str(item["date"]), str(item["elf"])):
        out.append("its name is not bench/bench-<view or state>-<date>-elf-<elf>.png")
    try:
        data = read(f"{FOLDER}/{name}")
    except FileNotFoundError:
        return ["listed and not in the folder"]
    if sha256_tag(data) != item["sha256"]:
        out.append("its bytes are not the ones filed")
    return out


def bench_findings(found: dict[str, Any], read: Reader = read_file) -> list[str]:
    """What is wrong with the manifest's bench entries: a key, a form, a name or the file."""
    return [
        f"{FOLDER}/{name}: {fault}"
        for name, item in sorted(found.get("images", {}).items())
        if item.get("kind") == "bench"
        for fault in _bench_faults(name, item, read)
    ]
