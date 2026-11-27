# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The rig's schematic: the kit it is drawn with, its generator, its renders' metadata.

The generator and the kit need nothing but Python; the netlist check needs
kicad-cli (CI's ``schematic`` job runs it in KiCad 10.0.8's container), the
PDF's metadata needs pypdf.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARDWARE = REPO_ROOT / "hardware"
KIT = HARDWARE / "kicad"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(KIT))

import kleidos_kicad as kit  # noqa: E402
import media_meta  # noqa: E402

GENERATED = (
    "rig.kicad_sch",
    "rig-calibration.kicad_sch",
    "rig.kicad_pro",
    "rig.kicad_sym",
    "sym-lib-table",
)


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_file_of_the_kit_is_the_one_its_manifest_records() -> None:
    manifest = json.loads((KIT / "KIT.json").read_text(encoding="utf-8"))
    assert manifest["kit"] == "kleidos-kicad/1"
    assert manifest["source"] == "https://github.com/kleidos-dev/brand"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]), "sync the kit from a brand commit"
    for name, digest in manifest["files"].items():
        held = hashlib.sha256((KIT / name).read_bytes()).hexdigest()
        assert held == digest, f"{name}: never edit a copy; sync it from brand"
    extra = {p.name for p in KIT.iterdir()} - set(manifest["files"]) - {"KIT.json", "__pycache__"}
    assert not extra, f"not part of the kit: {sorted(extra)}"


def test_the_generator_gives_back_the_committed_files(tmp_path: Path) -> None:
    copy = tmp_path / "hardware"
    shutil.copytree(KIT, copy / "kicad")
    shutil.copy(HARDWARE / "gen_rig.py", copy / "gen_rig.py")
    for _ in range(2):  # twice: the same bytes every run
        subprocess.run([sys.executable, str(copy / "gen_rig.py")], check=True, capture_output=True)
        for name in GENERATED:
            assert (copy / name).read_bytes() == (HARDWARE / name).read_bytes(), name


def test_the_project_names_the_kit_s_sheet_its_variables_and_one_line_width() -> None:
    pro = json.loads((HARDWARE / "rig.kicad_pro").read_text(encoding="utf-8"))
    sheet = (HARDWARE / pro["schematic"]["page_layout_descr_file"]).resolve()
    assert sheet == kit.sheet("dark").resolve()
    assert pro["text_variables"] == {
        "LICENSE": "CERN-OHL-S-2.0",
        "SOURCE_URL": "github.com/kleidos-dev/meter",
    }
    widths = {c["wire_width"] for c in pro["net_settings"]["classes"]}
    widths.add(pro["schematic"]["drawing"]["default_line_thickness"])
    assert widths == {kit.style()["lines"]["connection_mil"]}
    assert pro["erc"]["rule_severities"] == {}, "no ERC rule is ignored"


def test_every_title_block_line_fits_its_cell() -> None:
    rig = load("gen_rig_under_test", HARDWARE / "gen_rig.py")
    assert kit.cell_findings(rig.title_block_lines()) == []
    assert "qa/meter" not in (HARDWARE / "rig.kicad_sch").read_text(encoding="utf-8")


@pytest.mark.parametrize("sheet", ["rig.kicad_sch", "rig-calibration.kicad_sch"])
def test_the_schematic_sits_on_the_grid_and_reads_no_kicad_library(sheet: str) -> None:
    text = (HARDWARE / sheet).read_text(encoding="utf-8")
    grid = kit.style()["lines"]["grid"]
    connectable = re.findall(
        r"\n\t\((wire|junction|global_label|label|no_connect|symbol)\b([\s\S]*?)\n\t\)", text
    )
    assert len(connectable) > 50
    for kind, body in connectable:
        points = re.findall(r"\((?:xy|at) (-?[\d.]+) (-?[\d.]+)", body)
        for x, y in points[:1] if kind in ("symbol", "global_label", "label") else points:
            for value in (float(x), float(y)):
                assert abs(value / grid - round(value / grid)) < 1e-6, (kind, x, y)
    assert set(re.findall(r'\(lib_id "([^":]+):', text)) == {"rig"}


