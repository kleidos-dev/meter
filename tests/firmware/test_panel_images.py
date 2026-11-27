# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's images in ``docs/images/panel/`` are the views their files make.

``scripts/take_panel_shots.py`` takes every view of the panel's page in both
themes at a desktop's width and dark at a phone's, and the states the
documents explain; ``manifest.json`` records, for each image, the files that
shape its view by their SHA-256 (``scripts/panel_images.py``). This guard
holds the folder to them:

- every image the script takes is in the folder and listed, and nothing else
  is; every view of the page has its images;
- no image's view changed since it was taken: a change to a view's markup,
  the stylesheet, a module the view runs or the schema the page is drawn
  from names each image it touches, and only those, with "retake it";
- each image is within the large-file hook's 512 kB, carries its metadata
  record and passes ``scripts/check_metadata.py``;
- the rules the script and the browser half rest on: a view's text masked of
  what changes between sessions, a path in a view's text refused, the
  manifest a function of what was taken.

What the views render is the browser half's to hold
(``host/tests/test_panel_browser.py``, every image's signature).

Host-only: the files read as bytes, no browser.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "take_panel_shots.py"


def _script() -> ModuleType:
    """``scripts/take_panel_shots.py``, which puts ``scripts/`` on the path for its two modules."""
    name = "take_panel_shots"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


TAKE = _script()
IMAGES: Any = sys.modules["panel_images"]
META: Any = sys.modules["media_meta"]
CHECK_SPEC = importlib.util.spec_from_file_location(
    "check_metadata", ROOT / "scripts" / "check_metadata.py"
)
assert CHECK_SPEC and CHECK_SPEC.loader
CHECK = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(CHECK)

MANIFEST: dict[str, Any] = IMAGES.load()
FOLDER: Path = ROOT / IMAGES.FOLDER


def planted(path: str, change: Callable[[bytes], bytes]) -> Callable[[str], bytes]:
    """A reader of the tree with *path* changed by *change*."""

    def read(wanted: str) -> bytes:
        data = IMAGES.read_file(wanted)
        return change(data) if wanted == path else data

    return read


def names_of(found: list[str]) -> set[str]:
    return {line.split(":", 1)[0].rsplit("/", 1)[-1] for line in found}


# -- the folder and the manifest ----------------------------------------------------------


def test_every_image_is_taken_listed_and_in_the_folder() -> None:
    files = [path.relative_to(FOLDER).as_posix() for path in FOLDER.rglob("*") if path.is_file()]
    assert IMAGES.listing(MANIFEST, files) == []
    sim = [name for name, item in MANIFEST["images"].items() if item["kind"] == "sim"]
    assert sorted(sim) == sorted(image.name for image in IMAGES.IMAGES)
    assert {item["kind"] for item in MANIFEST["images"].values()} == {"sim", "bench"}
    assert MANIFEST["schema"] == IMAGES.SCHEMA


def test_every_view_of_the_page_has_its_images_in_every_take() -> None:
    _, sections = IMAGES.view_sections(IMAGES.read_file(IMAGES.INDEX).decode())
    assert set(sections) == set(IMAGES.VIEWS) == set(IMAGES.VIEW_MODULES)
    for scheme, width in IMAGES.TAKES:
        shown = {
            i.view for i in IMAGES.IMAGES if (i.scheme, i.width, i.state) == (scheme, width, None)
        }
        assert shown == set(IMAGES.VIEWS), (scheme, width)
    assert {i.state for i in IMAGES.IMAGES if i.state} == {s for s, _, _ in IMAGES.STATES}


def test_the_listing_names_an_image_unlisted_missing_or_untaken() -> None:
    images = dict(MANIFEST["images"])
    gone = images.pop("live-dark-1280.png")
    found = IMAGES.listing({"images": {**images, "old.png": gone}}, [*images, "stray.png"])
    untaken = "an image the script takes and the manifest does not list"
    assert found == [
        f"{IMAGES.FOLDER}/live-dark-1280.png: {untaken}",
        f"{IMAGES.FOLDER}/old.png: listed and not in the folder",
        f"{IMAGES.FOLDER}/stray.png: an image the manifest does not list",
    ]


# -- what shapes each view ----------------------------------------------------------------


def test_no_view_changed_since_its_image_was_taken() -> None:
    assert IMAGES.stale(MANIFEST) == []


def test_a_views_markup_changed_names_its_images_and_no_other() -> None:
    read = planted(
        IMAGES.INDEX, lambda data: data.replace(b'<h1 id="runs-title">', b'<h1 id="runs-title" x>')
    )
    found = IMAGES.stale(MANIFEST, read)
    assert names_of(found) == {"runs-dark-1280.png", "runs-light-1280.png", "runs-dark-390.png"}
    assert all(f"{IMAGES.INDEX}#runs changed" in line and "retake it" in line for line in found)


def test_the_pages_skeleton_the_stylesheet_or_the_schema_changed_names_every_image() -> None:
    every = {image.name for image in IMAGES.IMAGES}
    for path, change in (
        (IMAGES.INDEX, lambda data: data.replace(b"<title>", b"<title>A ")),
        (f"{IMAGES.STATIC}/panel.css", lambda data: data + b"\n/* another rule */\n"),
        (f"{IMAGES.PANEL}/schema.py", lambda data: data.replace(b"Relay 1", b"Relay one", 1)),
    ):
        found = IMAGES.stale(MANIFEST, planted(path, change))
        assert names_of(found) == every, path
        named = path if path != IMAGES.INDEX else f"{IMAGES.INDEX}#skeleton"
        assert all(named in line for line in found), path


def test_a_module_a_view_runs_changed_names_that_views_images() -> None:
    read = planted(f"{IMAGES.STATIC}/js/pins-math.js", lambda data: data + b"\n// a change\n")
    assert names_of(IMAGES.stale(MANIFEST, read)) == {
        image.name for image in IMAGES.IMAGES if image.view in ("pins", "settings")
    }
    read = planted(f"{IMAGES.STATIC}/js/console.js", lambda data: data + b"\n// a change\n")
    assert names_of(IMAGES.stale(MANIFEST, read)) == {
        image.name for image in IMAGES.IMAGES if image.view == "console"
    }


def test_a_module_the_shell_imports_is_followed() -> None:
    read = IMAGES.read_file
    shell = IMAGES.closure(IMAGES.SHELL_MODULES, read, stop=IMAGES.VIEW_MODULES.values())
    assert "chart-math.js" in shell  # status.js imports it
    assert not set(shell) & set(IMAGES.VIEW_MODULES.values())
    assert IMAGES.closure(["pins.js"], read) == [
        "api.js",
        "dialogs.js",
        "dom.js",
        "pins-math.js",
        "pins.js",
    ]
    assert IMAGES.imports('import {\n    a,\n    b,\n} from "./chart-math.js";\n') == [
        "chart-math.js"
    ]


def test_a_section_that_does_not_close_is_refused() -> None:
    with pytest.raises(ValueError, match="does not close"):
        IMAGES.view_sections('<section id="live" class="card" data-view="live"><section>')
    with pytest.raises(KeyError, match="no section for the view"):
        IMAGES.source_digests("nowhere")


def test_an_entry_the_script_does_not_take_or_with_other_sources_is_stale() -> None:
    entry = dict(MANIFEST["images"]["console-dark-1280.png"])
    found = IMAGES.stale(
        {
            "sources": MANIFEST["sources"],
            "images": {
                "gone.png": entry,
                "console-dark-1280.png": {**entry, "sources": [*entry["sources"], "an/old.js"]},
                "bench-live.png": {"kind": "bench"},
            },
        }
    )
    assert found == [
        f"{IMAGES.FOLDER}/console-dark-1280.png: an/old.js changed since it was taken;"
        " retake it with scripts/take_panel_shots.py",
        f"{IMAGES.FOLDER}/gone.png: the manifest lists an image the script does not take",
    ]


def test_the_manifest_is_a_function_of_what_was_taken() -> None:
    again = IMAGES.manifest(dict(MANIFEST["images"]))
    assert IMAGES.dumps(again) == (ROOT / IMAGES.MANIFEST).read_text(encoding="utf-8")
    entry = IMAGES.entry(IMAGES.BY_NAME["console-dark-1280.png"], "signed")
    assert entry["digest"] == MANIFEST["images"]["console-dark-1280.png"]["digest"]
    assert (entry["kind"], entry["signature"]) == ("sim", "signed")
    # Every digest is tagged, so gitleaks' generic-api-key rule does not read
    # `"…/api.js": "<hex>"` as a key (the kit allows no allowlist for it).
    digests = [*MANIFEST["sources"].values()]
    for item in MANIFEST["images"].values():
        keys = ("digest", "signature") if item["kind"] == "sim" else ("sha256", "elf_sha256")
        digests += [item[key] for key in keys]
    assert all(d.startswith("sha256:") and len(d) == 71 for d in digests)


# -- each image's file --------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MANIFEST["images"]))
def test_each_image_is_within_the_hooks_limit_and_carries_its_record(name: str) -> None:
    path = f"{IMAGES.FOLDER}/{name}"
    raw = (ROOT / path).read_bytes()
    assert len(raw) <= TAKE.MAX_BYTES
    assert META.read_png_text(raw) == META.png_text(META.record(path))
    assert META.png_extra_chunks(raw) == []
    assert CHECK.png_findings(path) == []


def test_every_image_is_a_media_file_with_a_record() -> None:
    listed = {f"{IMAGES.FOLDER}/{name}" for name in MANIFEST["images"]}
    assert listed <= set(META.MEDIA)
    assert [line for line in CHECK.findings() if IMAGES.FOLDER in line] == []


def test_an_image_without_its_record_or_with_a_time_is_found(tmp_path: Path) -> None:
    path = f"{IMAGES.FOLDER}/console-dark-1280.png"
    raw = (ROOT / path).read_bytes()
    chunks = META.png_chunks(raw)
    time = META._chunk(b"tIME", b"\x07\xea\x0b\x1a\x0c\x00\x00")
    text = META._chunk(b"tEXt", b"Software\x00a tool 1.0")
    bare = META.PNG_SIGNATURE + b"".join(
        META._chunk(k, d) for k, d in chunks if k[:1].isupper() or k == b"tRNS"
    )
    stamped = META.stamp_png(bare, META.record(path))
    assert stamped == raw and META.stamp_png(stamped, META.record(path)) == raw
    noisy = bare[:33] + time + text + bare[33:]
    assert META.png_extra_chunks(noisy) == ["tEXt", "tIME"]
    assert META.read_png_text(noisy) == [("Software", "a tool 1.0")]
    assert META.png_extra_chunks(META.stamp_png(noisy, META.record(path))) == []


def test_a_png_cut_short_or_corrupt_is_refused() -> None:
    raw = (ROOT / IMAGES.FOLDER / "console-dark-1280.png").read_bytes()
    for broken, why in (
        (b"GIF89a" + raw[6:], "not a PNG"),
        (raw[:-6], "cut short"),
        (raw[:-5] + b"X" + raw[-4:], "cut short or with a wrong CRC"),
        (raw[:8] + raw[-12:], "without its header"),
    ):
        with pytest.raises(META.MetaError, match=why):
            META.png_chunks(broken)


def test_the_text_a_png_may_carry_is_read_in_its_three_forms() -> None:
    import zlib

    head = (ROOT / IMAGES.FOLDER / "console-dark-1280.png").read_bytes()[:33]
    chunks = [
        META._chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(b"packed")),
        META._chunk(b"iTXt", b"Title\x00\x01\x00en\x00\x00" + zlib.compress("é".encode())),
    ]
    raw = head + b"".join(chunks) + META._chunk(b"IEND", b"")
    assert META.read_png_text(raw) == [("Comment", "packed"), ("Title", "é")]


