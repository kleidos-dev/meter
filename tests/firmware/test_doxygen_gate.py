# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Doxygen gates see an undocumented symbol, a broken link and a shadowed name.

``doxygen Doxyfile`` is the firmware reference's gate: the Doxyfile shows
undocumented symbols (``HIDE_UNDOC_MEMBERS`` / ``HIDE_UNDOC_CLASSES = NO``),
extracts the private ones too (private members, file-static and
anonymous-namespace symbols, classes local to a function: the four
``EXTRACT_`` options), warns on each one and on every undocumented parameter
or result, and turns any warning into a failure (``WARN_AS_ERROR =
FAIL_ON_WARNINGS``). Those settings are what make it a coverage gate of every
symbol and not only a lint, so they are held here, and the gate is run on a
copy of the tree with one undocumented symbol planted at a time (a public
function, a private member, an anonymous-namespace function), each of which
must fail it, beside the unchanged copy, which must pass; the two private
plants also pass once their ``EXTRACT_`` option is turned off, so the option
is shown to be what sees them. A link to a file the repository does not hold,
planted in a page, must fail it too, at the page's line. The copy is every
file the repository tracks, since the pages link to files outside ``INPUT``.

``scripts/check_doc_links.py`` is the second gate, run on the finished build:
what Doxygen writes broken without a warning (a link to a page it never
generated, a name an enclosing namespace also declares). It is held here to
this tree's build, to its self-test, and to the hook and the CI job that run
it after ``doxygen Doxyfile``. The Doxyfile expands ESP-IDF's placement
attributes to nothing, and no generated member signature may carry one.

Host-only. The runs need ``doxygen`` on ``PATH`` (CI's docs job installs
1.18.0) and are skipped without it; the settings and the wiring are checked
always.
"""

from __future__ import annotations

import html
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOXYFILE = ROOT / "Doxyfile"
LINK_GATE = ROOT / "scripts" / "check_doc_links.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
HOOKS = ROOT / ".pre-commit-config.yaml"
#: The finished build the link gate reads, as ``doxygen Doxyfile`` writes it.
HTML = "build/doxygen/html"
#: ESP-IDF's placement attributes: where the linker puts a symbol, not its type.
PLACEMENT = ("IRAM_ATTR", "DRAM_ATTR", "EXT_RAM_BSS_ATTR", "RTC_NOINIT_ATTR")
NEEDS_DOXYGEN = pytest.mark.skipif(shutil.which("doxygen") is None, reason="doxygen is not on PATH")

#: The settings that make the build a coverage gate.
GATE = {
    "EXTRACT_ALL": "NO",
    "EXTRACT_PRIVATE": "YES",
    "EXTRACT_STATIC": "YES",
    "EXTRACT_LOCAL_CLASSES": "YES",
    "EXTRACT_ANON_NSPACES": "YES",
    "HIDE_UNDOC_MEMBERS": "NO",
    "HIDE_UNDOC_CLASSES": "NO",
    "WARN_IF_UNDOCUMENTED": "YES",
    "WARN_IF_DOC_ERROR": "YES",
    "WARN_IF_INCOMPLETE_DOC": "YES",
    "WARN_NO_PARAMDOC": "YES",
    "WARN_AS_ERROR": "FAIL_ON_WARNINGS",
}


def _settings() -> dict[str, str]:
    found = {}
    text = DOXYFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    for line in text.splitlines():
        match = re.match(r"^([A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_the_doxyfile_is_a_coverage_gate() -> None:
    settings = _settings()
    for key, value in GATE.items():
        assert settings.get(key) == value, f"Doxyfile {key} must be {value}"
    assert "firmware/src" in settings["INPUT"].split()


def test_the_doxyfile_expands_the_placement_attributes_to_nothing() -> None:
    settings = _settings()
    assert settings.get("MACRO_EXPANSION") == "YES"
    assert settings.get("EXPAND_ONLY_PREDEF") == "YES"
    predefined = settings["PREDEFINED"].split()
    for attribute in PLACEMENT:
        assert f"{attribute}=" in predefined, f"PREDEFINED must hold {attribute}="


def _run(tree: Path) -> subprocess.CompletedProcess[str]:
    (tree / "build").mkdir(exist_ok=True)
    return subprocess.run(
        ["doxygen", "Doxyfile"], cwd=tree, capture_output=True, text=True, check=False
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of every file the repository tracks, where the gate is run and planted.

    The whole tree, not only what the Doxyfile reads: the filter refuses a
    link to a file the repository does not hold, and the pages link to
    sources, tools and tests outside ``INPUT``.
    """
    copy = tmp_path / "meter"
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    for name in filter(None, listed.split("\0")):
        source = ROOT / name
        if not source.is_file():  # deleted in the working tree, or a submodule
            continue
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return copy


