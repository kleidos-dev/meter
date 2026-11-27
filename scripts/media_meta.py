# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The embedded metadata of every media file this repository publishes (``kleidos-media-meta/1``).

The organization's metadata profile, as the boards repository writes it for
its schematic PDFs and drawings (``_shared/media_meta.py`` there): one record
per file, made from what the repository already knows, never typed and never
written by hand or with exiftool. Here the media are the rig's three renders:

- ``hardware/rig.pdf``: the Info dictionary (title, author, subject, keywords,
  ``/Creator`` the KiCad that exported it, ``/Producer`` this repository's
  finisher, both dates the title block's), the same facts as an uncompressed
  XMP stream, ``/Lang`` and ``/DisplayDocTitle``; written by
  ``hardware/export_rig.py``;
- ``hardware/rig.svg`` and ``hardware/rig-calibration.svg`` (sheet 1, the
  rig, and sheet 2, its calibration): the SPDX comment first, the root's accessible name, a
  descriptive ``<title>`` and ``<desc>`` in place of KiCad's (which hold the
  export's clock) and a Dublin Core and ccREL ``<metadata>`` block between
  REUSE's ignore markers; the drawing is not touched;
- ``docs/images/panel/*.png``, the panel's images ``scripts/take_panel_shots.py``
  takes (``scripts/panel_images.py`` lists them): ``iTXt`` chunks right after
  the header, ``Title``, ``Author``, ``Description``, ``Copyright`` and
  ``License`` (the usage terms with the license's address), and no other text,
  time or EXIF chunk.

The license and the holders are what ``REUSE.toml`` states for the path
(CERN-OHL-S-2.0 for ``hardware/``); ``scripts/check_metadata.py`` holds every
file to its record. No clock, path, user, host, e-mail or tool version goes in.
"""

# ruff: noqa: E501
# The long lines are XML the files carry, one element a line, as the boards repository writes it.

from __future__ import annotations

import re
import struct
import tomllib
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import panel_images

ROOT = Path(__file__).resolve().parents[1]

META_PROFILE = "kleidos-media-meta/1"
CREATOR = "José M. Requena Plens"
CREATOR_URL = "https://kleidos.dev"
PROJECT = "Kleidos"
PROJECT_URL = "https://kleidos.dev"
CREDIT = CREATOR
BASE_KEYWORDS = ("Kleidos", "hardware password manager", "open source")
TRADEMARK_NOTE_KLEIDOS = (
    "The Kleidos name and logo are subject to the terms in the brand repository's "
    "TRADEMARKS.md (https://github.com/kleidos-dev/brand/blob/main/TRADEMARKS.md)."
)
REPO_URL = "https://github.com/kleidos-dev/meter"
RIGHTS_URL = f"{REPO_URL}/blob/main/NOTICE.md"
#: The PDF's ``/Producer`` and the KiCad it is exported with (``.github/workflows/check.yml``).
PDF_PRODUCER = "kleidos-meter export_rig.py"
PDF_CREATOR = "KiCad 10.0.8 Eeschema"
DST_BASE = "http://cv.iptc.org/newscodes/digitalsourcetype/"
SPDX_LISTED = {"GPL-3.0-or-later", "CERN-OHL-S-2.0", "MIT"}
#: The rig's title block shows the Kleidos mark, from the KiCad kit's drawing sheet.
CREDIT_MARK = (
    "Title-block mark: the Kleidos mark, © 2026 José M. Requena Plens, GPL-3.0-or-later, "
    "placed unchanged under the press permission in brand's TRADEMARKS.md."
)

#: REUSE's ignore markers and tags, in two halves so REUSE does not read this file's own source.
REUSE_START = "REUSE-IgnoreStart"
REUSE_END = "REUSE-IgnoreEnd"
TAG_LICENSE = "SPDX-License-Identifier"
COPYRIGHT_SIGN = "©"

RIG_PDF = "hardware/rig.pdf"
RIG_SVG = "hardware/rig.svg"
RIG_CAL_SVG = "hardware/rig-calibration.svg"
RIG_SCH = "hardware/rig.kicad_sch"


class MetaError(ValueError):
    """A record the profile does not allow (a field over its limit, an unknown path)."""


def license_url(spdx_id: str) -> str:
    if spdx_id not in SPDX_LISTED:
        raise MetaError(f"{spdx_id}: no license page this repository can name")
    return f"https://spdx.org/licenses/{spdx_id}.html"


def _glob(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def reuse_truth(path: str) -> tuple[str, tuple[str, ...]]:
    """(SPDX expression, holders without e-mail) that ``REUSE.toml`` states for *path*: the last annotation that matches."""
    toml = tomllib.loads((ROOT / "REUSE.toml").read_text(encoding="utf-8"))
    found = None
    for annotation in toml.get("annotations", []):
        paths = annotation["path"] if isinstance(annotation["path"], list) else [annotation["path"]]
        if any(_glob(p).match(path) for p in paths):
            found = annotation
    if found is None:
        raise MetaError(f"{path}: REUSE.toml does not annotate it")
    holders = found["SPDX-FileCopyrightText"]
    holders = [holders] if isinstance(holders, str) else holders
    return found["SPDX-License-Identifier"], tuple(
        re.sub(r"\s*<[^>]*>", "", h).strip() for h in holders
    )


@dataclass(frozen=True)
class MediaMeta:
    """Everything one file's metadata says."""

    path: str
    title: str
    description: str
    alt: str
    keywords: tuple[str, ...]
    spdx: str
    holders: tuple[str, ...]
    media_type: str
    digital_source_type: str | None = None
    credits: tuple[str, ...] = ()
    trademarks: tuple[str, ...] = ()
    content_date: str | None = None
    language: str = "en"
    slug: str = "kl-meter-rig"

    @property
    def identifier_url(self) -> str:
        return f"{REPO_URL}/blob/main/{PurePosixPath(self.path).as_posix()}"

    @property
    def license_url(self) -> str:
        return license_url(self.spdx)

    @property
    def copyright(self) -> str:
        text = f"{COPYRIGHT_SIGN} " + f"; {COPYRIGHT_SIGN} ".join(self.holders) + f". {self.spdx}."
        if len(text.encode("utf-8")) > 128:
            raise MetaError(f"{self.path}: the copyright line is over 128 bytes: {text}")
        return text

    @property
    def usage_terms(self) -> str:
        return " ".join(
            [f"Licensed under {self.spdx}: {self.license_url}.", *self.credits, *self.trademarks]
        )


def _check(meta: MediaMeta) -> MediaMeta:
    if len(meta.title.encode("utf-8")) > 64:
        raise MetaError(f"{meta.path}: the title is over 64 bytes")
    if len(meta.description) > 300 or len(meta.alt) > 250:
        raise MetaError(f"{meta.path}: the description or the alt text is over its limit")
    if len(meta.keywords) > 12 or any(len(k.encode("utf-8")) > 64 for k in meta.keywords):
        raise MetaError(f"{meta.path}: more than 12 keywords, or one over 64 bytes")
    meta.copyright  # noqa: B018  (raises over 128 bytes)
    return meta


def schematic_date() -> str:
    """The date the rig's title block states: its only date."""
    text = (ROOT / RIG_SCH).read_text(encoding="utf-8")
    match = re.search(r'\(title_block.*?\(date "(\d{4}-\d{2}-\d{2})"\)', text, re.S)
    if not match:
        raise MetaError(f"{RIG_SCH}: the title block states no date")
    return match.group(1)


TITLE = "Kleidos bench meter rig schematic"
DESCRIPTION = (
    "Wiring of the Kleidos bench meter: an INA228 across a 0.1 Ω shunt between a board's cell "
    "and the board, with the relays that open the lead and bypass the shunt."
)
PDF_DESCRIPTION = (
    f"{DESCRIPTION} Its second sheet rewires the same parts for the shunt's calibration "
    "against a 100 Ω reference, four-wire."
)
CAL_TITLE = "Kleidos bench meter shunt calibration schematic"
CAL_DESCRIPTION = (
    "Sheet 2 of the Kleidos bench meter's rig: the same parts rewired for the shunt's two-point "
    "calibration, with a 100 Ω reference in the board's place, sensed four-wire."
)
KEYWORDS = (*BASE_KEYWORDS, "bench meter", "INA228", "schematic", "KiCad")


def record(path: str) -> MediaMeta:
    """The metadata record of the media file at *path* (relative to the repository's root)."""
    spdx, holders = reuse_truth(path)
    common: dict[str, Any] = {
        "path": path,
        "title": TITLE,
        "description": DESCRIPTION,
        "alt": "Schematic of the Kleidos bench meter's rig.",
        "keywords": KEYWORDS,
        "spdx": spdx,
        "holders": holders,
        "credits": (CREDIT_MARK,),
        "trademarks": (TRADEMARK_NOTE_KLEIDOS,),
    }
    if path == RIG_PDF:
        common["description"] = PDF_DESCRIPTION
        return _check(
            MediaMeta(media_type="application/pdf", content_date=schematic_date(), **common)
        )
    if path == RIG_CAL_SVG:
        common |= {
            "title": CAL_TITLE,
            "description": CAL_DESCRIPTION,
            "alt": "Schematic of the Kleidos bench meter's shunt calibration.",
            "slug": "kl-meter-rig-cal",
        }
    if path in (RIG_SVG, RIG_CAL_SVG):
        return _check(
            MediaMeta(media_type="image/svg+xml", digital_source_type="digitalCreation", **common)
        )
    if path.startswith(f"{panel_images.FOLDER}/") and path.endswith(".png"):
        return panel_record(path, spdx, holders)
    raise MetaError(f"{path}: no metadata record for this path")


#: Each state of the panel's images (``panel_images.STATES``) as a title says it.
PANEL_STATE_TITLES = {
    "live-smoothing-off": "smoothing off",
    "live-smoothing-100ms": "smoothing 100 ms",
    "pins-menu": "a pin's menu",
    "pins-conflict": "a conflict on a pin",
    "pins-reverted": "an apply reverted",
}
PANEL_KEYWORDS = (*BASE_KEYWORDS, "bench meter", "INA228", "panel", "screenshot")


def bench_record(path: str, item: dict[str, Any], spdx: str, holders: tuple[str, ...]) -> MediaMeta:
    """The record of one of the panel's bench images, from its manifest entry *item*."""
    view = panel_images.VIEWS[item["view"]]
    state = f", {PANEL_STATE_TITLES[item['state']]}" if item["state"] else ""
    shows = next(
        (words for name, _, words in panel_images.STATES if name == item["state"]),
        f"the {view} view",
    )
    description = (
        f"The panel of the Kleidos bench meter (kleidos-meter serve) over the cable to "
        f"instrument {item['instrument']} on firmware {item['fw']}, image elf {item['elf']}, "
        f"on the bench on {item['date']}: {shows}, {item['scheme']}, {item['width']} px wide."
    )
    return _check(
        MediaMeta(
            path=path,
            title=f"Kleidos meter panel on the bench: {view}{state}",
            description=description,
            alt=f"The Kleidos bench meter's panel on the bench, {item['date']}: {shows}.",
            keywords=(*PANEL_KEYWORDS, "bench"),
            spdx=spdx,
            holders=holders,
            media_type="image/png",
            digital_source_type="screenCapture",
            trademarks=(TRADEMARK_NOTE_KLEIDOS,),
            slug=f"kl-meter-panel-{path.rsplit('/', 1)[-1].removesuffix('.png')}",
        )
    )


def panel_record(path: str, spdx: str, holders: tuple[str, ...]) -> MediaMeta:
    """The record of one of the panel's images: what it shows, from ``panel_images``."""
    if path.startswith(f"{panel_images.FOLDER}/{panel_images.BENCH}/"):
        key = path.removeprefix(f"{panel_images.FOLDER}/")
        try:
            item = panel_images.load()["images"].get(key)
        except FileNotFoundError:
            item = None
        if item is None or item.get("kind") != "bench":
            raise MetaError(f"{path}: no metadata record for this path")
        return bench_record(path, item, spdx, holders)
    image = panel_images.BY_NAME.get(path.rsplit("/", 1)[-1])
    if image is None:
        raise MetaError(f"{path}: no metadata record for this path")
    view = panel_images.VIEWS[image.view]
    state = f", {PANEL_STATE_TITLES[image.state]}" if image.state else ""
    title = f"Kleidos meter panel: {view}{state}, {image.scheme}, {image.width} px"
    description = (
        f"The panel of the Kleidos bench meter (kleidos-meter serve) on its simulated "
        f"instrument: {image.shows}."
    )
    return _check(
        MediaMeta(
            path=path,
            title=title,
            description=description,
            alt=f"The Kleidos bench meter's panel: {image.shows}.",
            keywords=PANEL_KEYWORDS,
            spdx=spdx,
            holders=holders,
            media_type="image/png",
            digital_source_type="screenCapture",
            trademarks=(TRADEMARK_NOTE_KLEIDOS,),
            slug=f"kl-meter-panel-{image.name.removesuffix('.png')}",
        )
    )


def panel_media() -> tuple[str, ...]:
    """The panel's images the manifest lists, as paths (none before the first take)."""
    try:
        listed = panel_images.load()["images"]
    except FileNotFoundError:
        return ()
    return tuple(f"{panel_images.FOLDER}/{name}" for name in sorted(listed))


MEDIA = (RIG_PDF, RIG_SVG, RIG_CAL_SVG, *panel_media())


# ---------------------------------------------------------------- XMP and PDF


def xml_text(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


XMP_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "Iptc4xmpCore": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
    "plus": "http://ns.useplus.org/ldf/xmp/1.0/",
    "xmpRights": "http://ns.adobe.com/xap/1.0/rights/",
    "cc": "http://creativecommons.org/ns#",
    "pdf": "http://ns.adobe.com/pdf/1.3/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
}
XPACKET_BEGIN = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
XPACKET_END = '<?xpacket end="r"?>'


def _alt(tag: str, value: str, indent: str) -> str:
    return f'{indent}<{tag}><rdf:Alt><rdf:li xml:lang="x-default">{xml_text(value)}</rdf:li></rdf:Alt></{tag}>'


def pdf_date(date: str) -> tuple[str, str]:
    """(``D:YYYYMMDD000000Z``, ``YYYY-MM-DDT00:00:00Z``) of an authored ``YYYY-MM-DD``."""
    return f"D:{date.replace('-', '')}000000Z", f"{date}T00:00:00Z"


def content_date(meta: MediaMeta) -> str:
    """The date of the content a PDF's dates are written from: a PDF is never stamped without one."""
    if not meta.content_date:
        raise ValueError(f"{meta.title}: a PDF needs its content date")
    return meta.content_date


def pdf_info(meta: MediaMeta) -> dict[str, str]:
    """The Info dictionary of the rig's PDF, in its order."""
    when, _ = pdf_date(content_date(meta))
    return {
        "/Title": meta.title,
        "/Author": CREATOR,
        "/Subject": meta.description,
        "/Keywords": ", ".join(meta.keywords),
        "/Creator": PDF_CREATOR,
        "/Producer": PDF_PRODUCER,
        "/CreationDate": when,
        "/ModDate": when,
    }


def pdf_xmp(meta: MediaMeta, document_id: str, instance_id: str) -> str:
    """The PDF's XMP packet: the same facts as its Info dictionary, with the license and the credits."""
    _, iso = pdf_date(content_date(meta))
    i = "      "
    lines = [
        f"{i}<dc:format>{xml_text(meta.media_type)}</dc:format>",
        _alt("dc:title", meta.title, i),
        _alt("dc:description", meta.description, i),
        f"{i}<dc:creator><rdf:Seq><rdf:li>{xml_text(CREATOR)}</rdf:li></rdf:Seq></dc:creator>",
        _alt("dc:rights", meta.copyright, i),
        f"{i}<dc:subject><rdf:Bag>"
        + "".join(f"<rdf:li>{xml_text(k)}</rdf:li>" for k in meta.keywords)
        + "</rdf:Bag></dc:subject>",
        f"{i}<dc:identifier>{xml_text(meta.identifier_url)}</dc:identifier>",
        f"{i}<photoshop:Credit>{xml_text(CREDIT)}</photoshop:Credit>",
        f'{i}<Iptc4xmpCore:CreatorContactInfo rdf:parseType="Resource">',
        f"{i}  <Iptc4xmpCore:CiUrlWork>{CREATOR_URL}</Iptc4xmpCore:CiUrlWork>",
        f"{i}</Iptc4xmpCore:CreatorContactInfo>",
        f'{i}<plus:Licensor><rdf:Seq><rdf:li rdf:parseType="Resource">',
        f"{i}  <plus:LicensorName>{xml_text(CREATOR)}</plus:LicensorName>",
        f"{i}  <plus:LicensorURL>{xml_text(RIGHTS_URL)}</plus:LicensorURL>",
        f"{i}</rdf:li></rdf:Seq></plus:Licensor>",
        f"{i}<xmpRights:Marked>True</xmpRights:Marked>",
        f"{i}<xmpRights:WebStatement>{xml_text(meta.license_url)}</xmpRights:WebStatement>",
        _alt("xmpRights:UsageTerms", meta.usage_terms, i),
        f'{i}<cc:license rdf:resource="{xml_text(meta.license_url)}"/>',
        f"{i}<pdf:Keywords>{xml_text(', '.join(meta.keywords))}</pdf:Keywords>",
        f"{i}<pdf:Producer>{xml_text(PDF_PRODUCER)}</pdf:Producer>",
        f"{i}<xmp:CreatorTool>{xml_text(PDF_CREATOR)}</xmp:CreatorTool>",
        *(
            f"{i}<xmp:{tag}>{iso}</xmp:{tag}>"
            for tag in ("CreateDate", "ModifyDate", "MetadataDate")
        ),
        f"{i}<xmpMM:DocumentID>{document_id}</xmpMM:DocumentID>",
        f"{i}<xmpMM:InstanceID>{instance_id}</xmpMM:InstanceID>",
    ]
    ns = "\n".join(f'        xmlns:{p}="{uri}"' for p, uri in XMP_NS.items())
    return "\n".join(
        [
            XPACKET_BEGIN,
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
            '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
            '    <rdf:Description rdf:about=""',
            ns + ">",
            *lines,
            "    </rdf:Description>",
            "  </rdf:RDF>",
            "</x:xmpmeta>",
            XPACKET_END,
        ]
    )


# ---------------------------------------------------------------- SVG


def svg_block(meta: MediaMeta) -> str:
    """The ``<title>``, ``<desc>`` and Dublin Core and ccREL ``<metadata>`` of an SVG."""
    s = meta.slug
    lines = [
        f'<title id="{s}-t">{xml_text(meta.title)}</title>',
        f'<desc id="{s}-d">{xml_text(meta.description)}</desc>',
        f"<!-- {REUSE_START} -->",
        f'<metadata id="{s}-m">',
        '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"',
        '           xmlns:dc="http://purl.org/dc/elements/1.1/"',
        '           xmlns:cc="http://creativecommons.org/ns#"',
        '           xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">',
        '    <cc:Work rdf:about="">',
        "      <dc:format>image/svg+xml</dc:format>",
        '      <dc:type rdf:resource="http://purl.org/dc/dcmitype/StillImage"/>',
        f"      <dc:title>{xml_text(meta.title)}</dc:title>",
        f"      <dc:description>{xml_text(meta.description)}</dc:description>",
        f"      <dc:creator><cc:Agent><dc:title>{xml_text(CREATOR)}</dc:title></cc:Agent></dc:creator>",
        f"      <dc:publisher><cc:Agent><dc:title>{PROJECT}</dc:title></cc:Agent></dc:publisher>",
        f"      <dc:rights><cc:Agent><dc:title>{xml_text(meta.copyright)}</dc:title></cc:Agent></dc:rights>",
        f"      <dc:identifier>{xml_text(meta.identifier_url)}</dc:identifier>",
        f"      <dc:language>{meta.language}</dc:language>",
        "      <dc:subject><rdf:Bag>"
        + "".join(f"<rdf:li>{xml_text(k)}</rdf:li>" for k in meta.keywords)
        + "</rdf:Bag></dc:subject>",
        f'      <Iptc4xmpExt:DigitalSourceType rdf:resource="{DST_BASE}{meta.digital_source_type}"/>',
        f'      <cc:license rdf:resource="{xml_text(meta.license_url)}"/>',
        "    </cc:Work>",
        "  </rdf:RDF>",
        "</metadata>",
        f"<!-- {REUSE_END} -->",
    ]
    return "\n".join(lines) + "\n"


_HEAD = re.compile(r"\A.*?(?=<svg\b)", re.S)
_ROOT_TAG = re.compile(r"<svg\b[^>]*>\n?")
_ROOT_A11Y = re.compile(r'\s(?:role|aria-labelledby|lang)="[^"]*"')
_LEAD = re.compile(
    r"\A\s*(?:<title\b[^>]*>[^<]*</title>\s*)?(?:<desc\b[^>]*>[^<]*</desc>\s*)?"
    rf"(?:<!-- {REUSE_START} -->\s*<metadata\b.*?</metadata>\s*<!-- {REUSE_END} -->\s*)?",
    re.S,
)


def stamp_svg(svg: str, meta: MediaMeta) -> str:
    """*svg* (kicad-cli's, or one stamped before) with its record in its head; the drawing untouched.

    The XML declaration and the doctype go, so the SPDX comment comes first;
    KiCad's own ``<title>`` and ``<desc>`` (the export's clock, the tool's
    name) give way to the record's. Stamping a stamped file gives the same bytes.
    """
    body = _HEAD.sub("", svg, count=1)
    match = _ROOT_TAG.match(body)
    if not match:
        raise MetaError("no <svg> root")
    root = _ROOT_A11Y.sub("", match.group(0).rstrip("\n"))
    root = (
        root[:-1].rstrip()
        + f' role="img" aria-labelledby="{meta.slug}-t {meta.slug}-d" lang="{meta.language}">'
    )
    rest = body[match.end() :]
    lead = _LEAD.match(rest)
    if lead is None:
        raise MetaError(f"{meta.title}: the SVG's body does not start as the exporter writes it")
    rest = rest[lead.end() :]
    return f"<!-- {TAG_LICENSE}: {meta.spdx} -->\n{root}\n{svg_block(meta)}{rest}"


# ---------------------------------------------------------------- PNG

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: The ancillary chunks an image may keep beside its text: its transparency.
PNG_KEPT = {b"tRNS"}


def png_text(meta: MediaMeta) -> list[tuple[str, str]]:
    """The text chunks of an image, as (keyword, text), in their order."""
    return [
        ("Title", meta.title),
        ("Author", CREATOR),
        ("Description", meta.description),
        ("Copyright", meta.copyright),
        ("License", meta.usage_terms),
    ]


def png_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """The chunks of a PNG, as (type, data), each CRC checked.

    :raises MetaError: When *raw* is not a whole PNG.
    """
    if not raw.startswith(PNG_SIGNATURE):
        raise MetaError("not a PNG")
    chunks = []
    at = len(PNG_SIGNATURE)
    while at < len(raw):
        if at + 12 > len(raw):
            raise MetaError("a PNG cut short")
        (length,) = struct.unpack(">I", raw[at : at + 4])
        kind = raw[at + 4 : at + 8]
        data = raw[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", raw[at + 8 + length : at + 12 + length])
        if len(data) != length or zlib.crc32(kind + data) != crc:
            raise MetaError(f"a PNG chunk {kind!r} cut short or with a wrong CRC")
        chunks.append((kind, data))
        at += 12 + length
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1][0] != b"IEND":
        raise MetaError("a PNG without its header or its end")
    return chunks


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _itxt(keyword: str, text: str) -> bytes:
    """An uncompressed ``iTXt`` chunk's data: keyword, no language, UTF-8 text."""
    return keyword.encode("latin-1") + b"\0\0\0\0\0" + text.encode("utf-8")


def read_png_text(raw: bytes) -> list[tuple[str, str]]:
    """The text chunks of a PNG (``tEXt``, ``zTXt``, ``iTXt``), as (keyword, text)."""
    out = []
    for kind, data in png_chunks(raw):
        if kind == b"tEXt":
            keyword, _, text = data.partition(b"\0")
            out.append((keyword.decode("latin-1"), text.decode("latin-1")))
        elif kind == b"zTXt":
            keyword, _, rest = data.partition(b"\0")
            out.append((keyword.decode("latin-1"), zlib.decompress(rest[1:]).decode("latin-1")))
        elif kind == b"iTXt":
            keyword, _, rest = data.partition(b"\0")
            compressed, rest = rest[0], rest[2:]
            _language, _, rest = rest.partition(b"\0")
            _translated, _, text = rest.partition(b"\0")
            body = zlib.decompress(text) if compressed else text
            out.append((keyword.decode("latin-1"), body.decode("utf-8")))
    return out


def stamp_png(raw: bytes, meta: MediaMeta) -> bytes:
    """*raw* with its record's text right after its header and no other ancillary chunk.

    The image's own chunks (its header, palette, data, end) and its
    transparency stay as they are; any text, time, EXIF or color note goes.
    Stamping a stamped file gives the same bytes.
    """
    chunks = png_chunks(raw)
    kept = [(k, d) for k, d in chunks[1:] if k[:1].isupper() or k in PNG_KEPT]
    text = [(b"iTXt", _itxt(keyword, value)) for keyword, value in png_text(meta)]
    return PNG_SIGNATURE + b"".join(_chunk(k, d) for k, d in [chunks[0], *text, *kept])


def png_extra_chunks(raw: bytes) -> list[str]:
    """The ancillary chunks of a PNG beyond its text and its transparency."""
    return sorted(
        {
            kind.decode("latin-1")
            for kind, _ in png_chunks(raw)
            if not kind[:1].isupper() and kind not in PNG_KEPT | {b"iTXt"}
        }
    )


# ---------------------------------------------------------------- privacy

DENY = (
    ("an e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    (
        "a local path",
        re.compile(
            r"/Users/|/home/|/private/|/var/folders/|/tmp/|/Volumes/|file://|~/|[A-Za-z]:\\"
        ),
    ),
    (
        "a host or network address",
        re.compile(r"\.local\b|\.lan\b|\blocalhost\b|\b192\.168\.\d+\.\d+\b"),
    ),
    ("a machine date and time", re.compile(r"\d{4}[-:]\d{2}[-:]\d{2}[ T]\d{2}:\d{2}")),
    (
        "a tool or version string",
        re.compile(r"Eeschema-SVG|Eeschema-PDF|\bInkscape\b|Image::ExifTool"),
    ),
)


def deny_hits(value: str, allow_dates: bool = False) -> list[str]:
    """The classes of private data *value* holds."""
    return [
        name
        for name, pattern in DENY
        if not (allow_dates and name == "a machine date and time") and pattern.search(value)
    ]