def test_check_metadata_reports_a_png_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / IMAGES.FOLDER
    folder.mkdir(parents=True)
    (folder / "console-dark-1280.png").write_bytes(b"not a picture")
    shutil.copy(ROOT / "REUSE.toml", tmp_path / "REUSE.toml")
    monkeypatch.setattr(CHECK, "ROOT", tmp_path)
    monkeypatch.setattr(META, "ROOT", tmp_path)
    assert CHECK.png_findings(f"{IMAGES.FOLDER}/console-dark-1280.png") == [
        f"{IMAGES.FOLDER}/console-dark-1280.png: not a PNG"
    ]


def test_a_png_whose_text_is_not_its_record_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = f"{IMAGES.FOLDER}/console-dark-1280.png"
    raw = (ROOT / path).read_bytes()
    chunks = META.png_chunks(raw)
    assert chunks[1] == (b"iTXt", META._itxt("Title", META.record(path).title))
    fixed = META.PNG_SIGNATURE + META._chunk(*chunks[0])
    title = META._chunk(b"iTXt", META._itxt("Title", "Kleidos meter panel: /Users/x/a"))
    rest = b"".join(META._chunk(k, d) for k, d in chunks[2:])
    (tmp_path / IMAGES.FOLDER).mkdir(parents=True)
    (tmp_path / path).write_bytes(fixed + title + rest)
    shutil.copy(ROOT / "REUSE.toml", tmp_path / "REUSE.toml")
    monkeypatch.setattr(CHECK, "ROOT", tmp_path)
    monkeypatch.setattr(META, "ROOT", tmp_path)
    assert CHECK.png_findings(path) == [
        f"{path}: its text chunks are not the record's; retake it",
        f"{path}: a text chunk holds a local path",
    ]


