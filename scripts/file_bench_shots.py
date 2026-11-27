#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""File the panel's bench screenshots under ``docs/images/panel/bench/``.

    python3 scripts/file_bench_shots.py <folder>

*folder* is what ``host/tests/test_panel_bench.py --run-bench --bench-shots``
leaves: the PNGs of the panel over the cable to the meter and
``bench-shots.json``, their record (the date, the firmware, the image's
``elf=`` and ELF SHA-256, the commit it was built from, the instrument, the
rig, what took them, who checked them by eye for a port name, a USB serial, a
MAC or a path, and each PNG's view and SHA-256). Each PNG is held to the
SHA-256 it was recorded with and to its name,
``bench-<view or state>-<date>-elf-<elf>.png``, stamped with its metadata
record (``scripts/media_meta.py``) and refused over the 512 kB the
repository's hook allows; then the images are written and listed in
``manifest.json`` as ``kind`` ``bench``, beside the simulator's, which stay as
they were. Filed twice, the same bytes and the same manifest.

The pixels are not read: checking them by eye is the person's, and a record
that does not say it was done is refused. Exit status 0 when every image was
filed, 1 when one was refused (nothing is written then), 2 on a usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_meta
import panel_images

#: The large-file hook's limit, as ``scripts/take_panel_shots.py`` holds it.
MAX_BYTES = 512 * 1024
#: The record's keys that every entry copies.
COPIED = ("date", "fw", "elf", "instrument", "rig", "source_commit", "taken_by")


class Refused(Exception):
    """A shot, or the record, the filer will not file."""


def sha256(data: bytes) -> str:
    """*data*'s SHA-256 in hex, as ``bench-shots.json`` records it."""
    return hashlib.sha256(data).hexdigest()


def _view_and_state(shows: str) -> tuple[str, str | None]:
    """The view and the state of what a shot's record says it shows."""
    for state, view, _ in panel_images.STATES:
        if shows == state:
            return view, state
    return shows, None


def entries(folder: Path) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """The stamped PNGs and the manifest entries of the shots in *folder*, by manifest key."""
    try:
        record: dict[str, Any] = json.loads((folder / "bench-shots.json").read_text("utf-8"))
    except FileNotFoundError as missing:
        raise Refused(f"{folder}: no bench-shots.json") from missing
    if record.get("kind") != "bench":
        raise Refused("bench-shots.json: not a record of bench images")
    if not str(record.get("checked_by_eye", "")).strip():
        raise Refused("bench-shots.json: nobody checked these images by eye")
    pngs: dict[str, bytes] = {}
    out: dict[str, dict[str, Any]] = {}
    for file, shot in sorted(record["images"].items()):
        view, state = _view_and_state(str(shot["view"]))
        if view not in panel_images.VIEWS:
            raise Refused(f"{file}: {view} is not a view of the page")
        key = panel_images.bench_name(state or view, record["date"], record["elf"])
        if f"{panel_images.BENCH}/{file}" != key:
            raise Refused(f"{file}: its name is not {key.removeprefix('bench/')}")
        try:
            raw = (folder / file).read_bytes()
        except FileNotFoundError as missing:
            raise Refused(f"{file}: not in the folder") from missing
        if "sha256" not in shot:
            raise Refused(f"{file}: no SHA-256 recorded")
        if sha256(raw) != shot["sha256"]:
            raise Refused(f"{file}: changed since it was taken (its SHA-256 is not the record's)")
        item: dict[str, Any] = {
            "kind": "bench",
            "view": view,
            "state": state,
            "scheme": "light",
            "width": 1280,
            **{name: record[name] for name in COPIED},
            "elf_sha256": f"sha256:{record['elf_sha256']}",
            "checked": record["checked_by_eye"],
        }
        path = f"{panel_images.FOLDER}/{key}"
        stamped = media_meta.stamp_png(
            raw,
            media_meta.bench_record(path, item, *media_meta.reuse_truth(path)),
        )
        if len(stamped) > MAX_BYTES:
            raise Refused(f"{path}: {len(stamped)} bytes, over the hook's {MAX_BYTES}")
        item["sha256"] = panel_images.sha256_tag(stamped)
        pngs[key] = stamped
        out[key] = dict(sorted(item.items()))
    return pngs, out


def main(argv: list[str] | None = None) -> int:
    """File the shots of the folder given; see the module's text."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("folder", type=Path, help="what the panel's bench test left")
    args = parser.parse_args(argv)
    try:
        pngs, filed = entries(args.folder)
        listed = panel_images.load()["images"]
    except (Refused, media_meta.MetaError, FileNotFoundError) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    root = panel_images.ROOT
    (root / panel_images.FOLDER / panel_images.BENCH).mkdir(parents=True, exist_ok=True)
    for key, raw in pngs.items():
        (root / panel_images.FOLDER / key).write_bytes(raw)
    manifest = panel_images.manifest({**listed, **filed})
    (root / panel_images.MANIFEST).write_text(panel_images.dumps(manifest), encoding="utf-8")
    print(f"{len(pngs)} bench image(s) filed, {len(manifest['images'])} listed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
