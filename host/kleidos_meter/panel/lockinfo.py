# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Who holds a port: a note serve leaves in the user's runtime directory.

The port's exclusive lock (``SerialTransport``'s ``flock``) is what keeps a
second opener out; this note only lets the refused one say who holds it.
``kleidos-meter serve`` writes one per port it holds, a JSON file named by a
digest of the port's path (the path of a USB serial port carries the
adapter's serial number, which stays out of file names), readable by the
user alone, and removes it on exit. A note whose process is gone is stale and
is removed by the next reader.

The directory is ``$XDG_RUNTIME_DIR/kleidos-meter``, or ``kleidos-meter-<uid>``
in the system's temporary directory where there is no runtime directory
(macOS gives each user a temporary directory of their own). One that is a
symbolic link or belongs to another user is refused.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path

__all__ = ["describe", "info_path", "read_holder", "remove_info", "runtime_dir", "write_info"]


def runtime_dir() -> Path:
    """The directory the notes live in, created private to the user.

    :returns: The directory.
    :raises OSError: When it is a symbolic link, or another user's.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    uid = os.getuid() if hasattr(os, "getuid") else None
    if base:
        path = Path(base) / "kleidos-meter"
    else:
        path = Path(tempfile.gettempdir()) / f"kleidos-meter-{uid if uid is not None else 'user'}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"{path} is a symbolic link: refusing it as the runtime directory")
    if uid is not None and path.stat().st_uid != uid:
        raise OSError(f"{path} belongs to another user: refusing it as the runtime directory")
    return path


def _key(port: str) -> str:
    """The digest that names *port*'s note: its resolved path, or the URL as given."""
    name = port if "://" in port else os.path.realpath(port)
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def info_path(port: str) -> Path:
    """The note's path for *port*."""
    return runtime_dir() / f"serve-{_key(port)}.json"


def write_info(port: str, info: dict[str, object]) -> Path:
    """Write the note for *port*, readable by the user alone; *info* gains the pid.

    :param port: The device node or pyserial URL held.
    :param info: What to tell a refused opener (where the panel is, since when).
    :returns: The note's path.
    """
    path = info_path(port)
    record = {
        "holder": "kleidos-meter serve",
        "pid": os.getpid(),
        "since": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        **info,
    }
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    return path


def _alive(pid: int) -> bool:
    """Whether a process with *pid* exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, under another user
    return True


def read_holder(port: str) -> dict[str, object] | None:
    """The note of the process that holds *port*, None when there is none or it is stale.

    :param port: The device node or pyserial URL.
    :returns: The note's fields.
    """
    try:
        path = info_path(port)
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = record.get("pid") if isinstance(record, dict) else None
    if not isinstance(pid, int) or not _alive(pid):
        path.unlink(missing_ok=True)
        return None
    note: dict[str, object] = record
    return note


def remove_info(port: str) -> None:
    """Remove *port*'s note if this process wrote it."""
    try:
        path = info_path(port)
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(record, dict) and record.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def describe(record: dict[str, object]) -> str:
    """A holder's note as one phrase: who, which process, since when, its panel and passthrough."""
    parts = [f"pid {record.get('pid')}"]
    if record.get("since"):
        parts.append(f"since {record['since']}")
    if record.get("panel"):
        parts.append(f"panel {record['panel']}")
    if record.get("passthrough"):
        parts.append(f"passthrough {record['passthrough']}")
    return f"{record.get('holder', 'another process')} ({', '.join(parts)})"
