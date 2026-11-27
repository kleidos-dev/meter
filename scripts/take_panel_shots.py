#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Take the panel's images in ``docs/images/panel/`` from ``kleidos-meter serve --sim``.

    host/.venv/bin/python scripts/take_panel_shots.py            # every image, and the manifest
    host/.venv/bin/python scripts/take_panel_shots.py live-dark-1280.png   # these only

Runs the scenario of ``host/tests/panel_shots.py`` (a seeded simulator, a
registry made in a temporary directory, one recorded run) in headless
Chromium and, for every image ``scripts/panel_images.py`` lists, shows its
view, refuses it when any text of the page holds an absolute path, signs what
the view rendered, saves the whole page as a PNG, makes it smaller without
loss when ``oxipng`` is installed, stamps its metadata record
(``scripts/media_meta.py``) and refuses it over the 512 kB the repository's
hook allows. ``manifest.json`` is written last, from the images taken and,
with names given, the entries of the others as they were. Run twice, it
writes the same manifest.

Needs the host package's ``[browser]`` extra and its Chromium
(``host/tests/test_panel_browser.py`` says how). Exit status 0 when every
image was saved, 1 when one was refused (nothing is written then), 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_meta
import panel_images

#: The most an image may weigh: the large-file hook's limit (``.pre-commit-config.yaml``).
MAX_BYTES = 512 * 1024


class Refused(Exception):
    """An image that is not saved, and why."""


def smaller(raw: bytes) -> bytes:
    """*raw* recompressed without loss by ``oxipng`` when it is installed, else as it is."""
    tool = shutil.which("oxipng")
    if tool is None:
        return raw
    done = subprocess.run(
        [tool, "--opt", "4", "--strip", "all", "--stdout", "-"],
        input=raw,
        capture_output=True,
        check=True,
    )
    return done.stdout


def finish(image: Any, raw: bytes) -> bytes:
    """The PNG of *image* as it is saved: smaller, stamped and within the hook's limit.

    :raises Refused: When it is over :data:`MAX_BYTES`.
    """
    stamped = media_meta.stamp_png(smaller(raw), media_meta.record(image.path))
    if len(stamped) > MAX_BYTES:
        raise Refused(f"{image.path}: {len(stamped)} bytes, over the hook's {MAX_BYTES}")
    return stamped


def take(
    names: set[str] | None, browser: Any = None
) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """Run the scenario; every image's bytes and manifest entry, by name.

    :param names: Only these images, every one when None.
    :param browser: A Playwright browser to use; one of its own when None.
    :raises Refused: At the first image refused.
    """
    tests = str(panel_images.ROOT / "host" / "tests")
    if tests not in sys.path:
        sys.path.insert(0, tests)
    shots = importlib.import_module("panel_shots")
    pngs: dict[str, bytes] = {}
    entries: dict[str, dict[str, Any]] = {}

    def keep(image: Any, page: Any) -> None:
        try:
            panel_images.refuse_paths(image, shots.texts(page))
        except panel_images.PathInView as refusal:
            raise Refused(str(refusal)) from None
        entries[image.name] = panel_images.entry(image, shots.signed(page))
        pngs[image.name] = finish(image, page.screenshot(full_page=True))

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(base / "state")  # nothing recorded in the user's
        try:
            with shots.scene(base / "scene") as served:
                if browser is not None:
                    shots.run(browser, served, keep, names=names)
                else:
                    _with_own_browser(shots, served, keep, names)
        finally:
            if state is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = state
    return pngs, entries


def _with_own_browser(shots: Any, served: Any, keep: Any, names: set[str] | None) -> None:
    from playwright import sync_api

    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            shots.run(browser, served, keep, names=names)
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*", help="only these images (file names)")
    args = parser.parse_args(argv)
    unknown = sorted(set(args.names) - set(panel_images.BY_NAME))
    if unknown:
        parser.error(f"no such image: {', '.join(unknown)}")
    names = set(args.names) or None
    try:
        pngs, entries = take(names)
    except Refused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    folder = panel_images.ROOT / panel_images.FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    try:
        kept: dict[str, dict[str, Any]] = panel_images.load()["images"]
    except FileNotFoundError:
        kept = {}
    # The bench's images are not taken here, and with names the others are kept as they were.
    entries = {
        **{
            name: item
            for name, item in kept.items()
            if item.get("kind") != "sim" or (names is not None and name in panel_images.BY_NAME)
        },
        **entries,
    }
    for name, raw in pngs.items():
        (folder / name).write_bytes(raw)
    manifest = panel_images.manifest(entries)
    (panel_images.ROOT / panel_images.MANIFEST).write_text(
        panel_images.dumps(manifest), encoding="utf-8"
    )
    print(
        f"{len(pngs)} image(s) taken, {len(manifest['images'])} listed in {panel_images.MANIFEST}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
