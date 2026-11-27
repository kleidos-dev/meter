# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""Check the rig's schematic and write its renders.

The renders are ``rig.pdf``, ``rig.svg`` and ``rig-calibration.svg``.

Runs, in ``hardware/``, through the Kleidos KiCad kit's ``kleidos_kicad.py``
(``hardware/kicad/``, copied from the brand repository), the same way the
boards repository exports its schematics:

1. ``kicad-cli sch erc``: the style allows no message, error or warning;
2. the netlist against :data:`EXPECTED`, net for net and pin for pin: what the
   wiring is (rev D's, and sheet 2's for the calibration), as a check instead
   of a paragraph;
3. the PDF, in the kit's dark theme and on its drawing sheet, without the
   property pop-ups; every page draws the kit's mark once; then the finish
   (:func:`finish`): no script, a bookmark per sheet, every page's ``/Font``
   dictionary in key order, the metadata of ``scripts/media_meta.py``'s record
   and a file identifier derived from the sources, so the same sources give
   the same bytes;
4. the SVGs, one per sheet, in the same theme and drawing sheet, each one's
   background and wire width checked, its head stamped with its record
   (``media_meta.stamp_svg``).

Usage::

    python3 hardware/export_rig.py [--no-export] [--out DIR]

``KICAD_CLI`` names another kicad-cli. ``--out`` writes the three renders
elsewhere instead of beside the schematic. CI runs it in KiCad 10.0.8's
container and fails when the committed renders are not its bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "kicad"))
sys.path.insert(0, str(ROOT / "scripts"))
import kleidos_kicad as kit  # noqa: E402
import media_meta  # noqa: E402

SCHEMATIC = HERE / "rig.kicad_sch"
CALIBRATION = HERE / "rig-calibration.kicad_sch"
GROUND = "dark"
#: The SVG kicad-cli writes for each sheet (the project's name, then the sheet's),
#: and the name it is committed under.
SVGS = {"rig.svg": "rig.svg", "rig-calibration.svg": "rig-calibration.svg"}
#: The PDF's bookmarks, one per page.
BOOKMARKS = ("Rig wiring", "Shunt calibration")

#: The rig as wired (rev D), sheet 1, and the calibration's wiring, sheet 2:
#: every net, by name, and the pins on it. Sheet 2 draws sheet 1's parts again
#: numbered +100 (U101 is U1, R200 is R100), so its own nets are its own; its
#: named ones carry the sheet's name (``/calibration/``), and the global ones
#: (the ground, the supply, the bus, the relay drives) are the same nets as on
#: sheet 1, as they are on the bench.
EXPECTED: dict[str, set[tuple[str, str]]] = {
    "/VBUS": {("JP2", "1"), ("RW2", "2"), ("U1", "3")},
    "/VIN+": {("JP2", "2"), ("R100", "1"), ("U1", "1"), ("U4", "RL1A")},
    "/VIN-": {("K1", "COM"), ("R100", "2"), ("U1", "2")},
    "/DUT+": {("K1", "NC"), ("RW3", "1"), ("U4", "RL1B")},
    "/5V": {("JP1", "1"), ("U2", "8"), ("U3", "10")},
    "/calibration/VIN+": {("R200", "1"), ("RW102", "2"), ("U101", "1"), ("U104", "RL1A")},
    "/calibration/VIN-": {("K101", "COM"), ("R200", "2"), ("U101", "2")},
    "/calibration/DUT+": {("K101", "NC"), ("RW103", "1"), ("U104", "RL1B")},
    "/calibration/REF+": {("R4", "1"), ("RW103", "2"), ("U101", "3")},
    "GND": {
        ("BT1", "2"),
        ("BT101", "2"),
        ("C1", "2"),
        ("J1", "2"),
        ("R4", "2"),
        ("U1", "5"),
        ("U101", "5"),
        ("U104", "8"),
        ("U2", "10"),
        ("U3", "5"),
        ("U4", "8"),
    },
    "+3V3": {
        ("C1", "1"),
        ("R1", "1"),
        ("R2", "1"),
        ("R3", "1"),
        ("U1", "4"),
        ("U101", "4"),
        ("U104", "7"),
        ("U2", "9"),
        ("U4", "7"),
    },
    "SDA": {("R1", "2"), ("U1", "6"), ("U101", "6"), ("U2", "1")},
    "SCL": {("R2", "2"), ("U1", "7"), ("U101", "7"), ("U2", "2")},
    "ALERT": {("R3", "2"), ("U1", "8"), ("U101", "8"), ("U2", "3")},
    "BYPASS": {("U104", "16"), ("U2", "5"), ("U4", "16")},
    "JD-VCC": {
        ("JP1", "2"),
        ("K1", "A1"),
        ("K101", "A1"),
        ("K2", "A1"),
        ("K3", "A1"),
        ("K4", "A1"),
        ("U3", "11"),
    },
    "K1_DRV": {("K1", "A2"), ("K101", "A2"), ("U3", "1")},
    "K2_DRV": {("K2", "A2"), ("U3", "2")},
    "K3_DRV": {("K3", "A2"), ("U3", "3")},
    "K4_DRV": {("K4", "A2"), ("U3", "4")},
    "Net-(BT1-+)": {("BT1", "1"), ("RW1", "1")},
    "Net-(BT101-+)": {("BT101", "1"), ("RW101", "1")},
    "Net-(J1-+)": {("J1", "1"), ("RW3", "2")},
    "Net-(J2-Pad1)": {("J2", "1"), ("RW1", "2")},
    "Net-(J2-Pad2)": {("J2", "2"), ("RW2", "1")},
    "Net-(J102-Pad1)": {("J102", "1"), ("RW101", "2")},
    "Net-(J102-Pad2)": {("J102", "2"), ("RW102", "1")},
    "Net-(U2-GPIO7_(T7))": {("U2", "4"), ("U3", "6")},
    **{
        f"unconnected-({k}-Pad{c})": {(k, c)}
        for k, c in (
            ("K1", "NO"),
            ("K101", "NO"),
            ("K2", "COM"),
            ("K2", "NC"),
            ("K2", "NO"),
            ("K3", "COM"),
            ("K3", "NC"),
            ("K3", "NO"),
            ("K4", "COM"),
            ("K4", "NC"),
            ("K4", "NO"),
        )
    },
    **{
        f"unconnected-({u}-{name}-Pad{pin})": {(u, pin)}
        for u in ("U4", "U104")
        for name, pin in (("RL2_(AN,_1)", "1"), ("RL2_A", "RL2A"), ("RL2_B", "RL2B"))
    },
    **{f"unconnected-(U3-IN{n}-Pad{n + 5})": {("U3", str(n + 5))} for n in (2, 3, 4)},
    "unconnected-(U2-GPIO16_(T9)-Pad6)": {("U2", "6")},
    "unconnected-(U2-GPIO17_(T10)-Pad7)": {("U2", "7")},
}