#: One undocumented symbol per kind the gate must see: the source it goes into,
#: a text of that source, the same text with the symbol planted in it, the
#: name the warning must carry, and the Doxyfile option without which the gate
#: would not see it (None: seen whatever is extracted).
PLANTS = (
    (
        "scale.h",
        "/// @}\n\n}  // namespace kleidos::meter::scale",
        "int plantedUndocumented( int value ) noexcept;\n\n/// @}\n\n"
        "}  // namespace kleidos::meter::scale",
        "plantedUndocumented",
        None,
    ),
    (
        "relay.h",
        "    bool ready_           = false;         ///< Every relay pin was configured.\n",
        "    bool ready_           = false;         ///< Every relay pin was configured.\n"
        "    int  plantedPrivate_  = 0;\n",
        "plantedPrivate_",
        "EXTRACT_PRIVATE",
    ),
    (
        "relay.cpp",
        "namespace kleidos::meter::relay {\n\nnamespace {\n\n",
        "namespace kleidos::meter::relay {\n\nnamespace {\n\n"
        "int plantedAnonymous( int value ) noexcept {\n    return value;\n}\n\n",
        "plantedAnonymous",
        "EXTRACT_ANON_NSPACES",
    ),
)


def _with_option(tree: Path, option: str, value: str) -> None:
    """Sets one option of the copy's Doxyfile."""
    doxyfile = tree / "Doxyfile"
    text = doxyfile.read_text(encoding="utf-8")
    pattern = re.compile(rf"^({option}\s*)= \w+$", re.M)
    assert len(pattern.findall(text)) == 1, f"the Doxyfile sets {option} other than once"
    doxyfile.write_text(pattern.sub(rf"\g<1>= {value}", text), encoding="utf-8")


@NEEDS_DOXYGEN
def test_the_tree_passes_and_each_planted_undocumented_symbol_fails(tree: Path) -> None:
    clean = _run(tree)
    assert clean.returncode == 0, clean.stderr
    for name, anchor, plant, symbol, option in PLANTS:
        source = tree / "firmware" / "src" / name
        text = source.read_text(encoding="utf-8")
        assert text.count(anchor) == 1, f"{name} no longer holds the text {symbol} is planted at"
        source.write_text(text.replace(anchor, plant), encoding="utf-8")
        failed = _run(tree)
        assert failed.returncode != 0, f"the gate passed an undocumented {symbol}"
        assert symbol in failed.stderr
        if option is not None:
            _with_option(tree, option, "NO")
            unseen = _run(tree)
            assert unseen.returncode == 0, f"{symbol} is seen without {option}: {unseen.stderr}"
            _with_option(tree, option, "YES")
        source.write_text(text, encoding="utf-8")
    assert _run(tree).returncode == 0, "the tree did not pass again with every plant removed"


#: A link planted in a page of the copy: the page, a line it holds, and that
#: line with a link to a file the repository does not hold.
GONE_LINK = (
    "docs/usage.md",
    "[the protocol](scpi.md)",
    "[the protocol](scpi-gone.md)",
)


@NEEDS_DOXYGEN
def test_a_link_to_a_file_that_is_not_there_fails_the_gate(tree: Path) -> None:
    page, anchor, plant = GONE_LINK
    source = tree / page
    text = source.read_text(encoding="utf-8")
    assert text.count(anchor) >= 1, f"{page} no longer holds the link the plant replaces"
    line = text[: text.index(anchor)].count("\n") + 1
    source.write_text(text.replace(anchor, plant, 1), encoding="utf-8")
    failed = _run(tree)
    assert failed.returncode != 0, "the gate passed a link to a file that is not there"
    assert f"{page}:{line}: warning: the link scpi-gone.md names docs/scpi-gone.md" in (
        failed.stderr
    )
    assert f"{page}:{line}: error: unable to resolve reference to 'docs/scpi-gone.md'" in (
        failed.stderr
    )


