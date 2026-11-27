# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""No real instrument serial is in a tracked file.

An instrument's serial (``KM-`` and ten hex digits, ``firmware/src/identity.h``)
is the first five bytes of a SHA-256 over its factory MAC, and a MAC is a vendor
prefix and three more bytes: a search undoes the digest. So the bench
instrument is written as the placeholder ``KM-0000000000`` everywhere, its
registry entry, its records, the bench images and the examples included
(owner, 2026-11-27; ``calibration/README.md``, "Identity").

This guard does not name the real serial: it holds every serial-shaped string
of the tree to a short list, the placeholder, the digests of the made-up MACs
the identity tests use, and the made-up serials of the registry's tests. A
real serial is on no list, so it fails wherever it appears: in a text file, in
the raw bytes of any other file and in the text chunks of a PNG (an image's
description names its instrument).

Host-only: ``git ls-files`` and the files read as bytes.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import media_meta  # noqa: E402

#: The placeholder the bench instrument is written as.
PLACEHOLDER = "KM-0000000000"

#: The made-up MACs of the identity tests (``host/tests/test_identity.py`` and
#: ``firmware/test/native/test_identity.cpp``), whose serials the simulator and
#: the tests answer with: a locally administered address, all zeros, all ones.
MADE_UP_MACS = (
    bytes([0x02, 0x00, 0x5E, 0x10, 0x20, 0x30]),
    bytes(6),
    bytes([0xFF] * 6),
)

#: The registry tests' own serials (``host/tests/test_registry.py``,
#: ``host/tests/test_calibrate.py``): another instrument, and four of a fleet.
MADE_UP_SERIALS = frozenset(
    {"KM-FFFFFFFFFF", "KM-0000000001", "KM-0000000002", "KM-0000000003", "KM-0000000004"}
)

#: A serial's shape, in bytes: ``KM-`` and ten hex digits of either case.
SERIAL = re.compile(rb"KM-[0-9A-Fa-f]{10}(?![0-9A-Za-z])")


def serial_from_mac(mac: bytes) -> str:
    """The serial the firmware derives from *mac* (``firmware/src/identity.h``)."""
    digest = hashlib.sha256(b"kleidos-meter-serial-v1" + mac).digest()
    return "KM-" + digest[:5].hex().upper()


def allowed() -> frozenset[str]:
    """Every serial a tracked file may hold."""
    return frozenset(
        {PLACEHOLDER, *MADE_UP_SERIALS, *(serial_from_mac(mac) for mac in MADE_UP_MACS)}
    )


def _png_text(raw: bytes) -> bytes:
    """The text a PNG's chunks carry, its compressed ones inflated."""
    found = []
    for kind, data in media_meta.png_chunks(raw):
        if kind == b"zTXt":
            found.append(zlib.decompress(data.split(b"\0", 1)[1][1:]))
        elif kind == b"iTXt":
            keyword, rest = data.split(b"\0", 1)
            flag, rest = rest[0], rest[2:]
            text = rest.split(b"\0", 2)[2]
            found.append(keyword + b" " + (zlib.decompress(text) if flag else text))
    return b"\n".join(found)


def serials_in(name: str, raw: bytes) -> set[str]:
    """The serial-shaped strings of the file *name*, whose bytes are *raw*."""
    texts = [raw]
    if raw.startswith(media_meta.PNG_SIGNATURE):
        texts.append(_png_text(raw))
    return {match.decode() for text in texts for match in SERIAL.findall(text)}


def findings(files: dict[str, bytes]) -> list[str]:
    """Each serial of *files* (path to bytes) that is not on the list, by file."""
    ok = allowed()
    return [
        f"{name}: {serial}"
        for name, raw in sorted(files.items())
        for serial in sorted(serials_in(name, raw) - ok)
    ]


def _tracked() -> dict[str, bytes]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.split(b"\0")
    files = {}
    for name in listed:
        path = ROOT / name.decode()
        if name and path.is_file() and not path.is_symlink():
            files[name.decode()] = path.read_bytes()
    return files


def test_no_tracked_file_holds_a_serial_off_the_list() -> None:
    found = findings(_tracked())
    assert not found, (
        "a serial that is neither the placeholder nor a test's (write the bench"
        f" instrument as {PLACEHOLDER}):\n" + "\n".join(found)
    )


def test_the_placeholder_is_what_the_registry_files() -> None:
    names = [p.stem for p in (ROOT / "calibration" / "instruments").glob("*.json")]
    assert names == [PLACEHOLDER]


def test_the_list_is_the_identity_tests_vectors() -> None:
    assert serial_from_mac(MADE_UP_MACS[0]) == "KM-EE529C1B03"
    identity = (ROOT / "host" / "tests" / "test_identity.py").read_text(encoding="utf-8")
    for mac in MADE_UP_MACS:
        assert serial_from_mac(mac) in identity


def test_the_guard_sees_a_planted_serial() -> None:
    planted = "KM-" + "1234567890"
    assert findings({"docs/x.md": f"instrument `{planted}` on 0.18.0".encode()}) == [
        f"docs/x.md: {planted}"
    ]
    lower = "KM-" + "12345abcde"
    assert findings({"docs/x.md": f"`{lower}`".encode()}) == [f"docs/x.md: {lower}"]
    assert findings({"docs/x.md": f"`{PLACEHOLDER}`, KM-EE529C1B03".encode()}) == []
    # A PNG's description is read whether its chunk is plain or compressed.
    name = "docs/images/panel/bench/bench-live-2026-11-26-elf-80a257970e934ff6.png"
    chunks = media_meta.png_chunks((ROOT / name).read_bytes())

    def with_description(chunk: bytes) -> bytes:
        return media_meta.PNG_SIGNATURE + b"".join(
            chunk
            if kind == b"iTXt" and data.startswith(b"Description\0")
            else media_meta._chunk(kind, data)
            for kind, data in chunks
        )

    text = f"over the cable to instrument {planted}".encode()
    plain = with_description(media_meta._chunk(b"iTXt", b"Description\0\0\0\0\0" + text))
    squeezed = with_description(
        media_meta._chunk(b"iTXt", b"Description\0\1\0\0\0" + zlib.compress(text))
    )
    old = with_description(media_meta._chunk(b"zTXt", b"Description\0\0" + zlib.compress(text)))
    assert planted.encode() not in squeezed + old  # found only by inflating the chunk
    for raw in (plain, squeezed, old):
        assert findings({name: raw}) == [f"{name}: {planted}"]