def netlist_findings(xml: str) -> list[str]:
    """What the drawn netlist (kicad-cli's ``kicadxml``) says that :data:`EXPECTED` does not."""
    drawn: dict[str, set[tuple[str | None, str | None]]] = {}
    # S314: the XML is kicad-cli's netlist of our own schematic.
    nets = ET.fromstring(xml).find("nets")  # noqa: S314
    for net in nets if nets is not None else ():
        drawn[net.get("name", "")] = {
            (node.get("ref"), node.get("pin")) for node in net.findall("node")
        }
    found = []
    for name in sorted(set(EXPECTED) | set(drawn)):
        if name not in drawn:
            found.append(f"net {name}: not drawn")
        elif name not in EXPECTED:
            found.append(f"net {name}: drawn, but the rig has no such net: {sorted(drawn[name])}")
        elif drawn[name] != EXPECTED[name]:
            found.append(
                f"net {name}: drawn on {sorted(drawn[name])}, the rig has {sorted(EXPECTED[name])}"
            )
    return found


def sources_digest() -> hashlib._Hash:
    """SHA-256 of what the PDF is drawn from.

    The two sheets, then the kit's theme, sheet and style.
    """
    digest = hashlib.sha256(SCHEMATIC.read_bytes())
    digest.update(CALIBRATION.read_bytes())
    for path in (kit.theme_file(GROUND), kit.sheet(GROUND), kit.STYLE):
        digest.update(path.read_bytes())
    return digest


def strip_scripts(writer: Any) -> None:
    """Remove every JavaScript the document holds.

    kicad-cli keeps the pop-ups' script even without them.
    """
    from pypdf.generic import ArrayObject, NameObject

    root = writer._root_object
    names = root.get("/Names")
    if names is not None:
        names = names.get_object()
        names.pop("/JavaScript", None)
        if not names:
            del root["/Names"]
    for key in ("/OpenAction", "/AA"):
        root.pop(key, None)
    for page in writer.pages:
        page.pop("/AA", None)
        annots = page.get("/Annots")
        if annots is None:
            continue
        keep = ArrayObject(
            ref
            for ref in annots
            if not (
                (a := ref.get_object().get("/A")) is not None
                and a.get_object().get("/S") == "/JavaScript"
            )
        )
        page[NameObject("/Annots")] = keep


