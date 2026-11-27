# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's page: the files under ``static/`` that serve hands to a browser.

The page is one HTML document, one stylesheet and plain ES modules (its
tab icon is a ``data:`` SVG in the document, so it needs no file),
shipped as package data and served as they are: there is no build step, no
third-party script and nothing fetched from another origin. :class:`Page`
reads every file once, when serve starts, into a table keyed by the path it
is served at, so a request can only ever name a file of that table (a path
that walks out of ``static/`` is simply not in it) and the server's memory
does not grow with the requests.

The paths: ``/`` is ``index.html``, and ``/static/<name>`` is every file
under ``static/``, its subdirectories included. Each is served with the
content type its suffix names (:data:`CONTENT_TYPES`; a file of any other
suffix is refused when the table is built) and, for the HTML document, the
page's own Content-Security-Policy (:data:`PAGE_POLICY`): scripts, styles,
images and connections from the panel's own origin only (and ``data:``
images, the tab icon), no inline script or style, no form submission, no
framing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

__all__ = [
    "CONTENT_TYPES",
    "INDEX",
    "PAGE_POLICY",
    "PREFIX",
    "Asset",
    "Page",
    "packaged_page",
]

#: The path the page's document is served at.
INDEX = "/"

#: The path every other file of the page is served below.
PREFIX = "/static/"

#: The content type of each suffix the page may hold.
CONTENT_TYPES: Mapping[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

#: The page's Content-Security-Policy: its own origin for everything it loads
#: or connects to (and a ``data:`` image, its tab icon), nothing inline, nothing framed.
PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;"
    " connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@dataclass(frozen=True)
class Asset:
    """One file of the page, as it is served.

    :ivar name: Its path below ``static/`` (``js/main.js``).
    :ivar content_type: Its ``Content-Type``.
    :ivar data: Its bytes.
    """

    name: str
    content_type: str
    data: bytes

    @property
    def policy(self) -> str | None:
        """The Content-Security-Policy it is served with, None for the API's own."""
        return PAGE_POLICY if self.name.endswith(".html") else None


class Page:
    """The page's files by the path each is served at.

    :param files: Each file's path below ``static/`` and its bytes; one of
        them must be ``index.html``.
    :raises ValueError: For a file whose suffix has no content type, or
        without ``index.html``.
    """

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._assets: dict[str, Asset] = {}
        for name, data in files.items():
            suffix = name[name.rfind(".") :] if "." in name else ""
            content_type = CONTENT_TYPES.get(suffix)
            if content_type is None:
                raise ValueError(f"static/{name}: no content type for {suffix or 'no suffix'}")
            self._assets[PREFIX + name] = Asset(name, content_type, data)
        if PREFIX + "index.html" not in self._assets:
            raise ValueError("the page has no static/index.html")
        self._assets[INDEX] = self._assets[PREFIX + "index.html"]

    @staticmethod
    def owns(path: str) -> bool:
        """Whether *path* is the page's to answer (a file of it or not), not the API's."""
        return path == INDEX or path.startswith(PREFIX)

    def lookup(self, path: str) -> Asset | None:
        """The file served at *path*, None when the page has none there."""
        return self._assets.get(path)

    @property
    def paths(self) -> list[str]:
        """Every path a file is served at, ``/`` included, sorted."""
        return sorted(self._assets)


def _walk(directory: Traversable, prefix: str = "") -> Iterator[tuple[str, bytes]]:
    """Every file below *directory*, as its path below it and its bytes."""
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_dir():
            yield from _walk(entry, f"{prefix}{entry.name}/")
        else:
            yield prefix + entry.name, entry.read_bytes()


def packaged_page() -> Page:
    """The page as the package ships it, read from its ``static/`` directory.

    :raises ValueError: As :class:`Page`, for a package that ships a file it cannot serve.
    """
    return Page(dict(_walk(resources.files(__package__) / "static")))
