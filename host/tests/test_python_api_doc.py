# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/python-api.md is the docstrings', and every public name of the package has one.

``docs/python-api.md`` is written by ``scripts/gen_python_api_doc.py`` from the
sources: the page must be what the generator writes now, a change to a docstring
or a signature must write another, and every module is on the page or left out
for a stated reason.

The convention is ``host/README.md``'s, "Docstrings": every public module,
class, function and method of the package has a docstring; in the modules of
the page every parameter, every value returned and every field of a dataclass
has its field too, every signature its hints, and no field names what is not
there.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import kleidos_meter
import pytest
from kleidos_meter import Meter

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "python-api.md"
GENERATOR = ROOT / "scripts" / "gen_python_api_doc.py"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    """The generator, imported from ``scripts/``."""
    spec = importlib.util.spec_from_file_location("gen_python_api_doc", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_python_api_doc"] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


# -- the page is the generator's -----------------------------------------------------------


def test_the_page_is_what_the_docstrings_give(generator: ModuleType) -> None:
    assert _text() == generator.page(), (
        "docs/python-api.md is stale: python3 scripts/gen_python_api_doc.py"
    )
    assert generator.main(["--check"]) == 0


def test_the_page_has_every_public_method_of_meter_and_every_export() -> None:
    text = _text()
    methods = [name for name in vars(Meter) if not name.startswith("_")]
    assert len(methods) >= 30
    for name in methods:
        assert f"\n#### `Meter.{name}`\n" in text, name
    for name in kleidos_meter.__all__:
        assert f"\n### `{name}`\n" in text, name
        assert f"\n- [`{name}`](#{name.lower()}), of `" in text, name


def test_every_module_is_on_the_page_or_left_out_for_a_reason(generator: ModuleType) -> None:
    names = generator.module_names()
    assert "client" in names and "panel" in names
    assert generator.unplaced(names) == []
    assert generator.unplaced([*names, "planted"]) == ["planted"]
    assert generator.unplaced([name for name in names if name != "client"]) == ["client"]


def test_another_docstring_or_signature_gives_another_page(
    generator: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = generator.read_sources()
    for old, new in (
        ("``*TST?``: the instrument's self-test.", "``*TST?``: planted."),
        ("def set_range_index(self, index: int)", "def set_range_index(self, at: int)"),
        (
            "    def self_test(",
            '    def planted(self) -> None:\n        """P."""\n\n    def self_test(',
        ),
    ):
        assert old in sources["client"]
        changed = {**sources, "client": sources["client"].replace(old, new)}
        assert generator.page(changed) != generator.page(sources), old
    undocumented = {**sources, "identity": "def planted() -> None:\n    return None\n"}
    with pytest.raises(ValueError, match="planted has no docstring"):
        generator.page(undocumented)
    twice = {**sources, "identity": 'class Meter:\n    """Another."""\n'}
    with pytest.raises(ValueError, match="two entries named Meter"):
        generator.page(twice)

    stale = tmp_path / "python-api.md"
    assert generator.main(["--check", "--out", str(stale)]) == 1  # no page yet
    assert generator.main(["--out", str(stale)]) == 0
    assert generator.main(["--check", "--out", str(stale)]) == 0
    stale.write_text(stale.read_text(encoding="utf-8").replace("Meter.stop", "Meter.halt"))
    assert generator.main(["--check", "--out", str(stale)]) == 1
    assert "run python3 scripts/gen_python_api_doc.py" in capsys.readouterr().err


PLANTED = '''
"""Planted."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Row:
    """A row: ``a<b`` and c<d.

    It is :class:`~kleidos_meter.client.Meter`'s, or :data:`NOTHING`'s:

    - one, over
      two lines;
    - another of :meth:`size`.

    Usage::

        row = Row(1)
        print(row)

    :ivar at: Where it is,
        in samples.
    """

    at: int
    names: list[str] = field(default_factory=list)
    line: str = field(default="", compare=False)
    _hidden: int = field(init=False)

    @property
    def size(self) -> int:
        """Its size."""

    @classmethod
    def of(cls, text: str, *rest: int, strict: bool = True, **more: str) -> "Row":
        """Read one.

        :param text: The text.
        :returns: The row.
        :raises ValueError: When it is none.
        """

    def _private(self) -> None:
        pass


class Holder:
    """Holds.

    :param size: How many.
    :ivar held: What it holds.
    """

    def __init__(self, size=3) -> None:
        self.held = size


def main() -> int:
    """A command line."""
'''


def test_what_a_source_gives_is_written_as_markdown(generator: ModuleType) -> None:
    names = dict.fromkeys(generator.MODULES, '"""M."""\n')
    meter = 'class Meter:\n    """The instrument."""\n'
    sources = {
        **names,
        "__init__": "__all__ = ['Row']\n",
        "client": meter,
        "protocol": PLANTED.strip(),
    }
    text = generator.page(sources)
    section = text[text.index("### `Row`") : text.index("## `kleidos_meter.sim`")]
    assert section == "\n".join(
        [
            "### `Row`",
            "",
            "```python",
            "class Row(at: int, names: list[str] = [], line: str = '')",
            "```",
            "",
            "A row: `a<b` and c\\<d.",
            "",
            "It is [`Meter`](#meter)'s, or `NOTHING`'s:",
            "",
            "- one, over two lines;",
            "- another of [`size`](#rowsize).",
            "",
            "Usage:",
            "",
            "```text",
            "row = Row(1)",
            "print(row)",
            "```",
            "",
            "- `at`: Where it is, in samples.",
            "",
            "#### `Row.size`",
            "",
            "```python",
            "Row.size -> int",
            "```",
            "",
            "Its size.",
            "",
            "#### `Row.of`",
            "",
            "```python",
            "Row.of(text: str, *rest: int, strict: bool = True, **more: str) -> 'Row'",
            "```",
            "",
            "Read one.",
            "",
            "- `text`: The text.",
            "- returns: The row.",
            "- raises `ValueError`: When it is none.",
            "",
            "### `Holder`",
            "",
            "```python",
            "class Holder(size=3)",
            "```",
            "",
            "Holds.",
            "",
            "- `size`: How many.",
            "- attribute `held`: What it holds.",
            "",
            "",
        ]
    )
    assert "- [`Row`](#row), of `protocol`" in text


# -- the docstrings ------------------------------------------------------------------------


def _public(node: ast.Module | ast.ClassDef, prefix: str = "") -> Iterator[tuple[str, ast.AST]]:
    """The public classes, functions and methods below *node*, and their names."""
    for item in node.body:
        if not isinstance(item, ast.ClassDef | ast.FunctionDef) or item.name.startswith("_"):
            continue
        decorators = [ast.unparse(d) for d in item.decorator_list]
        if any(name.endswith(".setter") for name in decorators):
            continue
        yield prefix + item.name, item
        if isinstance(item, ast.ClassDef):
            yield from _public(item, f"{prefix}{item.name}.")


def _arguments(node: ast.FunctionDef, *, bound: bool) -> list[ast.arg]:
    """A function's parameters, ``self`` or ``cls`` left out."""
    args = [*node.args.posonlyargs, *node.args.args]
    extra = [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
    return [*(args[1:] if bound else args), *node.args.kwonlyargs, *extra]


def _missing(source: str, *, fields: bool) -> list[str]:
    """What *source*'s docstrings lack.

    :param source: A module's source.
    :param fields: Whether the fields and the hints are required too, as in a module of
        the page.
    :returns: A line for each thing missing, or named and not there.
    """
    tree = ast.parse(source)
    found = [] if ast.get_docstring(tree) else ["the module has no docstring"]
    for name, node in _public(tree):
        text = ast.get_docstring(node) or ""  # type: ignore[arg-type]  # a documented node
        if not text:
            found.append(f"{name} has no docstring")
        if not fields or not text:
            continue
        named = set(re.findall(r"^:param (\w+):", text, re.MULTILINE))
        attributes = set(re.findall(r"^:ivar (\w+):", text, re.MULTILINE))
        if isinstance(node, ast.ClassDef):
            init = [i for i in node.body if getattr(i, "name", "") == "__init__"]
            if init and isinstance(init[0], ast.FunctionDef):
                wanted = {arg.arg for arg in _arguments(init[0], bound=True)}
                hinted = all(arg.annotation for arg in _arguments(init[0], bound=True))
            else:
                wanted = {
                    i.target.id
                    for i in node.body
                    if isinstance(i, ast.AnnAssign)
                    and isinstance(i.target, ast.Name)
                    and not i.target.id.startswith("_")
                }
                hinted, named = True, named | attributes
        elif isinstance(node, ast.FunctionDef):
            wanted = {arg.arg for arg in _arguments(node, bound="." in name)}
            hinted = node.returns is not None and all(
                arg.annotation for arg in _arguments(node, bound="." in name)
            )
            read = any(ast.unparse(d) == "property" for d in node.decorator_list)
            gives = node.returns is not None and ast.unparse(node.returns) != "None"
            if gives and not read and not re.search(r"^:returns:", text, re.MULTILINE):
                found.append(f"{name} does not say what it returns")
            if not gives and re.search(r"^:returns:", text, re.MULTILINE):
                found.append(f"{name} says it returns something")
        else:
            continue
        found += [f"{name}: {arg} has no field" for arg in sorted(wanted - named)]
        found += [f"{name}: {arg} is not a parameter" for arg in sorted(named - wanted)]
        if not hinted:
            found.append(f"{name} has no hint")
    return found


def test_every_public_name_of_the_package_has_a_docstring(generator: ModuleType) -> None:
    package = generator.PACKAGE
    found = []
    for path in sorted(package.rglob("*.py")):
        on_page = path.parent == package and path.stem in generator.MODULES
        found += [
            f"{path.relative_to(package)}: {line}"
            for line in _missing(path.read_text(encoding="utf-8"), fields=on_page)
        ]
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    ("source", "lacks"),
    [
        ("x = 1\n", "the module has no docstring"),
        ('"""M."""\ndef f():\n    pass\n', "f has no docstring"),
        ('"""M."""\nclass C:\n    """C."""\n    def m(self): ...\n', "C.m has no docstring"),
        ('"""M."""\ndef f(a: int) -> None:\n    """F."""\n', "f: a has no field"),
        (
            '"""M."""\ndef f() -> None:\n    """F.\n\n    :param a: A.\n    """\n',
            "f: a is not a parameter",
        ),
        ('"""M."""\ndef f() -> int:\n    """F."""\n', "f does not say what it returns"),
        (
            '"""M."""\ndef f() -> None:\n    """F.\n\n    :returns: A.\n    """\n',
            "f says it returns something",
        ),
        ('"""M."""\ndef f(a) -> None:\n    """F.\n\n    :param a: A.\n    """\n', "f has no hint"),
        ('"""M."""\nclass C:\n    """C."""\n    a: int\n', "C: a has no field"),
        (
            '"""M."""\nclass C:\n    """C."""\n    def __init__(self, a: int) -> None: ...\n',
            "C: a has no field",
        ),
    ],
)
def test_the_guard_sees_what_is_missing(source: str, lacks: str) -> None:
    assert lacks in _missing(source, fields=True)


COMPLETE = '''
"""M."""


class C:
    """C.

    :ivar a: A.
    """

    a: int
    _b: int = 1

    @property
    def p(self) -> int:
        """P."""

    @p.setter
    def p(self, value: int) -> None: ...

    def _private(self): ...
'''


def test_the_guard_takes_what_the_convention_asks() -> None:
    assert _missing(COMPLETE.strip(), fields=True) == []
    undone = '"""M."""\ndef f(a):\n    """F."""\n'
    assert _missing(undone, fields=False) == [] and _missing(undone, fields=True)