def test_the_calibration_sheet_is_sheet_two_and_redraws_sheet_one_s_parts() -> None:
    rig = (HARDWARE / "rig.kicad_sch").read_text(encoding="utf-8")
    cal = (HARDWARE / "rig-calibration.kicad_sch").read_text(encoding="utf-8")
    assert '(property "Sheetfile" "rig-calibration.kicad_sch"' in rig
    assert '(page "2")' in rig

    def refs(text: str) -> set[str]:
        return set(re.findall(r'\(reference "([^"#][^"]*)"\)', text))

    again = {"BT1", "RW1", "J2", "RW2", "R100", "K1", "RW3", "U1", "U4"}
    assert again <= refs(rig) and not refs(cal) & refs(rig), "every reference once in the project"
    plus_100 = {re.sub(r"\d+$", lambda m: str(int(m.group()) + 100), name) for name in again}
    assert refs(cal) == plus_100 | {"R4"}, "sheet 2 is sheet 1's parts, +100, and the reference"
    for title in (rig, cal):
        assert re.search(r'\(title_block[\s\S]*?\(rev "D"\)', title)


def test_the_records_are_what_the_golden_files_hold() -> None:
    pdf = media_meta.record(media_meta.RIG_PDF)
    svg = media_meta.record(media_meta.RIG_SVG)
    cal = media_meta.record(media_meta.RIG_CAL_SVG)
    assert media_meta.pdf_xmp(pdf, "uuid:document", "uuid:instance") == (
        FIXTURES / "rig-pdf-xmp.xml"
    ).read_text(encoding="utf-8")
    assert media_meta.svg_block(svg) == (FIXTURES / "rig-svg-block.xml").read_text(encoding="utf-8")
    assert media_meta.svg_block(cal) == (FIXTURES / "rig-calibration-svg-block.xml").read_text(
        encoding="utf-8"
    )
    assert pdf.spdx == svg.spdx == cal.spdx == "CERN-OHL-S-2.0"
    assert (
        media_meta.CREDIT_MARK in pdf.usage_terms
        and media_meta.TRADEMARK_NOTE_KLEIDOS in svg.usage_terms
    )


def test_a_stamped_svg_stamps_to_itself_and_loses_kicad_s_clock() -> None:
    raw = (
        '<?xml version="1.0" standalone="no"?>\n <!DOCTYPE svg>\n<svg\n  xmlns="http://www.w3.org/2000/svg"'
        ' viewBox="0 0 1 1">\n'
        "<title>SVG Image created as rig.svg date 2026-11-19T20:27:51 </title>\n"
        "  <desc>Image generated by Eeschema-SVG </desc>\n<g/>\n</svg>\n"
    )
    meta = media_meta.record(media_meta.RIG_SVG)
    once = media_meta.stamp_svg(raw, meta)
    assert once.startswith("<!-- SPDX-")
    assert media_meta.stamp_svg(once, meta) == once
    assert "Eeschema" not in once and "20:27" not in once and "<g/>" in once


def test_every_render_carries_its_record() -> None:
    pytest.importorskip("pypdf")
    check = load("check_metadata_under_test", REPO_ROOT / "scripts" / "check_metadata.py")
    assert check.findings() == []


@pytest.mark.skipif(shutil.which(kit.kicad_cli()) is None, reason="kicad-cli is not installed")
def test_erc_is_clean_and_the_netlist_is_the_rig() -> None:
    out = subprocess.run(
        [sys.executable, str(HARDWARE / "export_rig.py"), "--no-export"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ERC: 0 messages" in out.stdout


def test_the_netlist_check_refuses_a_net_the_rig_does_not_have() -> None:
    pytest.importorskip("pypdf")
    rig = load("export_rig_under_test", HARDWARE / "export_rig.py")
    nets = "".join(
        f'<net name="{name}">'
        + "".join(f'<node ref="{r}" pin="{p}"/>' for r, p in sorted(nodes))
        + "</net>"
        for name, nodes in rig.EXPECTED.items()
    )
    assert rig.netlist_findings(f"<export><nets>{nets}</nets></export>") == []
    moved = nets.replace('<node ref="U1" pin="2"/>', "", 1)
    found = rig.netlist_findings(f"<export><nets>{moved}</nets></export>")
    assert any("net /VIN-" in line for line in found)