def test_the_records_of_the_panels_images() -> None:
    meta = META.record(f"{IMAGES.FOLDER}/pins-conflict-dark-1280.png")
    assert meta.title == "Kleidos meter panel: Pins, a conflict on a pin, dark, 1280 px"
    assert meta.spdx == "GPL-3.0-or-later" and meta.digital_source_type == "screenCapture"
    longest = max((META.record(f"{IMAGES.FOLDER}/{i.name}") for i in IMAGES.IMAGES), key=len_title)
    assert len(longest.title.encode()) <= 64
    with pytest.raises(META.MetaError, match="no metadata record"):
        META.record(f"{IMAGES.FOLDER}/another.png")


def len_title(meta: Any) -> int:
    return len(meta.title.encode())


def test_no_manifest_lists_no_panel_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path)
    assert META.panel_media() == ()


# -- what the script and the browser half rest on -----------------------------------------


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("Newest at 5.0042 s, window 5.01 s", "Newest at #, window #"),
        ("Current 23.93 mA (min 2.566 mA)", "Current # (min #)"),
        ("elf 5e1f0ba47c2291d3 · table 3b9a0f12", "elf <hex> · table <hex>"),
        ("no error · 0.123456 s", "no error · #"),
        ("Run 20261126T013048Z-STATE-0fce97", "Run #T#Z-STATE-<hex>"),
        ("GPIO18, 1,234 samples, −3.5 %", "GPIO#, # samples, #"),
        ("Relay 1 · in use", "Relay # · in use"),
    ],
)
def test_a_views_text_is_masked_of_what_changes_between_sessions(text: str, masked: str) -> None:
    assert IMAGES.mask(text) == masked


