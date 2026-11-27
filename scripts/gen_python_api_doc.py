#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write docs/python-api.md, the reference of the host package, from its docstrings.

    python3 scripts/gen_python_api_doc.py            # write the page
    python3 scripts/gen_python_api_doc.py --check    # fail when the page is not what it would write

Every public class, method and function of the modules in ``MODULES`` is on the
page, with the signature its source gives and its docstring (the convention is
``host/README.md``'s). The sources are parsed, not imported, so a default is
shown as it is written. A module of the package is in ``MODULES`` or in
``ELSEWHERE``, which says where its reference is instead; one in neither fails.
``host/tests/test_python_api_doc.py`` holds the page to this script.

No third-party module.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from doc_page import anchor, header, run, table  # noqa: E402

#: The page.
PAGE = ROOT / "docs" / "python-api.md"

#: The package.
PACKAGE = ROOT / "host" / "kleidos_meter"

#: The modules on the page, in its order, and what each is for.
MODULES = {
    "client": "the instrument on its port",
    "protocol": "the wire format, with no I/O",
    "sim": "a simulated instrument",
    "pins": "the pin map, its rules and a board's wiring",
    "identity": "which instrument it is",
    "registry": "the calibration registry",
    "calibration_cli": "an instrument against the registry",
    "calibrate": "the guided calibration",
    "relay_timing": "the relays' times",
}

#: The modules left out, and where what they are for is documented. Every module
#: of a package in it is left out with it.
ELSEWHERE = {
    "__init__": "the exports, first on the page",
    "cli": "the command line: docs/cli.md",
    "capture_cli": "the command line: docs/cli.md",
    "pins_cli": "the command line: docs/cli.md",
    "panel": "the panel's process: docs/http-api.md is its API, host/README.md how it works",
    "sim_table": "the simulator's copy of the firmware's command table",
    "invariant": "the package's own checks of what its code guarantees: internal",
}

#: The functions left out wherever they are: a tool's command line (docs/cli.md).
COMMAND_LINE = frozenset({"main", "build_parser"})

#: A cross-reference role: ``:meth:`~package.module.Class.method```.
ROLE = re.compile(r":(?:attr|class|data|exc|func|meth|mod):`~?([^`]+)`")

#: A field of a docstring: ``:param name: text``.
FIELD = re.compile(r":(param|returns|raises|ivar)(?: ([^:]+))?:(?: (.*))?$")

#: A list item's marker.
ITEM = re.compile(r"(?:[-*]|\d+\.) ")


@dataclass
class Docstring:
    """A docstring, taken apart.

    :ivar body: Its text before the fields, as lines of Markdown.
    :ivar fields: Its fields: the kind, the name (empty for ``returns``) and the text.
    """

    body: list[str] = field(default_factory=list)
    fields: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class Entry:
    """A class, a method or a function of the page.

    :ivar name: Its name, a method's behind its class's.
    :ivar node: Its definition.
    :ivar methods: A class's methods and properties, in the source's order.
    """

    name: str
    node: ast.ClassDef | ast.FunctionDef
    methods: list[Entry] = field(default_factory=list)


def module_names() -> list[str]:
    """The package's modules, a package in it by its own name alone.

    :returns: Their names, sorted.
    """
    names = [path.stem for path in PACKAGE.glob("*.py")]
    return sorted(names + [path.parent.name for path in PACKAGE.glob("*/__init__.py")])


def unplaced(names: Sequence[str]) -> list[str]:
    """The modules that are neither on the page nor left out, or are both or gone.

    :param names: The package's modules.
    :returns: Their names.
    """
    placed = [*MODULES, *ELSEWHERE]
    return sorted(
        {name for name in (*names, *placed) if placed.count(name) != 1 or name not in names}
    )


def entries(tree: ast.Module) -> list[Entry]:
    """The public classes and functions a module defines.

    :param tree: The module, parsed.
    :returns: Them, in the source's order, a class with its public methods.
    """
    found = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = [
                Entry(f"{node.name}.{item.name}", item)
                for item in node.body
                if isinstance(item, ast.FunctionDef)
                and not item.name.startswith("_")
                and not _is_setter(item)
            ]
            found.append(Entry(node.name, node, methods))
        elif (
            isinstance(node, ast.FunctionDef)
            and not node.name.startswith("_")
            and node.name not in COMMAND_LINE
        ):
            found.append(Entry(node.name, node))
    return found


def _decorators(node: ast.ClassDef | ast.FunctionDef) -> list[str]:
    """The names of *node*'s decorators, a call's without its arguments."""
    return [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list]


def _is_setter(node: ast.FunctionDef) -> bool:
    """Whether *node* sets a property another definition documents."""
    return any(name.endswith(".setter") for name in _decorators(node))


def _is_dataclass(node: ast.ClassDef | ast.FunctionDef) -> bool:
    """Whether *node* is a class whose constructor its fields make."""
    return any(name.split(".")[-1] == "dataclass" for name in _decorators(node))


def _is_property(node: ast.ClassDef | ast.FunctionDef) -> bool:
    """Whether *node* is read as an attribute."""
    return isinstance(node, ast.FunctionDef) and "property" in _decorators(node)


def _argument(arg: ast.arg, default: ast.expr | None, prefix: str = "") -> str:
    """One parameter, as a signature writes it."""
    text = prefix + arg.arg
    if arg.annotation is not None:
        text += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        text += f" = {ast.unparse(default)}" if arg.annotation is not None else ""
        text += "" if arg.annotation is not None else f"={ast.unparse(default)}"
    return text


def _parameters(node: ast.FunctionDef, *, bound: bool) -> list[str]:
    """A function's parameters, ``self`` or ``cls`` left out when *bound*."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += args.defaults
    parts = [_argument(a, d) for a, d in zip(positional, defaults, strict=True)]
    if bound:
        parts = parts[1:]
    if args.vararg is not None:
        parts.append(_argument(args.vararg, None, "*"))
    elif args.kwonlyargs:
        parts.append("*")
    parts += [_argument(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)]
    if args.kwarg is not None:
        parts.append(_argument(args.kwarg, None, "**"))
    return parts


def dataclass_fields(node: ast.ClassDef) -> list[str]:
    """A dataclass's public constructor parameters, as a signature writes them.

    :param node: The class.
    :returns: Them; empty for a class that is no dataclass.
    """
    if not _is_dataclass(node):
        return []
    parts = []
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        default = ast.unparse(item.value) if item.value is not None else ""
        if "init=False" in default:
            continue
        made = re.fullmatch(r"field\(default_factory=(\w+)\)", default)
        if made:
            default = {"list": "[]", "dict": "{}"}.get(made.group(1), f"{made.group(1)}()")
        elif default.startswith("field("):
            given = re.search(r"default=('[^']*'|\w+)", default)
            default = given.group(1) if given else ""
        text = f"{item.target.id}: {ast.unparse(item.annotation)}"
        parts.append(text + (f" = {default}" if default else ""))
    return parts


def signature(entry: Entry) -> str:
    """What an entry is called with, as source on one line or a line a parameter.

    :param entry: The entry.
    :returns: The signature.
    """
    node = entry.node
    if isinstance(node, ast.ClassDef):
        init = next(
            (i for i in node.body if isinstance(i, ast.FunctionDef) and i.name == "__init__"), None
        )
        bases = [ast.unparse(base) for base in node.bases]
        parts = dataclass_fields(node) if init is None else _parameters(init, bound=True)
        if not parts and bases:
            return f"class {node.name}({', '.join(bases)})"
        lead, tail = f"class {node.name}", ""
    else:
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        if _is_property(node):
            return f"{entry.name}{returns}"
        bound = "." in entry.name and "staticmethod" not in _decorators(node)
        parts = _parameters(node, bound=bound)
        lead, tail = entry.name, returns
    line = f"{lead}({', '.join(parts)}){tail}"
    if len(line) <= 96:
        return line
    return "\n".join([f"{lead}(", *(f"    {part}," for part in parts), f"){tail}"])


def _prose(text: str, links: Mapping[str, str]) -> str:
    """A paragraph of a docstring as Markdown: its roles linked, its literals as code."""

    def role(match: re.Match[str]) -> str:
        name = match.group(1)
        parts = name.split(".")
        for start in range(len(parts)):
            key = ".".join(parts[start:])
            if key in links:
                return f"[``{key}``](#{links[key]})"
        return f"``{parts[-1] if name.startswith('kleidos_meter.') else name}``"

    joined = ROLE.sub(role, " ".join(text.split()))
    pieces = re.split(r"(``.+?``)", joined)
    for at, piece in enumerate(pieces):
        if piece.startswith("``"):
            pieces[at] = piece[1:-1]
        else:
            pieces[at] = piece.replace("<", "\\<")
    return "".join(pieces)


def _scoped(links: Mapping[str, str], owner: str) -> dict[str, str]:
    """*links* with *owner*'s methods under their own names too (``:meth:`stop```)."""
    own = {
        name.removeprefix(f"{owner}."): target
        for name, target in links.items()
        if name.startswith(f"{owner}.")
    }
    return {**links, **own}


def parse(text: str, links: Mapping[str, str]) -> Docstring:
    """Take a docstring apart.

    :param text: The docstring, as the source has it.
    :param links: The fragment of each name the page has.
    :returns: Its body as Markdown, a line a paragraph, and its fields.
    """
    doc = Docstring()
    raw: list[tuple[str, str, list[str]]] = []
    block: list[str] = []
    lines = inspect.cleandoc(text).splitlines()

    def close() -> None:
        """End the paragraph in progress."""
        if block:
            doc.body += [*_paragraph(block, links), ""]
            block.clear()

    at = 0
    while at < len(lines):
        line = lines[at]
        found = FIELD.match(line)
        if found:
            close()
            raw.append((found.group(1), found.group(2) or "", [found.group(3) or ""]))
        elif raw and line.startswith(" "):
            raw[-1][2].append(line)
        elif not line.strip():
            close()
        else:
            if raw:
                raise ValueError(f"text after the fields: {line!r}")
            block.append(line)
            if line.endswith("::"):
                block[-1] = line[:-1]
                close()
                literal = []
                at += 1
                while at < len(lines) and (not lines[at].strip() or lines[at].startswith(" ")):
                    literal.append(lines[at])
                    at += 1
                doc.body += ["```text", *inspect.cleandoc("\n".join(literal)).splitlines()]
                doc.body += ["```", ""]
                continue
        at += 1
    close()
    doc.fields = [(kind, name, _prose(" ".join(rest), links)) for kind, name, rest in raw]
    return doc


def _paragraph(block: Sequence[str], links: Mapping[str, str]) -> Iterator[str]:
    """A paragraph, or a list's items, each on one line."""
    if not any(ITEM.match(line) for line in block):
        yield _prose(" ".join(block), links)
        return
    item: list[str] = []
    for line in block:
        if ITEM.match(line) and item:
            yield _listed(item, links)
            item = []
        item.append(line)
    yield _listed(item, links)


def _listed(item: Sequence[str], links: Mapping[str, str]) -> str:
    """A list's item on one line, its marker a dash or its number."""
    marker, _, rest = item[0].partition(" ")
    if not ITEM.match(item[0]):
        return _prose(" ".join(item), links)
    text = _prose(" ".join([rest, *item[1:]]), links)
    return f"{'-' if marker in '-*' else marker} {text}"


def field_lines(doc: Docstring, *, attributes: bool) -> list[str]:
    """The fields as a list, each kind behind its word.

    :param doc: The docstring.
    :param attributes: Whether an ``:ivar`` is an attribute alone; a dataclass's is
        its constructor's parameter too, and is shown as one.
    :returns: The lines, a blank one at the end when there are any.
    """
    lines = []
    for kind, name, text in doc.fields:
        if kind == "ivar" and attributes:
            lines.append(f"- attribute `{name}`: {text}")
        elif kind in ("param", "ivar"):
            lines.append(f"- `{name}`: {text}")
        elif kind == "raises":
            lines.append(f"- raises `{name}`: {text}")
        else:
            lines.append(f"- returns: {text}")
    return [*lines, ""] if lines else []


def entry_lines(entry: Entry, depth: int, links: Mapping[str, str]) -> list[str]:
    """An entry's section, its methods' included.

    :param entry: The entry.
    :param depth: Its heading's depth.
    :param links: The fragment of each name the page has.
    :returns: The lines.
    :raises ValueError: When it has no docstring.
    """
    text = ast.get_docstring(entry.node, clean=False)
    if not text:
        raise ValueError(f"{entry.name} has no docstring")
    doc = parse(text, _scoped(links, entry.name.split(".")[0]))
    lines = [
        f"{'#' * depth} `{entry.name}`",
        "",
        "```python",
        *signature(entry).splitlines(),
        "```",
        "",
        *doc.body,
        *field_lines(doc, attributes=not _is_dataclass(entry.node)),
    ]
    for method in entry.methods:
        lines += entry_lines(method, depth + 1, links)
    return lines


def exports(tree: ast.Module) -> list[str]:
    """The names a module's ``__all__`` lists.

    :param tree: The module, parsed.
    :returns: The names.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "__all__":
            return list(ast.literal_eval(node.value))
    return []


def read_sources() -> dict[str, str]:
    """The sources the page is written from.

    :returns: The text of the package's ``__init__`` and of each module on the page.
    """
    return {
        name: (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        for name in ("__init__", *MODULES)
    }


def page(sources: Mapping[str, str] | None = None) -> str:
    """The page's text.

    :param sources: Each module's source, by its name; the package's own by default.
    :returns: The text.
    :raises ValueError: For a module in neither list, two entries of one heading, or a
        public name with no docstring.
    """
    if sources is None:
        wrong = unplaced(module_names())
        if wrong:
            raise ValueError(f"in neither MODULES nor ELSEWHERE, or in both, or gone: {wrong}")
        sources = read_sources()
    trees = {name: ast.parse(source) for name, source in sources.items()}
    found = {name: entries(trees[name]) for name in MODULES}
    links: dict[str, str] = {}
    for name, listed in found.items():
        links[f"kleidos_meter.{name}"] = anchor(f"kleidos_meter.{name}")
        for entry in listed:
            for item in (entry, *entry.methods):
                if item.name in links or anchor(item.name) in links.values():
                    raise ValueError(f"two entries named {item.name}")
                links[item.name] = anchor(item.name)
    where = {entry.name: name for name, listed in found.items() for entry in listed}
    lines = [
        *header("gen_python_api_doc.py", "the docstrings of kleidos_meter"),
        "# The Python API",
        "",
        "The host package, `kleidos_meter`: every public class, method and function a program"
        " that drives the meter may use. This page is written by `scripts/gen_python_api_doc.py`"
        " from the sources' docstrings, and a test fails when the two differ. The package's"
        " installation and a first program are in [../host/README.md](../host/README.md); the"
        " command line is [cli.md](cli.md), the panel's HTTP API [http-api.md](http-api.md), and"
        " what each SCPI command, report key and error number means [scpi.md](scpi.md).",
        "",
        "A report comes back as its fields, text by key, as the instrument printed them, and an"
        " integer's name ends in its unit (`_us`, `_na`, `_pv`, `_nohm`).",
        "",
        *table(
            ["Module", "Holds"],
            [
                [f"[`kleidos_meter.{name}`](#{links[f'kleidos_meter.{name}']})", f"{what}"]
                for name, what in MODULES.items()
            ],
        ),
        "",
        "## Exported by the package",
        "",
        "`from kleidos_meter import …`; everything else is imported from its module.",
        "",
        *[
            f"- [`{name}`](#{links[name]}), of `{where[name]}`"
            for name in exports(trees["__init__"])
        ],
        "",
    ]
    for name, listed in found.items():
        text = ast.get_docstring(trees[name], clean=False) or ""
        lines += [f"## `kleidos_meter.{name}`", "", *parse(text, links).body]
        for entry in listed:
            lines += entry_lines(entry, 3, links)
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the page, or check it.

    :param argv: The arguments, ``sys.argv[1:]`` when None.
    :returns: 0, or 1 when ``--check`` finds the page is not the generator's.
    """
    return run(argv, page, PAGE, __doc__.splitlines()[0], "the docstrings")


if __name__ == "__main__":
    sys.exit(main())
