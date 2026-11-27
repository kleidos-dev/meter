#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""preview_reference.py — look at a built documentation site as its host would serve it.

``scripts/build_reference.py`` writes the pages and nothing of the host's:
they link ``../assets/tokens.css``, ``../assets/api.css``, the host's faces and
its mark, and load its search (``../assets/search.js``), which
``https://api.kleidos.dev/`` serves beside ``meter/``. This script puts a
built tree under ``meter/`` beside those files and serves the whole on this
computer (``127.0.0.1`` only) the way the host does: a page at
its address without ``.html`` (``/meter/usage_page``), the site's index at
``/meter/``, an old ``.html`` address sent to the clean one with a 301, and
anything missing a 404.

The host's files are fetched once from :data:`ASSETS_ORIGIN`, the sheets, the
search and the mark first and then every face the sheets name, into
``~/.cache/kleidos-meter/api-assets`` (``KLEIDOS_METER_CACHE`` moves it;
``--refresh`` fetches them again), and are never written into this
repository: the design is the host's, not a copy here. ``--assets <dir>``
takes them from a folder instead (the Kleidos firmware repository's
``site/api/assets/`` with the ``tokens.css`` its staging cuts).

Usage:
    python3 scripts/preview_reference.py --tree <built tree> [--port 8798]
    python3 scripts/preview_reference.py --tree <built tree> --stage <dir>   # write it, and exit

Exit code: 0 once stopped (Ctrl-C) or staged, 1 when a file of the host could
not be had, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from functools import partial
from pathlib import Path

#: Where the host's own files are fetched from.
ASSETS_ORIGIN = "https://api.kleidos.dev/"

#: The host's files a page of the site links, from the host's root.
HOST_FILES = (
    "assets/tokens.css",
    "assets/api.css",
    "assets/search.js",
    "assets/logo-mark.svg",
    "favicon.ico",
)

#: A file a sheet names (a face).
_URL_RE = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)")

#: The directory the site is served under.
REFERENCE = "meter"


def cache_dir() -> Path:
    """Return where the host's files are kept between runs."""
    base = os.environ.get("KLEIDOS_METER_CACHE")
    return (Path(base) if base else Path.home() / ".cache" / "kleidos-meter") / "api-assets"


def _fetch(path: str, into: Path) -> None:
    """Fetch the host's file *path* into *into*, at the same path."""
    target = into / path
    target.parent.mkdir(parents=True, exist_ok=True)
    # S310: the address is the fixed https origin above and a path of the host's.
    url = ASSETS_ORIGIN + path
    request = urllib.request.Request(url, headers={"User-Agent": "kleidos-meter-preview"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
        target.write_bytes(answer.read())


def host_files(into: Path, refresh: bool = False) -> list[str]:
    """Make sure *into* holds the host's files; return the paths it holds.

    :param into: The folder, laid out as the host's root.
    :param refresh: Fetch every file again.
    """
    wanted = list(HOST_FILES)
    held: list[str] = []
    while wanted:
        path = wanted.pop(0)
        if path in held:
            continue
        if refresh or not (into / path).is_file():
            _fetch(path, into)
        held.append(path)
        if path.endswith(".css"):
            folder = path.rsplit("/", 1)[0]
            names = _URL_RE.findall((into / path).read_text(encoding="utf-8"))
            wanted += [f"{folder}/{n}" for n in names if not re.match(r"^(?:[a-z]+:|/|data:)", n)]
    return held


def stage(tree: Path, assets: Path, out: Path) -> None:
    """Write the host's tree into *out*: the host's files and the site under ``meter/``."""
    for path in sorted(assets.rglob("*")):
        if path.is_file():
            target = out / path.relative_to(assets)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    shutil.copytree(tree, out / REFERENCE)


class CleanAddresses(http.server.SimpleHTTPRequestHandler):
    """Serve a staged tree as the host does: pages at their addresses without ``.html``."""

    def send_head(self):  # type: ignore[no-untyped-def]  # (the base class's signature)
        """Answer an old address with a 301 and a clean one with its page."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.endswith(".html"):
            clean = (
                path.removesuffix("index.html")
                if path.endswith("/index.html")
                else path.removesuffix(".html")
            )
            self.send_response(301)
            self.send_header("Location", clean)
            self.end_headers()
            return None
        local = Path(self.translate_path(path))
        if not local.exists() and local.with_suffix(".html").is_file():
            self.path = path + ".html"
        elif path in (f"/{REFERENCE}", ""):
            self.send_response(301)
            self.send_header("Location", f"/{REFERENCE}/")
            self.end_headers()
            return None
        return super().send_head()

    def log_message(self, format: str, *args: object) -> None:
        """Say each request on one line, without the client's address."""
        sys.stderr.write(f"preview: {format % args}\n")


def serve(root: Path, port: int) -> None:
    """Serve *root* on ``127.0.0.1:port`` until stopped."""
    handler = partial(CleanAddresses, directory=str(root))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"preview: http://127.0.0.1:{port}/{REFERENCE}/ (Ctrl-C stops it)", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, stage, and serve or exit."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--tree", type=Path, required=True, help="a tree build_reference.py wrote")
    parser.add_argument("--assets", type=Path, help="the host's files, laid out as its root")
    parser.add_argument("--refresh", action="store_true", help="fetch the host's files again")
    parser.add_argument("--port", type=int, default=8798, help="the port (default 8798)")
    parser.add_argument(
        "--stage", type=Path, help="write the host's tree into this new folder and exit"
    )
    args = parser.parse_args(argv)
    if not (args.tree / "index.html").is_file():
        parser.error(f"{args.tree} holds no built site (no index.html)")
    assets = args.assets or cache_dir()
    if args.assets is None:
        try:
            host_files(assets, args.refresh)
        except OSError as error:
            print(f"preview: could not fetch the host's files: {error}", file=sys.stderr)
            return 1
    if args.stage is not None:
        if args.stage.exists() and any(args.stage.iterdir()):
            parser.error(f"{args.stage} is not empty")
        stage(args.tree, assets, args.stage)
        print(f"preview: staged in {args.stage}")
        return 0
    with tempfile.TemporaryDirectory(prefix="kleidos-meter-preview-") as tmp:
        root = Path(tmp) / "site"
        stage(args.tree, assets, root)
        serve(root, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
