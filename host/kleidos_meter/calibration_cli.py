# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The instrument against the calibration registry: verify it, or restore its working copy.

The registry in this repository is the calibration; the instrument's NVS holds
a working copy, which a full flash erase loses (``calibration/README.md``).

Usage::

    kleidos-meter calibration verify  --port "$KLEIDOS_METER_PORT"            # device vs registry
    kleidos-meter calibration restore --port "$KLEIDOS_METER_PORT"            # registry -> device
    kleidos-meter calibration verify  --port ... --rev <commit>               # at a pinned commit
    kleidos-meter calibration verify  --sim                                   # simulated

``verify`` exits 0 when the working copy is the registry's (status
``applied``), 3 otherwise; ``--json`` prints the verdict for a script.
``restore`` writes the shunt of each range, the tag, the date and the VBUS
side of the calibration that holds today, reads them back and verifies; it
never writes a null, which is session state. It refuses an instrument the
registry does not know, or one whose image, bypass part or INA228 is not the
configuration the registry files it under.

The registry files the bench instrument under a placeholder serial, since
no real serial is tracked (``calibration/README.md``, "Identity"). Name the
serial it reports in ``KLEIDOS_METER_SERIAL_ALIASES``, in the environment or
in the repository's ``.env``, which git ignores
(:func:`~kleidos_meter.identity.load_aliases`): both commands then look it
up, and print it, as the serial the registry files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .client import Meter, SerialTransport
from .identity import ALIAS_VARIABLE, load_aliases
from .invariant import present
from .protocol import ErrorEntry
from .registry import ROOT, Registry, Verdict, build_index, compare, restore_messages
from .sim import SimulatedMeter

#: What each exit status says.
EXIT_STATUS = {
    0: "the instrument holds the registry's working copy (``applied``)",
    1: "an error ended the run, its traceback or its message on stderr: the port is held"
    " elsewhere, the instrument did not answer or refused the copy, or ``--rev`` holds no index",
    2: "a usage error (a ``KLEIDOS_METER_SERIAL_ALIASES`` that is not serial pairs among them),"
    " or a ``restore`` with no terminal to answer its question and no ``--yes``",
    3: "it does not: the verdict says why",
}


class Instrument(Protocol):
    """What verify and restore ask of an instrument: a :class:`~kleidos_meter.client.Meter`,
    or the panel's link through its executor (``kleidos_meter.panel.settings.InstrumentLink``).
    """

    def idn(self) -> str:
        """``*IDN?``.

        :returns: Its answer.
        """

    def info(self) -> dict[str, str]:
        """``SYSTem:INFormation?``.

        :returns: Its fields.
        """

    def calibration(self) -> dict[str, str]:
        """``CALibration:DATA?``.

        :returns: Its fields.
        """

    def exchange(self, units: Sequence[str]) -> tuple[list[str], list[ErrorEntry], bytes | None]:
        """Send *units* as one message.

        :param units: The message's units.
        :returns: The query responses, the errors, and a block if one came.
        :raises MeterError: When one is refused
            (:class:`~kleidos_meter.client.MeterError`).
        """


def load_index(root: Path = ROOT, revision: str | None = None) -> dict[str, Any]:
    """The index, from the working tree or from *revision* of the repository.

    At a revision it is read with ``git show``, so a dirty or newer checkout
    cannot leak into what a verification compares against. In the working
    tree it is rebuilt from the sources, so an index not yet regenerated
    after an edit is not trusted either.

    :param root: The ``calibration/`` directory.
    :param revision: A git revision to read it at, or None for the working tree.
    :returns: The index.
    :raises SystemExit: When *revision* holds no index.
    """
    if revision is not None:
        registry = Registry.from_git(root.parent, revision)
        text = registry.read("index.json")
        if text is None:
            raise SystemExit(f"no calibration/index.json at {revision}")
        index: dict[str, Any] = json.loads(text)
        return index
    return build_index(Registry.from_directory(root))


def identify(meter: Instrument) -> tuple[str, dict[str, str], dict[str, str]]:
    """Ask the instrument what it is and what calibration it holds.

    :param meter: The instrument.
    :returns: ``*IDN?``, and the fields of ``SYSTem:INFormation?`` and ``CALibration:DATA?``.
    """
    return meter.idn(), meter.info(), meter.calibration()


def verify(
    meter: Instrument,
    index: dict[str, Any],
    on: dt.date,
    aliases: Mapping[str, str] | None = None,
) -> Verdict:
    """Compare the instrument with the registry on *on*.

    :param meter: The instrument.
    :param index: The registry's index.
    :param on: The day.
    :param aliases: The serial aliases (:func:`~kleidos_meter.identity.load_aliases`).
    :returns: The verdict.
    """
    idn, info, cal = identify(meter)
    return compare(index, idn, info, cal, on, aliases)


