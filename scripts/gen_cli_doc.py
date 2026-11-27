#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write docs/cli.md, the reference of the command line, from its argparse parsers.

    python3 scripts/gen_cli_doc.py            # write the page
    python3 scripts/gen_cli_doc.py --check    # fail when the page is not what it would write

The commands are ``kleidos_meter.cli.SUBCOMMANDS`` and the command itself; of
each, the synopsis, the options and their defaults are its ``build_parser()``'s
and the exit statuses its ``EXIT_STATUS``. What is written here is what no
parser holds: where to read more, and an example on the simulator with the
output it gave. ``host/tests/test_cli_doc.py`` holds the page to this script,
the examples to what they print, and the statuses to what each ``main`` returns.

Nothing of the environment reaches the page: ``KLEIDOS_METER_PORT`` is taken
out while the parsers are built, and a path is shown from the repository's root.

No third-party module: the host package is imported from the tree.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "host")]

from doc_page import anchor, code, header, run, table  # noqa: E402
from kleidos_meter import cli  # noqa: E402

#: The page.
PAGE = ROOT / "docs" / "cli.md"

#: The variable the port defaults to.
PORT_VARIABLE = "KLEIDOS_METER_PORT"

#: A line of an example's output that stands for the lines left out.
ELIDED = "[...]"


@dataclass(frozen=True)
class Step:
    """One command of an example.

    :ivar line: The command, as typed in the repository's root.
    :ivar output: What it printed, the standard error's lines first.
    :ivar status: What it exited with.
    """

    line: str
    output: tuple[str, ...] = ()
    status: int = 0


@dataclass(frozen=True)
class About:
    """What the page says of a command beyond its parser.

    :ivar more: What it does, and where the rest is.
    :ivar steps: Its example on the simulator, in order.
    :ivar note: What to know of the example.
    """

    more: str
    steps: tuple[Step, ...] = field(default=())
    note: str = ""


