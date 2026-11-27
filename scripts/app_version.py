#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print the version an ESP-IDF application image carries.

    python3 scripts/app_version.py firmware/.pio/build/meter/firmware.bin

The version is ``esp_app_desc_t.version``, the field ``*IDN?`` and
``SYSTem:INFormation?`` report as ``fw=``: the application description sits at
the start of the image's first segment, after the 24-byte image header and the
8-byte segment header, and begins with the magic word ``0xABCD5432``; the
version is the 32-byte field 16 bytes in. CI uses it to check that a changed
``firmware/version.txt`` reaches an incremental build.

Standard library only.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

#: esp_app_desc_t's offset in the image: image header (24) + segment header (8).
DESC_OFFSET = 24 + 8
#: ESP_APP_DESC_MAGIC_WORD.
DESC_MAGIC = 0xABCD5432
#: The version field: after magic, secure_version and reserv1[2] (16 bytes), 32 long.
VERSION_OFFSET = 16
VERSION_LENGTH = 32


def app_version(image: bytes) -> str:
    """The version string of an application image; ValueError if it has none."""
    if len(image) < DESC_OFFSET + VERSION_OFFSET + VERSION_LENGTH:
        raise ValueError("too short for an application image")
    (magic,) = struct.unpack_from("<I", image, DESC_OFFSET)
    if magic != DESC_MAGIC:
        raise ValueError(f"no application description (magic 0x{magic:08X})")
    start = DESC_OFFSET + VERSION_OFFSET
    return image[start : start + VERSION_LENGTH].split(b"\0", 1)[0].decode("ascii")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        print(app_version(Path(argv[1]).read_bytes()))
    except (OSError, ValueError) as err:
        print(f"{argv[1]}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
