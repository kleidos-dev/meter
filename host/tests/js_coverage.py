# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""What of the page's modules the browser tests run, as Chromium counts it.

``test_panel_browser.py`` starts V8's precise block coverage in every page it
opens (:meth:`Recorder.start`, the DevTools protocol's
``Profiler.startPreciseCoverage``, through Playwright's own CDP session: no
new dependency), takes it when the page's context closes
(:meth:`Recorder.close`) and adds every take up, module by module
(:class:`Module`). Its last test holds the sums to ``host/js_floors.toml``
(:func:`verdict`), whose floors only rise, as ``host/branch_floors.toml``'s do.

Every module of the page is measured (:data:`MEASURED`). The ones with no DOM
(:data:`PURE`) are held whole: everything in them can be called from a test
through the page's own module, so each block is run or named with the reason
no test can run it. The others draw on the DOM and are held to a floor that
only rises, the blocks they leave listed in the gate's output; since what an
event-driven page runs moves by a few blocks from one run to the next, a
figure over its floor asks nothing of it.

The figures:

- **Functions**: the functions V8 reports, called at least once in any take.
- **Blocks**: V8 gives a range and a count to a function and to each block in
  it that can run another number of times (a branch, a loop's body, the right
  side of ``&&``, ``||`` or ``??``, an arm of ``?:``, what follows a jump).
  The bounds of every range seen cut the source into pieces; a piece with
  something to run in it is a block, covered when any take ran it.

What V8 does not count is not claimed: an ``if`` with no ``else`` has no block
for its false side when nothing jumps, ``?.`` has none, and a block that ran is
not a block whose result was checked (the mutants are for that).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

HOST = Path(__file__).resolve().parents[1]

#: The page's modules, as ``serve`` ships them.
STATIC = HOST / "kleidos_meter" / "panel" / "static" / "js"

#: Where they are served.
SERVED = "/static/js/"

#: The modules with no DOM, held whole.
PURE = ("chart-math.js", "pins-math.js")

#: Every module of the page, measured.
MEASURED = tuple(sorted(path.name for path in STATIC.glob("*.js")))

#: The floors, and the reason of each block a floor under 100 leaves out.
FLOORS = HOST / "js_floors.toml"

#: The most characters a block is shown and listed by.
TEXT_LIMIT = 120

#: The fewest characters that say anything: a shorter reason is none.
REASON_LEAST = 20

#: A comment, or what closes a block: nothing that runs.
_IDLE = re.compile(r"//[^\n]*|/\*.*?\*/|[\s{}();,]", re.DOTALL)


def one_line(text: str) -> str:
    """*text* with every run of spaces and newlines as one space."""
    return " ".join(text.split())


@dataclass(frozen=True)
class Block:
    """A stretch of a module nothing ran: its first line and its text, cut at :data:`TEXT_LIMIT`."""

    line: int
    text: str


class Module:
    """One module's source and what every take together ran of it.

    :param source: Its text, as served. V8 counts its offsets in UTF-16 units.
    """

    def __init__(self, source: str) -> None:
        self._units = source.encode("utf-16-le")
        self._size = len(self._units) // 2
        self._ran = bytearray(self._size)
        self._cuts = {0, self._size}
        self._functions: dict[tuple[int, int], bool] = {}

    def add(self, functions: Iterable[dict[str, Any]]) -> None:
        """Add one take: the ``functions`` of its ``ScriptCoverage``.

        :raises ValueError: When a range ends beyond the source, so it is another text's.
        """
        ran = bytearray(self._size)
        # A function comes after the one it is in, and so does a block: the
        # innermost range of a place is the last that writes it. A function V8
        # had compiled before (a page read again) is counted by its calls
        # alone: which of its blocks ran is not known, so none is taken to have.
        for function in functions:
            for index, span in enumerate(function["ranges"]):
                start, end = span["startOffset"], span["endOffset"]
                if end > self._size:
                    raise ValueError(f"a range ends at {end}, and the module at {self._size}")
                called = span["count"] > 0
                if index == 0:
                    self._functions[start, end] = self._functions.get((start, end), False) or called
                self._cuts.update((start, end))
                counted = called and function["isBlockCoverage"]
                ran[start:end] = bytes([counted]) * (end - start)
        self._ran = bytearray(a | b for a, b in zip(self._ran, ran, strict=True))

    def _text(self, start: int, end: int) -> str:
        return self._units[2 * start : 2 * end].decode("utf-16-le")

    def _pieces(self) -> list[tuple[int, int, bool, bool]]:
        """The source cut at every range's bounds, in order: start, end, whether
        it ran and whether there is anything in it to run."""
        bounds = sorted(self._cuts)
        return [
            (start, end, bool(self._ran[start]), bool(_IDLE.sub("", self._text(start, end))))
            for start, end in pairwise(bounds)
        ]

    @property
    def functions(self) -> tuple[int, int]:
        """How many functions were called, and how many there are."""
        return sum(self._functions.values()), len(self._functions)

    @property
    def blocks(self) -> tuple[int, int]:
        """How many blocks ran, and how many there are."""
        if not self._functions:
            return 0, 0
        ran = [ran for _, _, ran, code in self._pieces() if code]
        return sum(ran), len(ran)

    def percent(self, kind: str) -> float:
        """The share of its ``functions`` or ``blocks`` that ran, 0 of a module never read."""
        ran, total = getattr(self, kind)
        return 100.0 * ran / total if total else 0.0

    def uncovered(self) -> list[Block]:
        """What never ran, neighboring blocks as one."""
        found: list[list[int]] = []
        joined = False
        for start, end, ran, code in self._pieces():
            if code and not ran and joined:
                found[-1][1] = end
            elif code and not ran:
                found.append([start, end])
            joined = not ran and (code or joined)
        return [self._block(start, end) for start, end in found]

    def _block(self, start: int, end: int) -> Block:
        text = self._text(start, end)
        line = (
            1
            + self._text(0, start).count("\n")
            + text[: len(text) - len(text.lstrip())].count("\n")
        )
        return Block(line, one_line(text)[:TEXT_LIMIT].rstrip())


