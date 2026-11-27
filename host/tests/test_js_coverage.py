# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The page's coverage gate without a browser: its figures, its verdict, its floor file.

``tests/js_coverage.py`` reads what Chromium's precise coverage reports and
``host/js_floors.toml`` holds the figures to floors. Held here: the figures on
planted reports, the verdict on planted floors, the file's shape and, over its
history, that no floor ever fell. The gate itself, over the run in Chromium,
and its self-test on planted dead branches are in ``test_panel_browser.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import js_coverage
import pytest
from js_coverage import Exclusion, Floor, Module

ROOT = Path(__file__).resolve().parents[2]

SOURCE = """\
// µ is two bytes of UTF-8 and one UTF-16 unit; \U0001f50b is two.
export function pick(a) {
    if (a) {
        return "yes";
    }
    return "no"; // the rest
}
"""


def span(text: str, start: str, end: str) -> tuple[int, int]:
    """The UTF-16 offsets from *start* to the end of *end* in *text*, as V8 counts them."""
    first = text.index(start)
    last = text.index(end, first) + len(end)
    return len(text[:first].encode("utf-16-le")) // 2, len(text[:last].encode("utf-16-le")) // 2


def ranges(*spans: tuple[tuple[int, int], int]) -> list[dict[str, int]]:
    return [{"startOffset": a, "endOffset": b, "count": count} for (a, b), count in spans]


