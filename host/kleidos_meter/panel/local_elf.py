# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The local build's ELF hash, which the panel holds the instrument's ``elf=`` to.

``SYSTem:INFormation?`` and ``SYSTem:HEALth?`` name the running image by
``elf=``: the first 8 bytes of its ELF file's SHA-256 as 16 lower-case hex
digits, the digest esptool writes into the image's description
(``docs/scpi.md``). :class:`LocalElf` takes the same digest of a build's
``firmware.elf`` on the host (``shasum -a 256``, cut to 16 digits), so the
Health view can say whether the instrument runs that build. The file is
hashed again only when its size or its modification time changed, so a
rebuild is seen at the next look and a page that looks often costs one
``stat``. The build is named as :func:`~kleidos_meter.panel.paths.shown_path`
shows it, never by its absolute path.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from .paths import shown_path

__all__ = ["DIGITS", "LocalElf", "elf_digest"]

#: How many hex digits of the SHA-256 the instrument reports.
DIGITS = 16

#: Bytes read at a time while hashing.
_CHUNK = 1 << 20


def elf_digest(path: Path) -> str:
    """*path*'s ``elf=``: the first :data:`DIGITS` hex digits of its SHA-256.

    :raises OSError: When the file cannot be read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()[:DIGITS]


class LocalElf:
    """One build's ``firmware.elf`` and its digest, kept until the file changes.

    :param path: The ELF file; None when serve was given none.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._digest: str | None = None

    def digest(self) -> tuple[str | None, str]:
        """The file's ``elf=``, and what to say when there is none.

        :returns: The digest (None when there is no file or it cannot be
            read) and a line for a person: where the build is, or why it
            has no digest.
        """
        if self.path is None:
            return None, "serve was given no build to compare with (--elf)"
        shown = shown_path(self.path)
        try:
            stat = self.path.stat()
        except OSError:
            return None, f"no local build at {shown}"
        seen = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if seen != self._seen:
                try:
                    self._digest = elf_digest(self.path)
                except OSError as error:
                    self._seen, self._digest = None, None
                    return None, f"{shown} could not be read: {error.strerror or error}"
                self._seen = seen
            return self._digest, f"the build at {shown}"

    def compare(self, instrument: object) -> dict[str, Any]:
        """The local build against the instrument's ``elf=``.

        :param instrument: ``elf=`` as the instrument reported it (a string,
            or None for ``-``: an image without a description).
        :returns: ``path`` (the build as
            :func:`~kleidos_meter.panel.paths.shown_path` shows it, None
            without one), ``elf`` (the local digest or None), ``instrument``,
            ``matches`` (None when either side has no digest) and ``detail``.
        """
        local, detail = self.digest()
        theirs = instrument if isinstance(instrument, str) and instrument else None
        matches = None if local is None or theirs is None else local == theirs
        if matches is not None:
            verb = "runs" if matches else "runs another image than"
            detail = f"the instrument {verb} {detail}"
        return {
            "path": None if self.path is None else shown_path(self.path),
            "elf": local,
            "instrument": theirs,
            "matches": matches,
            "detail": detail,
        }
