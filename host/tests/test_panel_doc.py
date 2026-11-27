# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/http-api.md is the schema's, and docs/panel.md names all of it and shows what happens.

``docs/http-api.md`` is written by ``scripts/gen_http_api_doc.py`` from the
schema the panel serves: the page must be what the generator writes now, and a
schema with one thing more must write another page.

``docs/panel.md`` is written by hand, for the person at the bench. It must name
every route of the schema, as ``GET /api/schema``, and no route the schema does
not have; every sequence, in a row of its "Sequences" table, with its
parameters' defaults and bounds; and every option of ``serve``. Its examples on the
simulator are run as the page gives them: ``serve --sim``, every ``curl`` (one
serve, in the page's order) and the tool that comes in through the passthrough,
each printing what the page shows, and every other ``kleidos-meter --sim``
command. What a run of it makes its own (a time, a port, a path) is read as any
other; the bench's examples are the bench's, and no test opens the instrument.

"The views" has one subsection per view of the page, read from ``index.html``
in its order, each with the view's dark image (with its alt text) and links to
its light and phone images; every image ``docs/images/panel/manifest.json``
lists is on the page. "The chart" has a row per smoothing choice of
``chart-math.js`` and "The pin view" a word per pin state of ``pins-math.js``.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote, urlsplit

import pytest
from kleidos_meter.cli import main
from kleidos_meter.panel.schema import SCHEMA, Argument, Route
from kleidos_meter.panel.serve import build_parser
from panel_harness import Answer, Served, serving, until
from test_usage_doc import BLOCK, Command, _runs_as_shown, commands

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "panel.md"
REFERENCE = ROOT / "docs" / "http-api.md"
GENERATOR = ROOT / "scripts" / "gen_http_api_doc.py"

#: A route as a page names it.
NAMED = re.compile(r"`((?:GET|POST) /api/[^`\s?]*)`")

#: What differs from one run to the next in what is printed, and what it is read as.
OWN = (
    (re.compile(r"127\.0\.0\.1:\d+"), "<address>"),
    (re.compile(r"\b\d+\.\d+\.\d+\b"), "<version>"),
    (re.compile(r"up \d+\.\d s"), "up <seconds>"),
    (re.compile(r"recordings in .*"), "recordings in <directory>"),
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    """The generator, imported from ``scripts/``."""
    spec = importlib.util.spec_from_file_location("gen_http_api_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- docs/http-api.md ----------------------------------------------------------------------


def test_the_reference_is_what_the_schema_gives(generator: ModuleType) -> None:
    assert _text(REFERENCE) == generator.page(), (
        "docs/http-api.md is stale: python3 scripts/gen_http_api_doc.py"
    )
    assert generator.main(["--check"]) == 0


def test_a_schema_with_one_thing_more_gives_another_page(
    generator: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    taken = (Argument("what", "query", "a|b", required=True),)
    planted = Route("GET", "/api/planted/{run}", False, "planted", takes=taken, answers="nothing")
    text = generator.page(dataclasses.replace(SCHEMA, routes=(*SCHEMA.routes, planted)))
    assert text != generator.page()
    assert "| [`GET /api/planted/{run}`](#get-apiplantedrun) | no | yes | planted |" in text
    assert "### `GET /api/planted/{run}`\n\nPlanted.\n" in text
    assert "| `what` | query | yes | a\\|b |" in text
    for part in ("settings", "profiles", "relays", "sequences"):
        fewer = dataclasses.replace(SCHEMA, **{part: getattr(SCHEMA, part)[1:]})
        assert generator.page(fewer) != generator.page(), part

    stale = tmp_path / "http-api.md"
    assert generator.main(["--check", "--out", str(stale)]) == 1  # no page yet
    assert generator.main(["--out", str(stale)]) == 0
    assert generator.main(["--check", "--out", str(stale)]) == 0
    stale.write_text(_text(stale).replace("`GET /api/schema`", "`GET /api/scheme`", 1))
    assert generator.main(["--check", "--out", str(stale)]) == 1
    assert "run python3 scripts/gen_http_api_doc.py" in capsys.readouterr().err


# -- docs/panel.md -------------------------------------------------------------------------


def _unnamed(text: str) -> list[str]:
    """The routes of the schema *text* does not name, and those it names that are not there."""
    named = set(NAMED.findall(text))
    routes = {f"{route.method} {route.path}" for route in SCHEMA.routes}
    return sorted(f"{name}: not named" for name in routes - named) + sorted(
        f"{name}: not a route" for name in named - routes
    )


def _sequence_rows(text: str) -> dict[str, list[str]]:
    """The "Sequences" table's rows: each sequence's cells."""
    section = text[text.index("\n## What each sequence moves\n") :]
    section = section[: section.index("\n## ", 1)]
    rows = re.findall(r"^\| `([A-Z_]+)` \|(.*)\|$", section, re.MULTILINE)
    return {name: [cell.strip() for cell in cells.split("|")] for name, cells in rows}


def test_the_page_names_every_route_of_the_schema_and_no_other() -> None:
    assert _unnamed(_text(PAGE)) == []


def test_the_page_has_a_row_for_every_sequence_with_its_parameters() -> None:
    rows = _sequence_rows(_text(PAGE))
    assert list(rows) == [sequence.name for sequence in SCHEMA.sequences]
    for sequence in SCHEMA.sequences:
        shown = ", ".join(
            f"`{param.name}` ({param.default}, {param.low} to {param.high})"
            for param in sequence.params
        )
        assert rows[sequence.name][0] == (shown or "–"), sequence.name
        assert len(rows[sequence.name]) == 3 and all(rows[sequence.name]), sequence.name


def test_the_page_says_every_option_of_serve() -> None:
    actions = [a for a in build_parser()._actions if not isinstance(a, argparse._HelpAction)]
    for action in actions:
        assert f"`{action.option_strings[0]}" in _text(PAGE), action.option_strings


def test_the_page_and_the_index_lead_to_the_reference() -> None:
    assert "](http-api.md)" in _text(PAGE) and "](panel.md)" in _text(REFERENCE)


def test_the_guards_see_a_route_and_a_sequence_left_out() -> None:
    text = _text(PAGE)
    assert _unnamed(text.replace("`GET /api/health`", "health").replace("/api/runs`", "/api/run`"))
    left_out = re.sub(r"^\| `BURST` .*\n", "", text, flags=re.MULTILINE)
    assert "BURST" not in _sequence_rows(left_out) and "TRACE" in _sequence_rows(left_out)
    assert _unnamed("`GET /api/nothing`")[-1] == "GET /api/nothing: not a route"


def test_the_page_names_no_port() -> None:
    for block in BLOCK.finditer(_text(PAGE)):
        assert not re.search(r"/dev/(?:cu|tty)[.\w]", block.group(3))


# -- its views, each with its images -------------------------------------------------------

INDEX = ROOT / "host" / "kleidos_meter" / "panel" / "static" / "index.html"
IMAGES = ROOT / "docs" / "images" / "panel"
JS = INDEX.parent / "js"

#: A view of the page: its section's id, and the id of the heading that names it.
VIEW = re.compile(r'<section id="([\w-]+)" class="card" data-view="\1" aria-labelledby="([\w-]+)"')

#: A Markdown image, ``![alt](target)``, and a link, ``[text](target)``.
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s#]+)\)")


def views(html: str) -> list[tuple[str, str]]:
    """The page's views in its order: each section's id and the title its heading gives it."""
    found = []
    for view, heading in VIEW.findall(html):
        title = re.search(rf'<h1 id="{heading}">([^<]+)</h1>', html)
        assert title is not None, view
        found.append((view, title.group(1).strip()))
    return found


def _section_of(text: str, heading: str) -> str:
    """The body of *text*'s ``## heading`` section, to the next ``## ``."""
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end >= 0 else len(text)]


def subsections(section: str) -> dict[str, str]:
    """A section's ``### `` subsections in its order: each title and its body."""
    parts = re.split(r"^### (.+)$", section, flags=re.MULTILINE)
    return {title.strip(): body for title, body in zip(parts[1::2], parts[2::2], strict=True)}


def view_faults(text: str, html: str) -> list[str]:
    """What "The views" lacks: a subsection per view, in order, with its three images."""
    shown = subsections(_section_of(text, "The views"))
    wanted = views(html)
    faults = []
    if list(shown) != [title for _, title in wanted]:
        faults.append(f"the subsections are {list(shown)}, the page's views {wanted}")
    for view, title in wanted:
        body = shown.get(title, "")
        dark = f"images/panel/{view}-dark-1280.png"
        if not any(target == dark and alt.strip() for alt, target in IMAGE.findall(body)):
            faults.append(f"{title}: no image {dark} with its alt text")
        linked = LINK.findall(body)
        others = (f"images/panel/{view}-light-1280.png", f"images/panel/{view}-dark-390.png")
        faults.extend(f"{title}: no link to {other}" for other in others if other not in linked)
    return faults


def test_every_view_of_the_page_has_its_subsection_and_its_images() -> None:
    html = _text(INDEX)
    assert len(views(html)) == 8
    assert view_faults(_text(PAGE), html) == []


def test_the_views_guard_sees_a_view_without_its_subsection_or_its_image() -> None:
    html = _text(INDEX)
    text = _text(PAGE)
    no_runs = re.sub(r"^### Runs\n", "### Run list\n", text, flags=re.MULTILINE)
    assert any("subsections are" in fault for fault in view_faults(no_runs, html))
    no_image = text.replace("](images/panel/pins-dark-1280.png)", "](images/panel/pins.png)")
    assert view_faults(no_image, html) == [
        "Pins: no image images/panel/pins-dark-1280.png with its alt text"
    ]
    no_alt = re.sub(
        r"!\[[^\]]+\]\(images/panel/relays-dark-1280\.png\)",
        "![ ](images/panel/relays-dark-1280.png)",
        text,
    )
    assert view_faults(no_alt, html) == [
        "Relays: no image images/panel/relays-dark-1280.png with its alt text"
    ]
    no_phone = text.replace("](images/panel/health-dark-390.png)", "](images/panel/health.png)")
    assert view_faults(no_phone, html) == [
        "Calibration and health: no link to images/panel/health-dark-390.png"
    ]
    one_more = html.replace(
        '<section class="card" aria-labelledby="notices-title">',
        '<section id="extra" class="card" data-view="extra" aria-labelledby="extra-title">'
        '<h1 id="extra-title">Extra</h1></section>'
        '<section class="card" aria-labelledby="notices-title">',
    )
    assert views(one_more)[-1] == ("extra", "Extra")
    assert any("Extra: no image" in fault for fault in view_faults(text, one_more))


def test_every_image_of_the_panel_is_on_the_page_and_every_one_it_shows_exists() -> None:
    text = _text(PAGE)
    manifest = json.loads((IMAGES / "manifest.json").read_text(encoding="utf-8"))
    named = {target for _, target in IMAGE.findall(text)} | set(LINK.findall(text))
    panel = {target for target in named if target.startswith("images/panel/")}
    assert sorted(f"images/panel/{name}" for name in manifest["images"]) == sorted(panel)
    for alt, target in IMAGE.findall(text):
        assert alt.strip(), target
        assert (PAGE.parent / target).is_file(), target


def bench_faults(text: str, manifest: dict[str, Any]) -> list[str]:
    """What "On the bench" lacks: each bench image, with its alt text, and a caption under it
    that says it is a bench image and names its date and its ``elf=``."""
    section = _section_of(text, "On the bench")
    faults = []
    for name, item in sorted(manifest["images"].items()):
        if item.get("kind") != "bench":
            continue
        target = f"images/panel/{name}"
        at = section.find(f"]({target})")
        if at < 0 or not any(t == target and alt.strip() for alt, t in IMAGE.findall(section)):
            faults.append(f"{name}: not on the page with its alt text")
            continue
        caption = section[at:].split("\n\n", 2)[1] if "\n\n" in section[at:] else ""
        wanted = ("bench image", item["date"], f"`elf={item['elf']}`")
        faults.extend(f"{name}: its caption does not say {w}" for w in wanted if w not in caption)
    return faults


def test_the_bench_images_are_captioned_with_their_date_and_elf() -> None:
    manifest = json.loads((IMAGES / "manifest.json").read_text(encoding="utf-8"))
    assert any(item["kind"] == "bench" for item in manifest["images"].values())
    assert bench_faults(_text(PAGE), manifest) == []


def test_the_bench_guard_sees_an_image_or_a_caption_missing() -> None:
    text = _text(PAGE)
    manifest = json.loads((IMAGES / "manifest.json").read_text(encoding="utf-8"))
    name, item = next((n, i) for n, i in manifest["images"].items() if i["kind"] == "bench")
    gone = re.sub(rf"!\[[^\]]*\]\(images/panel/{re.escape(name)}\)", "", text)
    assert f"{name}: not on the page with its alt text" in bench_faults(gone, manifest)
    undated = text.replace(f"`elf={item['elf']}`", "`elf=?`")
    assert f"{name}: its caption does not say `elf={item['elf']}`" in bench_faults(
        undated, manifest
    )


def test_the_contents_list_every_section_in_order() -> None:
    text = _text(PAGE)
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    assert headings[0] == "Contents"
    listed = re.findall(
        r"^- \[([^\]]+)\]\(#([\w-]+)\)$", _section_of(text, "Contents"), re.MULTILINE
    )
    assert [title for title, _ in listed] == headings[1:]
    for title, anchor in listed:
        assert anchor == re.sub(r"[^\w\- ]", "", title.replace("`", "").lower()).replace(" ", "-")


def smoothing_choices(module: str) -> list[str]:
    """The labels of ``chart-math.js``'s ``SMOOTHING``, in its order."""
    block = module[module.index("export const SMOOTHING") :]
    return re.findall(r'\{ label: "([^"]+)", us: [\d_]+ \}', block[: block.index("]);")])


def pin_states(module: str) -> list[str]:
    """The words of ``pins-math.js``'s ``STATE_WORDS``, in its order."""
    block = module[module.index("export const STATE_WORDS") :]
    return re.findall(r'^\s+\w+: "([^"]+)",$', block[: block.index("});")], re.MULTILINE)


def anchors(text: str) -> list[str]:
    """GitHub's anchor of each heading of *text* outside fenced blocks, in order."""
    prose = re.sub(r"^```.*?^```$", "", text, flags=re.MULTILINE | re.DOTALL)
    titles = re.findall(r"^#{1,6} (.+)$", prose, flags=re.MULTILINE)
    return [re.sub(r"[^\w\- ]", "", t.replace("`", "").lower()).replace(" ", "-") for t in titles]


def test_no_two_headings_of_the_page_share_an_anchor() -> None:
    # A view's subsection named as a section (Sequences, once) would take its
    # anchor, and every link to the section would land on the view.
    found = anchors(_text(PAGE))
    assert len(found) == len(set(found)), sorted({a for a in found if found.count(a) > 1})
    assert len(set(anchors("## Runs\n### Runs\n```text\n# not a heading\n```\n"))) == 1


def test_the_page_explains_the_chart_the_pins_and_the_noise_reduction() -> None:
    text = _text(PAGE)
    chart = _section_of(text, "The chart")
    for said in ("arithmetic mean", "minimum", "maximum", "moving average", "RMS", "View as CSV"):
        assert said in chart, said
    choices = smoothing_choices(_text(JS / "chart-math.js"))
    assert choices == ["Off", "1 ms", "10 ms", "100 ms", "1 s"]
    rows = re.findall(r"^\| ([^|]+) \| [^|]+ \| [^|]+ \|$", chart, re.MULTILINE)
    assert rows == ["Choice", *choices], "a row of the step table per smoothing choice, in order"
    assert "never" in chart and "recorded" in chart
    pins = _section_of(text, "The pin view")
    states = pin_states(_text(JS / "pins-math.js"))
    assert states == ["free", "in use", "reserved by the board", "usable with caution"]
    for said in [*states, "conflict"]:
        assert f"**{said}**" in pins, said
    assert "`SYST:PINS:DEF`" in pins and "Apply" in pins and "`117`" in pins
    noise = _section_of(text, "Noise reduction in the INA228")
    assert "off" in noise and "`meta.json`" in noise


# -- its examples --------------------------------------------------------------------------

COMMANDS = commands(_text(PAGE))
SERVES = [c for c in COMMANDS if c.argv[:2] == ["kleidos-meter", "serve"] and c.simulated]
CLIENTS = [c for c in COMMANDS if any(word.startswith("socket://") for word in c.argv)]
ONE_SHOTS = [c for c in COMMANDS if c.simulated and c.argv[1] != "serve"]


def _curls(text: str) -> list[Command]:
    """The ``curl`` commands of *text*'s blocks, each with the output below it, if shown."""
    renamed = re.sub(r"^((?:\$ )?)curl ", r"\1kleidos-meter curl ", text, flags=re.MULTILINE)
    return [found for found in commands(renamed) if found.argv[:2] == ["kleidos-meter", "curl"]]


CURLS = _curls(_text(PAGE))


def _read(lines: list[str]) -> list[str]:
    """*lines* with what a run makes its own read as any other."""
    for pattern, name in OWN:
        lines = [pattern.sub(name, line) for line in lines]
    return lines


def _request(served: Served, argv: list[str]) -> Answer:
    """The request a ``curl`` command makes, made of *served*.

    :raises SystemExit: For an option this reader does not know, never a request of another kind.
    """
    parser = argparse.ArgumentParser(prog="curl", add_help=False)
    parser.add_argument("-s", action="store_true")
    parser.add_argument("-H", action="append", default=[])
    parser.add_argument("-d")
    parser.add_argument("url")
    args = parser.parse_args(argv)
    split = urlsplit(args.url)
    assert split.netloc == "127.0.0.1:8040", args.url
    target = split.path + ("?" + quote(split.query, safe="=&?:*;/") if split.query else "")
    headers = dict(header.split(": ", 1) for header in args.H)
    return served.request("GET" if args.d is None else "POST", target, args.d, headers=headers)


def _timeless(body: Any) -> Any:
    """*body* with every ``elapsed_s`` in it read as any other."""
    if isinstance(body, dict):
        return {k: None if k == "elapsed_s" else _timeless(v) for k, v in body.items()}
    return [_timeless(item) for item in body] if isinstance(body, list) else body


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    """One ``serve --sim`` with its passthrough, as the page starts it."""
    data_dir = tmp_path_factory.mktemp("panel-doc") / "runs"
    argv = ["--sim", "--data-dir", str(data_dir), "--scpi-passthrough", "0"]
    with serving(argv) as running:
        yield running
    assert running.status == [0], running.err.getvalue()


def test_the_page_s_serve_prints_what_the_page_shows(tmp_path: Path) -> None:
    assert [found.argv for found in SERVES] == [["kleidos-meter", "serve", "--sim"]]
    with serving(["--sim", "--data-dir", str(tmp_path / "runs")]) as running:
        assert running.get("/").status == 200
    assert running.status == [0], running.err.getvalue()
    shown = SERVES[0].output
    assert shown is not None
    assert _read(running.out.getvalue().splitlines()) == _read(shown)


@pytest.mark.parametrize("command", CURLS, ids=lambda command: f"line{command.line}")
def test_a_curl_of_the_page_is_answered_as_shown(served: Served, command: Command) -> None:
    if command.output is None:  # it starts something: the one before it must be over
        until(lambda: served.get("/api/sequences").body["state"] == "idle", "the sequence")
        until(lambda: served.get("/api/capture").body["state"] == "idle", "the capture")
    answer = _request(served, command.argv[2:])
    if command.output is None:
        assert answer.status == 200, (command.line, answer.body)
        return
    assert len(command.output) == 1, f"line {command.line}: one JSON object a line"
    assert _timeless(answer.body) == _timeless(json.loads(command.output[0])), command.line


@pytest.mark.parametrize("command", ONE_SHOTS, ids=lambda command: f"line{command.line}")
def test_a_simulator_command_of_the_page_prints_what_the_page_shows(
    command: Command,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert command.output, f"line {command.line}: a simulator command shows what it prints"
    _runs_as_shown(command, "docs/panel.md", capsys)


def test_the_passthrough_s_client_prints_what_the_page_shows(
    served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    assert len(CLIENTS) == 1 and served.passthrough is not None
    argv = [
        served.passthrough if word.startswith("socket://") else word for word in CLIENTS[0].argv
    ]
    assert main(argv[1:]) == 0
    printed = capsys.readouterr()
    shown = CLIENTS[0].output
    assert shown is not None
    assert _read(printed.err.splitlines() + printed.out.splitlines()) == _read(shown)


def test_the_page_shows_enough_and_the_reader_reads_it() -> None:
    assert len([c for c in CURLS if c.output]) >= 5 and len([c for c in CURLS if not c.output]) >= 4
    text = "```console\n$ curl -s 'http://127.0.0.1:8040/api/x?q=A?;:B?'\n{}\n```\n"
    assert _curls(text) == [
        Command(
            2,
            ["kleidos-meter", "curl", "-s", "http://127.0.0.1:8040/api/x?q=A?;:B?"],
            ["{}"],
        )
    ]
    assert _curls("```sh\ncurl -N http://127.0.0.1:8040/api/events\n```\n")[0].output is None
    assert _read(["serve: panel at http://127.0.0.1:8040/"]) == [
        "serve: panel at http://<address>/"
    ]
    assert _read(["KM,0.17.0", "up 1.1 s"]) == ["KM,<version>", "up <seconds>"]
