# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host's mutants stay runnable: every patch applies, and names a kill command that exists.

Each ``host/mutants/*.patch`` is one small wrong change to the host package:
its ``# Mutant`` line says what it breaks, and its ``# Kill:`` line the command
that must fail on a copy of the tree with the patch applied. The command is run
with ``sh -c`` at the copy's root, on the Python that ``PYTHON`` names
(``python3`` by default), and passes on the tree as it is; ``python -m pytest``
puts the copy's ``host/`` first on the path, so it is the copy's package that
runs and not an installed one. What every run holds is that the list has not
rotted: each patch applies, changes ``host/kleidos_meter`` only, names itself
as its file does, and its command names tests of this suite.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "host" / "tests"
PATCHES = sorted((ROOT / "host" / "mutants").glob("*.patch"))

#: The line of a patch's preamble that names it and says what it breaks.
HEADER = re.compile(r"^# Mutant (?P<name>[A-Za-z0-9-]+): (?P<what>.+)\.$", re.MULTILINE)

#: The line that gives its kill command.
KILL = re.compile(r"^# Kill: (?P<command>.+)$", re.MULTILINE)

#: How every kill command starts: this suite, from the copy's own ``host/``.
START = re.compile(
    r"cd host && (?:METER_BROWSER_REQUIRED=1 )?"
    r"\$\{PYTHON:-python3\} -m pytest -q -W error -p no:cacheprovider tests/"
)


def test_there_are_mutants() -> None:
    """A folder that moved would otherwise leave nothing to check."""
    assert len(PATCHES) >= 20


@pytest.mark.parametrize("patch", PATCHES, ids=[patch.stem for patch in PATCHES])
def test_a_mutant_says_what_it_breaks_and_touches_only_the_package(patch: Path) -> None:
    text = patch.read_text(encoding="utf-8")
    header = HEADER.search(text)
    assert header, f"{patch.name}: no '# Mutant <name>: <what it breaks>.' line"
    assert header["name"] == patch.stem, f"{patch.name}: the header names {header['name']}"
    touched = [line.split(" b/", 1)[1] for line in text.splitlines() if line.startswith("diff ")]
    assert touched and all(path.startswith("host/kleidos_meter/") for path in touched), touched


@pytest.mark.parametrize("patch", PATCHES, ids=[patch.stem for patch in PATCHES])
def test_a_mutants_kill_command_names_tests_of_this_suite(patch: Path) -> None:
    commands = KILL.findall(patch.read_text(encoding="utf-8"))
    assert len(commands) == 1, f"{patch.name}: {len(commands)} '# Kill:' lines, one is asked"
    command = commands[0]
    assert START.match(command), command
    words = shlex.split(command)
    target, *selection = words[words.index("no:cacheprovider") + 1 :]
    module, *names = target.split("::")
    source = (ROOT / "host" / module).read_text(encoding="utf-8")
    defined = re.findall(r"^def (test_\w+)", source, re.MULTILINE)
    if selection:
        assert selection[0] == "-k", command
        names = []
        for part in selection[1].split(" or "):
            found = [name for name in defined if part in name]
            assert found, f"{patch.name}: no test of {module} has {part!r} in its name"
            names += found
        # A selection that is the whole module would judge its coverage floors
        # (js_coverage.whole_run) instead of the mutant.
        assert set(names) < set(defined)
    assert set(names) <= set(defined), f"{patch.name}: {names} are not all in {module}"
    # The page's tests skip with no browser, and a skip kills nothing.
    assert ("test_panel_browser.py" in module) == ("METER_BROWSER_REQUIRED=1" in command)


@pytest.mark.parametrize("patch", PATCHES, ids=[patch.stem for patch in PATCHES])
def test_a_mutant_applies_to_the_package_as_it_is(patch: Path) -> None:
    done = subprocess.run(
        ["git", "apply", "--check", str(patch)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (
        f"{patch.name} no longer applies ({done.stderr.strip()}): write it again against "
        "the moved lines, or remove it with the code it broke"
    )