def test_the_signature_sees_words_and_boxes_and_not_figures() -> None:
    base = IMAGES.signature("Mean 7.2 mA\n\n  over  5 s ", ["section#live 1 14"])
    assert base == IMAGES.signature("Mean 9.91 µA\nover 12 ms", ["section#live 1 14"])
    assert base != IMAGES.signature("Median 7.2 mA\nover 5 s", ["section#live 1 14"])
    assert base != IMAGES.signature("Mean 7.2 mA\nover 5 s", ["section#live 1 15"])
    assert IMAGES.lines(" a  b \n\n#c") == ["a b", "#c"]


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("/Users/someone/firmware.elf", True),
        ("the build (/private/tmp/x)", True),
        ("~/runs", True),
        ("file:///x", True),
        ("C:\\runs", True),
        ("build/firmware.elf", False),
        ("GET /api/health", False),
        ("http://127.0.0.1:8040/", False),
        ("n/a, 1 / 2, nV/√Hz", False),
    ],
)
def test_the_path_rule(text: str, path: bool) -> None:
    assert (IMAGES.path_hits([text]) != []) is path


def test_a_view_whose_text_holds_a_path_is_refused() -> None:
    image = IMAGES.BY_NAME["health-dark-1280.png"]
    IMAGES.refuse_paths(image, ["build/firmware.elf", "meter/runs"])
    with pytest.raises(IMAGES.PathInView, match="health-dark-1280.png.*/Users/x/build"):
        IMAGES.refuse_paths(image, ["meter/runs", "/Users/x/build/firmware.elf"])


# -- the script, its browser left out ------------------------------------------------------


def test_the_script_refuses_an_image_over_the_hooks_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    image = IMAGES.BY_NAME["console-dark-1280.png"]
    raw = (FOLDER / image.name).read_bytes()
    monkeypatch.setattr(TAKE.shutil, "which", lambda _tool: None)
    assert TAKE.finish(image, raw) == raw
    monkeypatch.setattr(TAKE, "MAX_BYTES", 1000)
    with pytest.raises(TAKE.Refused, match="over the hook's 1000"):
        TAKE.finish(image, raw)


def test_the_script_recompresses_with_oxipng_when_it_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Done:
        stdout = b"smaller"

    def run(argv: list[str], **kwargs: Any) -> Done:
        calls.append(argv)
        assert kwargs["input"] == b"raw" and kwargs["check"] is True
        return Done()

    monkeypatch.setattr(TAKE.shutil, "which", lambda tool: f"/opt/{tool}")
    monkeypatch.setattr(TAKE.subprocess, "run", run)
    assert TAKE.smaller(b"raw") == b"smaller"
    assert calls == [["/opt/oxipng", "--opt", "4", "--strip", "all", "--stdout", "-"]]


