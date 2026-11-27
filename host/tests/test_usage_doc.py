# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulator commands of docs/usage.md and README.md run, and print what they show.

A page's code blocks come in two kinds: ``sh`` holds commands alone, and
``console`` holds prompts (``$ ``), each followed by its output. Every command
of either kind that starts with ``kleidos-meter`` and takes ``--sim`` runs
here, as the page gives it, and must exit 0; one in a ``console`` block must
print the lines below it, in order, a version read as any version. ``serve
--sim`` runs until it is stopped, so it is served, asked for its page and
stopped, and must exit 0 too. A command without ``--sim`` is the bench's: it
was run there, and no test opens the instrument.

``~`` and the default data directory are the test's own.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from kleidos_meter.cli import main
from panel_harness import serving

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "usage.md"
README = ROOT / "README.md"

#: A fenced block: its indent (a list item's), its language and its body.
BLOCK = re.compile(r"^( *)```(\w+)\n(.*?)^\1```$", re.MULTILINE | re.DOTALL)

#: What a prompt of a ``console`` block starts with.
PROMPT = "$ "

#: A firmware version, which a release changes and the page does not track.
VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")


@dataclass
class Command:
    """One command of the page.

    :ivar line: The page's line it is on, for a failure's message.
    :ivar argv: Its arguments, the program's name first.
    :ivar output: The lines the page shows below it; None in a block that shows none.
    """

    line: int
    argv: list[str]
    output: list[str] | None = field(default=None)

    @property
    def simulated(self) -> bool:
        """Whether it is ``kleidos-meter`` on the simulated instrument."""
        return self.argv[:1] == ["kleidos-meter"] and "--sim" in self.argv


def commands(text: str) -> list[Command]:
    """The commands of *text*'s ``sh`` and ``console`` blocks, in order."""
    found: list[Command] = []
    for block in BLOCK.finditer(text):
        indent, language, body = block.groups()
        first = text.count("\n", 0, block.start(3)) + 1
        for number, raw in enumerate(body.splitlines(), start=first):
            line = raw.removeprefix(indent)
            if language == "sh" and line.startswith("kleidos-meter "):
                found.append(Command(number, shlex.split(line, comments=True)))
            elif language == "console" and line.startswith(PROMPT):
                found.append(Command(number, shlex.split(line[len(PROMPT) :]), []))
            elif language == "console":
                output = found[-1].output if found else None
                assert output is not None, f"line {number}: output before any prompt"
                output.append(line)
    return found


def _masked(lines: list[str]) -> list[str]:
    """*lines*, each version in them read as the same one."""
    return [VERSION.sub("<version>", line) for line in lines]


SIMULATED = [found for found in commands(PAGE.read_text(encoding="utf-8")) if found.simulated]
IN_README = [found for found in commands(README.read_text(encoding="utf-8")) if found.simulated]


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``~`` and the state directory under the test's own directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _runs_as_shown(command: Command, page: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Run *command* of *page*, which must exit 0 and print what the page shows."""
    arguments = [str(Path(word).expanduser()) if word[0] == "~" else word for word in command.argv]
    assert main(arguments[1:]) == 0, f"{page}:{command.line}"
    printed = capsys.readouterr()
    if command.output is not None:
        # The note of a reboot is the one line on stderr, before any answer.
        shown = printed.err.splitlines() + printed.out.splitlines()
        assert _masked(shown) == _masked(command.output), f"{page}:{command.line}"


@pytest.mark.parametrize(
    "command",
    [found for found in SIMULATED if found.argv[1] != "serve"],
    ids=lambda command: f"line{command.line}",
)
def test_a_simulator_command_of_the_page_runs(
    command: Command, capsys: pytest.CaptureFixture[str]
) -> None:
    _runs_as_shown(command, "docs/usage.md", capsys)


