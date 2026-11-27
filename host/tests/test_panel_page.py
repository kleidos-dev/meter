# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's page: what it ships, how serve hands it out, and that it reaches nowhere else.

Five claims are held here. The page names no resource of another origin:
every ``http://`` or ``https://`` in its files is refused but the SVG
namespace (a name, never fetched), and every file the HTML, the stylesheet
and the modules refer to is one the package ships. Serve hands out each
file at its path with its content type, the document with the page's own
Content-Security-Policy, behind the same ``Host`` guard as the API. The
page has a control for everything ``/api/schema`` offers: a row per setting
(``data-setting``), a card per sequence with an input per parameter
(``data-sequence``, ``data-param``) and a card per relay order with a
button per choice and a box per flag (``data-relay``, ``data-choice``,
``data-flag``), each with the place its refusal is shown. A real
``kleidos-meter serve --sim`` serves them all over a socket, fetched with
the standard library alone. And the wheel the package builds carries them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest
from kleidos_meter.panel import static
from kleidos_meter.panel.schema import PROFILES, SCHEMA
from kleidos_meter.panel.static import (
    CONTENT_TYPES,
    PAGE_POLICY,
    Page,
    packaged_page,
)
from kleidos_meter.pins import BOARD
from kleidos_meter.sim import DEFAULT_CHANNELS
from kleidos_meter.sim import PROFILES as SIM_PROFILES
from test_panel_http import CHANGE, Panel, serving_panel

#: Where the page's files are in the source tree.
STATIC = Path(static.__file__).parent / "static"

#: The ``http(s)://`` strings a file may hold because nothing fetches them:
#: XML namespace names.
NAMESPACES = frozenset({"http://www.w3.org/2000/svg"})

#: The files the page is built from; a file added or removed changes this list on purpose.
SHIPPED = [
    "index.html",
    "js/api.js",
    "js/chart-math.js",
    "js/chart.js",
    "js/console.js",
    "js/dialogs.js",
    "js/dom.js",
    "js/health.js",
    "js/live.js",
    "js/main.js",
    "js/noise.js",
    "js/pins-math.js",
    "js/pins.js",
    "js/relays.js",
    "js/runs.js",
    "js/sequences.js",
    "js/session.js",
    "js/settings.js",
    "js/status.js",
    "js/views.js",
    "panel.css",
]

#: The one ``data:`` reference the page holds: its tab icon, an SVG.
DATA_SVG = "data:image/svg+xml,"

#: A URL of any origin.
_URL = re.compile(r"https?://[^\s\"'<>)]*", re.IGNORECASE)
#: What a file refers to: an attribute, a CSS ``url()`` or ``@import``, an ES import.
_REFERENCES = (
    re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", re.IGNORECASE),
    re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.IGNORECASE),
    re.compile(r"""@import\s+["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""(?<!@)\bimport\s+(?:[^"';]*?\bfrom\s+)?["']([^"']+)["']"""),
    re.compile(r"""\bimport\(\s*["']([^"']+)["']\s*\)"""),
)


def foreign_urls(text: str) -> list[str]:
    """Every ``http://`` or ``https://`` string in *text* that is not a namespace name."""
    return [url for url in _URL.findall(text) if url not in NAMESPACES]


def references(name: str, text: str) -> list[str]:
    """What *text*, the file *name* below ``static/``, refers to, resolved below ``static/``.

    A reference that leaves the page's own paths (another origin, a
    protocol-relative ``//host``, a scheme such as ``data:``, a path outside
    ``/static/``) is returned as written, so the caller finds no file for it.
    """
    found: list[str] = []
    base = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    for pattern in _REFERENCES:
        for target in pattern.findall(text):
            if target.startswith("#") or target == "":
                continue  # a fragment of the document itself
            if target.startswith("//") or ":" in target.split("/", 1)[0]:
                found.append(target)
            elif target.startswith("/"):
                found.append(
                    target.removeprefix("/static/") if target.startswith("/static/") else target
                )
            else:
                parts: list[str] = []
                for part in (base + target).split("/"):
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part not in ("", "."):
                        parts.append(part)
                found.append("/".join(parts))
    return found


