# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# PlatformIO pre-upload guard for the bench power meter.
#
# The meter runs on a generic ESP32-S3 board, and `board = esp32-s3-devkitc-1`
# declares the NATIVE USB hwid (303a:1001). Every ESP32-S3 board of the Kleidos
# fleet presents that same hwid, and several share this bench. So an upload
# with no port named is not merely ambiguous: PlatformIO would pick one by
# auto-detection, and the meter's firmware would land on a Kleidos board.
#
# This guard hooks ONLY the "upload" action, so plain builds are never gated. It
# refuses when:
#
#   * no port is named (KLEIDOS_METER_PORT unset and no --upload-port), or
#   * the named port does not exist, or
#   * the named port is not the carrier's CH343 bridge (USB 1a86:55d3), which
#     is how the instrument is always used: the native port stays unplugged
#     (docs/carrier-board.md SS 7), and no fleet board carries a CH343, or
#   * the board at that port is one of the fleet, by its USB serial, matched
#     against the fleet's inventory when one is reachable: the file named by
#     KLEIDOS_FLEET_INVENTORY, or by default the firmware checkout beside this
#     one (../kleidos/qa/inventory/devices.local.yaml).
#
# The bridge check is the one that matters. It is what stands between a
# mistyped port and a reflashed DUT, and it needs no file from another
# repository.

from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from serial.tools.list_ports_common import ListPortInfo

    #: What PlatformIO's SCons hands the script: its construction environment.
    env: Any

    def Import(*names: str) -> None:
        """SCons's import of a name from the calling SConscript."""


Import("env")

#: The environment variable platformio.ini reads the port from.
PORT_VARIABLE = "KLEIDOS_METER_PORT"

#: The environment variable that names the fleet inventory, when it is not
#: the default sibling checkout's.
INVENTORY_VARIABLE = "KLEIDOS_FLEET_INVENTORY"

#: The carrier's USB-UART bridge, a WCH CH343P (docs/carrier-board.md SS 7).
METER_VID_PID = (0x1A86, 0x55D3)

#: SCons execs this script without defining ``__file__``; the project
#: directory comes from PlatformIO: <repo>/firmware.
REPO_ROOT = Path(env.subst("$PROJECT_DIR")).resolve().parent  # noqa: F821  SCons injects env
DEFAULT_INVENTORY = REPO_ROOT.parent / "kleidos" / "qa" / "inventory" / "devices.local.yaml"

#: Serial strings in the inventory: a chip serial, or a MAC with separators.
_IDENTITY = re.compile(r"(?:usb_serial|mac):\s*['\"]?([0-9A-Fa-f:]+)['\"]?\s*$")


def _abort(message: str) -> NoReturn:
    """Stop the upload with *message*, before anything is written."""
    print(f"\n*** meter upload refused: {message}\n", file=sys.stderr)
    env.Exit(1)  # noqa: F821  SCons injects env
    raise SystemExit(1)  # env.Exit raises it already; said again for the reader


def _inventory_path() -> Path | None:
    """Return the fleet inventory to check against, or ``None``."""
    named = os.environ.get(INVENTORY_VARIABLE, "").strip()
    path = Path(named) if named else DEFAULT_INVENTORY
    return path if path.is_file() else None


def _fleet_identities(path: Path) -> set[str]:
    """Return the USB identities of every board in *path*, lowercased.

    Read straight from the text rather than through PyYAML, which the
    PlatformIO interpreter does not carry.

    :param path: The inventory file.
    :returns: Chip serials and MACs, separators removed.
    """
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _IDENTITY.search(line.strip())
        if match:
            found.add(match.group(1).lower().replace(":", ""))
    return found


def _port_info(port: str) -> ListPortInfo | None:
    """Return pyserial's description of the device at *port*, or ``None``."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for info in list_ports.comports():
        if info.device == port:
            return info
    return None


def _guard_meter_upload(source: object, target: object, env: Any) -> None:
    """Refuse an upload that has no port, or whose port is not the meter's."""
    port = (env.subst("$UPLOAD_PORT") or os.environ.get(PORT_VARIABLE, "")).strip()
    if not port:
        _abort(
            f"no port named. Set {PORT_VARIABLE} to the meter's CH343 node.\n"
            "  Without it PlatformIO auto-detects, and every ESP32-S3 of the fleet\n"
            "  answers to the same hwid as the meter: the image could land on a DUT."
        )
    if not glob.glob(port):
        _abort(f"{port} does not exist. Is the meter's CH343 cable plugged in?")

    info = _port_info(port)
    if info is None:
        _abort(f"cannot describe {port} (is pyserial in PlatformIO's interpreter?)")
    if (info.vid, info.pid) != METER_VID_PID:
        _abort(
            f"{port} is {info.vid or 0:04x}:{info.pid or 0:04x}, not the carrier's CH343"
            f" ({METER_VID_PID[0]:04x}:{METER_VID_PID[1]:04x}).\n"
            "  The meter is flashed through its CH343 port only."
        )

    inventory = _inventory_path()
    if inventory is None:
        print("meter upload guard: no fleet inventory reachable; the bridge check stands alone")
        return
    serial = (info.serial_number or "").lower().replace(":", "")
    if serial and serial in _fleet_identities(inventory):
        _abort(
            f"{port} is a Kleidos board: its USB serial is in {inventory}.\n"
            "  Uploading here would replace that board's firmware."
        )


env.AddPreAction("upload", _guard_meter_upload)  # noqa: F821  SCons injects env