WHOLE = (0, len(SOURCE.encode("utf-16-le")) // 2)
PICK = span(SOURCE, "function pick", "the rest\n}")
THEN = span(SOURCE, "{\n        return", "    }")
REST = span(SOURCE, '\n    return "no"', "the rest\n}")


def report(*functions: tuple[str, list[dict[str, int]]], block: bool = True) -> list[Any]:
    """One take's functions of the planted module."""
    return [
        {"functionName": name, "ranges": spans, "isBlockCoverage": block}
        for name, spans in functions
    ]


def taken(then: int, rest: int) -> list[Any]:
    """A take in which ``pick`` ran, its two blocks as often as said."""
    return report(
        ("", ranges((WHOLE, 1))),
        ("pick", ranges((PICK, then + rest), (THEN, then), (REST, rest))),
    )


# -- the figures --------------------------------------------------------------------------


def test_a_module_whose_every_block_ran_is_whole() -> None:
    module = Module(SOURCE)
    module.add(taken(1, 1))
    assert module.functions == (2, 2)
    assert module.blocks[0] == module.blocks[1] > 0
    assert module.uncovered() == []


def test_a_block_no_take_ran_is_listed_by_its_text_and_line() -> None:
    module = Module(SOURCE)
    module.add(taken(0, 3))
    assert module.functions == (2, 2)
    assert module.blocks[1] - module.blocks[0] == 1
    assert [(block.line, block.text) for block in module.uncovered()] == [(3, '{ return "yes"; }')]


def test_blocks_taken_in_different_takes_add_up() -> None:
    module = Module(SOURCE)
    module.add(taken(0, 1))
    module.add(taken(1, 0))
    assert module.uncovered() == []
    assert module.percent("blocks") == 100.0


def test_a_function_never_called_is_counted_and_listed() -> None:
    module = Module(SOURCE)
    module.add(report(("", ranges((WHOLE, 1))), ("pick", ranges((PICK, 0)))))
    assert module.functions == (1, 2)
    assert module.percent("functions") == 50.0
    assert len(module.uncovered()) == 1
    assert module.uncovered()[0].text.startswith("function pick(a) {")


def test_a_call_in_one_take_covers_the_function_in_all() -> None:
    module = Module(SOURCE)
    module.add(report(("", ranges((WHOLE, 1))), ("pick", ranges((PICK, 0)))))
    module.add(taken(1, 1))
    assert module.functions == (2, 2)


def test_a_module_that_was_never_read_is_not_whole() -> None:
    module = Module(SOURCE)
    assert module.functions == (0, 0)
    assert module.percent("functions") == 0.0
    assert module.percent("blocks") == 0.0
    assert [block.line for block in module.uncovered()] == [1]


@pytest.mark.parametrize(
    "rest", ["\n}", " /* note */ }", "\n    // only a comment\n}", ";", "  ", ")", "\n}\n});"]
)
def test_a_range_with_nothing_to_run_is_no_block(rest: str) -> None:
    """V8 ranges what follows a return up to its brace: there is nothing there to run."""
    text = "function f() { return 1;" + rest
    tail = (len("function f() { return 1;"), len(text))
    module = Module(text)
    module.add(report(("f", ranges(((0, len(text)), 1), (tail, 0)))))
    assert module.blocks == (1, 1)
    assert module.uncovered() == []


@pytest.mark.parametrize("rest", ['"//x";', "a / b;", "x();", "/*c*/ y();"])
def test_a_range_with_code_is_a_block_whatever_comments_it_holds(rest: str) -> None:
    module = Module(rest)
    module.add(report(("f", ranges(((0, len(rest)), 0)))))
    assert module.blocks == (0, 1)
    assert len(module.uncovered()) == 1


def test_neighboring_blocks_that_never_ran_are_listed_as_one() -> None:
    text = "a(); b(); c();"
    module = Module(text)
    module.add(report(("f", ranges(((0, 14), 1), ((0, 5), 0), ((5, 10), 0)))))
    assert module.blocks == (1, 3)
    assert [block.text for block in module.uncovered()] == ["a(); b();"]


def test_a_function_counted_without_its_blocks_is_called_and_covers_none_of_them() -> None:
    """After a reload V8 runs what it had compiled and counts only its calls."""
    module = Module(SOURCE)
    module.add(report(("", ranges((WHOLE, 1))), ("pick", ranges((PICK, 4))), block=False))
    assert module.functions == (2, 2)
    assert module.blocks[0] == 0
    module.add(taken(1, 1))
    assert module.uncovered() == []


def test_two_functions_never_called_are_two_though_only_a_comment_is_between() -> None:
    text = "a() { x(); } /* ran */ b() { y(); }"
    second = text.index("b()")
    module = Module(text)
    module.add(report(("", ranges(((0, len(text)), 1), ((0, 12), 0), ((second, len(text)), 0)))))
    assert [block.text for block in module.uncovered()] == ["a() { x(); }", "b() { y(); }"]


def test_a_long_block_is_listed_by_its_first_words() -> None:
    text = "call( " + "x, " * 100 + "y );"
    module = Module(text)
    module.add(report(("f", ranges(((0, len(text)), 0)))))
    shown = module.uncovered()[0].text
    assert len(shown) <= js_coverage.TEXT_LIMIT and shown.startswith("call( x, x,")


def test_a_take_of_another_text_is_refused() -> None:
    module = Module("short")
    with pytest.raises(ValueError, match="ends at 999"):
        module.add(report(("f", ranges(((0, 999), 1)))))


# -- the verdict --------------------------------------------------------------------------

WHOLE_FLOOR = Floor(functions=100.0, blocks=100.0)


def covered(then: int = 1, rest: int = 1) -> dict[str, Module]:
    module = Module(SOURCE)
    module.add(taken(then, rest))
    return {"m.js": module}


def test_a_whole_module_passes_its_floor() -> None:
    assert held_whole(covered(), {"m.js": WHOLE_FLOOR}, []) == ([], [])


def test_a_module_under_its_floor_names_the_figure_and_the_block() -> None:
    problems, notes = held_whole(covered(then=0), {"m.js": WHOLE_FLOOR}, [])
    assert any(p.startswith("m.js: blocks ") and "under its floor of 100 %" in p for p in problems)
    assert 'm.js:3: never ran, and no reason is given: { return "yes"; }' in problems
    assert notes == []


def test_a_block_listed_with_its_reason_still_needs_its_floor() -> None:
    reason = Exclusion("m.js", '{ return "yes"; }', "a reason long enough to say something")
    modules = covered(then=0)
    floor = Floor(functions=100.0, blocks=modules["m.js"].percent("blocks"))
    assert held_whole(modules, {"m.js": floor}, [reason]) == ([], [])
    problems, _ = held_whole(modules, {"m.js": WHOLE_FLOOR}, [reason])
    assert len(problems) == 1 and "under its floor" in problems[0]


def test_a_reason_for_a_block_that_runs_is_stale() -> None:
    stale = Exclusion("m.js", '{ return "yes"; }', "a reason long enough to say something")
    problems, _ = held_whole(covered(), {"m.js": WHOLE_FLOOR}, [stale])
    assert problems == ['m.js: a reason for a block that runs, or is gone: { return "yes"; }']


def test_a_function_under_its_floor_fails() -> None:
    module = Module(SOURCE)
    module.add(report(("", ranges((WHOLE, 1))), ("pick", ranges((PICK, 0)))))
    problems, _ = held_whole({"m.js": module}, {"m.js": Floor(100.0, 0.0)}, [])
    assert "m.js: functions 50.00 % (1 of 2), under its floor of 100 %" in problems


def test_a_figure_over_its_floor_is_a_note_to_raise_it() -> None:
    problems, notes = held_whole(covered(), {"m.js": Floor(90.0, 99.0)}, [])
    assert problems == []
    assert notes == [
        "m.js: functions 100.00 %, over its floor of 90 %: raise it",
        "m.js: blocks 100.00 %, over its floor of 99 %: raise it",
    ]


def test_a_module_without_a_floor_and_a_floor_without_a_module_fail() -> None:
    problems, _ = held_whole(covered(), {"gone.js": WHOLE_FLOOR}, [])
    assert problems == ["m.js: no floor", "gone.js: a floor, and no such module was measured"]


# -- the file -----------------------------------------------------------------------------


def held_whole(*args: Any, **options: Any) -> tuple[list[str], list[str]]:
    """:func:`js_coverage.verdict` with the planted modules held whole, unless told otherwise."""
    options.setdefault("pure", ("m.js", "gone.js"))
    return js_coverage.verdict(*args, **options)


def test_a_drawn_module_is_held_to_its_floor_alone() -> None:
    """A module with a DOM: the blocks it leaves need no reason, and a figure over
    its floor asks nothing; one under it fails as any other."""
    ran = covered(then=0)["m.js"]
    assert held_whole({"m.js": ran}, {"m.js": Floor(0.0, 0.0)}, [], pure=()) == ([], [])
    problems, _ = held_whole({"m.js": ran}, {"m.js": WHOLE_FLOOR}, [], pure=())
    assert problems and all("under its floor" in p for p in problems)


def test_every_module_of_the_page_has_a_floor() -> None:
    floors, _ = js_coverage.load()
    assert sorted(floors) == sorted(js_coverage.MEASURED)
    assert set(js_coverage.PURE) <= set(js_coverage.MEASURED)
    assert all(
        floors[name] == js_coverage.Floor(100.0, 100.0)
        or name not in js_coverage.PURE
        or any(e.module == name for e in js_coverage.load()[1])
        for name in js_coverage.PURE
    )


def test_a_module_under_100_lists_what_it_leaves_out() -> None:
    """A pure module: full marks, or a reason per block, an entry naming a floor under 100
    and the other way. A drawn module may name blocks too, never more than it leaves."""
    floors, exclusions = js_coverage.load()
    pure = {n for n, f in floors.items() if n in js_coverage.PURE and f.blocks < 100}
    assert {e.module for e in exclusions if e.module in js_coverage.PURE} == pure


def test_each_reason_names_text_of_its_module() -> None:
    _, exclusions = js_coverage.load()
    for entry in exclusions:
        source = (js_coverage.STATIC / entry.module).read_text(encoding="utf-8")
        assert entry.block in js_coverage.one_line(source), entry


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ('[floors."a.js"]\nfunctions = "100"\nblocks = 100\n', "is not a percentage"),
        ('[floors."a.js"]\nfunctions = true\nblocks = 100\n', "is not a percentage"),
        ('[floors."a.js"]\nfunctions = 100\nblocks = 100.5\n', "is not a percentage"),
        ('[floors."a.js"]\nfunctions = 100\nblocks = -1\n', "is not a percentage"),
        ('[floors."a.js"]\nfunctions = 100\n', "is not a percentage"),
        ('[[uncovered]]\nmodule = "a.js"\nblock = "x();"\nreason = "no"\n', "gives no reason"),
        ('[[uncovered]]\nmodule = "a.js"\nreason = "a reason long enough"\n', "names no block"),
    ],
)
def test_a_floor_file_that_says_less_is_refused(tmp_path: Path, text: str, why: str) -> None:
    path = tmp_path / "floors.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=why):
        js_coverage.load(path)