# --- The placement attributes -------------------------------------------------

#: A cell of the generated HTML that prints a member's signature: the detailed
#: description's heading and the two columns of a page's member summary.
SIGNATURE_CELL_RE = re.compile(
    r'<td class="(?:memname|memItemLeft|memItemRight|memTemplItemLeft|memTemplItemRight)"'
    r"[^>]*>(.*?)</td>",
    re.S,
)


def _signatures(tree: Path) -> list[str]:
    """Every member signature the HTML build of ``tree`` prints, as plain text."""
    cells: list[Any] = []
    for page in sorted((tree / HTML).glob("*.html")):
        cells.extend(
            html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
            for match in SIGNATURE_CELL_RE.finditer(page.read_text(encoding="utf-8"))
        )
    return cells


def _with_placement(signatures: list[str]) -> list[str]:
    return [cell.strip() for cell in signatures if any(a in cell for a in PLACEMENT)]


@NEEDS_DOXYGEN
def test_no_generated_member_signature_carries_a_placement_attribute(tree: Path) -> None:
    """The reference shows each function's type, not the section it is linked into.

    Without the Doxyfile's PREDEFINED entries Doxygen reads ``IRAM_ATTR`` as
    part of the return type of every hot-path function; the copy built
    without them must show it, or this test could not see the defect.
    """
    assert _run(tree).returncode == 0
    signatures = _signatures(tree)
    assert len(signatures) > 500, f"only {len(signatures)} signatures read: the pattern drifted"
    assert any("Ina228Driver::readRegister" in cell for cell in signatures), (
        "Ina228Driver::readRegister, an IRAM_ATTR member, is no longer in the reference"
    )
    assert _with_placement(signatures) == []

    doxyfile = tree / "Doxyfile"
    text = doxyfile.read_text(encoding="utf-8")
    for attribute in PLACEMENT:
        text = re.sub(rf"\s*\\\n\s*{attribute}=(?=\s)", "", text)
    assert all(f"{a}=" not in text for a in PLACEMENT), "the attributes were not taken out"
    doxyfile.write_text(text, encoding="utf-8")
    shutil.rmtree(tree / "build")
    assert _run(tree).returncode == 0
    assert any("IRAM_ATTR" in cell for cell in _with_placement(_signatures(tree)))


# --- The link gate (scripts/check_doc_links.py) -------------------------------


def _link_gate() -> ModuleType:
    """The link gate, imported as a module (it does nothing on import)."""
    spec = importlib.util.spec_from_file_location("check_doc_links", LINK_GATE)
    assert spec is not None and spec.loader is not None
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_link_gate_reads_this_tree_s_sources() -> None:
    gate = _link_gate()
    assert gate.SRC_DIR in _settings()["INPUT"].split()
    sources = {p.relative_to(ROOT).as_posix() for p in gate.source_files(ROOT)}
    assert "firmware/src/relay.h" in sources
    assert {"docs/groups.dox", "docs/firmware.dox"} <= sources


def test_every_finding_class_has_a_planted_defect() -> None:
    """A class the self-test never raises is a class nobody proved can fail."""
    gate = _link_gate()
    planted = {kind for kind, _what, _changes in gate.PLANTS}
    assert planted == set(gate.FINDING_CLASSES)


def test_only_the_three_generator_defects_are_not_failed() -> None:
    """The broken links the gate tolerates do not grow quietly.

    A link Doxygen writes broken by itself joins GENERATOR_CAUSES, with the
    rule that bounds it, only once it is shown no comment or setting fixes it.
    """
    assert _link_gate().GENERATOR_CAUSES == (
        "generator-nav-entry-at-file-page",
        "generator-index-letter",
        "generator-empty-nav-index-key",
    )


@NEEDS_DOXYGEN
def test_the_link_gate_passes_on_the_tree(tree: Path) -> None:
    """No broken link, shadowed name or XML-only warning in the reference."""
    assert _run(tree).returncode == 0
    result = _link_gate().check(tree, tree / HTML)
    found = {kind: rows for kind, rows in result["findings"].items() if rows}
    assert found == {}
    assert result["counted"]["pages"] > 100