#: Each command's text and example; the empty name is the command with no subcommand.
ABOUT: dict[str, About] = {
    "": About(
        "Each message is one argument, its response one line of standard output: the units,"
        " `;` between them, after any block they carried. A response is awaited for every message"
        " and commands have none, so end each message with `;:SYST:ERR:ALL?`, which answers and"
        " says whether anything in it was refused. A stream record that comes meanwhile is printed"
        " behind a `#`. The messages are [scpi.md](scpi.md)'s, and [usage.md](usage.md) says which"
        " to send.",
        (
            Step(
                "kleidos-meter --sim 'MEAS:CURR?;:SYST:ERR:ALL?' 'SENS:PROF NONE;:SYST:ERR:ALL?'",
                (
                    "# the instrument rebooted (*ESR? power-on bit set)",
                    '0.088600000;0,"No error"',
                    '-224,"Illegal parameter value;use SENSe:PROFile TRACe|STATe|SLEep|BURSt"',
                ),
            ),
        ),
        "A refused message is answered like any other: the status says only that an answer came.",
    ),
    "capture": About(
        "The run is a directory of its own, readable by you alone, with what the instrument wrote"
        " and a `meta.json`. The settings the capture changed are written back, and Ctrl-C ends it"
        " early and still finishes the run. [usage.md](usage.md#record-a-capture) says which"
        " profile to take and how to export a run.",
        (
            Step(
                "kleidos-meter capture --sim --profile SLEEP --seconds 1 --out ~/meter-runs",
                (
                    "capture: recording ~/meter-runs/20261124T060534Z-SLEEP-47e42b",
                    "capture: wrote back profile TRACE (it was SLEEP)",
                    'capture: wrote back channels "" (it was "CURR","VOLT","TEMP")',
                    "capture: run 20261124T060534Z-SLEEP-47e42b, ended by seconds, samples=16"
                    " dropped=0, stream=16 marks=0, complete",
                ),
            ),
        ),
    ),
    "serve": About(
        "It is the port's one opener until it is stopped with Ctrl-C (or `SIGTERM`), and puts back"
        " the settings it changed on the way out. [panel.md](panel.md) is how the panel is used"
        " and [http-api.md](http-api.md) what it answers.",
        (
            Step(
                "kleidos-meter serve --sim",
                (
                    "serve: KM-EE529C1B03, firmware 0.18.0, image meter, elf 5e1f0ba47c2291d3,"
                    " q=cal",
                    "serve: reset poweron, up 1.1 s",
                    "serve: panel at http://127.0.0.1:8040/",
                    "serve: panel API at http://127.0.0.1:8040/api/",
                    "serve: recordings in ~/.local/state/kleidos-meter/runs",
                    "serve: holding the port; stop with Ctrl-C",
                    "serve: released the port",
                ),
            ),
        ),
        "The last line is what Ctrl-C printed.",
    ),
    "calibrate": About(
        "It asks for each step at the bench, judges the session by the gates of its configuration"
        " and files it in the registry, accepted or refused. Nothing is committed."
        " [../calibration/README.md](../calibration/README.md) is the procedure: read it first."
        " Without `--registry` the session is filed in this checkout, so a rehearsal names a copy.",
        (
            Step("cp -R calibration ~/meter-registry"),
            Step(
                "kleidos-meter calibrate --sim --yes --registry ~/meter-registry"
                " --configuration mjkdz-r100-relay2click",
                (
                    "Instrument KM-EE529C1B03, firmware 0.18.0, configuration"
                    " mjkdz-r100-relay2click",
                    ELIDED,
                    "  [ok] ranges within 1000 ppm of each other: 0 ppm",
                    "  R = 100.123000 mΩ ± 0.059273 mΩ (k = 2; u_c 296 ppm)",
                    "",
                    "Step 4 of 4. Put the bench back: the VBUS wire to its screw, the cell's + to"
                    " the VBUS screw, the reference out, pigtail 3 back into the DUT.",
                    "Record: ~/meter-registry/records/KM-EE529C1B03/2026-11-24-01.md",
                    "Left to do: commit and push the registry here, then in the Kleidos firmware"
                    " repository",
                    "  python -m qa_lib.power meter-lock",
                ),
            ),
        ),
        "The simulator takes no time over the nulls, and what it measures is its own.",
    ),
    "calibration": About(
        "The registry is the calibration and the instrument holds a working copy of it, which a"
        " full erase loses. `verify` compares the two and changes neither; `restore` writes"
        " the registry's copy and verifies it. Run `verify` before the first number of a session"
        " ([usage.md](usage.md)); what each verdict means is in"
        " [../calibration/README.md](../calibration/README.md).",
        (
            Step(
                "kleidos-meter calibration verify --sim --registry ~/meter-registry",
                (
                    "KM-EE529C1B03: working_copy_lost",
                    "  calibration 2026-11-24-01 (mjkdz-r100-relay2click), valid 2026-11-24 to"
                    " 2027-11-24: range 0 100.123000 mΩ (u 296 ppm), range 1 100.123000 mΩ"
                    " (u 296 ppm)",
                    "  - the instrument holds no measured shunt (its calibration at the defaults)",
                ),
                3,
            ),
            Step(
                "kleidos-meter calibration restore --sim --yes --registry ~/meter-registry",
                (
                    "KM-EE529C1B03: working_copy_lost",
                    "  calibration 2026-11-24-01 (mjkdz-r100-relay2click), valid 2026-11-24 to"
                    " 2027-11-24: range 0 100.123000 mΩ (u 296 ppm), range 1 100.123000 mΩ"
                    " (u 296 ppm)",
                    "  - the instrument holds no measured shunt (its calibration at the defaults)",
                    'Will send: SENS:CURR:SHUN 0.100123; CAL:TAG "R100_20261124";'
                    " CAL:DATE 2026,11,24; CAL:VBUS SOUR",
                    "KM-EE529C1B03: applied",
                    "  calibration 2026-11-24-01 (mjkdz-r100-relay2click), valid 2026-11-24 to"
                    " 2027-11-24: range 0 100.123000 mΩ (u 296 ppm), range 1 100.123000 mΩ"
                    " (u 296 ppm)",
                ),
            ),
        ),
        "The registry is the one [`calibrate`](#kleidos-meter-calibrate) filed above; each command"
        " starts a simulated instrument with nothing stored, so `verify` exits with 3.",
    ),
    "relay-timing": About(
        "It moves the relay under a `TRACE` capture and finds each step in the ring. `k1` takes"
        " the DUT off its battery for `--hold` ms a move, so the DUT needs another supply or must"
        " stand it. What the rig's contacts measured is [relay-timing.md](relay-timing.md).",
        (
            Step(
                "kleidos-meter relay-timing --sim --relay k1 --repeats 1 --hold 20",
                (
                    "k1:closed: n=1 median=-5320 us range=-5320..-5320 us min_contrast=inf",
                    "k1:open: n=1 median=-5320 us range=-5320..-5320 us min_contrast=inf",
                ),
            ),
        ),
        "The simulated DUT draws the same whatever the contact does, so its ring holds no step and"
        " these figures mean nothing; on the bench a delay is positive.",
    ),
    "pins": About(
        "The pin map is which GPIO each of the instrument's five roles takes (the INA228's SDA,"
        " SCL and ALERT, Relay 1 and Relay 2), how each is pulled or driven, the INA228's address"
        " and the bus clock at boot ([scpi.md](scpi.md#the-pin-map)). `show` prints it; `set`"
        " stages a change on the instrument and prints the rules' verdict; `apply` stages the"
        " changes it is given and applies the staged map, which the instrument keeps only once"
        " the INA228 answers on it and puts back otherwise; `defaults` applies the rig's map and"
        " needs no INA228. With `--sim`, `--sim-wiring` says where the simulated board's parts"
        " are, so a map that is not the board's is reverted as the instrument would.",
        (
            Step(
                "kleidos-meter --sim pins show",
                (
                    "pins: the live map, src=default verified=1 table=0x90072FE9 (this package's"
                    " board)",
                    "  SDA      GPIO4   pull up",
                    "  SCL      GPIO5   pull up",
                    "  ALERT    GPIO6   pull up",
                    "  Relay 1  GPIO7   active low",
                    "  Relay 2  GPIO15  active high",
                    "  address  auto (a scan of 0x40 to 0x4F)",
                    "  clock    400000 Hz at boot",
                ),
            ),
            Step(
                "kleidos-meter pins apply sda=18 --sim",
                (
                    '# 117,"Pin map reverted;the INA228 did not answer on sda=18 scl=5 addr=auto:'
                    ' the last good map is back"',
                    "pins: reverted, reason no_chip, INA228 mfg - die -, self-test 1, saved 0",
                    ELIDED,
                ),
                3,
            ),
            Step(
                "kleidos-meter pins apply sda=18 scl=21 --sim --sim-wiring sda=18,scl=21",
                (
                    "pins: ok, reason -, INA228 mfg 0x5449 die 0x228, self-test 0, saved 1",
                    "pins: the live map, src=stored verified=1 table=0x90072FE9 (this package's"
                    " board)",
                    "  SDA      GPIO18  pull up",
                    "  SCL      GPIO21  pull up",
                    ELIDED,
                ),
            ),
        ),
        "Each command starts a simulated instrument with the rig's map: the second finds no INA228"
        " on GPIO18 and keeps the rig's, the third is told the board has it there.",
    ),
}


