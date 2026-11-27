#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The IRAM gate: nothing a conversion executes is fetched from flash.

    python3 scripts/check_iram_hot_path.py \\
        --objdump <xtensa-esp32s3-elf-objdump> --nm <xtensa-esp32s3-elf-nm> \\
        firmware/.pio/build/meter

The ESP32-S3's instruction cache is shared by both cores, so code the sampler
runs from flash on Core 1 waits for every line Core 0's work has evicted
(``docs/architecture.md`` SS 5.2). This walks the call graph of the
measurement's hot path from the relocations of every object the image was
linked from (the firmware's own ``src/*.o`` and every ESP-IDF archive), which
names each call and each literal-pool reference exactly, where a disassembly
of the image can lose an edge to a misdecoded instruction. The walk starts at
:data:`ROOTS` (the sampler task's loop, which reaches the own I2C master's
transactions through the INA228's driver, the ALERT pin's handler, which the
port's dispatcher calls from the CPU line it is routed to, and UART0's
handler on Core 0, whose own line stays enabled while the flash is
written), does not follow the failure
exits :data:`STOP` matches, and stops at the functions :data:`COLD` names,
each with the reason no conversion runs it. Then it reads where the linked
image put each function it reached, and fails:

* on a reached function in a flash section (``.flash.*``);
* on a named constant a reached function loads (``.rodata.<name>``, not a
  string or ``__func__``) that the image keeps in flash;
* on a root it cannot find, and on a :data:`COLD` entry the walk never meets
  (a stale exception is a false statement about the image).

It prints how many functions the hot path reaches and where, and the cold
exits it left in flash, the counts CI writes to the job summary.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: Where the walk starts: what runs on Core 1 for every conversion, and
#: UART0's handler on Core 0 (FW-12), which must not fetch from flash for a
#: reason of its own: its line is the firmware's, routed with no allocator,
#: and ESP-IDF leaves such a line enabled while the flash is written
#: (``esp_intr_noniram_disable()`` masks only the lines its allocator gave a
#: handler outside IRAM), so it runs with the cache off.
ROOTS = (
    "_ZN7kleidos5meter7Sampler10runCaptureEv",  # Sampler::runCapture(), the loop
    "_ZN7kleidos5meter7Sampler8alertIsrEPv",  # Sampler::alertIsr(), ALERT's line's handler
    "_ZN7kleidos5meter5uart012_GLOBAL__N_1L11onInterruptEPv",  # uart0's onInterrupt()
)

#: Failure exits, never taken on a sample: not followed.
STOP = re.compile(
    r"abort|panic|__assert|esp_log|_esp_error_check|esp_rom_printf|__stack_chk|esp_system_abort"
)

#: Reached, left in flash, and why no conversion runs them. The walk stops at
#: each and does not follow it.
COLD = {
    # watchdog::subscribe(), FW-11: the own watchdog's check-in, checkIn(), is in IRAM.
    "_ZN7kleidos5meter8watchdog9subscribeENS1_5PartyE": "once, when the sampler task starts",
    # i2c_bus's recover(), FW-13: the two resets and the bus clear.
    "_ZN7kleidos5meter7i2c_bus12_GLOBAL__N_1L7recoverERKNS2_5StateE$constprop$0": (
        "a timed-out, faulted or stuck transfer only"
    ),
    # Ina228Driver::transferOnBusCore(): a transaction asked off the bus's core.
    "_ZN7kleidos5meter12Ina228Driver17transferOnBusCoreENS1_7BusCallEPKhjPhj": (
        "a transaction asked from the other core, the console's: the sampler is on the bus's core"
    ),
}

#: Data references that are not a constant the loop reads: strings, assertion
#: names, and the compiler's local constant labels.
STRINGS = re.compile(r"\.str\d|\.str$|__func__|^\.LC")


@dataclass
class Graph:
    """Calls and data references, by function, from the objects' relocations."""

    calls: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    data: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


MEMBER = re.compile(r"^(\S+):\s+file format")
SYMBOL = re.compile(r"^([0-9a-f]{8}) (.{7}) (\S+)\s+([0-9a-f]{8}) (\S+)$")
RECORDS = re.compile(r"^RELOCATION RECORDS FOR \[(.+)\]:")
RELOC = re.compile(r"^([0-9a-f]{8}) (\S+)\s+(\S+?)(?:\+0x([0-9a-f]+))?$")
TEXT = (".text", ".iram1", ".iram0")
DATA = (".rodata", ".data", ".bss", ".sbss", ".sdata", ".srodata", ".dram", ".ext_ram")


#: The functions of each (member, section): start, size and name.
Functions = Mapping[tuple[str, str], list[tuple[int, int, str]]]


def function_sections(symtab: str) -> dict[tuple[str, str], list[tuple[int, int, str]]]:
    """The functions of each (member, section), from ``objdump -t``."""
    out: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    member = ""
    for line in symtab.splitlines():
        if m := MEMBER.match(line):
            member = m.group(1)
        elif (m := SYMBOL.match(line)) and "F" in m.group(2):
            out[(member, m.group(3))].append((int(m.group(1), 16), int(m.group(4), 16), m.group(5)))
    return out


def _function_at(funcs: Functions, member: str, section: str, offset: int) -> str | None:
    for start, size, name in funcs.get((member, section), ()):
        if start <= offset < start + max(size, 1):
            return name
    if section.startswith(".text."):
        return section[len(".text.") :]
    return None


def _owner(funcs: Functions, member: str, section: str, offset: int) -> str | None:
    """The function a relocation in *section* at *offset* belongs to."""
    if section.startswith(".literal."):
        return section[len(".literal.") :]
    if section.endswith(".literal"):
        named = funcs.get((member, section[: -len(".literal")]), [])
        return named[0][2] if len(named) == 1 else None
    if section.startswith(TEXT):
        return _function_at(funcs, member, section, offset)
    return None


def add_relocations(graph: Graph, relocations: str, funcs: Functions) -> None:
    """Adds the edges of one ``objdump -r`` listing to *graph*."""
    member = ""
    section = None
    for line in relocations.splitlines():
        if m := MEMBER.match(line):
            member = m.group(1)
            continue
        if m := RECORDS.match(line):
            section = m.group(1)
            continue
        m = RELOC.match(line)
        if not m or section is None:
            continue
        source = _owner(funcs, member, section, int(m.group(1), 16))
        if source is None:
            continue
        value = m.group(3)
        addend = int(m.group(4), 16) if m.group(4) else 0
        if value.startswith(".literal") or value.endswith(".literal"):
            continue
        if value.startswith(TEXT):
            target = _function_at(funcs, member, value, addend)
            if target and target != source:
                graph.calls[source].add(target)
        elif value.startswith(DATA):
            graph.data[source].add(value)
        elif not value.startswith("."):
            graph.calls[source].add(value)


def walk(
    graph: Graph,
    roots: Iterable[str],
    cold: Mapping[str, str],
    stop: re.Pattern[str] = STOP,
) -> dict[str, str | None]:
    """Every function reachable from *roots*, each with the caller it was met from."""
    seen: dict[str, str | None] = {}
    queue: deque[tuple[str, str | None]] = deque((root, None) for root in roots)
    while queue:
        name, caller = queue.popleft()
        if name in seen:
            continue
        seen[name] = caller
        if stop.search(name) or name in cold:
            continue
        queue.extend(
            (callee, name) for callee in sorted(graph.calls.get(name, ())) if callee not in seen
        )
    return seen


@dataclass
class Image:
    """Where the linked image put each symbol."""

    sections: list[tuple[str, int, int]]
    address: dict[str, int]
    kind: dict[str, str]

    def section_of(self, name: str) -> str | None:
        """The output section of *name*, ``None`` when the image has no such symbol."""
        addr = self.address.get(name)
        if addr is None:
            return None
        for section, start, size in self.sections:
            if size and start <= addr < start + size:
                return section
        return "(absolute)"


def parse_image(headers: str, symbols: str) -> Image:
    """An :class:`Image` from ``objdump -h`` and ``nm`` of the ELF."""
    sections = []
    for line in headers.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0].isdigit():
            sections.append((parts[1], int(parts[3], 16), int(parts[2], 16)))
    address: dict[str, int] = {}
    kind: dict[str, str] = {}
    for line in symbols.splitlines():
        parts = line.split()
        if len(parts) == 3:
            address.setdefault(parts[2], int(parts[0], 16))
            kind.setdefault(parts[2], parts[1])
    return Image(sections, address, kind)