@pytest.mark.parametrize("command", IN_README, ids=lambda command: f"line{command.line}")
def test_a_simulator_command_of_the_readme_runs(
    command: Command, capsys: pytest.CaptureFixture[str]
) -> None:
    _runs_as_shown(command, "README.md", capsys)


def test_the_page_s_serve_runs_and_stops(tmp_path: Path) -> None:
    served = [found for found in SIMULATED if found.argv[1] == "serve"]
    assert [found.argv for found in served] == [["kleidos-meter", "serve", "--sim"]]
    with serving(["--sim", "--data-dir", str(tmp_path / "runs")]) as running:
        assert running.get("/").status == 200
    assert running.status == [0], running.err.getvalue()


def test_the_pages_show_the_simulator_and_name_no_port() -> None:
    assert len(SIMULATED) >= 5
    assert len(IN_README) >= 1
    for page in (PAGE, README):
        for block in BLOCK.finditer(page.read_text(encoding="utf-8")):
            shown = re.search(r"/dev/(?:cu|tty)[.\w]", block.group(3))
            assert not shown, f"{page.name} names a device node in a block"


#: A Markdown image of the panel, ``![alt](…images/panel/<name>.png)``.
PANEL_IMAGE = re.compile(r"!\[([^\]]*)\]\(((?:docs/)?images/panel/[\w-]+\.png)\)")


def panel_images(text: str) -> list[tuple[str, str]]:
    """The images of the panel *text* shows: each one's alt text and target."""
    return PANEL_IMAGE.findall(text)


def test_the_guide_and_the_readme_each_show_the_panel_once() -> None:
    for page in (PAGE, README):
        shown = panel_images(page.read_text(encoding="utf-8"))
        assert len(shown) == 1, f"{page.name} shows {shown}"
        alt, target = shown[0]
        assert alt.strip(), page.name
        assert (page.parent / target).is_file(), target
    usage = PAGE.read_text(encoding="utf-8")
    section = usage[usage.index("\n## The panel, and its exports\n") :]
    assert panel_images(section[: section.index("\n## ", 1)]), (
        "the image belongs to the panel's section"
    )


def test_the_image_reader_sees_one_and_its_alt() -> None:
    assert panel_images("![The Live view](images/panel/live-dark-1280.png)") == [
        ("The Live view", "images/panel/live-dark-1280.png")
    ]
    assert panel_images("![](docs/images/panel/a.png) [link](images/panel/b.png)") == [
        ("", "docs/images/panel/a.png")
    ]


def test_the_reader_finds_both_kinds_of_block() -> None:
    text = "\n".join(
        [
            "```console",
            "$ kleidos-meter --sim '*IDN?' 'SYST:ERR:ALL?'",
            "KLEIDOS",
            '0,"No error"',
            "$ kleidos-meter calibration verify",
            "```",
            "```sh",
            "export A=1",
            "kleidos-meter serve --sim   # until Ctrl-C",
            "```",
            "```text",
            "kleidos-meter --sim '*RST'",
            "```",
            "1. A list item's block, indented with it:",
            "",
            "   ```console",
            "   $ kleidos-meter --sim '*OPC?'",
            "   1",
            "   ```",
            "",
        ]
    )
    assert commands(text) == [
        Command(
            2, ["kleidos-meter", "--sim", "*IDN?", "SYST:ERR:ALL?"], ["KLEIDOS", '0,"No error"']
        ),
        Command(5, ["kleidos-meter", "calibration", "verify"], []),
        Command(9, ["kleidos-meter", "serve", "--sim"]),
        Command(17, ["kleidos-meter", "--sim", "*OPC?"], ["1"]),
    ]
    assert [found.simulated for found in commands(text)] == [True, False, True, True]
    with pytest.raises(AssertionError, match="output before any prompt"):
        commands("```console\nKLEIDOS\n```\n")
    assert _masked(["fw=0.17.0 up"]) == _masked(["fw=1.2.3 up"])
