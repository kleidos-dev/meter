#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The heap census: every allocation and free a census build saw, by site.

A census build is the plain image compiled with ``METER_HEAP_CENSUS`` defined
(``firmware/src/heap_monitor.h``), with no environment of its own::

    PLATFORMIO_BUILD_SRC_FLAGS=-DMETER_HEAP_CENSUS \\
        pio run --project-dir firmware -e meter -t upload --upload-port "$KLEIDOS_METER_PORT"

Its heap hooks keep every event from the first allocation of ESP-IDF's
startup in a static ring of 384, with its size, its capabilities, its block,
whether it came before ``app_main()``'s first line, and the call sites above
the hook, and its console answers ``DIAGnostic:HEAP:CENSus?`` and
``DIAGnostic:HEAP:CENSus:EVENt? <n>`` (``docs/scpi.md``). This script reads
every event the ring holds, symbolizes the call sites with the build's
``firmware.elf`` and prints the census as a Markdown table, one row per site
and phase::

    host/.venv/bin/python scripts/heap_census.py --port "$KLEIDOS_METER_PORT" \\
        --elf firmware/.pio/build/meter/firmware.elf --save census.txt
    host/.venv/bin/python scripts/heap_census.py --from census.txt \\
        --elf firmware/.pio/build/meter/firmware.elf

A **site** is the first call site that is not the allocator: the hook, the
monitor, ``heap_caps_*``, ``multi_heap_*``, the C library's allocator entry
points and FreeRTOS's ``pvPortMalloc`` are climbed over (:data:`ALLOCATOR_NAMES`,
:data:`ALLOCATOR_PREFIXES`), and so are the functions that allocate an object
of their own kind for their caller (a task, a queue, a ring buffer, NVS's and
mbedTLS's wrappers, :data:`WRAPPER_NAMES`), which the ``Through`` column names.
A row thus names the code that asked (an ESP-IDF driver's init, a task's
creator), the innermost frame of the firmware's own code above it (``From``:
``led::blankAll``, ``Console::begin``, or ``ESP-IDF`` for its startup and its
own tasks), and, with ``--chains``, every frame above it. A first table sums
the events by phase and ``From``. ``--save`` keeps the
instrument's answers as they came, which ``--from`` reads again; the
port's own reading needs the host package (``kleidos_meter``) and pyserial.

The census's per-phase totals are the image's own counts: a census build and
the plain image allocate the same (the ring is static), so the totals equal
``DIAGnostic:HEAP?``'s ``pre_main_allocs`` and ``allocs_since_main`` read on
the plain image at the same point.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The toolchain's symboliser, where PlatformIO installs it.
DEFAULT_ADDR2LINE = (
    Path.home()
    / ".platformio"
    / "packages"
    / "toolchain-xtensa-esp-elf"
    / "bin"
    / "xtensa-esp32s3-elf-addr2line"
)

#: Function names that are the allocator itself, climbed over to find a site.
ALLOCATOR_NAMES = frozenset(
    {
        "malloc",
        "calloc",
        "realloc",
        "free",
        "_malloc_r",
        "_calloc_r",
        "_realloc_r",
        "_free_r",
        "aligned_alloc",
        "memalign",
        "posix_memalign",
        "strdup",
        "strndup",
        "pvPortMalloc",
        "vPortFree",
    }
)

#: Prefixes of function names that are the allocator or the census itself.
ALLOCATOR_PREFIXES = (
    "heap_caps_",
    "multi_heap_",
    "tlsf_",
    "esp_heap_trace_",
    "kleidos::meter::heap::",
    "operator new",
    "operator delete",
)

#: Functions that allocate on their caller's behalf, an object of their own
#: kind (a FreeRTOS task, queue, semaphore or ring buffer, NVS's and mbedTLS's
#: allocation wrappers): climbed over too, so that the site is the code that
#: asked for the object, and named in the table's ``Through`` column.
WRAPPER_NAMES = frozenset(
    {
        "xTaskCreatePinnedToCore",
        "xTaskCreate",
        "prvDeleteTCB",
        "xQueueGenericCreate",
        "xQueueCreateMutex",
        "xQueueCreateCountingSemaphore",
        "xQueueCreateWithCaps",
        "vQueueDelete",
        "vQueueDeleteWithCaps",
        "xSemaphoreCreateGenericWithCaps",
        "vSemaphoreDeleteWithCaps",
        "xRingbufferCreate",
        "xRingbufferCreateWithCaps",
        "vRingbufferDelete",
        "vRingbufferDeleteWithCaps",
        "esp_mbedtls_mem_calloc",
        "esp_mbedtls_mem_free",
    }
)

