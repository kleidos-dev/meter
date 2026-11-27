# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: MIT
"""Run kicad-cli with the Kleidos KiCad kit, the same way in every repository.

This file travels with the kit: brand writes it, and ``tools/sync_kicad.py``
copies it, with the two drawing sheets, the two color themes and
``style.json``, into a consumer's kit folder (``boards/_shared/kicad/``,
``meter/hardware/kicad/``). It reads everything from the folder it is in, so
a consumer imports it from its copy and never needs brand checked out. Never
edit a copy: change brand and sync.

What it does, and what kicad-cli would let pass in silence:

- :func:`environment`: a KiCad settings folder of its own (``KICAD_CONFIG_HOME``)
  that asks for English (kicad-cli prints, and writes into a PDF, in the
  install's language) and holds both themes under ``colors/``;
- :func:`erc`: ``kicad-cli sch erc --severity-all``; the style allows no
  message, error or warning;
- :func:`export_pdf` and :func:`export_svg`: the theme of the ground, its
  drawing sheet (``--drawing-sheet``), and ``--default-font`` when the kit names
  a face; the PDF without the property pop-ups;
- :func:`svg_findings`: kicad-cli falls back to its default theme without a
  word when it cannot find the one named, so every SVG's background must be
  the theme's; and every line in the wire color has the one connection width;
- :func:`pdf_findings`: every page draws the mark once, the same image object
  on every page, at the logo file's pixel size, with its soft mask;
- :func:`sort_fonts`: a page's ``/Font`` dictionary in key order, so a PDF
  rewritten by pypdf comes out with the same bytes on every run (kicad-cli
  writes two outline faces in either order).

Python 3.10 or later and the standard library; :func:`pdf_findings` and
:func:`sort_fonts` take pypdf objects and import nothing themselves.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parent
STYLE = KIT / "style.json"
GROUNDS = ("dark", "light")
MIL = 0.0254


def style() -> dict[str, Any]:
    """Return the kit's ``style.json``: the sheet's geometry, the sizes, the widths, each ground's
    colors."""
    data: dict[str, Any] = json.loads(STYLE.read_text(encoding="utf-8"))
    return data


def sheet(ground: str = "dark") -> Path:
    """Return the drawing sheet of *ground*."""
    return KIT / f"kleidos-{ground}.kicad_wks"


def theme_file(ground: str = "dark") -> Path:
    """Return the color theme of *ground*."""
    return KIT / f"kleidos-{ground}.json"


def theme_name(ground: str = "dark") -> str:
    """Return the name kicad-cli is given for the theme of *ground*: its file's name under
    ``colors/``."""
    return f"kleidos-{ground}"


def connection_mm() -> float:
    """Return the one width of every connection (wires, pin lines), in mm."""
    return float(style()["lines"]["connection_mil"]) * MIL


def rgb(color: str) -> tuple[int, int, int]:
    """Return the red, green and blue of a ``#rrggbb`` color."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def theme_colors(ground: str = "dark") -> dict[str, str]:
    """Return the theme's schematic colors as an SVG writes them (``#RRGGBB``), by KiCad key."""
    data = json.loads(theme_file(ground).read_text(encoding="utf-8"))["schematic"]
    out = {}
    for key, value in data.items():
        match = re.fullmatch(r"rgba?\((\d+), (\d+), (\d+)(?:, [\d.]+)?\)", str(value))
        if match:
            out[key] = "#" + "".join(f"{int(part):02X}" for part in match.groups())
    return out


#: The roles a drawing writes as explicit colors, which a theme cannot turn.
EXPLICIT_ROLES = ("addition", "rule")


def explicit(ground: str = "dark") -> dict[str, tuple[int, int, int]]:
    """Return the explicit colors a generator writes on *ground* (the addition marker, a table's
    rules), by role."""
    colors = style()["colors"][ground]
    return {role: rgb(colors[role]) for role in EXPLICIT_ROLES}