def command_name(name: str) -> str:
    """A command as it is typed.

    :param name: The subcommand, or the empty name.
    :returns: ``kleidos-meter`` and the subcommand.
    """
    return f"kleidos-meter {name}".strip()


def module_of(name: str, subcommands: Mapping[str, str]) -> ModuleType:
    """The module a command's parser and statuses are in.

    :param name: The subcommand, or the empty name.
    :param subcommands: The subcommands' modules, by name.
    :returns: The module.
    """
    return importlib.import_module(subcommands[name]) if name else cli


def parser_of(module: ModuleType) -> argparse.ArgumentParser:
    """Build a command's parser with no port of this computer's in it.

    :param module: The command's module.
    :returns: The parser.
    """
    port = os.environ.pop(PORT_VARIABLE, None)
    try:
        parser: argparse.ArgumentParser = module.build_parser()
        return parser
    finally:
        if port is not None:
            os.environ[PORT_VARIABLE] = port


def value_form(action: argparse.Action) -> str:
    """What stands for an argument's value in a synopsis.

    :param action: The argument.
    :returns: Its metavariable, or its choices in braces.
    """
    if action.metavar is not None:
        return str(action.metavar)
    if action.choices is not None:
        return "{" + ",".join(map(str, action.choices)) + "}"
    return action.dest.upper() if action.option_strings else action.dest


