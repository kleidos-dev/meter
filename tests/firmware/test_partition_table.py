# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The partition table puts both record logs where the firmware writes them.

FW-16 keeps the calibration as records of a log over two 4 KiB sectors of raw
flash (``firmware/src/flash_log.h``) at a fixed address the firmware knows as a
constant, ``kLogBaseAddress`` in ``firmware/src/flash_port.h``, so that it
never looks a partition up (``esp_partition_find()`` allocates); FW-03 keeps
the pin map the same way in a second log, at ``kPinLogBaseAddress``. The
addresses are only right if the partition table puts the ``calib`` and
``pins`` partitions there, the table is the one both builds use, and the
image's own partition sits clear of both. These tests hold
``firmware/partitions.csv`` to the constants, to ``sdkconfig.defaults``
(ESP-IDF's ``CONFIG_PARTITION_TABLE_CUSTOM_FILENAME``) and to
``platformio.ini`` (``board_build.partitions``, which PlatformIO generates
``partitions.bin`` from).

Host-only: the files read as text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware"
CSV = FIRMWARE / "partitions.csv"
PORT = FIRMWARE / "src" / "flash_port.h"
LOG = FIRMWARE / "src" / "flash_log.h"

#: Where ESP-IDF puts the partition table (``CONFIG_PARTITION_TABLE_OFFSET``,
#: its default, which sdkconfig.defaults does not change), and its length.
TABLE_OFFSET = 0x8000
TABLE_BYTES = 0x1000


def _number(text: str) -> int:
    """A size or offset as the CSV writes it: hex, decimal, or with K or M."""
    text = text.strip()
    scale = {"K": 1024, "M": 1024 * 1024}.get(text[-1:].upper(), 1)
    digits = text[:-1] if scale != 1 else text
    return int(digits, 0) * scale


class Row(TypedDict):
    """One partition of the table."""

    type: str
    subtype: str
    offset: int
    size: int
    flags: str


def rows() -> dict[str, Row]:
    """The table's partitions by name: type, subtype, offset, size and flags."""
    out: dict[str, Row] = {}
    for line in CSV.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        name, kind, subtype, offset, size = fields[:5]
        flags = fields[5] if len(fields) > 5 else ""
        out[name] = {
            "type": kind,
            "subtype": subtype,
            "offset": _number(offset),
            "size": _number(size),
            "flags": flags,
        }
    return out


def _cpp_constant(path: Path, name: str) -> str:
    """The initializer of ``constexpr std::uint32_t <name> = ...;`` in *path*."""
    found = re.search(
        rf"constexpr\s+std::uint32_t\s+{name}\s*=\s*([^;]+);", path.read_text(encoding="utf-8")
    )
    assert found, f"{path.name} no longer defines {name}"
    return found.group(1).strip()


def _cpp_number(text: str) -> int:
    return int(text.rstrip("Uu"), 0)


def _log_bytes() -> int:
    """Each log's bytes: ``kSectorCount`` sectors of ``kSectorBytes``."""
    sector = _cpp_number(_cpp_constant(LOG, "kSectorBytes"))
    sectors = _cpp_number(_cpp_constant(LOG, "kSectorCount"))
    return sectors * sector


def test_the_calib_partition_is_where_the_firmware_writes() -> None:
    calib = rows()["calib"]
    base = _cpp_number(_cpp_constant(PORT, "kLogBaseAddress"))
    assert calib["type"] == "data" and calib["subtype"] == "undefined"
    assert calib["offset"] == base == 0x9000
    assert calib["size"] == _log_bytes() == 0x2000
    assert calib["flags"] == "", "a read-only or encrypted calib partition would refuse the log"


def test_the_pins_partition_is_where_the_firmware_writes() -> None:
    pins = rows()["pins"]
    calib = rows()["calib"]
    # flash_port.h writes the pin log's base as the calib log's base plus its
    # bytes: the CSV's row is held to that sum, and to 0xB000 itself.
    initializer = _cpp_constant(PORT, "kPinLogBaseAddress")
    assert initializer == "kLogBaseAddress + kLogBytes"
    base = _cpp_number(_cpp_constant(PORT, "kLogBaseAddress")) + _log_bytes()
    assert pins["type"] == "data" and pins["subtype"] == "undefined"
    assert pins["offset"] == base == 0xB000 == calib["offset"] + calib["size"]
    assert pins["size"] == _log_bytes() == 0x2000
    assert pins["flags"] == "", "a read-only or encrypted pins partition would refuse the log"


def test_the_logs_end_before_the_image() -> None:
    table = rows()
    image = _cpp_number(_cpp_constant(PORT, "kImageAddress"))
    assert table["factory"]["offset"] == image == 0x10000
    for name in ("calib", "pins"):
        assert table[name]["offset"] + table[name]["size"] <= image, f"{name} runs into the image"


def test_the_table_is_the_logs_the_image_and_nothing_else() -> None:
    table = rows()
    assert set(table) == {"calib", "pins", "factory"}, "NVS and phy_init left the table in FW-16"
    factory = table["factory"]
    assert factory["type"] == "app" and factory["subtype"] == "factory"
    offset = factory["offset"]
    assert isinstance(offset, int) and offset % 0x10000 == 0, "an app partition starts on 64 KiB"
    places = [(p["offset"], p["size"]) for p in table.values()]
    assert all(isinstance(a, int) and isinstance(b, int) for a, b in places)
    spans = sorted((int(str(a)), int(str(a)) + int(str(b))) for a, b in places)
    assert spans[0][0] >= TABLE_OFFSET + TABLE_BYTES, "a partition overlaps the table"
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert end <= start, "two partitions overlap"
    assert spans[-1][1] <= 16 * 1024 * 1024, "past the end of the 16 MB part"


def test_both_builds_take_this_table() -> None:
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "\nCONFIG_PARTITION_TABLE_CUSTOM=y\n" in defaults
    assert '\nCONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"\n' in defaults
    assert "CONFIG_PARTITION_TABLE_OFFSET" not in defaults, "the table moved: TABLE_OFFSET too"
    ini = (FIRMWARE / "platformio.ini").read_text(encoding="utf-8")
    assert re.search(r"^board_build\.partitions\s*=\s*partitions\.csv\s*$", ini, re.M)


def test_the_parser_reads_the_shapes_the_csv_may_use() -> None:
    assert _number("0x9000") == 0x9000
    assert _number("8K") == 0x2000
    assert _number("1M") == 0x100000
    assert _number(" 4096 ") == 4096
    assert _cpp_number("0x9000U") == 0x9000