def recolor(text: str, ground: str) -> str:
    """Return a ``.kicad_sch`` written for the dark ground with its explicit colors turned to
    *ground*'s.

    The committed sources are the dark ground; a light export runs on a
    recolored copy and never rewrites them.
    """
    if ground == "dark":
        return text
    dark, other = explicit("dark"), explicit(ground)
    for role in EXPLICIT_ROLES:
        old = "(color {} {} {} 1)".format(*dark[role])
        text = text.replace(old, "(color {} {} {} 1)".format(*other[role]))
    return text


def text_width(text: str, size: float) -> float:
    """Return how wide KiCad sets *text* at *size* (its nominal size, in mm) in the kit's face.

    KiCad's stroke font: the sum of the measured advances in ``style.json``
    (bold takes the same ones); a character not measured counts as the widest.
    """
    advances: dict[str, float] = style()["font"]["stroke_advance"]
    widest = max(advances.values())
    return sum(advances.get(char, widest) for char in text) * size


def cell_findings(lines: list[tuple[str, str]]) -> list[str]:
    """Return every title-block line wider than its cell, which KiCad would condense.

    :param lines: ``(cell, text)`` pairs: a cell of ``style.json``'s sheet
        (``title``, ``file``, ``comments`` and so on) and the text it prints.
    """
    geo = style()["sheet"]
    found = []
    for cell, text in lines:
        box = geo["cells"][cell]
        room = box["box"][2] - box["box"][0] - 2 * geo["text_inset"]
        width = text_width(text, box["size"])
        if width > room:
            found.append(f"{text!r}: {width:.1f} mm in the {cell} cell, which holds {room:g} mm")
    return found


def kicad_cli() -> str:
    """Return the kicad-cli to run: ``KICAD_CLI``, or the one on the path."""
    return os.environ.get("KICAD_CLI") or "kicad-cli"


def environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment kicad-cli runs in: a settings folder of its own, in English, with both
    themes.

    The folder is made under the system's temporary folder and left for the
    caller's process to end with; it holds no secret.
    """
    home = Path(tempfile.mkdtemp(prefix="kleidos-kicad-"))
    for version in ("9.0", "10.0", "11.0"):
        (home / version / "colors").mkdir(parents=True)
        (home / version / "kicad_common.json").write_text(
            json.dumps({"system": {"language": "English"}}), encoding="utf-8"
        )
        for ground in GROUNDS:
            shutil.copyfile(
                theme_file(ground), home / version / "colors" / f"{theme_name(ground)}.json"
            )
    env = dict(os.environ if base is None else base)
    env["KICAD_CONFIG_HOME"] = str(home)
    return env


def _font_args() -> list[str]:
    family = style()["font"]["family"]
    return ["--default-font", family] if family else []


def run(
    args: list[str], cwd: Path, env: dict[str, str], cli: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run kicad-cli with *args* in *cwd*."""
    return subprocess.run(
        [cli or kicad_cli(), *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def erc(schematic: Path, env: dict[str, str], cli: str | None = None) -> tuple[int | None, str]:
    """Run ERC on *schematic* with every severity; return the number of messages (None when it did
    not run) and the report."""
    with tempfile.TemporaryDirectory(prefix="kleidos-erc-") as scratch:
        report = Path(scratch) / "erc.rpt"
        run(
            ["sch", "erc", "--severity-all", "-o", str(report), schematic.name],
            schematic.parent,
            env,
            cli,
        )
        text = report.read_text(encoding="utf-8") if report.exists() else ""
    match = re.search(r"ERC messages: (\d+)", text)
    return (int(match.group(1)) if match else None), text


def export_args(ground: str = "dark") -> list[str]:
    """Return the arguments every export takes: the ground's theme, its sheet, the kit's face."""
    return ["--theme", theme_name(ground), "--drawing-sheet", str(sheet(ground)), *_font_args()]


def export_pdf(
    schematic: Path, pdf: Path, env: dict[str, str], ground: str = "dark", cli: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Export every sheet of *schematic* to *pdf*, in the ground's theme and sheet, without the
    property pop-ups."""
    return run(
        [
            "sch",
            "export",
            "pdf",
            *export_args(ground),
            "--exclude-pdf-property-popups",
            "-o",
            str(pdf),
            schematic.name,
        ],
        schematic.parent,
        env,
        cli,
    )


def export_svg(
    schematic: Path, folder: Path, env: dict[str, str], ground: str = "dark", cli: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Export every sheet of *schematic*, one SVG each, into *folder*, in the ground's theme and
    sheet."""
    return run(
        ["sch", "export", "svg", *export_args(ground), "-o", str(folder), schematic.name],
        schematic.parent,
        env,
        cli,
    )


def svg_findings(svg: Path, ground: str = "dark", wires: bool = True) -> list[str]:
    """Return what is wrong with a page kicad-cli wrote: not the theme's background, or a
    wire-colored line of another width."""
    text = svg.read_text(encoding="utf-8")
    palette = theme_colors(ground)
    found = []
    # [^<>]: an attempt that starts at one group's opening never runs past the
    # next tag, so a page of unclosed openings is read in linear time.
    background = re.search(
        r'<g style="fill:(#[0-9A-F]{6});[^<>]*>\s*<rect x="0\.0+" y="0\.0+"', text
    )
    if not background or background.group(1) != palette["background"]:
        found.append(
            f"{svg.name}: background {background.group(1) if background else 'missing'}, the "
            "theme's is"
            f" {palette['background']} (was the theme found?)"
        )
    wire, width = palette["wire"], round(connection_mm(), 4)
    widths = set()
    for match in re.finditer(r'<g style="([^"]*)"', text):
        css = match.group(1)
        if f"stroke:{wire};" in css and "fill:none" in css:
            stroke = re.search(r"stroke-width:([\d.]+)", css)
            if stroke:
                widths.add(round(float(stroke.group(1)), 4))
    if widths and widths != {width}:
        found.append(
            f"{svg.name}: lines in the wire color take widths {sorted(widths)} mm, not only "
            f"{width:.4f}"
        )
    if wires and not widths:
        found.append(f"{svg.name}: no line in the wire color {wire}")
    return found


def pdf_findings(reader: Any, ground: str = "dark") -> list[str]:
    """Return what is wrong with the mark in a PDF kicad-cli wrote (a pypdf ``PdfReader`` or
    ``PdfWriter``).

    Every page draws one image: the mark, at the logo file's pixel size, with
    its soft mask (the transparency kept), and the same object on every page.
    """
    px = style()["grounds"][ground]["mark_px"]
    found = []
    seen = set()
    for number, page in enumerate(reader.pages, start=1):
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else {}
        objects = resources.get("/XObject")
        objects = objects.get_object() if objects is not None else {}
        images = []
        for ref in objects.values():
            target = ref.get_object()
            if target.get("/Subtype") == "/Image":
                images.append((getattr(ref, "idnum", id(target)), target))
        if len(images) != 1:
            found.append(f"page {number}: {len(images)} images; the page draws the mark once")
            continue
        idnum, image = images[0]
        if [
            int(image.get("/Width", 0)),
            int(image.get("/Height", 0)),
        ] != px or "/SMask" not in image:
            found.append(
                f"page {number}: its image is not the mark of {px[0]} x {px[1]} px with its soft "
                "mask"
            )
        seen.add(idnum)
    if len(seen) > 1:
        found.append(
            f"the pages draw {len(seen)} image objects; the mark is stored once for every page"
        )
    return found


def sort_fonts(writer: Any) -> None:
    """Put every page's ``/Font`` dictionary in key order (a pypdf ``PdfWriter``), so the rewrite is
    reproducible."""
    from pypdf.generic import DictionaryObject, NameObject

    for page in writer.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        resources = resources.get_object()
        fonts = resources.get("/Font")
        if fonts is None:
            continue
        held = fonts.get_object()
        resources[NameObject("/Font")] = DictionaryObject(
            {NameObject(key): held[key] for key in sorted(held)}
        )
