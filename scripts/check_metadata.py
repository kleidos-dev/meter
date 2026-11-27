#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hold every media file of the repository to its metadata record (``scripts/media_meta.py``).

    python3 scripts/check_metadata.py     # exit 1 on any finding

- ``hardware/rig.pdf``: its Info dictionary is the record's, in full; its XMP
  stream holds the record's title, description, rights, identifier, license
  and usage terms; ``/Lang`` and ``/DisplayDocTitle`` are set; no script;
- ``hardware/rig.svg``: stamping it again with its record gives its bytes
  (the SPDX comment, the root's accessible name, the title, desc and metadata
  block), and KiCad's own title and desc are gone;
- the panel's images (``docs/images/panel/*.png``): their text chunks are
  the record's, in its order, and they hold no other text, time, EXIF or
  color chunk;
- no value holds an e-mail address, a local path, a host, a machine's time or
  a tool's name; every tracked media file has a record.

Needs pypdf for the PDF.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_meta

ROOT = media_meta.ROOT
MEDIA_SUFFIXES = {".pdf", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm"}
#: The media files that carry no record of this repository, and why.
EXEMPT: tuple[tuple[str, str], ...] = ()


def pdf_findings(path: str) -> list[str]:
    from pypdf import PdfReader

    meta = media_meta.record(path)
    reader = PdfReader(ROOT / path)
    found = []
    info = {key: str(value) for key, value in (reader.metadata or {}).items()}
    if info != media_meta.pdf_info(meta):
        found.append(f"{path}: its Info dictionary is not the record's")
    root: Any = reader.trailer["/Root"]  # a PdfObject; pypdf types it no closer
    xmp = root.get("/Metadata")
    packet = xmp.get_object().get_data().decode("utf-8") if xmp is not None else ""
    found.extend(
        f"{path}: its XMP does not hold {value!r}"
        for value in (meta.title, meta.copyright, meta.identifier_url, meta.license_url)
        if media_meta.xml_text(value) not in packet
    )
    if media_meta.xml_text(meta.usage_terms) not in packet:
        found.append(f"{path}: its XMP does not hold the usage terms")
    if str(root.get("/Lang")) != meta.language or "/ViewerPreferences" not in root:
        found.append(f"{path}: no /Lang or no /DisplayDocTitle")
    if "/JavaScript" in (root.get("/Names") or {}) or "/OpenAction" in root:
        found.append(f"{path}: holds a script")
    for value in [*info.values(), packet]:
        found.extend(
            f"{path}: a metadata value holds {what}"
            for what in media_meta.deny_hits(value, allow_dates=True)
        )
    return found


def svg_findings(path: str) -> list[str]:
    meta = media_meta.record(path)
    text = (ROOT / path).read_text(encoding="utf-8")
    found = []
    if media_meta.stamp_svg(text, meta) != text:
        found.append(f"{path}: its head is not the record's; run hardware/export_rig.py")
    head = text[: text.find("<g ")] if "<g " in text else text
    found.extend(f"{path}: its head holds {what}" for what in media_meta.deny_hits(head))
    return found


def png_findings(path: str) -> list[str]:
    meta = media_meta.record(path)
    raw = (ROOT / path).read_bytes()
    try:
        text = media_meta.read_png_text(raw)
        extra = media_meta.png_extra_chunks(raw)
    except media_meta.MetaError as error:
        return [f"{path}: {error}"]
    found = []
    if text != media_meta.png_text(meta):
        found.append(f"{path}: its text chunks are not the record's; retake it")
    found.extend(f"{path}: holds a {kind} chunk" for kind in extra)
    for _, value in text:
        found.extend(f"{path}: a text chunk holds {what}" for what in media_meta.deny_hits(value))
    return found


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(line for line in out.splitlines() if (ROOT / line).is_file())


def findings() -> list[str]:
    found = []
    for path in tracked():
        if Path(path).suffix.lower() not in MEDIA_SUFFIXES or path.startswith(
            tuple(p for p, _ in EXEMPT)
        ):
            continue
        if path not in media_meta.MEDIA:
            found.append(f"{path}: a media file with no metadata record")
        elif path.endswith(".pdf"):
            found += pdf_findings(path)
        elif path.endswith(".png"):
            found += png_findings(path)
        else:
            found += svg_findings(path)
    return found


def main() -> int:
    found = findings()
    for line in found:
        print(line)
    print(f"{len(media_meta.MEDIA)} media files checked, {len(found)} finding(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
