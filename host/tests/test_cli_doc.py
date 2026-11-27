# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/cli.md is the parsers', its statuses are what each ``main`` returns, its examples run.

``docs/cli.md`` is written by ``scripts/gen_cli_doc.py`` from the parsers of
``kleidos_meter.cli`` and its subcommands: the page must be what the generator
writes now, and a command, an option or a status more must write another. Every
synopsis is argparse's own usage. Every example is run on the simulator, in the
page's order, its registry a copy: it must exit with the status the generator
states and print what the page shows, what a run makes its own (a version, a
date, a port, a path, a count of real time) read as any other and ``[...]`` as
any lines.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import shlex
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from kleidos_meter import cli
from panel_harness import serving
from test_usage_doc import commands

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "cli.md"
GENERATOR = ROOT / "scripts" / "gen_cli_doc.py"

#: What differs from one run to the next in what is printed, and what it is read as.
OWN = (
    (re.compile(r"127\.0\.0\.1:\d+"), "<address>"),
    (re.compile(r"\b\d+\.\d+\.\d+\b(?!\.)"), "<version>"),
    (re.compile(r"\d{8}T\d{6}Z-([A-Z]+)-[0-9a-f]{6}"), r"<run>-\1"),
    (re.compile(r"\b20\d\d-\d\d-\d\d\b"), "<date>"),
    (re.compile(r"R100_20\d{6}"), "R100_<date>"),
    (re.compile(r"CAL:DATE 20\d\d,\d+,\d+"), "CAL:DATE <date>"),
    (re.compile(r"\b(samples|stream)=\d+"), r"\1=<count>"),
    (re.compile(r"up \d+\.\d s"), "up <seconds>"),
    (re.compile(r"recordings in .*"), "recordings in <directory>"),
)


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    """The generator, imported from ``scripts/``."""
    spec = importlib.util.spec_from_file_location("gen_cli_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_cli_doc"] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


# -- the page is the generator's -----------------------------------------------------------


def test_the_page_is_what_the_parsers_give(generator: ModuleType) -> None:
    assert _text() == generator.page(), "docs/cli.md is stale: python3 scripts/gen_cli_doc.py"
    assert generator.main(["--check"]) == 0


def test_the_page_has_every_subcommand_option_and_status(generator: ModuleType) -> None:
    text = _text()
    for name in ("", *cli.SUBCOMMANDS):
        title = f"kleidos-meter {name}".strip()
        section = text[text.index(f"\n## `{title}`\n") :]
        section = section[: section.index("\n## ", 1)] if "\n## " in section[1:] else section
        module = generator.module_of(name, cli.SUBCOMMANDS)
        for action in module.build_parser()._actions:
            if not isinstance(action, argparse._HelpAction):
                assert f"| `{generator.form(action)}` |" in section, (title, action.dest)
        for status in module.EXIT_STATUS:
            assert f"| {status} | " in section, (title, status)
        assert "--sim" in section[section.index("```console") :], title