def form(action: argparse.Action) -> str:
    """An argument as a synopsis writes it, without the brackets of an optional one.

    :param action: The argument.
    :returns: Its option and its value's form.
    """
    value = "" if action.nargs == 0 else value_form(action)
    if action.nargs == "?":
        value = f"[{value}]"
    elif action.nargs == "*":
        value = f"[{value} ...]"
    elif action.nargs == "+":
        value = f"{value} [{value} ...]"
    return " ".join(part for part in (*action.option_strings[:1], value) if part)


def synopsis(parser: argparse.ArgumentParser) -> str:
    """The parser's usage on one line, as argparse orders it: options, then positionals.

    :param parser: The parser.
    :returns: The line.
    """
    options = [action for action in parser._actions if action.option_strings]
    positionals = [action for action in parser._actions if not action.option_strings]
    parts = [parser.prog]
    parts.extend(form(action) if action.required else f"[{form(action)}]" for action in options)
    return " ".join([*parts, *map(form, positionals)])


def wrapped(line: str) -> list[str]:
    """A synopsis on lines of at most 96 columns, never broken inside an argument.

    :param line: The synopsis.
    :returns: Its lines.
    """
    # A space inside an argument is held together while the line is broken.
    held, depth = "", 0
    for at, char in enumerate(line):
        depth += (char == "[") - (char == "]")
        glued = line[:at].split(" ")[-1].startswith("--") and not depth
        held += "\x00" if char == " " and (depth or glued) else char
    lines = textwrap.wrap(
        held, 96, subsequent_indent="    ", break_on_hyphens=False, break_long_words=False
    )
    return [text.replace("\x00", " ") for text in lines]


def default_of(action: argparse.Action) -> str:
    """An argument's default, as the table says it.

    :param action: The argument.
    :returns: It, a path from the repository's root.
    """
    if action.required:
        return "required"
    if action.dest == "port":
        return f"`{PORT_VARIABLE}`"
    if action.nargs == 0:
        return "off"
    if isinstance(action.default, Path):
        path = action.default
        tail = "/" if path.is_dir() else ""
        return f"this checkout's `{path.relative_to(ROOT).as_posix()}{tail}`"
    if action.default is None or action.default == "":
        return "–"
    return f"`{action.default}`"


def says(action: argparse.Action) -> str:
    """What an argument is.

    :param action: The argument.
    :returns: Its help, and whether it may be given again.
    """
    if not action.help:
        raise ValueError(f"{action.dest}: an argument with no help")
    again = "; may be given again" if isinstance(action, argparse._AppendAction) else ""
    return action.help + again


def example_lines(about: About) -> list[str]:
    """A command's example.

    :param about: What the page says of the command.
    :returns: The block and its note.
    """
    lines = ["Example, on the simulator:", "", "```console"]
    for step in about.steps:
        lines += [f"$ {step.line}", *step.output]
    lines += ["```", ""]
    if about.note:
        lines += [about.note, ""]
    return lines