def describe(verdict: Verdict) -> list[str]:
    """The verdict, as lines for a person.

    :param verdict: The verdict.
    :returns: Its lines.
    """
    lines = [f"{verdict.serial or '(no serial)'}: {verdict.status}"]
    if verdict.aliased:
        lines.append(f"  the instrument's own serial is mapped to it by {ALIAS_VARIABLE}")
    if verdict.entry is not None:
        entry = verdict.entry
        ranges = ", ".join(
            f"range {name} {rng['applied_nohm'] / 1e6:.6f} mΩ (u {rng['u_ppm']} ppm)"
            for name, rng in sorted(entry["ranges"].items())
        )
        lines.append(
            f"  calibration {entry['event']} ({entry['configuration']}), "
            f"valid {entry['valid_from']} to {entry['valid_until']}: {ranges}"
        )
    lines += [f"  - {d}" for d in verdict.differences]
    lines += [f"  ! {w}" for w in verdict.warnings]
    return lines


def restore(
    meter: Instrument,
    index: dict[str, Any],
    on: dt.date,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    aliases: Mapping[str, str] | None = None,
) -> Verdict:
    """Write the registry's working copy to the instrument and verify it.

    :param meter: The instrument.
    :param index: The registry's index.
    :param on: The day.
    :param ask: Shows the confirmation and returns the answer.
    :param say: Prints a line.
    :param aliases: The serial aliases (:func:`~kleidos_meter.identity.load_aliases`).
    :returns: The verdict after the restore (or before it, when nothing was
        written: the copy was already right, or the instrument is refused).
    """
    verdict = verify(meter, index, on, aliases)
    for line in describe(verdict):
        say(line)
    if verdict.status == "applied":
        say("Nothing to restore: the working copy is the registry's.")
        return verdict
    if verdict.status not in ("working_copy_lost", "working_copy_differs"):
        say("Not restored: the registry has no calibration to give this instrument.")
        return verdict
    messages = restore_messages(present(verdict.entry, "the registry's entry"))
    say("Will send: " + "; ".join(messages))
    if ask("Write the registry's working copy to the instrument? [y/N] ").strip().lower() not in (
        "y",
        "yes",
    ):
        say("Not restored.")
        return verdict
    meter.exchange(messages)
    after = verify(meter, index, on, aliases)
    for line in describe(after):
        say(line)
    return after


def ask_terminal(prompt: str) -> str:
    """Ask on the terminal; an end of input is a no, not a traceback.

    :param prompt: The question.
    :returns: The answer.
    """
    try:
        return input(prompt)
    except EOFError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter calibration``.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kleidos-meter calibration", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "action",
        choices=["verify", "restore"],
        help="compare the instrument with the registry, or write it the registry's copy",
    )
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="talk to the simulated instrument")
    parser.add_argument("--registry", type=Path, default=ROOT, help="the calibration/ directory")
    parser.add_argument("--rev", help="read the registry at this git revision (git show)")
    parser.add_argument(
        "--date", type=dt.date.fromisoformat, default=None, help="as of this day (today)"
    )
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON (verify)")
    parser.add_argument("--yes", action="store_true", help="answer the confirmation (restore)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``kleidos-meter calibration verify|restore``.

    :param argv: The arguments after ``calibration``, ``sys.argv[1:]`` when None.
    :returns: The exit status (:data:`EXIT_STATUS`).
    :raises SystemExit: With status 2 on a usage error (argparse's).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.sim and not args.port:
        parser.error("name the port with --port or KLEIDOS_METER_PORT, or use --sim")
    try:
        aliases = load_aliases()
    except ValueError as error:
        parser.error(str(error))
    index = load_index(args.registry, args.rev)
    on = args.date or dt.datetime.now().astimezone().date()
    transport = SimulatedMeter() if args.sim else SerialTransport(args.port)
    with Meter(transport) as meter:
        if args.action == "verify":
            verdict = verify(meter, index, on, aliases)
            if args.json:
                print(
                    json.dumps(
                        {
                            "status": verdict.status,
                            "serial": verdict.serial,
                            "aliased": verdict.aliased,
                            "event": verdict.entry["event"] if verdict.entry else None,
                            "differences": verdict.differences,
                            "warnings": verdict.warnings,
                        },
                        indent=2,
                    )
                )
            else:
                print("\n".join(describe(verdict)))
        else:
            if not args.yes and not sys.stdin.isatty():
                # A restore writes the instrument: with no terminal to answer
                # its question it is a usage error, not an EOFError (queue
                # item 44 of 2026-11-20).
                print(
                    "Not restored: the confirmation needs a terminal; pass --yes to answer it.",
                    file=sys.stderr,
                )
                return 2
            ask = (lambda prompt: "y") if args.yes else ask_terminal
            verdict = restore(meter, index, on, ask=ask, aliases=aliases)
    return 0 if verdict.ok else 3


if __name__ == "__main__":
    sys.exit(main())