def test_the_script_names_an_unknown_image(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        TAKE.main(["nowhere.png"])
    assert stopped.value.code == 2
    assert "no such image: nowhere.png" in capsys.readouterr().err


def _tree(tmp_path: Path) -> None:
    """What the manifest's sources read, under *tmp_path*."""
    shutil.copytree(
        ROOT / IMAGES.PANEL, tmp_path / IMAGES.PANEL, ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(ROOT / "host/kleidos_meter/board_pins.json", tmp_path / "host/kleidos_meter")


def test_the_script_writes_what_it_took_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path)
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path)
    bench = {"kind": "bench", "date": "2026-11-26", "elf": "80a257970e934ff6"}
    sim = {name: item for name, item in MANIFEST["images"].items() if item["kind"] == "sim"}
    old = {**sim, "bench-live.png": bench}
    (tmp_path / IMAGES.FOLDER).mkdir(parents=True)
    (tmp_path / IMAGES.MANIFEST).write_text(json.dumps({"images": old}), encoding="utf-8")
    entry = IMAGES.entry(IMAGES.BY_NAME["runs-dark-390.png"], "new", IMAGES.read_file)
    taken = ({"runs-dark-390.png": b"png"}, {"runs-dark-390.png": entry})
    asked: list[Any] = []

    def take(names: Any) -> Any:
        asked.append(names)
        return taken

    monkeypatch.setattr(TAKE, "take", take)
    assert TAKE.main(["runs-dark-390.png"]) == 0
    assert asked == [{"runs-dark-390.png"}]
    written = json.loads((tmp_path / IMAGES.MANIFEST).read_text(encoding="utf-8"))
    assert written["images"]["runs-dark-390.png"]["signature"] == "new"
    assert written["images"]["bench-live.png"] == bench
    assert len(written["images"]) == len(IMAGES.IMAGES) + 1
    assert (tmp_path / IMAGES.FOLDER / "runs-dark-390.png").read_bytes() == b"png"
    assert "1 image(s) taken" in capsys.readouterr().out
    # Every image retaken: the bench's stay, and an entry nothing takes any more goes.
    (tmp_path / IMAGES.MANIFEST).write_text(
        json.dumps({"images": {**old, "gone.png": dict(entry)}}), encoding="utf-8"
    )
    assert TAKE.main([]) == 0
    written = json.loads((tmp_path / IMAGES.MANIFEST).read_text(encoding="utf-8"))
    assert set(written["images"]) == {"runs-dark-390.png", "bench-live.png"}


def test_the_script_writes_nothing_when_an_image_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path)

    def refused(names: Any) -> Any:
        raise TAKE.Refused("docs/images/panel/x.png: its view's text holds an absolute path")

    monkeypatch.setattr(TAKE, "take", refused)
    assert TAKE.main([]) == 1
    assert "refused: docs/images/panel/x.png" in capsys.readouterr().err
    assert not (tmp_path / IMAGES.FOLDER).exists()