def test_a_floor_file_with_a_reason_is_read(tmp_path: Path) -> None:
    path = tmp_path / "floors.toml"
    path.write_text(
        '[floors."a.js"]\nfunctions = 100\nblocks = 99.5\n\n'
        '[[uncovered]]\nmodule = "a.js"\nblock = "x();"\nreason = "nothing calls it with a map"\n',
        encoding="utf-8",
    )
    assert js_coverage.load(path) == (
        {"a.js": Floor(100.0, 99.5)},
        [Exclusion("a.js", "x();", "nothing calls it with a map")],
    )


def _bench_support() -> ModuleType:
    path = ROOT / "tests" / "bench" / "bench_support.py"
    spec = importlib.util.spec_from_file_location("bench_support_for_js_floors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_support_for_js_floors"] = module
    spec.loader.exec_module(module)
    return module


def fallen(old: dict[str, Floor], new: dict[str, Floor]) -> list[str]:
    """The floors lower in *new* than in *old*; a module gone has none."""
    return [
        f"{module}: {kind} {getattr(old[module], kind):g} -> {getattr(new[module], kind):g}"
        for module in sorted(old.keys() & new.keys())
        for kind in ("functions", "blocks")
        if getattr(new[module], kind) < getattr(old[module], kind)
    ]


