# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The reachability gate judges planted images right, passes the device's, and CI runs it.

``scripts/check_heap_reachability.py`` builds the linked image's call graph
from the relocations the device link keeps and fails when an allocator is
reachable from any of the firmware's own functions. Its self-test writes an
image per case (a direct call, a long call through a literal, an allocator two
calls deep, an own function nothing calls, a table an own function loads, a
jump, an unknown ROM function, a stale ROM leaf, a cut, a stale cut, a broken
cut, a renamed allocator, an ambiguous source name, an image linked without
its relocations) and checks the verdict on each; these tests run it, and hold
its decoder to the instructions it reads.

On a checkout with a device build they also run it on the image, hold the
decoder to the toolchain's disassembly at every relocated site it prints a
target for, and check what the deny list says against the graph: each
allocating ESP-IDF entry point the image links reaches an allocator, and each
made-safe call does not. Without a build those skip, and fail under
``METER_BUILD_REQUIRED=1`` (``conftest.py``).

Needs pyelftools (``pip install pyelftools==0.33``; CI installs it); the
tests skip without it.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("elftools", reason="pyelftools reads the images: pip install pyelftools==0.33")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_heap_reachability.py"
OBJECT_GATE = ROOT / "scripts" / "check_heap_objects.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
BUILD_DIR = ROOT / "firmware" / ".pio" / "build" / "meter"
ELF = BUILD_DIR / "firmware.elf"
BUILD = "`pio run --project-dir firmware -e meter`"
TOOLCHAIN = Path.home() / ".platformio" / "packages" / "toolchain-xtensa-esp-elf" / "bin"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # the gate's dataclasses look their module up
    spec.loader.exec_module(module)
    return module


gate = _load(SCRIPT, "check_heap_reachability")