def finish(pdf: Path) -> None:
    """Rewrite *pdf* in place.

    No script, a bookmark per page, fonts in order, the record's metadata, a
    derived identifier.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        ByteStringObject,
        DecodedStreamObject,
        DictionaryObject,
        Fit,
        NameObject,
        TextStringObject,
    )

    meta = media_meta.record(media_meta.RIG_PDF)
    writer = PdfWriter(clone_from=PdfReader(pdf))
    strip_scripts(writer)
    if "/Outlines" in writer._root_object:
        del writer._root_object["/Outlines"]
    if len(writer.pages) != len(BOOKMARKS):
        raise SystemExit(
            f"rig.pdf has {len(writer.pages)} pages; the project draws {len(BOOKMARKS)} sheets"
        )
    for page, title in enumerate(BOOKMARKS):
        writer.add_outline_item(title=title, page_number=page, fit=Fit.fit())
    writer._root_object[NameObject("/PageMode")] = NameObject("/UseOutlines")
    digest = sources_digest()
    document = "uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, meta.identifier_url))
    instance = "uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{meta.identifier_url}#{digest.hexdigest()}")
    )
    writer.metadata = {
        NameObject(k): TextStringObject(v) for k, v in media_meta.pdf_info(meta).items()
    }
    stream = DecodedStreamObject()
    stream.set_data(media_meta.pdf_xmp(meta, document, instance).encode("utf-8"))
    stream[NameObject("/Type")] = NameObject("/Metadata")
    stream[NameObject("/Subtype")] = NameObject("/XML")
    root = writer._root_object
    root[NameObject("/Metadata")] = writer._add_object(stream)
    root[NameObject("/Lang")] = TextStringObject(meta.language)
    root[NameObject("/ViewerPreferences")] = DictionaryObject(
        {NameObject("/DisplayDocTitle"): BooleanObject(True)}
    )
    file_id = digest.digest()[:16]
    writer._ID = ArrayObject([ByteStringObject(file_id), ByteStringObject(file_id)])
    kit.sort_fonts(writer)
    with open(pdf, "wb") as handle:
        writer.write(handle)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--no-export", action="store_true", help="check only, write no render")
    parser.add_argument(
        "--out", type=Path, help="write rig.pdf and the SVGs here instead of hardware/"
    )
    args = parser.parse_args(argv)
    env = kit.environment()
    count, report = kit.erc(SCHEMATIC, env)
    if count is None:
        print(report or "ERC did not run")
        return 1
    print(f"ERC: {count} messages")
    if count:
        print("\n".join(report.splitlines()[:60]))
        return 1
    with tempfile.TemporaryDirectory(prefix="kleidos-rig-") as scratch:
        xml = Path(scratch) / "rig.xml"
        kit.run(
            ["sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), SCHEMATIC.name],
            HERE,
            env,
        )
        errors = netlist_findings(xml.read_text(encoding="utf-8"))
        for error in errors:
            print(f"FAIL {error}")
        if errors:
            return 1
        print(
            f"netlist: the drawing is the rig, {len(EXPECTED)} nets and "
            f"{sum(map(len, EXPECTED.values()))} pins"
        )
        if args.no_export:
            return 0
        out = (args.out or HERE).resolve()
        out.mkdir(parents=True, exist_ok=True)
        pdf = Path(scratch) / "rig.pdf"
        done = kit.export_pdf(SCHEMATIC, pdf, env, GROUND)
        if done.returncode:
            print(done.stdout, done.stderr)
            return 1
        from pypdf import PdfReader

        errors = kit.pdf_findings(PdfReader(pdf), GROUND)
        if errors:
            print("\n".join(f"FAIL the mark: {e}" for e in errors))
            return 1
        finish(pdf)
        svg_dir = Path(scratch) / "svg"
        done = kit.export_svg(SCHEMATIC, svg_dir, env, GROUND)
        written = sorted(p.name for p in svg_dir.glob("*.svg")) if svg_dir.is_dir() else []
        if done.returncode or written != sorted(SVGS):
            print(
                done.stdout,
                done.stderr,
                f"kicad-cli wrote {written}, one SVG per sheet is {sorted(SVGS)}",
            )
            return 1
        stamped = {}
        for exported, committed in SVGS.items():
            svg = svg_dir / exported
            errors = kit.svg_findings(svg, GROUND)
            if errors:
                print("\n".join(f"FAIL {e}" for e in errors))
                return 1
            stamped[committed] = media_meta.stamp_svg(
                svg.read_text(encoding="utf-8"), media_meta.record(f"hardware/{committed}")
            )
        shutil.copyfile(pdf, out / "rig.pdf")
        for committed, body in stamped.items():
            (out / committed).write_text(body, encoding="utf-8")
    print(
        f"wrote rig.pdf, {', '.join(SVGS.values())} in {out}: the kit's theme and drawing "
        "sheet, the mark, the records' metadata"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
