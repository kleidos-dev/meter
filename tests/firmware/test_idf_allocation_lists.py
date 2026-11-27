# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The three lists of allocating ESP-IDF entry points are one list.

What ESP-IDF allocates on the firmware's behalf is held in three places, each
with its own copy of the same lists: the deny list (the ESP-IDF entry points
that allocate or free on the device), the allow-list (the ones the firmware
still called, each a site of docs/misra.md's D1; empty since FW-16, and gone
from the harness since FW-17), and the made-safe list (FW-16: the ones the
firmware calls because the image's configuration takes their allocation out,
each with its mechanism):

- the host harness, ``firmware/test/native/harness/idf_ledger.h``
  (``kAllocatingIdf``, ``kMadeSafeIdf``, and no ``kAllowedIdf`` from FW-17):
  every fake of a listed entry point records its call, and a case that called
  one not made safe fails;
- the source gate, ``tests/firmware/test_no_dynamic_memory.py``
  (``IDF_ALLOCATING``, ``IDF_ALLOWED``, by source file);
- the object gate, ``scripts/check_heap_objects.py`` (the same names, by the
  object's source file).

These tests hold the three equal, the gates' allow-lists empty as the
harness's absent one is; every fake in ``harness/sim.cpp`` of a
listed entry point to a call of ``sim::noteIdfAllocation()`` with its own
name; every allowed call to the deviation of docs/misra.md that names it;
every made-safe call to its mechanism, found in the tree (:data:`MECHANISMS`:
a made-safe entry with none written there fails); and each list's history,
read from git, to an allow-list that only shrinks and a deny list that only
grows (a shallow clone has no history, and passes).

Host-only: the files read as text, the Python ones also through ``ast``.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "firmware" / "test" / "native" / "harness" / "idf_ledger.h"
SIM = ROOT / "firmware" / "test" / "native" / "harness" / "sim.cpp"
SOURCE_GATE = ROOT / "tests" / "firmware" / "test_no_dynamic_memory.py"
OBJECT_GATE = ROOT / "scripts" / "check_heap_objects.py"
MISRA = ROOT / "docs" / "misra.md"

#: The deviations an allowed call may be a site of (the maintenance
#: operations' deviation is gone since FW-16).
DEVIATIONS = {"D1"}

#: For each made-safe entry point, what in the tree makes it allocate
#: nothing: (file under the repository, text it must hold).
MECHANISMS: dict[str, tuple[tuple[str, str], ...]] = {
    "esp_flash_write": (
        (
            "firmware/CMakeLists.txt",
            '"-Wl,--wrap=esp_partition_is_flash_region_writable" APPEND)',
        ),
        ("firmware/sdkconfig.defaults", "\nCONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED=y\n"),
        ("firmware/src/flash_port.cpp", "__wrap_esp_partition_is_flash_region_writable("),
    ),
    "esp_flash_read": (
        # A destination outside internal RAM is refused before the call.
        ("firmware/src/flash_port.cpp", "if ( !esp_ptr_in_dram( data ) ) {"),
        # The log reads into its own buffer, a static in internal RAM.
        ("firmware/src/flash_log.cpp", "std::array<std::uint8_t, kMaxRecordBytes> buffer"),
    ),
    "esp_flash_erase_region": (
        (
            "firmware/CMakeLists.txt",
            '"-Wl,--wrap=esp_partition_is_flash_region_writable" APPEND)',
        ),
        ("firmware/sdkconfig.defaults", "\nCONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED=y\n"),
        ("firmware/src/flash_port.cpp", "__wrap_esp_partition_is_flash_region_writable("),
    ),
}


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def harness_list(text: str, name: str) -> list[str] | None:
    """The list *name* of an ``idf_ledger.h``, or None if it has none.

    Read in either shape it is written in: ``std::to_array<std::string_view>(
    { ... } )``, or an empty ``std::array<std::string_view, 0> ... = {}``.
    """
    pattern = rf"\b{name}\s*=\s*std::to_array<std::string_view>\(\s*\{{(.*?)\}}\s*\)"
    found = re.search(pattern, text, re.S)
    if found is None:
        empty = rf"std::array<std::string_view,\s*0>\s+{name}\s*=\s*\{{\s*\}}"
        return [] if re.search(empty, text) else None
    body = re.sub(r"//[^\n]*", "", found.group(1))
    return re.findall(r'"(\w+)"', body)


def harness_lists(text: str) -> tuple[list[str], list[str]] | None:
    """``kAllocatingIdf`` and ``kAllowedIdf`` of an ``idf_ledger.h``, or None before the lists.

    A ledger with a deny list and no allow-list (FW-17 removed the empty one)
    allows nothing: its allow-list reads empty.
    """
    deny = harness_list(text, "kAllocatingIdf")
    if deny is None:
        return None
    return deny, harness_list(text, "kAllowedIdf") or []


def python_lists(text: str) -> tuple[list[str], dict[tuple[str, str], tuple[str, ...]]] | None:
    """``IDF_ALLOCATING`` and ``IDF_ALLOWED`` of a gate's source, or None if it has neither."""
    values = {}
    for node in ast.parse(text).body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and node.value is not None
            and isinstance(target, ast.Name)
            and target.id in ("IDF_ALLOCATING", "IDF_ALLOWED")
        ):
            values[target.id] = ast.literal_eval(node.value)
    if set(values) != {"IDF_ALLOCATING", "IDF_ALLOWED"}:
        return None
    return list(values["IDF_ALLOCATING"]), dict(values["IDF_ALLOWED"])


