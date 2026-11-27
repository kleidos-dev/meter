# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The instrument's pin map: show it, stage a change, apply it, or put the rig's back.

Usage::

    kleidos-meter pins show --port "$KLEIDOS_METER_PORT"                # the live map
    kleidos-meter pins show --pending --port ...                         # the staged one
    kleidos-meter pins set sda=18 scl=21 --port ...                      # stage, judge, keep
    kleidos-meter pins apply relay1=16 relay1_active=high --port ...     # stage and apply
    kleidos-meter pins defaults --port ...                               # the rig's map back
    kleidos-meter pins apply sda=18 --sim --sim-wiring sda=18            # simulated

A change is ``key=value``: a role (``sda``, ``scl``, ``alert``, ``relay1``,
``relay2``) takes a GPIO number or ``none``; ``pull_sda``, ``pull_scl`` and
``pull_alert`` take ``up`` or ``off``; ``relay1_active`` and
``relay2_active`` ``high`` or ``low``; ``addr`` ``auto`` or an address
(``0x41``); ``scl_hz`` the bus clock at boot. The staging lives on the
instrument until it is applied or cleared: ``set`` leaves it there for a
later ``apply`` on the same instrument.

``apply`` proves the INA228 on the new map before it keeps it; a map it
cannot prove is put back, and the instrument says why. Nothing is applied
while a capture or a null runs or a relay is not at rest. ``defaults`` needs
no INA228 and is the way back from a map that left none.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import pins
from .client import Meter, MeterTimeout, PortBusyError, SerialTransport
from .sim import SimulatedMeter

#: What each exit status says.
EXIT_STATUS = {
    0: "the map was shown, staged valid, or applied and proved",
    1: "the port is held elsewhere or the instrument did not answer",
    2: "a usage error (a change that does not read, no port and no ``--sim``)",
    3: "the instrument refused the change or put the last good map back: what it said is on stderr",
}

#: How each role is named on a line of the map.
_NAMES = {
    "sda": "SDA",
    "scl": "SCL",
    "alert": "ALERT",
    "relay1": "Relay 1",
    "relay2": "Relay 2",
}


def change(text: str) -> tuple[str, int | str | None]:
    """One ``key=value`` change of a map, typed as :meth:`Meter.stage` takes it.

    :param text: The change.
    :returns: The key and its value.
    :raises argparse.ArgumentTypeError: When the key is no field of a map or
        the value is not one it takes.
    """
    key, sep, value = text.partition("=")
    key, value = key.strip().lower(), value.strip().lower()
    if not sep or key not in pins.KEYS:
        raise argparse.ArgumentTypeError(
            f"{text!r}: a change is key=value, the key one of {', '.join(pins.KEYS)}"
        )
    try:
        return key, pins.typed_change(key, value)
    except ValueError as why:
        raise argparse.ArgumentTypeError(f"{text!r}: {why}") from None


def describe(report: dict[str, str], *, staged: bool) -> list[str]:
    """A map's report as lines: where it came from, then a line a role.

    :param report: ``SYSTem:PINS?``'s fields, or ``SYSTem:PINS:PENDing?``'s with *staged*.
    :param staged: Whether it is the staged map.
    :returns: The lines.
    """
    if staged:
        verdict = (
            "valid"
            if report["valid"] == "1"
            else f"refused by the rules: {report['problem']} at {report['at']}"
        )
        lines = [f"pins: the staged map, {verdict}"]
    else:
        known = (
            "this package's board"
            if report["table"] == pins.DIGEST
            else f"not this package's board, {pins.DIGEST}"
        )
        lines = [
            f"pins: the live map, src={report['src']} verified={report['verified']}"
            f" table={report['table']} ({known})"
        ]
    for role, name in _NAMES.items():
        gpio = report[role]
        option = (
            f"active {report[f'{role}_active']}"
            if role in pins.RELAYS
            else f"pull {report[f'pull_{role}']}"
        )
        pad = "none" if gpio == "none" else f"GPIO{gpio}"
        lines.append(f"  {name:<8} {pad:<7} {option}")
    address = report["addr"]
    scan = " (a scan of 0x40 to 0x4F)" if address == "auto" else ""
    lines.append(f"  {'address':<8} {address}{scan}")
    lines.append(f"  {'clock':<8} {report['scl_hz']} Hz at boot")
    return lines


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter pins``.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(prog="kleidos-meter pins", description=__doc__.splitlines()[0])
    parser.add_argument(
        "action",
        choices=["show", "set", "apply", "defaults"],
        help="print the map, stage a change, stage and apply it, or apply the rig's map",
    )
    parser.add_argument(
        "changes",
        nargs="*",
        type=change,
        metavar="KEY=VALUE",
        help="the changes to stage (set, apply): sda=18, relay1_active=high, addr=0x41",
    )
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="talk to the simulated instrument")
    parser.add_argument(
        "--sim-wiring",
        type=pins.wiring_argument,
        default=pins.RIG_WIRING,
        metavar="PAIRS",
        help="where the simulated board's parts are, key=value pairs over the rig's"
        " (sda=18,addr=0x41); the rig by default",
    )
    parser.add_argument("--pending", action="store_true", help="show the staged map (show)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``kleidos-meter pins show|set|apply|defaults``.

    :param argv: The arguments after ``pins``, ``sys.argv[1:]`` when None.
    :returns: The exit status (:data:`EXIT_STATUS`).
    :raises SystemExit: With status 2 on a usage error (argparse's).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.sim and not args.port:
        parser.error("name the port with --port or KLEIDOS_METER_PORT, or use --sim")
    if args.changes and args.action in ("show", "defaults"):
        parser.error(f"{args.action} takes no change")
    if args.action == "set" and not args.changes:
        parser.error("set takes at least one change")
    try:
        transport = (
            SimulatedMeter(wiring=args.sim_wiring) if args.sim else SerialTransport(args.port)
        )
    except PortBusyError as exc:
        print(f"# {exc}", file=sys.stderr)
        return 1
    with Meter(transport) as meter:
        try:
            meter.wait_ready()
            if args.action == "show":
                report = meter.pins(pending=args.pending)
                print("\n".join(describe(report, staged=args.pending)))
                return 0
            if args.changes:
                staged = meter.stage(**dict(args.changes))
                if args.action == "set":
                    print("\n".join(describe(staged, staged=True)))
                    return 0 if staged["valid"] == "1" else 3
            result, errors = meter.apply() if args.action == "apply" else meter.pins_defaults()
            report = meter.pins()
        except MeterTimeout as exc:
            print(f"# the instrument is not answering: {exc}", file=sys.stderr)
            return 1
    for entry in errors:
        print(f"# {entry}", file=sys.stderr)
    print(
        f"pins: {result['result']}, reason {result['reason']}, INA228 mfg {result['mfg']}"
        f" die {result['die']}, self-test {result['tst']}, saved {result['saved']}"
    )
    print("\n".join(describe(report, staged=False)))
    return 0 if result["result"] == "ok" and not errors else 3


if __name__ == "__main__":
    sys.exit(main())