class Recorder:
    """The coverage of the pages a run opened.

    :param sources: The text each module is served as, by its name.
    """

    def __init__(self, sources: dict[str, str]) -> None:
        self.modules = {name: Module(text) for name, text in sources.items()}
        self._sessions: list[tuple[Any, Any]] = []

    @classmethod
    def of_the_package(cls) -> Recorder:
        """A recorder of :data:`MEASURED` as the package ships them."""
        return cls({name: (STATIC / name).read_text(encoding="utf-8") for name in MEASURED})

    def start(self, page: Any) -> None:
        """Count what *page* runs, from its next navigation on."""
        session = page.context.new_cdp_session(page)
        session.send("Profiler.enable")
        session.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
        self._sessions.append((page.context, session))

    def close(self, context: Any) -> None:
        """Add up what *context*'s pages ran, and close it."""
        for opened, session in [entry for entry in self._sessions if entry[0] is context]:
            self._sessions.remove((opened, session))
            self._take(session)
        context.close()

    def _take(self, session: Any) -> None:
        for script in session.send("Profiler.takePreciseCoverage")["result"]:
            path = urlsplit(script["url"]).path
            name = path.removeprefix(SERVED)
            if path.startswith(SERVED) and name in self.modules:
                self.modules[name].add(script["functions"])


@dataclass(frozen=True)
class Floor:
    """The least percentage of a module's functions and blocks the tests may run."""

    functions: float
    blocks: float


@dataclass(frozen=True)
class Exclusion:
    """A block that never runs, by its text as :class:`Block` gives it, and why."""

    module: str
    block: str
    reason: str


def load(path: Path = FLOORS) -> tuple[dict[str, Floor], list[Exclusion]]:
    """The floor file: each module's floors and the reasons.

    :raises ValueError: A floor that is no percentage, an entry that names no
        block or gives no reason.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    floors = {}
    for module, figures in data.get("floors", {}).items():
        for kind in ("functions", "blocks"):
            value = figures.get(kind)
            number = isinstance(value, int | float) and not isinstance(value, bool)
            if not number or not 0 <= value <= 100:
                raise ValueError(f"{path.name}: {module}: {kind} {value!r} is not a percentage")
        floors[module] = Floor(float(figures["functions"]), float(figures["blocks"]))
    exclusions = []
    for entry in data.get("uncovered", []):
        module, block = entry.get("module", ""), entry.get("block", "")
        reason = entry.get("reason", "")
        if not module or not block:
            raise ValueError(f"{path.name}: {entry!r} names no block")
        if len(reason) < REASON_LEAST:
            raise ValueError(f"{path.name}: {module}: {block!r} gives no reason")
        exclusions.append(Exclusion(module, block, reason))
    return floors, exclusions


def verdict(
    modules: dict[str, Module],
    floors: dict[str, Floor],
    exclusions: list[Exclusion],
    pure: Iterable[str] = PURE,
) -> tuple[list[str], list[str]]:
    """What fails the floors, and what is worth raising.

    :param modules: What the run measured.
    :param floors: :func:`load`'s floors.
    :param exclusions: Its reasons.
    :param pure: The modules held whole, :data:`PURE` by default.
    :returns: The problems (a figure under its floor, a block of a
        :data:`PURE` module that never ran with no reason, a reason for one
        that ran, a module with no floor, a floor whose module is gone) and the
        notes (a pure module's figure over its floor).
    """
    problems = [f"{name}: no floor" for name in sorted(modules.keys() - floors.keys())]
    problems += [
        f"{name}: a floor, and no such module was measured"
        for name in sorted(floors.keys() - modules.keys())
    ]
    notes = []
    for name in sorted(modules.keys() & floors.keys()):
        module = modules[name]
        whole = name in pure
        for kind in ("functions", "blocks"):
            figure, floor = module.percent(kind), getattr(floors[name], kind)
            said = f"{name}: {kind} {figure:.2f} %"
            if figure < floor:
                ran, total = getattr(module, kind)
                problems.append(f"{said} ({ran} of {total}), under its floor of {floor:g} %")
            elif whole and figure > floor:
                notes.append(f"{said}, over its floor of {floor:g} %: raise it")
        reasons = {entry.block for entry in exclusions if entry.module == name}
        left = module.uncovered()
        problems += [
            f"{name}:{block.line}: never ran, and no reason is given: {block.text}"
            for block in left
            if whole and block.text not in reasons
        ]
        problems += [
            f"{name}: a reason for a block that runs, or is gone: {text}"
            for text in sorted(reasons - {block.text for block in left})
        ]
    return problems, notes


def whole_run(module: ModuleType, items: Iterable[Any], deselected: Iterable[Any]) -> bool:
    """Whether a run has every test of *module*: a selection of them covers less by its nature.

    :param items: The tests the run collected and kept.
    :param deselected: The ones it left out (``-k``, ``--lf``).
    """
    kept = {item.originalname for item in items if item.module is module}
    tests = {name for name in vars(module) if name.startswith("test_")}
    return tests <= kept and not any(item.module is module for item in deselected)