class FakePage:
    """What ``take`` asks of a page: its texts and its picture."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def screenshot(self, full_page: bool) -> bytes:
        assert full_page
        return (FOLDER / "runs-dark-390.png").read_bytes()


class FakeShots:
    """``panel_shots`` with no browser: its scene and its run hand each image one page."""

    def __init__(self, texts: list[str]) -> None:
        self.page = FakePage(texts)
        self.browsers: list[Any] = []

    @staticmethod
    def texts(page: FakePage) -> list[str]:
        return page.texts

    @staticmethod
    def signed(page: FakePage) -> str:
        return "signed"

    @contextlib.contextmanager
    def scene(self, base: Path) -> Iterator[str]:
        yield "served"

    def run(self, browser: Any, served: str, keep: Any, names: set[str] | None) -> None:
        self.browsers.append(browser)
        for name in sorted(names or ()):
            keep(IMAGES.BY_NAME[name], self.page)


def test_take_signs_and_stamps_each_image_and_puts_the_state_home_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shots = FakeShots(["build/firmware.elf", "meter/runs"])
    monkeypatch.setattr(TAKE.importlib, "import_module", lambda name: shots)
    monkeypatch.setattr(TAKE.shutil, "which", lambda _tool: None)
    monkeypatch.setenv("XDG_STATE_HOME", "/somewhere/state")
    pngs, entries = TAKE.take({"runs-dark-390.png"}, "a browser")
    assert shots.browsers == ["a browser"]
    assert entries["runs-dark-390.png"]["signature"] == "signed"
    path = f"{IMAGES.FOLDER}/runs-dark-390.png"
    assert META.read_png_text(pngs["runs-dark-390.png"]) == META.png_text(META.record(path))
    assert os.environ["XDG_STATE_HOME"] == "/somewhere/state"
    monkeypatch.delenv("XDG_STATE_HOME")
    own: list[Any] = []
    monkeypatch.setattr(TAKE, "_with_own_browser", lambda *args: own.append(args))
    TAKE.take({"runs-dark-390.png"})
    assert [args[1:] for args in own] == [("served", own[0][2], {"runs-dark-390.png"})]
    assert "XDG_STATE_HOME" not in os.environ


def test_take_refuses_a_view_whose_text_holds_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    shots = FakeShots(["Local build", "/Users/someone/build/firmware.elf"])
    monkeypatch.setattr(TAKE.importlib, "import_module", lambda name: shots)
    with pytest.raises(TAKE.Refused, match="health-dark-1280.png: its view's text holds"):
        TAKE.take({"health-dark-1280.png"}, "a browser")


def test_a_first_take_writes_a_manifest_where_there_was_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path)
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path)
    entry = IMAGES.entry(IMAGES.BY_NAME["runs-dark-390.png"], "first", IMAGES.read_file)
    monkeypatch.setattr(
        TAKE, "take", lambda names: ({"runs-dark-390.png": b"png"}, {"runs-dark-390.png": entry})
    )
    assert TAKE.main([]) == 0
    written = json.loads((tmp_path / IMAGES.MANIFEST).read_text(encoding="utf-8"))
    assert list(written["images"]) == ["runs-dark-390.png"]


# -- the bench's images -------------------------------------------------------------------

FILE_SPEC = importlib.util.spec_from_file_location(
    "file_bench_shots", ROOT / "scripts" / "file_bench_shots.py"
)
assert FILE_SPEC and FILE_SPEC.loader
FILE = importlib.util.module_from_spec(FILE_SPEC)
FILE_SPEC.loader.exec_module(FILE)

BENCH: dict[str, dict[str, Any]] = {
    name: item for name, item in MANIFEST["images"].items() if item.get("kind") == "bench"
}
ELF_SHA = "80a257970e934ff61faca739d5b439cc08cf02d4f31faf980dda2e961a430293"
RIG = "hardware/rig.kicad_sch rev D, T-Deck asleep as the DUT (about 43 uA through the shunt)"
TAKEN_BY = (
    "host/tests/test_panel_bench.py --run-bench --bench-shots"
    " (headless Chromium 1280x900, light theme, full page)"
)


def test_the_bench_images_of_the_release_are_filed() -> None:
    """The run of record's images of the Live, Pins and Calibration and health views."""
    assert sorted(BENCH) == [
        "bench/bench-health-2026-11-26-elf-80a257970e934ff6.png",
        "bench/bench-live-2026-11-26-elf-80a257970e934ff6.png",
        "bench/bench-pins-2026-11-26-elf-80a257970e934ff6.png",
        "bench/bench-pins-reverted-2026-11-26-elf-80a257970e934ff6.png",
    ]
    assert IMAGES.bench_findings(MANIFEST) == []
    for item in BENCH.values():
        assert (item["fw"], item["elf"], item["date"]) == (
            "0.18.0",
            "80a257970e934ff6",
            "2026-11-26",
        )
        assert item["elf_sha256"] == f"sha256:{ELF_SHA}"
        assert item["instrument"] == "KM-0000000000"
    assert BENCH["bench/bench-pins-reverted-2026-11-26-elf-80a257970e934ff6.png"]["state"] == (
        "pins-reverted"
    )


def test_the_listing_reads_the_bench_folder_too() -> None:
    files = [p.relative_to(FOLDER).as_posix() for p in FOLDER.rglob("*.png")]
    assert {f"bench/{p.name}" for p in (FOLDER / "bench").glob("*.png")} == set(BENCH)
    assert IMAGES.listing(MANIFEST, files) == []
    stray = IMAGES.listing(MANIFEST, [*files, "bench/stray.png"])
    assert stray == [f"{IMAGES.FOLDER}/bench/stray.png: an image the manifest does not list"]


def _bench_item(**change: Any) -> dict[str, Any]:
    name = "bench/bench-pins-reverted-2026-11-26-elf-80a257970e934ff6.png"
    return {**BENCH[name], **change}