#: Prefixes of wrapper names (NVS's allocator class, whose members are C++).
WRAPPER_PREFIXES = ("ExceptionlessAllocatable::",)

#: What a symboliser gives for an address: (function, ``file:line``).
Symbol = tuple[str, str]


@dataclass(frozen=True)
class Event:
    """One census event, as ``DIAGnostic:HEAP:CENSus:EVENt?`` reports it.

    :ivar index: Its place since the first hook call.
    :ivar allocation: An allocation, else a free.
    :ivar before_main: Before ``app_main()``'s first line.
    :ivar size: Bytes asked for (0 for a free).
    :ivar caps: The capabilities asked for.
    :ivar block: The block's address.
    :ivar callers: The call sites, innermost first.
    """

    index: int
    allocation: bool
    before_main: bool
    size: int
    caps: int
    block: int
    callers: tuple[int, ...]


@dataclass
class Census:
    """The ring as read: its summary and the events it held.

    :ivar capacity: Slots of the ring.
    :ivar events: Events recorded since the first hook call.
    :ivar first: The oldest event held; above 0, older ones were lost.
    :ivar held: The events read, oldest first.
    """

    capacity: int
    events: int
    first: int
    held: list[Event] = field(default_factory=list)


def fields(line: str) -> dict[str, str]:
    """A report line's ``key=value`` fields.

    :param line: One response unit.
    :returns: The fields; a token with no ``=`` is ignored.
    """
    out = {}
    for token in line.split():
        key, sep, value = token.partition("=")
        if sep:
            out[key] = value
    return out


def parse_summary(line: str) -> Census:
    """``DIAGnostic:HEAP:CENSus?``'s answer.

    :raises ValueError: When a key is missing or the census is not built.
    """
    report = fields(line)
    census = Census(int(report["capacity"]), int(report["events"]), int(report["first"]))
    if census.capacity == 0:
        raise ValueError("the image keeps no census (capacity=0): build it with METER_HEAP_CENSUS")
    return census


def parse_event(line: str) -> Event:
    """``DIAGnostic:HEAP:CENSus:EVENt?``'s answer.

    :raises ValueError: When a key is missing or a value is not a number.
    """
    report = fields(line)
    if report["op"] not in ("alloc", "free") or report["phase"] not in ("pre", "main"):
        raise ValueError(f"not a census event: {line!r}")
    callers = report["callers"]
    return Event(
        index=int(report["n"]),
        allocation=report["op"] == "alloc",
        before_main=report["phase"] == "pre",
        size=int(report["size"]),
        caps=int(report["caps"], 16),
        block=int(report["block"], 16),
        callers=() if callers == "-" else tuple(int(site, 16) for site in callers.split(",")),
    )


def parse_saved(lines: Iterable[str]) -> Census:
    """A census as ``--save`` wrote it: the summary line, then one line per event."""
    rows = [line.strip() for line in lines if line.strip()]
    if not rows:
        raise ValueError("an empty census file")
    census = parse_summary(rows[0])
    census.held = [parse_event(row) for row in rows[1:]]
    return census


def read_census(query: Callable[[str], str]) -> tuple[Census, list[str]]:
    """Reads every event the ring holds through *query*.

    :param query: Sends one query and returns its response unit; raises when
        the instrument refused it.
    :returns: The census and the lines as they came, for ``--save``.
    """
    summary = query("DIAGnostic:HEAP:CENSus?")
    census = parse_summary(summary)
    lines = [summary]
    for index in range(census.first, census.events):
        line = query(f"DIAGnostic:HEAP:CENSus:EVENt? {index}")
        census.held.append(parse_event(line))
        lines.append(line)
    return census, lines