@NEEDS_DOXYGEN
def test_the_link_gate_sees_a_relay_constant_named_like_a_measured_channel(tree: Path) -> None:
    """relay::kChannelCount under kleidos::meter::kChannelCount was the finding renamed."""
    relay = tree / "firmware" / "src" / "relay.h"
    text = relay.read_text(encoding="utf-8")
    assert text.count("constexpr std::size_t kRelayCount = 4;") == 1
    relay.write_text(
        text.replace(
            "constexpr std::size_t kRelayCount = 4;",
            "constexpr std::size_t kRelayCount = 4;\n"
            "/// @brief A planted second name.\nconstexpr std::size_t kChannelCount = 4;",
        ),
        encoding="utf-8",
    )
    assert _run(tree).returncode == 0
    shadowed = _link_gate().check(tree, tree / HTML)["findings"]["shadowed-name"]
    assert [row["inner"] for row in shadowed] == ["kleidos::meter::relay::kChannelCount"]


@NEEDS_DOXYGEN
def test_the_link_gate_self_test_raises_every_planted_defect() -> None:
    result = subprocess.run(
        [sys.executable, str(LINK_GATE), "--selftest"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS]" in result.stdout


def _docs_job_runs() -> list[str]:
    """The ``run:`` commands of the workflow's docs job, in order."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^  docs:\n(.*?)(?=^  [\w-]+:\n)", text, re.S | re.M)
    assert match, "check.yml no longer has a docs job"
    return [c.strip() for c in re.findall(r"^\s+run: (?!\|)(.+)$", match.group(1), re.M)]


def test_the_docs_job_checks_the_build_it_just_made() -> None:
    runs = _docs_job_runs()
    build = "mkdir -p build && doxygen Doxyfile"
    gate = f"python3 scripts/check_doc_links.py --html {HTML}"
    assert build in runs and gate in runs, runs
    assert runs.index(gate) > runs.index(build), "the link gate runs before its build"
    assert "python3 scripts/check_doc_links.py --selftest" in runs


def test_the_doxygen_hook_runs_the_link_gate_after_the_build() -> None:
    text = HOOKS.read_text(encoding="utf-8")
    block = text[text.index("      - id: doxygen\n") :]
    block = block[: block.index("\n      - id: ", 1)]
    assert (
        f"mkdir -p build && doxygen Doxyfile && python3 scripts/check_doc_links.py --html {HTML}"
        in block
    )
    match = re.search(r"files: '(.*)'", block)
    assert match
    for path in (
        "firmware/src/relay.h",
        "docs/groups.dox",
        "Doxyfile",
        "scripts/check_doc_links.py",
    ):
        assert re.search(match.group(1), path), f"the hook does not run on {path}"


def test_a_change_to_the_gates_runs_the_docs_job() -> None:
    """The changes filter reads scripts/ and tests/ as not the firmware's, except these."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"build_guards='(.*)'", text)
    assert match
    for path in (
        "scripts/check_doc_links.py",
        "tests/firmware/test_doxygen_gate.py",
        "docs/groups.dox",
        "docs/firmware.dox",
    ):
        assert re.search(match.group(1), path), f"a change to {path} skips the docs job"


#: Pages that record their date and are kept as written (docs/README.md,
#: "History"); what they say of the command is not an instruction.
HISTORY_PAGES = {"CHANGELOG.md", "docs/firmware-design.md"}


def test_every_page_that_gives_the_build_command_makes_its_directory() -> None:
    """Doxygen cannot create build/doxygen in a tree with no build/.

    So a page, or a script's message, that tells a reader to run the reference's
    build names it as the gates run it, ``mkdir -p build && doxygen Doxyfile``.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "*.md", "scripts/*.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    bare: list[Any] = []
    for name in tracked:
        if name in HISTORY_PAGES or name.startswith("calibration/records/"):
            continue
        text = (ROOT / name).read_text(encoding="utf-8")
        bare.extend(
            (name, span)
            for span in re.findall(r"`+([^`]*doxygen Doxyfile[^`]*)`+", text)
            if "mkdir -p build && doxygen Doxyfile" not in span and " gate" not in span
        )
    assert not bare, f"the build command without its directory: {bare}"
