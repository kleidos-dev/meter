# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which instrument this is: the serial ``*IDN?`` reports, derived as the firmware derives it.

The serial (firmware 0.4.0 and later) is ``KM-`` and ten hex digits, the first
five bytes of ``SHA-256("kleidos-meter-serial-v1" || MAC)`` over the
ESP32-S3's factory eFuse MAC (``firmware/src/identity.h``): a name that
survives a full flash erase, and a pseudonym rather than the MAC, which never
enters a tracked file.
"""

from __future__ import annotations

import hashlib
import re

#: The domain string the firmware hashes ahead of the MAC (``identity.h``).
SERIAL_DOMAIN = b"kleidos-meter-serial-v1"
SERIAL_PATTERN = re.compile(r"^KM-[0-9A-F]{10}$")


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