def command_lines(name: str, module: ModuleType, about: About) -> list[str]:
    """One command's section.

    :param name: The subcommand, or the empty name.
    :param module: Its module.
    :param about: What the page says of it.
    :returns: The lines.
    """
    parser = parser_of(module)
    if parser.description is None:
        raise ValueError(f"{name}: a command with no description")
    arguments = [a for a in parser._actions if not isinstance(a, argparse._HelpAction)]
    statuses = [
        [str(number), text.replace("``", "`")] for number, text in module.EXIT_STATUS.items()
    ]
    return [
        f"## `{command_name(name)}`",
        "",
        parser.description,
        "",
        "```text",
        *wrapped(synopsis(parser)),
        "```",
        "",
        about.more,
        "",
        *table(
            ["Argument", "Default", "What it is"],
            [[f"`{form(a)}`", default_of(a), says(a)] for a in arguments],
        ),
        "",
        *table(["Status", "Says"], statuses),
        "",
        *example_lines(about),
    ]


def page(
    subcommands: Mapping[str, str] = cli.SUBCOMMANDS, about: Mapping[str, About] = ABOUT
) -> str:
    """The page's text.

    :param subcommands: The subcommands' modules, by name.
    :param about: What the page says of each command.
    :returns: The text.
    :raises KeyError: For a subcommand *about* says nothing of.
    """
    names = ["", *subcommands]
    modules = {name: module_of(name, subcommands) for name in names}
    summaries = {name: parser_of(modules[name]).description or "" for name in names}
    lines = [
        *header("gen_cli_doc.py", "the parsers of kleidos_meter"),
        "# The command line",
        "",
        "Every command of `kleidos-meter`: what it takes, what it exits with and an example. This"
        " page is written by `scripts/gen_cli_doc.py` from the commands' own parsers, and a test"
        " fails when the two differ; `kleidos-meter --help` and `kleidos-meter SUBCOMMAND --help`"
        " say the same at the terminal. How a measuring session goes is [usage.md](usage.md), and"
        " the same instrument from Python is [python-api.md](python-api.md).",
        "",
        *table(
            ["Command", "Does"],
            [
                [
                    f"[`{command_name(name)}`](#{anchor(command_name(name))})",
                    summaries[name],
                ]
                for name in names
            ],
        ),
        "",
        "## What every command shares",
        "",
        f"- **The port** is `--port`, or the environment's `{PORT_VARIABLE}`: the device node, or"
        " a pyserial URL (`socket://127.0.0.1:5025`, the passthrough of a running `serve`).",
        "- **The port is opened exclusively.** One that another program holds is refused at once;"
        " `serve` and `capture` name the holder when it left a note"
        " ([panel.md](panel.md#start-it)).",
        "- **`--sim` takes the simulated instrument** instead, with nothing plugged in. It speaks"
        " the same protocol and nothing it says is a measurement.",
        "- **Status 1 is also an error no command expects**, its Python traceback on standard"
        " error: a port that does not exist, for one.",
        f"- **{code(['-h', '--help'])}** prints what the command takes and exits with 0.",
        "",
        "An example's output is what the command printed; what a run makes its own (a version, a"
        f" date, a directory, a count of real time) differs in yours, and `{ELIDED}` stands for"
        " lines left out. The examples are run by `host/tests/test_cli_doc.py`, from the"
        " repository's root and in the order of the page.",
        "",
    ]
    for name in names:
        lines += command_lines(name, modules[name], about[name])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Write the page, or check it.

    :param argv: The arguments, ``sys.argv[1:]`` when None.
    :returns: 0, or 1 when ``--check`` finds the page is not the generator's.
    """
    return run(argv, page, PAGE, __doc__.splitlines()[0], "the parsers")


if __name__ == "__main__":
    sys.exit(main())