def addr2line_symboliser(elf: Path, tool: Path) -> Callable[[Sequence[int]], dict[int, Symbol]]:
    """A symboliser over the toolchain's ``addr2line`` and the build's ELF.

    :returns: A function from addresses to (function, ``file:line``), one
        ``addr2line`` run for all of them.
    """

    def symbolize(addresses: Sequence[int]) -> dict[int, Symbol]:
        unique = sorted(set(addresses))
        if not unique:
            return {}
        result = subprocess.run(
            [str(tool), "-f", "-C", "-e", str(elf), *(f"0x{a:08x}" for a in unique)],
            check=True,
            capture_output=True,
            text=True,
        )
        lines = result.stdout.splitlines()
        if len(lines) != 2 * len(unique):
            raise RuntimeError(f"{tool.name} gave {len(lines)} lines for {len(unique)} addresses")
        return {a: (lines[2 * i], lines[2 * i + 1]) for i, a in enumerate(unique)}

    return symbolize


def short_path(location: str) -> str:
    """``file:line`` cut to the part a reader needs: from the component or ``src/`` on."""
    for marker in ("/components/", "/firmware/src/", "/src/"):
        at = location.rfind(marker)
        if at >= 0:
            return location[at + 1 :]
    return location.rsplit("/", 1)[-1]


def is_allocator(function: str) -> bool:
    """Whether *function* is the allocator, or the hook and the census."""
    return function in ALLOCATOR_NAMES or function.startswith(ALLOCATOR_PREFIXES)


def is_wrapper(function: str) -> bool:
    """Whether *function* allocates an object of its own kind for its caller."""
    return function in WRAPPER_NAMES or function.startswith(WRAPPER_PREFIXES)


#: Where the firmware's own sources are, as :func:`short_path` writes them.
OWN_SOURCES = "firmware/src/"


def own_name(function: str) -> str:
    """A firmware function's name without its namespace and its parameters."""
    name = function
    for prefix in ("kleidos::meter::", "(anonymous namespace)::"):
        name = name.removeprefix(prefix)
    return name.split("(", 1)[0]


@dataclass(frozen=True)
class Site:
    """Where one event came from.

    :ivar site: ``function (file:line)`` of the first frame that is neither
        the allocator nor a wrapper; the innermost wrapper when nothing above
        it could be read; ``unknown`` when not even that.
    :ivar via: The innermost wrapper's name, empty when the site called the
        allocator itself.
    :ivar owner: The innermost frame of the firmware's own code
        (``firmware/src``), its name without namespace and parameters;
        ``ESP-IDF`` when no frame is ours (ESP-IDF's startup and its own
        tasks).
    :ivar chain: Every frame from the site outward.
    """

    site: str
    via: str
    owner: str
    chain: tuple[str, ...]


def site_of(event: Event, symbols: dict[int, Symbol]) -> Site:
    """The event's :class:`Site`, read from its call sites."""
    frames = [
        (function, short_path(location))
        for function, location in (symbols.get(a, ("??", "??:0")) for a in event.callers)
    ]
    owner = next(
        (
            own_name(f)
            for f, location in frames
            if location.startswith(OWN_SOURCES) and not is_allocator(f)
        ),
        "ESP-IDF",
    )
    via = ""
    wrapper_at = -1
    for at, (function, location) in enumerate(frames):
        if is_allocator(function):
            continue
        if is_wrapper(function):
            if not via:
                via, wrapper_at = function, at
            continue
        chain = tuple(f"{f} ({loc})" for f, loc in frames[at:])
        return Site(f"{function} ({location})", via, owner, chain)
    if wrapper_at >= 0:
        function, location = frames[wrapper_at]
        return Site(f"{function} ({location})", via, owner, (f"{function} ({location})",))
    return Site("unknown (no frame outside the allocator)", via, owner, ())


@dataclass
class Row:
    """One row of the table: a phase, an operation and a site."""

    phase: str
    op: str
    site: Site
    count: int = 0
    bytes: int = 0


def phase_of(event: Event) -> str:
    """``pre_main`` or ``since_main``."""
    return "pre_main" if event.before_main else "since_main"


def tabulate(census: Census, symbols: dict[int, Symbol]) -> list[Row]:
    """The events grouped by phase, operation and site, largest count first in each phase."""
    rows: dict[tuple[str, str, str, str], Row] = {}
    for event in census.held:
        op = "alloc" if event.allocation else "free"
        where = site_of(event, symbols)
        key = (phase_of(event), op, where.site, where.via)
        row = rows.setdefault(key, Row(phase_of(event), op, where))
        row.count += 1
        row.bytes += event.size
    order = {"pre_main": 0, "since_main": 1}
    return sorted(rows.values(), key=lambda r: (order[r.phase], r.op, -r.count, r.site.site))