def test_fallen_finds_a_lowered_floor() -> None:
    old = {"a": Floor(90, 90), "b": Floor(100, 95), "gone": Floor(99, 99)}
    new = {"a": Floor(91, 90), "b": Floor(99.5, 94)}
    assert fallen(old, new) == ["b: functions 100 -> 99.5", "b: blocks 95 -> 94"]


def test_the_floors_only_rise_over_the_files_history(tmp_path: Path) -> None:
    current, _ = js_coverage.load()
    lowered = []
    for commit, text in _bench_support().committed_versions(js_coverage.FLOORS):
        version = tmp_path / "floors.toml"
        version.write_text(text, encoding="utf-8")
        lowered += [
            f"{commit[:10]}: {fall}" for fall in fallen(js_coverage.load(version)[0], current)
        ]
    assert not lowered, "a floor only rises:\n" + "\n".join(lowered)


# -- when the gate may judge --------------------------------------------------------------


class _Item:
    """What :func:`js_coverage.whole_run` reads of a collected test."""

    def __init__(self, name: str, module: ModuleType) -> None:
        self.originalname = name
        self.module = module


def test_the_gate_judges_only_a_run_of_the_whole_module() -> None:
    """A selection covers less by its nature: ``-k``, a node id and ``--lf`` are no gate."""
    module = ModuleType("planted")
    for name in ("test_a", "test_b", "helper"):
        setattr(module, name, lambda: None)
    both = [_Item("test_a", module), _Item("test_b", module)]
    other = _Item("test_a", ModuleType("another"))
    assert js_coverage.whole_run(module, both, [])
    assert js_coverage.whole_run(module, both, [other])
    assert not js_coverage.whole_run(module, both[:1], [])
    assert not js_coverage.whole_run(module, both, [_Item("test_b", module)])