def page_files() -> dict[str, str]:
    """Every file of the page in the source tree, as text, by its path below ``static/``."""
    return {
        path.relative_to(STATIC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(STATIC.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


# -- what the page holds -------------------------------------------------------------------


def test_the_page_ships_its_files_and_each_has_a_content_type() -> None:
    files = page_files()
    assert sorted(files) == SHIPPED
    for name in files:
        assert Path(name).suffix in CONTENT_TYPES, name


def test_no_file_of_the_page_names_a_resource_of_another_origin() -> None:
    for name, text in page_files().items():
        assert foreign_urls(text) == [], name


def test_the_scan_finds_a_planted_resource_of_another_origin() -> None:
    planted = '<script src="https://cdn.example/lib.js"></script><a href="http://x.test/">x</a>'
    assert foreign_urls(planted) == ["https://cdn.example/lib.js", "http://x.test/"]
    assert foreign_urls('<svg xmlns="http://www.w3.org/2000/svg"></svg>') == []
    assert foreign_urls("@import url(HTTPS://fonts.example/a.css);") == [
        "HTTPS://fonts.example/a.css"
    ]


def test_every_file_the_page_refers_to_is_one_it_ships() -> None:
    files = page_files()
    for name, text in files.items():
        for target in references(name, text):
            if target.startswith(DATA_SVG):
                continue  # the tab icon, written in the document
            assert target in files, f"{name} refers to {target}, which the page does not ship"
    found = references("index.html", files["index.html"])
    assert {t for t in found if not t.startswith(DATA_SVG)} == {"panel.css", "js/main.js"}
    assert len([t for t in found if t.startswith(DATA_SVG)]) == 1


def test_a_reference_that_leaves_the_page_is_not_resolved_to_one_of_its_files() -> None:
    assert references("index.html", '<link href="//cdn.example/a.css">') == ["//cdn.example/a.css"]
    assert references("index.html", '<img src="data:image/png;base64,AAAA">') == [
        "data:image/png;base64,AAAA"
    ]
    assert references("js/a.js", 'import { x } from "../../http.py";') == ["http.py"]
    assert references("js/a.js", 'import "./b.js"; const m = import("./c.js");') == [
        "js/b.js",
        "js/c.js",
    ]
    assert references("panel.css", "@import 'x.css'; a { background: url(/etc/passwd) }") == [
        "/etc/passwd",
        "x.css",
    ]


def test_every_module_is_type_checked_and_every_file_names_its_license() -> None:
    # In two pieces, so that REUSE does not read this line as a license of its own.
    license = ": ".join(("SPDX-License-Identifier", "GPL-3.0-or-later"))
    for name, text in page_files().items():
        assert license in text, name
        if name.endswith(".js"):
            assert "// @ts-check" in text.splitlines()[:4], name
            assert "@file" in text, name


# -- the page against the schema ---------------------------------------------------------

#: Elements that never have content (no end tag).
_VOID = frozenset({"input", "br", "hr", "img", "meta", "link", "source", "wbr", "col", "area"})


@dataclass
class Node:
    """An element of the page: its tag, its attributes and its children."""

    tag: str
    attrs: dict[str, str]
    children: list[Node] = field(default_factory=list)

    def walk(self) -> Iterator[Node]:
        """This element and every element below it, in document order."""
        yield self
        for child in self.children:
            yield from child.walk()

    def find_all(self, attribute: str, value: str | None = None) -> list[Node]:
        """Every element below this one (itself included) with *attribute*, of *value* if given."""
        return [
            node
            for node in self.walk()
            if attribute in node.attrs and (value is None or node.attrs[attribute] == value)
        ]


class _TreeBuilder(HTMLParser):
    """The page's elements as a tree of :class:`Node`."""

    def __init__(self) -> None:
        super().__init__()
        self.root = Node("#document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return


def page_tree(text: str | None = None) -> Node:
    """The document's elements (``index.html`` unless *text* is given)."""
    builder = _TreeBuilder()
    builder.feed((STATIC / "index.html").read_text(encoding="utf-8") if text is None else text)
    builder.close()
    return builder.root


def missing_controls(tree: Node, schema: dict[str, Any]) -> list[str]:
    """What ``/api/schema`` offers that *tree* has no control for, one line each."""
    found: list[str] = []
    views = {node.attrs["data-view"]: node for node in tree.find_all("data-view")}
    settings_view = views.get("settings", Node("section", {}))
    per_profile = {setting["name"] for setting in schema["settings"] if setting["per_profile"]}
    parts = ("data-current", "data-period", "data-reason", "data-refusal", "data-action")
    for profile in schema["profiles"]:
        cards = settings_view.find_all("data-noise", profile["name"])
        if len(cards) != 1:
            found.append(f"noise reduction of {profile['name']}: {len(cards)} cards")
            continue
        controls = cards[0].find_all("data-noise-setting")
        fields = {node.attrs["data-noise-setting"] for node in controls}
        if fields != per_profile:
            found.append(
                f"noise reduction of {profile['name']}: {sorted(fields)}, not {sorted(per_profile)}"
            )
        found.extend(
            f"noise reduction of {profile['name']}: no {part}"
            for part in parts
            if not cards[0].find_all(part)
        )
    for setting in schema["settings"]:
        if setting["per_profile"]:
            continue
        rows = settings_view.find_all("data-setting", setting["name"])
        if len(rows) != 1:
            found.append(f"setting {setting['name']}: {len(rows)} rows")
            continue
        found.extend(
            f"setting {setting['name']}: no {part}"
            for part in ("data-current", "data-input", "data-refusal", "data-reason")
            if not rows[0].find_all(part)
        )
        if setting["writable"] and not rows[0].find_all("data-action", "set"):
            found.append(f"setting {setting['name']}: no Set button")
    for sequence in schema["sequences"]:
        cards = tree.find_all("data-sequence", sequence["name"])
        if len(cards) != 1:
            found.append(f"sequence {sequence['name']}: {len(cards)} cards")
            continue
        card = cards[0]
        if not card.find_all("data-action", "run") or not card.find_all("data-refusal"):
            found.append(f"sequence {sequence['name']}: no Run button or refusal")
        params = {node.attrs["data-param"] for node in card.find_all("data-param")}
        wanted = {param["name"] for param in sequence["params"]}
        if params != wanted:
            found.append(
                f"sequence {sequence['name']}: params {sorted(params)}, not {sorted(wanted)}"
            )
    for relay in schema["relays"]:
        cards = tree.find_all("data-relay", relay["name"])
        if len(cards) != 1:
            found.append(f"relay {relay['name']}: {len(cards)} cards")
            continue
        card = cards[0]
        choices = {node.attrs["data-choice"] for node in card.find_all("data-choice")}
        wanted = set(relay["choices"]) or {""}
        flags = {node.attrs["data-flag"] for node in card.find_all("data-flag")}
        if choices != wanted:
            found.append(f"relay {relay['name']}: choices {sorted(choices)}, not {sorted(wanted)}")
        if flags != {option.lower() for option in relay["options"]}:
            found.append(f"relay {relay['name']}: flags {sorted(flags)}")
        if not relay["choices"] and not card.find_all("data-ms"):
            found.append(f"relay {relay['name']}: no duration")
        if not card.find_all("data-refusal"):
            found.append(f"relay {relay['name']}: no refusal")
    for name in ("sequence", "setting", "relay"):
        known = {item["name"] for item in schema[f"{name}s"]}
        found.extend(
            f"{name} {node.attrs[f'data-{name}']}: not in the schema"
            for node in tree.find_all(f"data-{name}")
            if node.attrs[f"data-{name}"] not in known
        )
    return found


def served_schema() -> dict[str, Any]:
    """``/api/schema`` as serve hands it out."""
    served: dict[str, Any] = json.loads(json.dumps(SCHEMA.to_json()))
    return served


def test_every_setting_sequence_and_relay_of_the_schema_has_a_control() -> None:
    assert missing_controls(page_tree(), served_schema()) == []


def test_the_control_check_finds_what_the_page_lacks_or_adds() -> None:
    schema = served_schema()
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    without_row = text.replace('data-setting="pretrigger"', 'data-setting-gone="pretrigger"')
    assert missing_controls(page_tree(without_row), schema) == ["setting pretrigger: 0 rows"]
    without_param = text.replace('data-param="off_ms"', 'data-gone="off_ms"')
    assert missing_controls(page_tree(without_param), schema) == [
        "sequence COLD_BOOT: params ['seconds'], not ['off_ms', 'seconds']"
    ]
    without_count = text.replace(
        'id="noise-state-count" data-noise-setting="average_count"', 'id="noise-state-count"'
    ).replace('data-noise="SLEep"', 'data-noise="SLEPt"')
    assert missing_controls(page_tree(without_count), schema) == [
        "noise reduction of STATe: ['average', 'average_aperture'],"
        " not ['average', 'average_aperture', 'average_count']",
        "noise reduction of SLEep: 0 cards",
    ]
    without_period = text.replace('id="noise-trace-period" data-period', 'id="noise-trace-period"')
    assert missing_controls(page_tree(without_period), schema) == [
        "noise reduction of TRACe: no data-period"
    ]
    schema["sequences"].append({"name": "NEW_ONE", "params": []})
    schema["settings"][0]["name"] = "renamed"
    assert missing_controls(page_tree(), schema) == [
        "setting renamed: 0 rows",
        "sequence NEW_ONE: 0 cards",
        "setting i2c_hz: not in the schema",
    ]


def test_every_view_has_its_section_and_its_link() -> None:
    tree = page_tree()
    sections = {node.attrs["data-view"]: node for node in tree.find_all("data-view")}
    assert list(sections) == [
        "live",
        "sequences",
        "runs",
        "relays",
        "pins",
        "settings",
        "health",
        "console",
    ]
    links = [node.attrs["href"] for node in tree.walk() if node.tag == "a" and "href" in node.attrs]
    for name, section in sections.items():
        assert section.attrs["id"] == name and f"#{name}" in links, name
        assert ("hidden" in section.attrs) == (name != "live"), name  # Live until the router runs


def test_the_relays_the_rig_does_not_wire_are_drawn_and_the_dialogs_are_native() -> None:
    tree = page_tree()
    unwired = [relay["name"] for relay in served_schema()["relays"] if not relay["wired"]]
    assert unwired == ["relay3", "relay4"]
    svg = next(node for node in tree.walk() if node.tag == "svg")
    segments = {node.attrs["data-segment"] for node in svg.find_all("data-segment")}
    assert segments == {"cell", "shunt", "k1", "k2", "dut"}
    assert {node.attrs.get("id") for node in svg.walk()} >= {"k1-blade", "k2-blade"}
    dialogs = [node for node in tree.walk() if node.tag == "dialog"]
    assert [d.attrs["id"] for d in dialogs] == ["confirm-dialog", "prompt-dialog", "pin-dialog"]
    for dialog in dialogs:
        assert "aria-labelledby" in dialog.attrs and "role" not in dialog.attrs
    safe = [n for n in dialogs[0].walk() if n.tag == "button" and "autofocus" in n.attrs]
    assert [n.attrs["value"] for n in safe] == ["cancel"]


#: What the page never names outside its recommendation note: a relay by its
#: designator, or a part.
PART_NAMES = re.compile(r"\bK[1-4]\b|HW-?316|\bClick\b|relay2click|LCA717|PhotoMOS|OptoMOS")

#: What a JavaScript file holds that a reader of the page can see: its strings.
_STRINGS = re.compile(r'"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`')


def named_parts(text: str, note: str) -> list[str]:
    """Every part *text* names once the recommendation *note* is taken out of it."""
    return PART_NAMES.findall(text.replace(note, "") if note else text)


def recommendation(text: str) -> str:
    """The document's recommendation note: its ``<aside data-recommendation>``."""
    notes = re.findall(r"<aside [^>]*data-recommendation[^>]*>.*?</aside>", text, re.DOTALL)
    assert len(notes) == 1
    return str(notes[0])


def test_relays_are_named_by_number_and_no_part_is_named_but_in_the_recommendation() -> None:
    files = page_files()
    note = recommendation(files["index.html"])
    assert named_parts(files["index.html"], note) == []
    for name, text in files.items():
        if name.endswith(".js"):
            assert named_parts("\n".join(_STRINGS.findall(text)), note) == [], name
    assert named_parts(json.dumps(served_schema()), note) == []
    assert named_parts(json.dumps(BOARD), note) == []
    # The note is where the part is: solid state, and why the other is not.
    for said in ("solid-state", "LCA717", "HW-316", "10 mV", "leaks"):
        assert said in note


def test_the_scan_finds_a_planted_designator_and_a_planted_part() -> None:
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    note = recommendation(text)
    planted = text.replace('<h1 id="relays-title">Relays', "<h1>K1 on the HW-316, a Click", 1)
    assert named_parts(planted, note) == ["K1", "HW-316", "Click"]
    assert named_parts(note, "") != []


# -- the page as serve holds it -----------------------------------------------------------


def test_the_packaged_page_is_served_at_its_paths() -> None:
    page = packaged_page()
    assert page.paths == sorted(["/", *(f"/static/{name}" for name in SHIPPED)])
    index = page.lookup("/")
    assert index is not None and index is page.lookup("/static/index.html")
    assert index.content_type == "text/html; charset=utf-8" and index.policy == PAGE_POLICY
    script = page.lookup("/static/js/main.js")
    assert script is not None and script.content_type == "text/javascript; charset=utf-8"
    assert script.policy is None
    assert page.lookup("/static/nope.js") is None
    assert page.lookup("/static/../http.py") is None


def test_a_page_refuses_a_file_it_cannot_type_and_needs_its_document() -> None:
    with pytest.raises(ValueError, match="no content type for .py"):
        Page({"index.html": b"", "evil.py": b""})
    with pytest.raises(ValueError, match="no content type for no suffix"):
        Page({"index.html": b"", "README": b""})
    with pytest.raises(ValueError, match="no static/index.html"):
        Page({"panel.css": b""})
    assert Page.owns("/") and Page.owns("/static/x") and not Page.owns("/api/schema")


def test_a_page_read_from_a_directory_leaves_out_hidden_files_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "pkg"
    (package / "static" / "js" / "__pycache__").mkdir(parents=True)
    (package / "static" / "index.html").write_bytes(b"<!doctype html>")
    (package / "static" / "js" / "a.js").write_bytes(b"export {};")
    (package / "static" / "js" / "__pycache__" / "a.pyc").write_bytes(b"")
    (package / "static" / ".DS_Store").write_bytes(b"")
    monkeypatch.setattr(resources, "files", lambda _: package)
    assert packaged_page().paths == ["/", "/static/index.html", "/static/js/a.js"]


def test_the_page_policy_loads_nothing_from_another_origin_and_nothing_inline() -> None:
    directives = dict(part.strip().split(" ", 1) for part in PAGE_POLICY.split(";"))
    assert directives["default-src"] == "'none'"
    for name in ("script-src", "style-src", "connect-src"):
        assert directives[name] == "'self'", name
    assert directives["img-src"] == "'self' data:"  # the tab icon
    assert directives["frame-ancestors"] == "'none'" and directives["form-action"] == "'none'"
    assert "unsafe" not in PAGE_POLICY


@pytest.fixture
def paged() -> Iterator[Panel]:
    with serving_panel(page=packaged_page()) as served:
        yield served


def test_serve_hands_out_each_file_with_its_type(paged: Panel) -> None:
    for name in SHIPPED:
        answer = paged.get(f"/static/{name}")
        assert answer.status == 200, name
        assert answer.headers["content-type"] == CONTENT_TYPES[Path(name).suffix], name
        assert answer.raw == (STATIC / name).read_bytes(), name
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["cache-control"] == "no-store"
        assert answer.headers["referrer-policy"] == "no-referrer"
        assert not [h for h in answer.headers if h.startswith("access-control-")]
    assert paged.sent == []  # the page sends nothing to the instrument


def test_the_document_is_served_at_the_root_with_the_page_policy(paged: Panel) -> None:
    answer = paged.get("/")
    assert answer.status == 200
    assert answer.headers["content-type"] == "text/html; charset=utf-8"
    assert answer.headers["content-security-policy"] == PAGE_POLICY
    assert answer.raw == (STATIC / "index.html").read_bytes()
    script = paged.get("/static/js/main.js")
    assert script.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_a_name_the_page_does_not_ship_is_not_found(paged: Panel) -> None:
    for path in ("/static/nope.js", "/static/../http.py", "/static/", "/static/js"):
        answer = paged.get(path)
        assert answer.status == 404 and answer.body["error"] == "not_found", path
    assert paged.get("/api/nowhere").body["detail"] == "no route /api/nowhere"


def test_the_page_takes_get_only(paged: Panel) -> None:
    for method in ("POST", "PUT", "DELETE"):
        answer = paged.request(method, "/", headers=CHANGE, body=b"")
        assert answer.status == 405 and answer.headers["allow"] == "GET", method
    assert paged.request("POST", "/static/js/main.js", headers=CHANGE, body=b"").status == 405


def test_the_page_is_behind_the_host_guard(paged: Panel) -> None:
    assert paged.get("/", host="attacker.example:8040").status == 421
    assert paged.get("/static/panel.css", host=None).status == 421
    assert paged.get("/", host="localhost:8040").status == 200


def test_the_page_is_served_while_another_controller_holds_the_line(paged: Panel) -> None:
    paged.api.hold("a passthrough client")
    try:
        assert paged.get("/").status == 200
        assert paged.get("/api/relays").status == 423
    finally:
        paged.api.release()


# -- the schema the page draws from -------------------------------------------------------


def test_the_profiles_are_the_profile_settings_choices_with_the_firmwares_figures() -> None:
    choices = next(s for s in SCHEMA.settings if s.name == "profile").choices
    assert [p.name for p in PROFILES] == list(choices)
    for profile in PROFILES:
        key = next(k for k in SIM_PROFILES if k.startswith(profile.name.upper()[:4]))
        assert (profile.period_us, profile.ring) == SIM_PROFILES[key]
        assert len(profile.channels) == len(profile.columns)
        if profile.ring:
            assert profile.channels == ()
        else:
            assert len(profile.channels) == len(DEFAULT_CHANNELS[key])
    served = json.loads(json.dumps(SCHEMA.to_json()))["profiles"]
    assert served[1] == {
        "name": "STATe",
        "period_us": 1152,
        "ring": False,
        "channels": ["CURR"],
        "columns": ["i_na"],
        "averaging": True,
        "count": 1,
        "conversion_us": 1052,
        "other_us": 100,
    }


def test_a_profiles_period_is_its_conversions_times_its_count() -> None:
    periods = {p.name: (p.conversion_us + p.other_us) * p.count for p in PROFILES}
    # BURSt's samples are paced by the timer, every second conversion.
    assert periods == {p.name: p.period_us for p in PROFILES} | {"BURSt": 50}
    assert [p.name for p in PROFILES if not p.averaging] == ["BURSt"]


# -- serve itself, over a socket ---------------------------------------------------------


@pytest.fixture
def served(tmp_path: Path) -> Iterator[str]:
    """``kleidos-meter serve --sim`` in a process of its own; its panel's address."""
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    (tmp_path / "run").mkdir()
    command = [sys.executable, "-m", "kleidos_meter.cli", "serve", "--sim", "--http", "0"]
    command += ["--data-dir", str(tmp_path / "state" / "runs")]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    ) as process:
        assert process.stdout is not None
        url = None
        try:
            for line in process.stdout:
                match = re.match(r"serve: panel at (http://127\.0\.0\.1:\d+/)$", line.strip())
                if match:
                    url = match.group(1)
                    break
            assert url is not None, "serve printed no panel address"
            yield url
        finally:
            process.send_signal(signal.SIGINT)
            _, errors = process.communicate(timeout=30)
            assert process.returncode == 0, errors


def test_serve_sim_serves_every_file_of_the_page(served: str) -> None:
    def fetch(path: str) -> tuple[int, str, bytes]:
        # S310: the test's own server on the loopback, over http.
        with urlopen(Request(served.rstrip("/") + path), timeout=10) as response:  # noqa: S310
            return response.status, response.headers["Content-Type"], response.read()

    status, kind, body = fetch("/")
    assert (status, kind) == (200, "text/html; charset=utf-8")
    assert body == (STATIC / "index.html").read_bytes()
    for name in SHIPPED:
        status, kind, body = fetch(f"/static/{name}")
        assert (status, kind) == (200, CONTENT_TYPES[Path(name).suffix]), name
        assert body == (STATIC / name).read_bytes(), name
    status, kind, _ = fetch("/api/schema")
    assert (status, kind) == (200, "application/json; charset=utf-8")


# -- the wheel ----------------------------------------------------------------------------


def test_the_wheel_carries_every_file_of_the_page(tmp_path: Path) -> None:
    host = Path(__file__).resolve().parent.parent
    source = tmp_path / "host"
    source.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(host / name, source / name)
    shutil.copytree(
        host / "kleidos_meter",
        source / "kleidos_meter",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    build = (
        "import sys; from setuptools import build_meta; print(build_meta.build_wheel(sys.argv[1]))"
    )
    done = subprocess.run(
        [sys.executable, "-c", build, str(tmp_path / "dist")],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    wheel = tmp_path / "dist" / done.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for name in SHIPPED:
            member = f"kleidos_meter/panel/static/{name}"
            assert member in names, member
            assert archive.read(member) == (STATIC / name).read_bytes(), member
    assert "kleidos_meter/panel/static.py" in names
    assert not [
        n for n in names if n.startswith("kleidos_meter/panel/static/") and n.endswith(".py")
    ]
