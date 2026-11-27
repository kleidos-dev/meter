# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""How serve names a file of this computer to the page and the API: never by an absolute path.

The page is a picture of the bench that gets copied into documents and
screenshots, so a path it shows must not carry the layout of the computer
that served it (a home directory, a user name, a temporary directory).
:func:`shown_path` gives a path relative to the working directory when it
is inside it, relative to the repository when it is inside that, and
otherwise its last two components, never with a leading ``/``. A base that
holds the home directory below it (``/``, ``/Users``) is not used, since a
path relative to it would spell the home directory out; and a path under the
home directory is cut below it first. :func:`shown_error` says what an
``OSError`` says, with the file it names shown the same way.

What serve prints to its own terminal (``recordings in ...``) is the person's
own console and keeps the whole path.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from ..registry import ROOT as REGISTRY_ROOT

__all__ = ["PROJECT", "shown_error", "shown_path"]

#: The repository this package runs from: the parent of its ``calibration/``.
PROJECT = REGISTRY_ROOT.parent

#: How many components a path outside every base keeps.
TAIL = 2


def shown_path(
    path: Path | str,
    bases: Sequence[Path] | None = None,
    home: Path | None = None,
) -> str:
    """*path* as the page and the API show it, with ``/`` between components.

    :param path: Absolute or relative to the working directory.
    :param bases: The directories a path is shown relative to, in order;
        None for the working directory, then :data:`PROJECT`.
    :param home: The home directory; None for the user's.
    :returns: The path relative to the first base that holds it (``.`` for
        the base itself), else its last :data:`TAIL` components below the
        home directory or the root; ``.`` for the root.
    """
    resolved = Path(path).resolve()
    home = (Path.home() if home is None else home).resolve()
    shown_from = (Path.cwd(), PROJECT) if bases is None else bases
    for base in (each.resolve() for each in shown_from):
        if base in home.parents:
            continue  # relative to it, the path would name the home directory
        if resolved == base or base in resolved.parents:
            return resolved.relative_to(base).as_posix()
    parts = resolved.parts[1:]  # without the anchor: no leading separator
    inside = home.parts[1:]
    if parts[: len(inside)] == inside:
        parts = parts[len(inside) :]
    return "/".join(parts[-TAIL:]) or "."


def shown_error(error: OSError, bases: Sequence[Path] | None = None) -> str:
    """What *error* says, its file shown by :func:`shown_path`.

    :param error: The error; ``str(error)`` would quote its file's absolute path.
    :param bases: As :func:`shown_path`.
    :returns: Its message alone when it names no file, else its reason (its
        ``strerror``, or its type's name) and the file shown.
    """
    if error.filename is None:
        return str(error)
    reason = error.strerror or type(error).__name__
    return f"{reason}: {shown_path(os.fsdecode(error.filename), bases)}"