def verdict(
    graph: Graph,
    image: Image,
    roots: Iterable[str] = ROOTS,
    cold: Mapping[str, str] = COLD,
    stop: re.Pattern[str] = STOP,
    listing: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """The report lines and the failures, for the walk over *graph* placed by *image*.

    *listing*, when given, receives one ``<section> <name>`` line per hot function.
    """
    failures = [
        f"root {root} is not in the image" for root in roots if image.section_of(root) is None
    ]
    seen = walk(graph, roots, cold, stop)
    hot: dict[str, int] = defaultdict(int)
    for name, caller in sorted(seen.items()):
        section = image.section_of(name)
        if (
            section is None
            or section == "(absolute)"
            or image.kind.get(name, "t").lower() not in ("t", "w")
        ):
            continue  # a weak undefined, an absolute (ROM, register block), or data
        if stop.search(name):
            continue
        if name in cold:
            continue
        hot[section] += 1
        if listing is not None:
            listing.append(f"{section} {name}")
        if section.startswith(".flash"):
            failures.append(f"{name} is in {section}, reached from {caller}")
        for ref in sorted(graph.data.get(name, ())):
            if not ref.startswith(".rodata.") or STRINGS.search(ref):
                continue
            constant = ref[len(".rodata.") :]
            placed = image.section_of(constant)
            if placed is not None and placed.startswith(".flash"):
                failures.append(f"{name} loads {constant}, which is in {placed}")
    failures += [
        f"cold exit {name} is never reached: the list is stale" for name in cold if name not in seen
    ]
    report = [
        f"hot path: {count} function(s) in {section}" for section, count in sorted(hot.items())
    ]
    report += [
        f"cold, left in {image.section_of(name)}: {name} ({reason})"
        for name, reason in cold.items()
        if name in seen
    ]
    return report, failures


def run(*argv: str) -> str:
    """The standard output of one tool run."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=True).stdout


def demangle(cxxfilt: str, lines: list[str]) -> list[str]:
    """*lines* with every C++ name demangled, or unchanged when c++filt cannot run."""
    if not lines:
        return lines
    try:
        out = subprocess.run(
            [cxxfilt], input="\n".join(lines), capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return lines
    return out.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--objdump", default="objdump", help="the toolchain's objdump")
    parser.add_argument("--nm", default="nm", help="the toolchain's nm")
    parser.add_argument("--cxxfilt", help="the toolchain's c++filt (default: beside --nm)")
    parser.add_argument(
        "--list", action="store_true", help="also list every hot function and its section"
    )
    parser.add_argument(
        "build", type=Path, help="the env's build directory (firmware/.pio/build/meter)"
    )
    args = parser.parse_args(argv)

    objects = sorted(args.build.glob("src/*.o")) + sorted(args.build.glob("esp-idf/**/*.a"))
    if not objects:
        print(f"no objects under {args.build}", file=sys.stderr)
        return 1
    graph = Graph()
    for obj in objects:
        funcs = function_sections(run(args.objdump, "-t", str(obj)))
        add_relocations(graph, run(args.objdump, "-r", str(obj)), funcs)
    elf = args.build / "firmware.elf"
    image = parse_image(run(args.objdump, "-h", str(elf)), run(args.nm, str(elf)))

    cxxfilt = args.cxxfilt or (
        args.nm[: -len("nm")] + "c++filt" if args.nm.endswith("nm") else "c++filt"
    )
    listing: list[str] = []
    report, failures = verdict(graph, image, listing=listing if args.list else None)
    for line in demangle(cxxfilt, listing + report):
        print(line)
    for line in demangle(cxxfilt, failures):
        print(f"FAIL: {line}")
    print(f"{len(objects)} object(s) and archive(s), {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
