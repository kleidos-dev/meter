# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which instrument this is: the serial ``*IDN?`` reports, derived as the firmware derives it.

The serial (firmware 0.4.0 and later) is ``KM-`` and ten hex digits, the first
five bytes of ``SHA-256("kleidos-meter-serial-v1" || MAC)`` over the
ESP32-S3's factory eFuse MAC (``firmware/src/identity.h``): a name that
survives a full flash erase, and a pseudonym rather than the MAC, which never
enters a tracked file.

Because a serial is a digest a search can undo, no real one is tracked
(``calibration/README.md``, "Identity"): the registry files the bench
instrument under a placeholder of the serial's shape. The instrument still
reports its own serial, so a host maps it to the one the registry files it
under through an **alias** it reads only from what git ignores: the
environment variable :data:`ALIAS_VARIABLE`, or else that key in the
repository's ``.env`` (:func:`load_aliases`). Its value is pairs
``<reported>=<filed>``, separated by commas or white space.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path

#: The domain string the firmware hashes ahead of the MAC (``identity.h``).
SERIAL_DOMAIN = b"kleidos-meter-serial-v1"
SERIAL_PATTERN = re.compile(r"^KM-[0-9A-F]{10}$")

#: The environment variable, and the key of the ``.env`` git ignores, that
#: maps a serial an instrument reports to the one the registry files it under.
ALIAS_VARIABLE = "KLEIDOS_METER_SERIAL_ALIASES"

#: The repository's ``.env``, which ``.gitignore`` keeps out of every commit.
DOTENV = Path(__file__).resolve().parents[2] / ".env"

#: One ``KEY=VALUE`` line of an ``.env``, ``export`` allowed before the key.
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def serial_from_mac(mac: bytes) -> str:
    """The instrument's serial, as the firmware derives it from its factory MAC.

    :param mac: The six bytes of the ESP32-S3's eFuse MAC.
    :returns: ``KM-`` and ten hex digits.
    :raises ValueError: When *mac* is not six bytes.
    """
    if len(mac) != 6:
        raise ValueError("a MAC is six bytes")
    digest = hashlib.sha256(SERIAL_DOMAIN + mac).digest()
    return "KM-" + digest[:5].hex().upper()


def parse_idn(text: str) -> dict[str, str]:
    """``*IDN?``'s four fields.

    :param text: The answer.
    :returns: ``manufacturer``, ``model``, ``serial`` and ``firmware``, each empty
        when the answer has no such field.
    """
    parts = [part.strip() for part in text.strip().split(",")]
    while len(parts) < 4:
        parts.append("")
    return {
        "manufacturer": parts[0],
        "model": parts[1],
        "serial": parts[2],
        "firmware": parts[3],
    }


def version_tuple(text: str) -> tuple[int, ...]:
    """A version as numbers, to compare with another.

    :param text: ``"0.4.0"``.
    :returns: ``(0, 4, 0)``; ``()`` for anything unreadable.
    """
    try:
        return tuple(int(part) for part in text.strip().split("."))
    except ValueError:
        return ()


def parse_aliases(text: str) -> dict[str, str]:
    """The alias map written in *text*: pairs ``<reported>=<filed>``.

    The pairs are separated by commas or white space, and each side is a
    serial of either case, kept upper case as the firmware reports it. No
    message names a serial: an alias's whole point is that its left side is
    private, so a mistake is named by the pair's place.

    :param text: The value of :data:`ALIAS_VARIABLE`.
    :returns: Each reported serial and the serial the registry files it under.
    :raises ValueError: When a pair is not two serials joined by ``=``, or one
        reported serial is given two filed ones.
    """
    aliases: dict[str, str] = {}
    for place, pair in enumerate(re.split(r"[\s,]+", text.strip()), start=1):
        if not pair:
            continue
        reported, _, filed = (side.strip().upper() for side in pair.partition("="))
        if not (SERIAL_PATTERN.match(reported) and SERIAL_PATTERN.match(filed)):
            raise ValueError(
                f"{ALIAS_VARIABLE}: pair {place} is not <reported serial>=<filed serial>"
            )
        if aliases.setdefault(reported, filed) != filed:
            raise ValueError(f"{ALIAS_VARIABLE}: pair {place} gives a serial a second alias")
    return aliases


def _dotenv_value(path: Path, key: str) -> str | None:
    """*key*'s value in the ``.env`` at *path*, None when the file or the key is absent.

    :param path: The file.
    :param key: The variable.
    :returns: The value, its quotes and a trailing `` # comment`` removed.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        match = _ENV_LINE.match(line)
        if match is None or match.group(1) != key:
            continue
        value = match.group(2).strip()
        if value[:1] in ("'", '"') and value.endswith(value[0]) and len(value) > 1:
            return value[1:-1]
        return value.split(" #", 1)[0].strip()
    return None


def load_aliases(
    environment: Mapping[str, str] | None = None, dotenv: Path = DOTENV
) -> dict[str, str]:
    """The alias map: :data:`ALIAS_VARIABLE` in the environment, else in *dotenv*.

    The environment's value wins even when empty, which turns off one the
    ``.env`` holds. Neither present is no alias.

    :param environment: The variables, ``os.environ`` when None.
    :param dotenv: The ``.env``, which git ignores, read when the environment has none.
    :returns: :func:`parse_aliases` of the value found.
    :raises ValueError: As :func:`parse_aliases`.
    """
    environment = os.environ if environment is None else environment
    text = environment.get(ALIAS_VARIABLE)
    if text is None:
        text = _dotenv_value(dotenv, ALIAS_VARIABLE)
    return parse_aliases(text or "")