def test_the_self_test_judges_every_planted_case(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.selftest() == 0, capsys.readouterr().out
    lines = capsys.readouterr().out.splitlines()
    assert len([line for line in lines if line.startswith("ok: ")]) >= 20
    assert not [line for line in lines if line.startswith("FAIL")]


@pytest.mark.parametrize(
    ("site", "code", "expected"),
    [
        # call8 forward and backward, from an unaligned site
        (0x42000010, "e50e00", (0x42000100, "code")),
        (0x42000101, "e5effe", (0x41FFF000, "code")),
        # j backward
        (0x42000020, "46f9ff", (0x42000009, "code")),
        # l32r: the word before the next aligned one
        (0x42000021, "81f7ff", (0x42000000, "literal")),
        # beqz (12-bit), bnez.n (6-bit), a bbci (8-bit)
        (0x42000100, "16a900", (0x4200010E, "code")),
        (0x42000100, "cc34", (0x42000107, "code")),
        (0x4037406E, "f774ce", (0x40374040, "code")),
        # movi.n carries no address
        (0x42000010, "0c0000", None),
    ],
)
def test_the_decoder_reads_each_instruction_form(
    site: int, code: str, expected: tuple[int, str] | None
) -> None:
    raw = bytes.fromhex(code)
    assert gate.decode_target(site, (raw + bytes(3))[:3]) == expected


def test_the_sinks_cover_the_object_gates_allocators() -> None:
    """Every allocator the object gate bans by name is a sink here."""
    objects = _load(OBJECT_GATE, "check_heap_objects")
    creates = {name for name in objects.FORBIDDEN_NAMES if name.startswith(("x", "X"))}
    missing = [n for n in objects.FORBIDDEN_NAMES - creates if not gate.SINKS.match(n)]
    assert not missing, f"allocators the reachability gate does not stop at: {missing}"
    for name in (
        "heap_caps_malloc",
        "heap_caps_aligned_calloc",
        "_Znwj",
        "_ZdlPvj",
        "_ZnajRKSt9nothrow_t",
    ):
        assert gate.SINKS.match(name), name
    for name in ("heap_caps_get_free_size", "multi_heap_free_size", "esp_heap_trace_free_hook"):
        assert not gate.SINKS.match(name), f"{name} reads the heap, it does not allocate"


def test_every_cut_and_rom_leaf_says_why() -> None:
    for (source, target), cut in gate.CUTS.items():
        assert cut.reason and source and target
        assert cut.guards, f"{source} -> {target}: a cut with nothing to make it fail"
    for name, reason in gate.ROM_LEAVES.items():
        assert reason, name


def test_ci_runs_the_gate_on_the_image() -> None:
    """CI's firmware job installs pyelftools, runs the self-test and the gate."""
    text = WORKFLOW.read_text(encoding="utf-8")
    job = text[text.index("\n  firmware:\n") :]
    job = job[: job.index("\n  schematic:\n")]
    assert "pyelftools==0.33" in job
    assert "python3 scripts/check_heap_reachability.py --selftest" in job
    assert re.search(
        r"python3 scripts/check_heap_reachability\.py"
        r" --elf firmware/\.pio/build/meter/firmware\.elf"
        r" firmware/\.pio/build/meter/src/\*\.o",
        job,
    )
    assert "GITHUB_STEP_SUMMARY" in job[job.index("check_heap_reachability.py --elf") :][:400]


def test_the_guards_job_can_run_these_tests() -> None:
    """The jobs that run tests/ install pyelftools, so these tests do not skip there."""
    text = WORKFLOW.read_text(encoding="utf-8")
    installs = [line for line in text.splitlines() if "pip install -e 'host[dev]'" in line]
    assert installs and all("pyelftools==0.33" in line for line in installs), installs


def _image(built: Callable[[Path, str], Path]) -> Any:
    elf = built(ELF, BUILD)
    objects = sorted((BUILD_DIR / "src").glob("*.o"))
    image = gate.read_image(elf)
    graph, problems = gate.build_graph(image, (gate.read_object(path) for path in objects))
    return image, graph, problems


def test_the_device_image_reaches_no_allocator(built: Callable[[Path, str], Path]) -> None:
    image, graph, problems = _image(built)
    assert image.relocations, "firmware.elf holds no relocations: an image before FW-19's link"
    report, failures = gate.verdict(graph)
    assert not problems + failures, "\n".join(problems + failures)
    assert int(report[0].split()[0]) > 300, report[0]


def _named(graph: Any, name: str) -> Any:
    return next((node for node, names in graph.names.items() if name in names), None)


def test_the_deny_list_is_what_the_graph_says_allocates(built: Callable[[Path, str], Path]) -> None:
    """Each allocating entry point the image links reaches an allocator; no made-safe one does."""
    _, graph, _ = _image(built)
    objects = _load(OBJECT_GATE, "check_heap_objects")
    made_safe = {entry for _, entry in objects.IDF_MADE_SAFE}

    def reaches_a_sink(name: str) -> bool:
        node = _named(graph, name)
        walk = gate.Graph(names=graph.names, edges=graph.edges, own={node})
        _, failures = gate.verdict(walk, rom_leaves={}, cuts=gate.CUTS, core_sinks=())
        return any(f.startswith("an allocator is reachable") for f in failures)

    linked = [entry for entry in objects.IDF_ALLOCATING if _named(graph, entry) is not None]
    assert linked, "the image links none of the deny list's entry points"
    silent = [e for e in linked if e not in made_safe and not reaches_a_sink(e)]
    assert not silent, f"on the deny list, linked, and reaching no allocator: {silent}"
    allocating = [e for e in made_safe if _named(graph, e) is not None and reaches_a_sink(e)]
    assert not allocating, f"made safe, and an allocator is reachable from them: {allocating}"


def test_the_flash_calls_are_walked_through_their_run_time_table(
    built: Callable[[Path, str], Path],
) -> None:
    """ESP-IDF's flash operations call through the os-function table startup
    installs (blind spot 3): the walk follows it, so the made-safe test above
    is not vacuous. From a write the walk reaches the wrapped region check,
    which a link without the wrap would replace by ESP-IDF's allocating one;
    from a read, without the bounce buffer's cuts, the heap."""
    _, graph, problems = _image(built)
    assert not [p for p in problems if "run-time edge" in p], problems

    def reached_from(name: str, cuts: Any) -> tuple[set[str], list[str]]:
        walk = gate.Graph(names=graph.names, edges=graph.edges, own={_named(graph, name)})
        report, failures = gate.verdict(walk, rom_leaves={}, cuts=cuts, core_sinks=())
        seen = set()
        queue = [_named(graph, name)]
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            queue += [
                n for n in graph.edges.get(node, ()) if gate._cut(graph, cuts, node, n) is None
            ]
        return {n for node in seen for n in graph.names.get(node, ())}, failures

    names, _ = reached_from("esp_flash_write", gate.CUTS)
    assert "esp_flash_spi1_default_os_functions" in names
    # The chip driver and the host driver, the other two tables startup
    # installs (HM2-02): every operation of the made-safe list reaches them.
    for operation, reached in (
        ("esp_flash_write", ("spi_flash_chip_generic_write", "spi_flash_hal_program_page")),
        ("esp_flash_read", ("spi_flash_chip_generic_read", "memspi_host_read_data_slicer")),
        ("esp_flash_erase_region", ("spi_flash_chip_generic_erase_sector",)),
    ):
        names_of, _ = reached_from(operation, gate.CUTS)
        missing = [name for name in reached if name not in names_of]
        assert not missing, f"{operation} reaches none of {missing}"
    assert "__wrap_esp_partition_is_flash_region_writable" in names
    assert "esp_partition_is_flash_region_writable" not in names
    _, failures = reached_from("esp_flash_read", {})
    assert any("get_buffer_malloc -> heap_caps_malloc" in f for f in failures), failures
    _, failures = reached_from("esp_flash_read", gate.CUTS)
    assert not [f for f in failures if f.startswith("an allocator is reachable")], failures


def test_a_relaxed_long_call_is_an_edge(built: Callable[[Path, str], Path]) -> None:
    """The linker relaxes a long call to ``nop; call8`` and leaves only an
    R_XTENSA_ASM_SIMPLIFY at it (HM2-01): printf's one call, to vfprintf, is
    such a site, and the graph holds it."""
    image, graph, _ = _image(built)
    assert any(r.type == gate.R_XTENSA_ASM_SIMPLIFY for r in image.relocations)
    callees = {
        n for node in graph.edges.get(_named(graph, "printf"), ()) for n in graph.names[node]
    }
    assert "vfprintf" in callees, callees


def test_the_decoder_agrees_with_the_disassembly(built: Callable[[Path, str], Path]) -> None:
    """At every relocated site where objdump prints a target, the decoder finds the same one."""
    elf = built(ELF, BUILD)
    objdump = shutil.which("xtensa-esp32s3-elf-objdump") or str(
        TOOLCHAIN / "xtensa-esp32s3-elf-objdump"
    )
    built(Path(objdump), "the firmware's toolchain (`pio run --project-dir firmware -e meter`)")
    listing = subprocess.run(
        [objdump, "-d", str(elf)], capture_output=True, text=True, check=True
    ).stdout
    printed = {}
    for line in listing.splitlines():
        found = re.match(r"^ *([0-9a-f]+):\t[0-9a-f ]+\t\S+\s+.*?\b([0-9a-f]{8}) <", line)
        if found:
            printed[int(found.group(1), 16)] = int(found.group(2), 16)
    image = gate.read_image(elf)
    layout = gate._Layout(image)
    compared = 0
    wrong = []
    for reloc in image.relocations:
        if reloc.type != gate.R_XTENSA_SLOT0_OP or reloc.site not in printed:
            continue
        decoded = gate.decode_target(reloc.site, layout.word(reloc.site, 3))
        compared += 1
        if decoded is None or decoded[0] != printed[reloc.site]:
            wrong.append(f"{reloc.site:#x}: {decoded} != {printed[reloc.site]:#x}")
    assert compared > 1000, compared
    assert not wrong, "\n".join(wrong[:20])