def test_another_command_option_or_status_gives_another_page(
    generator: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    planted = ModuleType("planted")
    parser = argparse.ArgumentParser(prog="kleidos-meter planted", description="Planted.")
    parser.add_argument("what", nargs="?", help="a word", choices=["a", "b"])
    parser.add_argument("--level", type=int, default=3, help="a|b", action="append")
    planted.build_parser = lambda: parser  # type: ignore[attr-defined]  # a planted module
    planted.EXIT_STATUS = {0: "``done``"}  # type: ignore[attr-defined]  # a planted module
    sys.modules["planted"] = planted
    try:
        with pytest.raises(KeyError):
            generator.page({**cli.SUBCOMMANDS, "planted": "planted"})
        about = {
            **generator.ABOUT,
            "planted": generator.About("More.", (generator.Step("kleidos-meter planted"),)),
        }
        text = generator.page({**cli.SUBCOMMANDS, "planted": "planted"}, about)
    finally:
        del sys.modules["planted"]
    section = text[text.index("\n## `kleidos-meter planted`\n") :]
    assert "kleidos-meter planted [-h] [--level LEVEL] [{a,b}]\n" in section
    assert "| `--level LEVEL` | `3` | a\\|b; may be given again |" in section
    assert "| `[{a,b}]` | – | a word |" in section
    assert "| 0 | `done` |" in section
    assert "\n$ kleidos-meter planted\n```\n" in section
    assert "| [`kleidos-meter planted`](#kleidos-meter-planted) | Planted. |" in text

    stale = tmp_path / "cli.md"
    assert generator.main(["--check", "--out", str(stale)]) == 1  # no page yet
    assert generator.main(["--out", str(stale)]) == 0
    assert generator.main(["--check", "--out", str(stale)]) == 0
    stale.write_text(stale.read_text(encoding="utf-8").replace("`--sim`", "`--simulated`", 1))
    assert generator.main(["--check", "--out", str(stale)]) == 1
    assert "run python3 scripts/gen_cli_doc.py" in capsys.readouterr().err


def test_the_page_holds_nothing_of_the_environment(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KLEIDOS_METER_PORT", "/dev/a-port-of-this-computer")
    assert generator.page() == _text()
    assert cli.build_parser().get_default("port") == "/dev/a-port-of-this-computer"
    assert not re.search(r"/dev/(?:cu|tty)[.\w]|/Users/|/home/", _text())


@pytest.mark.parametrize("name", ["", *cli.SUBCOMMANDS])
def test_a_synopsis_is_argparse_s_own_usage(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("COLUMNS", "400")
    parser = generator.parser_of(generator.module_of(name, cli.SUBCOMMANDS))
    usage = " ".join(parser.format_usage().split()).removeprefix("usage: ")
    assert generator.synopsis(parser) == usage
    lines = generator.wrapped(usage)
    assert all(len(line) <= 96 for line in lines)
    assert " ".join(line.strip() for line in lines) == usage


# -- the exit statuses ---------------------------------------------------------------------


def _kept(node: ast.AST) -> ast.expr | None:
    """What a node of ``main`` returns, or keeps as its status; None for any other node."""
    if isinstance(node, ast.Return):
        return node.value
    if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "status":
        return node.value
    return None


def _returned(module: ModuleType) -> set[int]:
    """The statuses a module's ``main`` can return: every integer it returns or keeps."""
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    values = [value for value in map(_kept, ast.walk(main)) if value is not None]
    found: set[int] = set()
    for value in values:
        ends = [value.body, value.orelse] if isinstance(value, ast.IfExp) else [value]
        found |= {
            end.value for end in ends if isinstance(end, ast.Constant) and type(end.value) is int
        }
    return found


@pytest.mark.parametrize("name", ["", *cli.SUBCOMMANDS])
def test_every_status_returned_is_stated(generator: ModuleType, name: str) -> None:
    module = generator.module_of(name, cli.SUBCOMMANDS)
    returned = _returned(module)
    assert 0 in returned
    # 1 is also a traceback's and 2 also argparse's, which no return shows.
    assert returned <= set(module.EXIT_STATUS) == returned | {1, 2}


def test_every_subcommand_has_a_main_a_parser_and_statuses(generator: ModuleType) -> None:
    for name in cli.SUBCOMMANDS:
        module = generator.module_of(name, cli.SUBCOMMANDS)
        assert callable(module.main) and module.build_parser().prog == f"kleidos-meter {name}"
    assert generator.module_of("", cli.SUBCOMMANDS).build_parser().prog == "kleidos-meter"


def test_the_help_names_every_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    printed = " ".join(capsys.readouterr().out.split())
    assert ", ".join(cli.SUBCOMMANDS) in printed


# -- the examples --------------------------------------------------------------------------


def _read(lines: list[str], home: Path) -> list[str]:
    """*lines* with what a run makes its own read as any other."""
    read = []
    for line in lines:
        text = line.replace(str(home), "~")
        for pattern, name in OWN:
            text = pattern.sub(name, text)
        read.append(text)
    return read


def _as_shown(printed: list[str], shown: list[str], elided: str) -> bool:
    """Whether *printed* is what *shown* shows, ``[...]`` standing for any lines."""
    if elided not in shown:
        return printed == shown
    at = shown.index(elided)
    head, tail = shown[:at], shown[at + 1 :]
    return (
        len(printed) >= len(head) + len(tail)
        and printed[:at] == head
        and printed[len(printed) - len(tail) :] == tail
    )


def test_the_examples_run_and_print_what_the_page_shows(
    generator: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ran = 0
    for about in generator.ABOUT.values():
        for step in about.steps:
            argv = [
                str(tmp_path / word[2:]) if word.startswith("~/") else word
                for word in shlex.split(step.line)
            ]
            if argv[0] == "cp":
                assert argv[:2] == ["cp", "-R"] and not step.output
                shutil.copytree(ROOT / argv[2], argv[3])
                continue
            assert argv[0] == "kleidos-meter" and "--sim" in argv, step.line
            if argv[1] == "serve":
                with serving(argv[2:]) as running:
                    assert running.get("/").status == 200
                status, printed = running.status[0], running.out.getvalue().splitlines()
            else:
                status = cli.main(argv[1:])
                captured = capsys.readouterr()
                printed = captured.err.splitlines() + captured.out.splitlines()
            assert status == step.status, step.line
            assert _as_shown(
                _read(printed, tmp_path), _read(list(step.output), tmp_path), generator.ELIDED
            ), (step.line, printed)
            ran += 1
    assert ran >= len(cli.SUBCOMMANDS) + 1


def test_the_examples_of_the_page_are_the_ones_run(generator: ModuleType) -> None:
    steps = [step for about in generator.ABOUT.values() for step in about.steps]
    shown = [found for found in commands(_text()) if found.output is not None]
    assert [found.argv for found in shown] == [shlex.split(step.line) for step in steps]
    assert [found.output for found in shown] == [list(step.output) for step in steps]


def test_the_reader_tells_what_is_shown_from_what_is_not() -> None:
    assert _as_shown(["a", "b"], ["a", "b"], "[...]")
    assert not _as_shown(["a", "c"], ["a", "b"], "[...]")
    assert _as_shown(["a", "b", "c", "d"], ["a", "[...]", "d"], "[...]")
    assert _as_shown(["a", "d"], ["a", "[...]", "d"], "[...]")
    assert not _as_shown(["a"], ["a", "[...]", "a"], "[...]")
    assert not _as_shown(["a", "b", "e"], ["a", "[...]", "d"], "[...]")
    home = Path("/somewhere/else")
    assert _read(["/somewhere/else/meter-runs/20261124T060534Z-SLEEP-47e42b"], home) == [
        "~/meter-runs/<run>-SLEEP"
    ]
    assert _read(["samples=16 dropped=0, stream=16 marks=0"], home) == [
        "samples=<count> dropped=0, stream=<count> marks=0"
    ]
    assert _read(["R = 100.123000 mΩ", "firmware 0.17.0"], home) == [
        "R = 100.123000 mΩ",
        "firmware <version>",
    ]