def committed_texts(path: Path) -> list[tuple[str, str]]:
    """The file's text at each commit that changed it, newest first; empty without git history."""
    try:
        listing = subprocess.run(
            ["git", "log", "--format=%H", "--", path.name],
            cwd=path.parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return []
    texts = []
    for commit in listing:
        shown = subprocess.run(
            ["git", "show", f"{commit}:./{path.name}"],
            cwd=path.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode == 0:
            texts.append((commit, shown.stdout))
    return texts


def section(markdown: str, heading: str) -> str:
    """The text of the ``###`` section of *markdown* whose heading starts with *heading*."""
    found = re.search(rf"^### {re.escape(heading)}.*?(?=^#{{2,3}} )", markdown, re.S | re.M)
    assert found, f"docs/misra.md has no section {heading}"
    return found.group(0)


def made_safe_lists() -> tuple[list[str], dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """The made-safe list of the harness, the source gate and the object gate."""
    harness = harness_list(LEDGER.read_text("utf-8"), "kMadeSafeIdf")
    gates = []
    for path in (SOURCE_GATE, OBJECT_GATE):
        found = None
        for node in ast.parse(path.read_text("utf-8")).body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "IDF_MADE_SAFE"
                and node.value is not None
            ):
                found = ast.literal_eval(node.value)
        assert found is not None, f"{path.name} has no IDF_MADE_SAFE"
        gates.append(found)
    assert harness is not None, "idf_ledger.h has no kMadeSafeIdf"
    return harness, gates[0], gates[1]


def _current() -> tuple[tuple[list[str], list[str]], tuple[Any, ...], tuple[Any, ...]]:
    harness = harness_lists(LEDGER.read_text("utf-8"))
    source = python_lists(SOURCE_GATE.read_text("utf-8"))
    objects = python_lists(OBJECT_GATE.read_text("utf-8"))
    assert harness and source and objects
    return harness, source, objects


def test_the_three_deny_lists_and_allow_lists_are_equal() -> None:
    (harness_deny, harness_allow), (source_deny, source_allow), (object_deny, object_allow) = (
        _current()
    )
    assert harness_deny == source_deny == object_deny
    assert source_allow == object_allow
    assert sorted(harness_allow) == sorted({entry for _, entry in source_allow})
    assert len(set(harness_deny)) == len(harness_deny), "an entry point listed twice"
    assert len(set(harness_allow)) == len(harness_allow), "an entry point allowed twice"


def test_the_harness_has_no_allow_list_and_the_gates_allow_nothing() -> None:
    """FW-17: the harness's empty allow-list is removed, so a host case fails
    on any listed call not made safe; a gate that allowed one would pass what
    the host tests fail."""
    text = LEDGER.read_text("utf-8")
    assert "kAllowedIdf" not in re.sub(r"//[^\n]*", "", text), "idf_ledger.h declares an allow-list"
    _, (_, source_allow), (_, object_allow) = _current()
    assert source_allow == {}, f"the source gate allows {sorted(source_allow)}"
    assert object_allow == {}, f"the object gate allows {sorted(object_allow)}"


def test_the_three_made_safe_lists_are_equal_and_on_the_deny_list() -> None:
    harness, source, objects = made_safe_lists()
    _, (deny, allow), _ = _current()
    assert source == objects
    assert sorted(harness) == sorted({entry for _, entry in source})
    assert len(set(harness)) == len(harness), "an entry point made safe twice"
    for (path, entry), how in source.items():
        assert entry in deny, f"{path}: {entry} is made safe but not on the deny list"
        assert (path, entry) not in allow, f"{path}: {entry} is both allowed and made safe"
        assert how, f"{path}: {entry} names no mechanism"
        assert (ROOT / "firmware" / "src" / path).is_file(), path


def test_every_made_safe_call_has_its_mechanism_in_the_tree() -> None:
    harness, _, _ = made_safe_lists()
    missing = [entry for entry in harness if entry not in MECHANISMS]
    assert not missing, "made safe with no mechanism written here: " + ", ".join(missing)
    absent = [
        f"{entry}: {path} lacks {text!r}"
        for entry in harness
        for path, text in MECHANISMS[entry]
        if text not in (ROOT / path).read_text("utf-8")
    ]
    assert not absent, "\n".join(absent)


def test_every_allowed_call_is_on_the_deny_list_and_a_site_of_d1() -> None:
    _, (deny, allow), _ = _current()
    for (source, entry), deviations in allow.items():
        assert entry in deny, f"{source}: {entry} is allowed but not on the deny list"
        assert deviations and set(deviations) <= DEVIATIONS, f"{source}: {entry}: {deviations}"
        assert (ROOT / "firmware" / "src" / source).is_file(), source


def test_docs_misra_names_every_allowed_call_under_its_deviation() -> None:
    _, (_, allow), _ = _current()
    markdown = MISRA.read_text("utf-8")
    sections = {"D1": section(markdown, "D1.")}
    missing = [
        f"{deviation}: `{entry}()` ({source})"
        for (source, entry), deviations in allow.items()
        for deviation in deviations
        if f"`{entry}()`" not in sections[deviation]
    ]
    assert not missing, "docs/misra.md does not name:\n" + "\n".join(missing)


def test_every_fake_of_a_listed_entry_point_records_its_call() -> None:
    lists = harness_lists(LEDGER.read_text("utf-8"))
    assert lists is not None
    deny, allowed = lists
    sim = SIM.read_text("utf-8")
    definitions = {
        m.group(1): m.end()
        for m in re.finditer(r"^(?:esp_err_t|void|int)\s+(\w+)\s*\([^;{]*\)\s*\{", sim, re.M)
    }
    faked = [entry for entry in deny if entry in definitions]
    # Every call the firmware may still make has a fake (a fake the pattern
    # above did not find would be a call the ledger never sees); the fakes of
    # retired drivers go with them (FW-10 took the RMT's four, FW-16 NVS's).
    made_safe = harness_list(LEDGER.read_text("utf-8"), "kMadeSafeIdf") or []
    unfaked = sorted((set(allowed) | set(made_safe)) - set(faked))
    assert not unfaked, "allowed entry points with no fake found in sim.cpp: " + ", ".join(unfaked)
    silent = []
    for entry in faked:
        body = sim[definitions[entry] : sim.index("\n}\n", definitions[entry])]
        if f'sim::noteIdfAllocation( "{entry}" );' not in body:
            silent.append(entry)
    assert not silent, "fakes that do not record their call on the ledger: " + ", ".join(silent)
    for named in re.findall(r'noteIdfAllocation\( "(\w+)" \)', sim):
        assert named in faked, f"noteIdfAllocation names {named}, which no fake of that name makes"


def test_the_allow_lists_only_shrink_and_the_deny_lists_only_grow() -> None:
    (harness_deny, harness_allow), (source_deny, source_allow), (object_deny, object_allow) = (
        _current()
    )
    current = {
        LEDGER: (set(harness_deny), set(harness_allow)),
        SOURCE_GATE: (set(source_deny), set(source_allow)),
        OBJECT_GATE: (set(object_deny), set(object_allow)),
    }
    parse = {LEDGER: harness_lists, SOURCE_GATE: python_lists, OBJECT_GATE: python_lists}
    found = []
    for path, (deny, allow) in current.items():
        for commit, text in committed_texts(path):
            old = parse[path](text)
            if old is None:
                continue  # before the lists existed
            old_deny, old_allow = set(old[0]), set(old[1])
            found += [f"{path.name} {commit[:10]}: {e} left the deny list" for e in old_deny - deny]
            found += [
                f"{path.name} {commit[:10]}: {e} joined the allow-list" for e in allow - old_allow
            ]
    assert not found, "\n".join(sorted(found))


def test_the_parsers_read_the_shapes_they_are_given() -> None:
    header = (
        "inline constexpr auto kAllocatingIdf = std::to_array<std::string_view>( {\n"
        '    // I2C\n    "a",\n    "b",\n} );\n'
        'inline constexpr auto kAllowedIdf = std::to_array<std::string_view>( { "a" } );\n'
    )
    assert harness_lists(header) == (["a", "b"], ["a"])
    assert harness_lists("// nothing\n") is None
    empty = header.replace(
        'inline constexpr auto kAllowedIdf = std::to_array<std::string_view>( { "a" } );',
        "inline constexpr std::array<std::string_view, 0> kAllowedIdf = {};",
    )
    assert harness_lists(empty) == (["a", "b"], [])
    absent = header.replace(
        'inline constexpr auto kAllowedIdf = std::to_array<std::string_view>( { "a" } );\n', ""
    )
    assert harness_lists(absent) == (["a", "b"], [])
    gate = 'IDF_ALLOCATING = ("a", "b")\nIDF_ALLOWED: dict = {("x.cpp", "a"): ("D1",)}\n'
    assert python_lists(gate) == (["a", "b"], {("x.cpp", "a"): ("D1",)})
    assert python_lists("ALLOWED = set()\n") is None
    assert committed_texts(Path("/nonexistent/idf_ledger.h")) == []


def test_the_register_has_no_maintenance_deviation_and_d1_only_its_residue() -> None:
    """FW-19: no operation is maintenance and nothing allocates after ``app_main()``'s
    first line, so the register names no maintenance deviation and D1 holds the
    residue before that line alone (what left it is recorded after the census)."""
    markdown = MISRA.read_text("utf-8")
    assert not re.search(r"\bD8\b", markdown), "docs/misra.md names a D8"
    d1 = markdown[
        markdown.index("### D1.") : markdown.index("\n### D", markdown.index("### D1.") + 1)
    ]
    assert "before `app_main()`" in d1.splitlines()[0]
    assert re.findall(r"^#### .*$", d1, re.M) == ["#### The residue"]
    assert "since `app_main()`" not in d1.split("#### The residue", 1)[1]