def by_owner(rows: Sequence[Row]) -> list[tuple[str, str, int, int, int]]:
    """The rows summed by phase and owner: (phase, owner, allocations, frees, bytes)."""
    sums: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        entry = sums.setdefault((row.phase, row.site.owner), [0, 0, 0])
        entry[0 if row.op == "alloc" else 1] += row.count
        entry[2] += row.bytes
    order = {"pre_main": 0, "since_main": 1}
    return sorted(
        ((phase, owner, a, f, b) for (phase, owner), (a, f, b) in sums.items()),
        key=lambda item: (order[item[0]], -item[2], item[1]),
    )


def render(census: Census, rows: list[Row], *, chains: bool = False) -> str:
    """The census as Markdown: the totals by phase and by owner, then one row per site."""
    totals = {(p, o): 0 for p in ("pre_main", "since_main") for o in ("alloc", "free")}
    for row in rows:
        totals[(row.phase, row.op)] += row.count
    out = [
        f"Census: {census.events} event(s), ring of {census.capacity}, {len(census.held)} held.",
        "",
        f"- before app_main: {totals[('pre_main', 'alloc')]} allocation(s), "
        f"{totals[('pre_main', 'free')]} free(s)",
        f"- since app_main: {totals[('since_main', 'alloc')]} allocation(s), "
        f"{totals[('since_main', 'free')]} free(s)",
    ]
    if census.first > 0:
        out.append(
            f"- **{census.first} event(s) were overwritten before they were read**: "
            "the totals above are not the image's"
        )
    out += ["", "| Phase | From | Allocs | Frees | Bytes |", "|---|---|---:|---:|---:|"]
    for phase, owner, allocs, frees, size in by_owner(rows):
        out.append(f"| {phase} | {owner} | {allocs} | {frees} | {size} |")
    out += [
        "",
        "| Phase | Op | Count | Bytes | Site | Through | From |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for row in rows:
        site = row.site.site
        if chains and len(row.site.chain) > 1:
            site += " <- " + " <- ".join(row.site.chain[1:])
        through = row.site.via or "-"
        out.append(
            f"| {row.phase} | {row.op} | {row.count} | {row.bytes} | {site} | {through} "
            f"| {row.site.owner} |"
        )
    return "\n".join(out) + "\n"


def meter_query(port: str) -> tuple[Callable[[str], str], Callable[[], None]]:
    """A query function over the instrument's line, and the call that closes it.

    Needs the host package and pyserial (``host/.venv``).
    """
    from kleidos_meter.client import Meter, SerialTransport

    meter = Meter(SerialTransport(port))
    meter.wait_ready()

    def query(text: str) -> str:
        return meter.query(text)

    return query, meter.close


def main(argv: Sequence[str] | None = None) -> int:
    """The command line (module docstring)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--port", help="read a census build's ring from this port")
    source.add_argument("--from", dest="saved", type=Path, help="read a census --save wrote")
    parser.add_argument("--elf", type=Path, required=True, help="the census build's firmware.elf")
    parser.add_argument("--addr2line", type=Path, default=None, help="the toolchain's addr2line")
    parser.add_argument("--save", type=Path, help="keep the instrument's answers here")
    parser.add_argument("--chains", action="store_true", help="show the frames above each site")
    args = parser.parse_args(argv)

    tool = args.addr2line or (
        DEFAULT_ADDR2LINE
        if DEFAULT_ADDR2LINE.exists()
        else Path(shutil.which("xtensa-esp32s3-elf-addr2line") or DEFAULT_ADDR2LINE)
    )
    if not args.elf.exists():
        parser.error(f"{args.elf} does not exist")
    if args.saved is not None:
        census = parse_saved(args.saved.read_text(encoding="utf-8").splitlines())
    else:
        query, close = meter_query(args.port)
        try:
            census, lines = read_census(query)
        finally:
            close()
        if args.save is not None:
            args.save.write_text("\n".join(lines) + "\n", encoding="utf-8")
    symbols = addr2line_symboliser(args.elf, tool)(
        [address for event in census.held for address in event.callers]
    )
    sys.stdout.write(render(census, tabulate(census, symbols), chains=args.chains))
    return 0 if census.first == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
