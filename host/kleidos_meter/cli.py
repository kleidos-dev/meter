# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Send program messages to the meter and print each response line.

Usage::

    kleidos-meter --port /dev/cu.usbmodemXXXX1 '*IDN?' 'SYST:ERR:ALL?' 'MEAS:CURR?'
    kleidos-meter --sim 'MEAS:CURR?'
    kleidos-meter calibrate --port /dev/cu.usbmodemXXXX1    # the guided shunt calibration
    kleidos-meter calibration verify --port ...           # the instrument against the registry
    kleidos-meter calibration restore --port ...          # the registry's copy, written back
    kleidos-meter relay-timing --relay k1 --port ...      # the relays' operate and release times
    kleidos-meter serve --port ...                        # hold the port for the panel
    kleidos-meter capture --profile STATE --seconds 10 --port ...  # one capture, recorded
    kleidos-meter pins show --port ...                    # the pin map; set, apply, defaults

The port defaults to ``KLEIDOS_METER_PORT``; a pyserial URL
(``socket://host:port``) names a socket instead. The port is opened
exclusively: one held by another opener ends the run with status 1 at once.
The line is checked first (``Meter.wait_ready``): a note on stderr says when
the instrument had rebooted, and an instrument that does not answer ends the
run with status 1. Stream records that arrive while a response is awaited are
printed as they come, and every block a response carries before its units.
The options of the command itself may come before a subcommand:
``kleidos-meter --sim pins show`` is ``kleidos-meter pins show --sim``.

A message that goes unanswered within ``--timeout`` is a ``#`` line on stderr,
and the later ones are still sent. The exit statuses are :data:`EXIT_STATUS`;
a subcommand returns its own. Every subcommand, its options and its statuses
are ``docs/cli.md``, which ``scripts/gen_cli_doc.py`` writes from
:data:`SUBCOMMANDS` and the parsers.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys

from .client import Meter, MeterTimeout, PortBusyError, SerialTransport
from .sim import SimulatedMeter

#: The subcommands: the module each one's ``build_parser()``, ``main(argv)`` and
#: ``EXIT_STATUS`` are in.
SUBCOMMANDS = {
    "capture": "kleidos_meter.capture_cli",
    "serve": "kleidos_meter.panel.serve",
    "calibrate": "kleidos_meter.calibrate",
    "calibration": "kleidos_meter.calibration_cli",
    "relay-timing": "kleidos_meter.relay_timing",
    "pins": "kleidos_meter.pins_cli",
}

#: The options of the command itself that take a value, which may come before a subcommand.
_VALUED = ("--port", "--timeout")

#: What each exit status says.
EXIT_STATUS = {
    0: "every message was answered",
    1: "the port is held elsewhere, the instrument did not answer the readiness check, or a"
    " message went unanswered within ``--timeout``",
    2: "a usage error (no message, or no port and no ``--sim``)",
}


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter`` with no subcommand.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kleidos-meter",
        description=__doc__.splitlines()[0],
        epilog="A subcommand takes the place of the messages: "
        + ", ".join(SUBCOMMANDS)
        + ". kleidos-meter SUBCOMMAND --help says what it takes.",
    )
    parser.add_argument("messages", nargs="+", help="program messages, one per argument")
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="talk to the simulated instrument")
    parser.add_argument(
        "--timeout", type=float, default=8.0, help="seconds to wait for each response"
    )
    return parser


def subcommand_at(argv: list[str]) -> int | None:
    """Where a subcommand is in *argv*, past only the command's own options; None for none.

    :param argv: The arguments.
    :returns: The subcommand's index, or None when a message comes first.
    """
    index = 0
    while index < len(argv):
        word = argv[index]
        if word in SUBCOMMANDS:
            return index
        if word in _VALUED:
            index += 2
        elif word == "--sim" or word.startswith(tuple(f"{option}=" for option in _VALUED)):
            index += 1
        else:
            return None
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    :param argv: The arguments, ``sys.argv[1:]`` when None.
    :returns: The exit status (:data:`EXIT_STATUS`), or a subcommand's own.
    :raises SystemExit: With status 2 on a usage error (argparse's).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    at = subcommand_at(argv)
    if at is not None:
        # Imported when it is asked for: a message needs none of them. The
        # options before it are the subcommand's, after its own arguments.
        status: int = importlib.import_module(SUBCOMMANDS[argv[at]]).main(
            argv[at + 1 :] + argv[:at]
        )
        return status
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.sim and not args.port:
        parser.error("name the port with --port or KLEIDOS_METER_PORT, or use --sim")
    try:
        transport = SimulatedMeter() if args.sim else SerialTransport(args.port)
    except PortBusyError as exc:
        print(f"# {exc}", file=sys.stderr)
        return 1
    status = 0
    with Meter(transport) as meter:
        try:
            rebooted = meter.wait_ready()
        except MeterTimeout as exc:
            print(f"# the instrument is not answering: {exc}", file=sys.stderr)
            return 1
        if rebooted:
            print("# the instrument rebooted (*ESR? power-on bit set)", file=sys.stderr)
        for message in args.messages:
            seen = len(meter.capture.rows)
            try:
                response = meter.send(message, timeout=args.timeout)
            except MeterTimeout as exc:
                print(f"# {exc}", file=sys.stderr)
                status = 1
                continue
            for t_us, row in sorted(meter.capture.rows.items())[seen:]:
                print(f"# {t_us} {row}")
            for block in response.blocks:
                sys.stdout.write(block.decode("ascii"))
            print(";".join(response.units))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