@pytest.mark.parametrize(
    ("name", "change", "finding"),
    [
        (None, {"date": "26-11-2026"}, "its date is not a date"),
        (None, {"elf": "80A257970E934FF6"}, "its elf= is not 16 hex digits"),
        (
            None,
            {"elf_sha256": "sha256:" + "0" * 64},
            "its ELF SHA-256 does not begin with its elf=",
        ),
        (None, {"fw": "0.18"}, "its firmware is not a version"),
        (None, {"instrument": "km-0000000000"}, "its instrument is not a serial"),
        (None, {"view": "plots"}, "its view is not a view of the page"),
        (None, {"state": "pins-menu-open"}, "its state is not a state of its view"),
        (None, {"state": "live-smoothing-off"}, "its state is not a state of its view"),
        (None, {"checked": ""}, "it was not checked by eye"),
        (None, {"sha256": "sha256:" + "0" * 64}, "its bytes are not the ones filed"),
        (
            "bench/bench-pins-2026-11-25-elf-80a257970e934ff6.png",
            {},
            "its name is not bench/bench-<view or state>-<date>-elf-<elf>.png",
        ),
        (
            "bench-pins-reverted-2026-11-26-elf-80a257970e934ff6.png",
            {},
            "its name is not bench/bench-<view or state>-<date>-elf-<elf>.png",
        ),
    ],
)
def test_a_bench_entry_out_of_form_is_found(
    name: str | None, change: dict[str, Any], finding: str
) -> None:
    real = "bench/bench-pins-reverted-2026-11-26-elf-80a257970e934ff6.png"
    item = _bench_item(**change)
    raw = (FOLDER / real).read_bytes()

    def read(path: str) -> bytes:
        return raw if path.endswith(".png") else IMAGES.read_file(path)

    found = IMAGES.bench_findings({"images": {name or real: item}}, read)
    assert found == [f"{IMAGES.FOLDER}/{name or real}: {finding}"]


def test_a_bench_entry_missing_a_key_or_its_file_is_found() -> None:
    real = "bench/bench-live-2026-11-26-elf-80a257970e934ff6.png"
    item = {k: v for k, v in BENCH[real].items() if k != "rig"}
    assert IMAGES.bench_findings({"images": {real: item}}) == [
        f"{IMAGES.FOLDER}/{real}: it lacks rig"
    ]

    def missing(path: str) -> bytes:
        raise FileNotFoundError(path)

    assert IMAGES.bench_findings({"images": {real: BENCH[real]}}, missing) == [
        f"{IMAGES.FOLDER}/{real}: listed and not in the folder"
    ]
    # The simulator's entries are the retake check's, not this one's.
    sim = {"live-dark-1280.png": MANIFEST["images"]["live-dark-1280.png"]}
    assert IMAGES.bench_findings({"images": sim}) == []


def test_the_records_of_the_bench_images() -> None:
    name = "bench/bench-pins-reverted-2026-11-26-elf-80a257970e934ff6.png"
    meta = META.record(f"{IMAGES.FOLDER}/{name}")
    assert meta.title == "Kleidos meter panel on the bench: Pins, an apply reverted"
    assert "2026-11-26" in meta.description and "elf 80a257970e934ff6" in meta.description
    assert "KM-0000000000" in meta.description and "simulated" not in meta.description
    assert meta.digital_source_type == "screenCapture" and meta.spdx == "GPL-3.0-or-later"
    for bench in BENCH:
        assert len(META.record(f"{IMAGES.FOLDER}/{bench}").title.encode()) <= 64
    with pytest.raises(META.MetaError, match="no metadata record"):
        META.record(f"{IMAGES.FOLDER}/bench/bench-unknown.png")


# -- the bench's images, filed by scripts/file_bench_shots.py ------------------------------


def _shots(folder: Path, **change: Any) -> dict[str, Any]:
    """A folder of shots as the panel's bench test leaves them: one PNG and its record."""
    folder.mkdir(parents=True, exist_ok=True)
    real = FOLDER / "bench" / "bench-live-2026-11-26-elf-80a257970e934ff6.png"
    chunks = META.png_chunks(real.read_bytes())
    raw = META.PNG_SIGNATURE + b"".join(
        META._chunk(k, d) for k, d in chunks if k[:1].isupper() or k == b"tRNS"
    )
    name = "bench-live-2026-11-26-elf-80a257970e934ff6.png"
    (folder / name).write_bytes(raw)
    record = {
        "kind": "bench",
        "date": "2026-11-26",
        "fw": "0.18.0",
        "image": "meter",
        "elf": "80a257970e934ff6",
        "elf_sha256": ELF_SHA,
        "source_commit": (
            "90fafb7 (branch meter-next/integration), firmware/src tree ca4ba6e5b7c5063b"
        ),
        "instrument": "KM-0000000000",
        "rig": RIG,
        "taken_by": TAKEN_BY,
        "checked_by_eye": BENCH["bench/bench-live-2026-11-26-elf-80a257970e934ff6.png"]["checked"],
        "images": {name: {"view": "live", "sha256": FILE.sha256(raw)}},
    }
    record.update(change)
    (folder / "bench-shots.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _bench_tree(root: Path) -> None:
    _tree(root)
    (root / IMAGES.FOLDER).mkdir(parents=True, exist_ok=True)
    sim = {k: v for k, v in MANIFEST["images"].items() if v["kind"] == "sim"}
    (root / IMAGES.MANIFEST).write_text(IMAGES.dumps(IMAGES.manifest(sim)), encoding="utf-8")


def test_the_filer_stamps_the_shots_and_lists_them_beside_the_simulators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bench_tree(tmp_path / "repo")
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path / "repo")
    _shots(tmp_path / "shots")
    assert FILE.main([str(tmp_path / "shots")]) == 0
    assert "1 bench image(s) filed" in capsys.readouterr().out
    written = json.loads((tmp_path / "repo" / IMAGES.MANIFEST).read_text(encoding="utf-8"))
    name = "bench/bench-live-2026-11-26-elf-80a257970e934ff6.png"
    assert written["images"][name] == BENCH[name]
    assert len(written["images"]) == len(IMAGES.IMAGES) + 1
    filed = (tmp_path / "repo" / IMAGES.FOLDER / name).read_bytes()
    assert filed == (FOLDER / name).read_bytes()
    assert IMAGES.bench_findings(written, lambda p: (tmp_path / "repo" / p).read_bytes()) == []
    # Filed again, the same bytes and the same manifest.
    assert FILE.main([str(tmp_path / "shots")]) == 0
    again = json.loads((tmp_path / "repo" / IMAGES.MANIFEST).read_text(encoding="utf-8"))
    assert again == written


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"checked_by_eye": ""}, "nobody checked these images by eye"),
        ({"kind": "sim"}, "not a record of bench images"),
        ({"elf": "9b23c01c"}, "bench-live-2026-11-26-elf-80a257970e934ff6.png: its name is not"),
        ({"date": "2026-11-25"}, "bench-live-2026-11-26-elf-80a257970e934ff6.png: its name is not"),
        (
            {"images": {"bench-pins-2026-11-26-elf-80a257970e934ff6.png": {"view": "pins"}}},
            "bench-pins-2026-11-26-elf-80a257970e934ff6.png: not in the folder",
        ),
        (
            {"images": {"bench-live-2026-11-26-elf-80a257970e934ff6.png": {"view": "live"}}},
            "bench-live-2026-11-26-elf-80a257970e934ff6.png: no SHA-256 recorded",
        ),
        (
            {
                "images": {
                    "bench-live-2026-11-26-elf-80a257970e934ff6.png": {
                        "view": "live",
                        "sha256": "0" * 64,
                    }
                }
            },
            "changed since it was taken",
        ),
        (
            {"images": {"bench-plots-2026-11-26-elf-80a257970e934ff6.png": {"view": "plots"}}},
            "bench-plots-2026-11-26-elf-80a257970e934ff6.png: plots is not a view of the page",
        ),
    ],
)
def test_the_filer_refuses_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    refusal: str,
) -> None:
    _bench_tree(tmp_path / "repo")
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path / "repo")
    before = (tmp_path / "repo" / IMAGES.MANIFEST).read_bytes()
    _shots(tmp_path / "shots", **change)
    assert FILE.main([str(tmp_path / "shots")]) == 1
    assert refusal in capsys.readouterr().err
    assert (tmp_path / "repo" / IMAGES.MANIFEST).read_bytes() == before
    assert not (tmp_path / "repo" / IMAGES.FOLDER / "bench").exists()


def test_the_filer_refuses_an_image_over_the_hooks_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bench_tree(tmp_path / "repo")
    monkeypatch.setattr(IMAGES, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(FILE, "MAX_BYTES", 1000)
    _shots(tmp_path / "shots")
    assert FILE.main([str(tmp_path / "shots")]) == 1
    assert "over the hook's 1000" in capsys.readouterr().err


def test_the_filer_names_a_folder_without_its_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert FILE.main([str(tmp_path)]) == 1
    assert "bench-shots.json" in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:
        FILE.main([])
    assert usage.value.code == 2
