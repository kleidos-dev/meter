#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The reachability gate: no allocator is reachable from the firmware's own code.

    python3 scripts/check_heap_reachability.py \\
        --elf firmware/.pio/build/meter/firmware.elf firmware/.pio/build/meter/src/*.o

The source gate reads what the firmware's sources say, the object gate what
its objects reference by name, and the device counts what one run allocated.
None of them sees an allocation two calls deep in ESP-IDF, behind a call the
firmware makes (``docs/misra.md``, "The gates"). This gate does: it builds the
call graph of the **linked image** and fails when an allocator (:data:`SINKS`)
is reachable from any function of the firmware's own code, every one of them a
root, whether or not anything calls it.

The graph is read from the image's own relocations, which the device link
keeps (``-Wl,--emit-relocs``, ``firmware/CMakeLists.txt``: the loaded bytes
are the same, only the ELF file carries the records). Each relocation marks a
place that holds an address, so no instruction is decoded but at a relocated
site, where a disassembly of the image would decode literal pools and
alignment bytes as instructions and invent calls:

* a **direct call or jump** (``callN``, ``j`` and the branches, Xtensa's
  ``R_XTENSA_SLOT0_OP``): its target is decoded from the instruction's
  PC-relative offset;
* a **long call the linker relaxed** to a direct one (``R_XTENSA_ASM_SIMPLIFY``,
  where ``l32r``; ``callx8`` became ``nop``; ``call8``, the call keeping no
  relocation of its own): the ``call8`` after the ``nop`` is decoded, or, if
  it is not one, the relocation's symbol and addend are the target; and an
  ``R_XTENSA_ASM_EXPAND`` (a long call left as it is) names its callee, a
  function or a section;
* a **literal load** (``l32r``, the same relocation): its target is the
  literal-pool word it loads, and the word's own relocation (``R_XTENSA_32``)
  says what the word holds; its value, resolved by the linker, is the address:
  the *resolved literal*. A long call (``l32r`` of the callee, then
  ``callx8``) and a function's address taken for a callback or a table are
  both this edge;
* an **address in data** (``R_XTENSA_32`` in ``.rodata``, ``.data``, a literal
  pool): an edge from the object, or the unnamed region, that holds it.

A node is a function or a data object of the symbol table (its address range),
a literal-pool word, or an unnamed region up to the next symbol; the unnamed
ones are contracted away, so what is reported, cut and listed is named. An
address in the ROM's code range (:data:`ROM_CODE`) is a ROM function, read off
the absolute symbols the ROM linker scripts define.

It fails:

* on an allocator reached from an own function, printing the path;
* on a ROM function reached that :data:`ROM_LEAVES` does not name: the ROM's
  code is not in the image, so the gate cannot read what it calls, and each
  one reached is listed with the reason it allocates nothing;
* on an edge of :data:`CUTS` the walk never meets, or a :data:`ROM_LEAVES`
  entry it never reaches (a stale exception is a false statement about the
  image);
* on an image with no relocations, an image holding none of the allocators
  (renamed, so the names would no longer find them), an own object it cannot
  match to the image (its file name twice among the image's file symbols, or
  its local functions absent while one of its global functions is linked),
  and no own function found. An own object the link left out whole (no global
  function of it in the image and no file symbol for its locals: nothing in
  the image calls into it, as with FW-03's pin map store before the boot
  calls it) is reported as not linked: none of its code is in the image to
  reach anything.

Blind spots, each stated and none silent:

1. **Reachability, not feasibility.** An edge is any address a function or
   table holds; a call that a configuration makes impossible is still an
   edge, and a data reference is taken as a call. Over-approximation only
   raises false alarms; each is answered by an edge of :data:`CUTS` with the
   reason it never runs, and a cut must be met.
2. **The ROM** is not read: a call into it ends there, and :data:`ROM_LEAVES`
   carries the claim that it allocates nothing and calls nothing back through
   a table set at run time.
3. **Addresses made at run time** from something other than a relocated value
   (an offset added to a base, a pointer read from a register block or from
   memory the program filled with a non-relocated number) are not edges, and
   neither is a call through a table that code the walk does not start from
   installs (a relocated table set by startup code, which is not a root,
   reached from a caller that only reads the pointer to it). The firmware's
   callbacks (task functions, interrupt handlers, the IPC call) are relocated
   literals of the code that registers them, and are own functions, so roots,
   anyway; each table ESP-IDF's startup installs and the firmware's calls go
   through is named in :data:`RUNTIME_EDGES` (the flash operations' three:
   the os-function table, the chip drivers and the host driver), which adds
   the edge by name and fails as stale when the image lacks it.
4. **Thread-local accesses** (``R_XTENSA_TLS_*``) carry an offset, not an
   address: they are taken as a reference to the whole TLS template
   (``.flash.tdata``), so whatever its words point to is reached.
5. **What runs without an own caller** (ESP-IDF's startup, its IPC and idle
   tasks, its tick and cross-core interrupts) is not a root: the device's
   ``DIAGnostic:HEAP?`` counts it (``pre_main_allocs``, ``allocs_since_main``)
   and the bench suite holds those counts.
6. **The sinks are names**: an allocator called by another name would be
   missed. :data:`SINKS` lists the heap's public entry points, the multi-heap
   layer and the TLSF allocator under them, and the C++ operators, and the
   gate fails when :data:`CORE_SINKS` are not all in the image.
7. A function with no size (assembly) spans to the next symbol.

It prints how many own functions it walked from, how many functions it
reached, the ROM functions among them, and the cuts it met: the counts CI
writes to the job summary.

    python3 scripts/check_heap_reachability.py --selftest

plants each case in an image it writes itself and checks the gate's verdict on
it, with no toolchain. Reading an image needs pyelftools (``pip install
pyelftools==0.33``, CI's firmware and guards jobs).
"""

from __future__ import annotations

import argparse
import bisect
import re
import struct
import sys
import tempfile
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The allocators: ESP-IDF's heap entry points, the multi-heap layer and the
#: TLSF allocator under them, the C library's, FreeRTOS's and the C++
#: operators. A name matched here is never walked through: reaching it is the
#: failure.
SINKS = re.compile(
    r"^(?:malloc|calloc|realloc|reallocarray|free|cfree|valloc|pvalloc|memalign|aligned_alloc"
    r"|posix_memalign|strdup|strndup|asprintf|vasprintf|_malloc_r|_calloc_r|_realloc_r|_free_r"
    r"|pvPortMalloc|vPortFree|pvPortCalloc"
    r"|heap_caps_(?:malloc|calloc|realloc|free|aligned_alloc|aligned_calloc|aligned_free)\w*"
    r"|multi_heap_(?:malloc|free|realloc|aligned_alloc|aligned_free)(?:_impl)?(?:_offs)?"
    r"|multi_heap_aligned_alloc_impl_offs"
    r"|tlsf_(?:malloc|free|realloc|memalign)\w*"
    r"|_Znwj\w*|_Znaj\w*|_ZdlPv\w*|_ZdaPv\w*)$"
)

#: Allocators every image holds (ESP-IDF's startup allocates through them):
#: if one is missing the names above no longer find the allocator. Not
#: ``malloc``: the image links it wrapped (``-Wl,--wrap=malloc``, the review of
#: 2026-11-24), so a call of it is a call of the firmware's own
#: ``__wrap_malloc()`` and the C library's leaves the image with its callers.
CORE_SINKS = ("calloc", "free", "heap_caps_malloc", "heap_caps_free", "multi_heap_malloc")

#: The ROM's code (ESP32-S3's internal ROM, as ESP-IDF 6.1.1's
#: ``esp32s3.rom.ld`` places its functions): an address here is a ROM
#: function. Outside the image's sections and this range an address is a
#: register block or ROM data, and is not an edge.
ROM_CODE = (0x40000000, 0x40060000)

#: The ROM functions the firmware's code reaches, each with why it allocates
#: nothing: a leaf, or one whose callees are other leaves of the ROM. A ROM
#: function is matched by any of its names.
ROM_LEAVES: dict[str, str] = {
    "__divdi3": "libgcc's 64-bit division: arithmetic",
    "__moddi3": "libgcc's 64-bit remainder: arithmetic",
    "__udivdi3": "libgcc's 64-bit unsigned division: arithmetic",
    "__umoddi3": "libgcc's 64-bit unsigned remainder: arithmetic",
    "__popcountsi2": "libgcc's population count: arithmetic",
    "_xtos_set_intlevel": "writes PS.INTLEVEL: one instruction and a return",
    "esp_rom_delay_us": "a cycle-counted busy wait",
    "esp_rom_printf": (
        "the ROM's printf, reached from ESP-IDF's xt_unhandled_interrupt (the firmware"
        " calls none since FW-26): formats onto the stack and writes through the ROM's"
        " putc routine (a UART FIFO write), with no buffer of its own"
    ),
    "esp_rom_output_putc": (
        "the ROM's console character output (ets_write_char_uart): one UART FIFO"
        " write, a newline as CR LF, with no buffer of its own (app_main()'s"
        " fallback, FW-26)"
    ),
    "_regi2c_impl_write_mask": (
        "a register write over the analog I2C bus (ESP-IDF 6.1's name for the ROM"
        " function 6.0 called esp_rom_regi2c_write_mask)"
    ),
    "esp_rom_route_intr_matrix": "writes one interrupt matrix register",
    "itoa": "formats into the caller's buffer",
    "memchr": "finds a byte in a buffer (chars::untilNul(), FW-25)",
    "memcmp": "compares two buffers",
    "memcpy": "copies between two buffers",
    "memmove": "copies between two buffers",
    "memset": "fills a buffer",
    "strlen": "measures a string",
    "strcat": "appends one string to another in the caller's buffer (abort()'s message)",
    "rom_Cache_Suspend_ICache": "suspends the instruction cache: a register sequence",
    "Cache_Resume_ICache": "resumes the instruction cache: a register sequence",
    "Cache_Invalidate_Addr": (
        "invalidates the cache lines of an address range (the flash host driver's"
        " cache flush after a write, HM2-02): a register sequence"
    ),
    "rom_Cache_Suspend_DCache": "suspends the data cache: a register sequence",
    "Cache_Resume_DCache": "resumes the data cache: a register sequence",
}


@dataclass(frozen=True)
class Cut:
    """An edge the image holds and that never runs, and what would make it run.

    :ivar reason: Why the edge never runs in this image.
    :ivar guards: The functions through which it would run: the cut holds only
        while the walk reaches none of them, and fails as broken once it does.
    """

    reason: str
    guards: tuple[str, ...] = ()


#: What runs a C stream's close or setvbuf operation: the calls no firmware
#: source makes (``tests/firmware/test_no_dynamic_memory.py`` bans the C
#: stream functions, FW-18), held by the walk here for ESP-IDF's code too.
_STREAM_CALLS = (
    "fclose",
    "freopen",
    "fopen",
    "fdopen",
    "setvbuf",
    "setbuf",
    "setbuffer",
    "setlinebuf",
    "__bufio_setvbuf",
    "__wrap___bufio_setvbuf",
    "__bufio_close",
)

#: Edges that the image holds and that never run. The walk does not follow
#: them; each must be met (its source reached and the edge in the graph), or
#: the gate fails on it as stale, and none of its guards may be reached.
#:
#: The C library's formatted and character output and input: each calls a
#: FILE's operations, so reaching one would make the cuts below false. A call
#: the linker relaxed (``R_XTENSA_ASM_SIMPLIFY``) is how printf reaches
#: vfprintf; the gate reads those since the review of 2026-11-24 (HM2-01).
_STREAM_IO = (
    "printf",
    "fprintf",
    "vfprintf",
    "__d_vfprintf",
    "__f_vfprintf",
    "__i_vfprintf",
    "sprintf",
    "fflush",
    "puts",
    "fputs",
    "fwrite",
    "putchar",
    "fputc",
    "putc",
    "getchar",
    "fgetc",
    "getc",
    "fgets",
    "fread",
    "scanf",
    "fscanf",
    "vfscanf",
)

#: picolibc's three FILE objects, ``__stdin``, ``__stdout`` and ``__stderr``
#: (from ESP-IDF 6.1 ``stderr`` is an object of its own; in 6.0 it was
#: ``stdout``), are what every task's TLS template points to, so FreeRTOS's
#: stack set-up, which copies the template, reaches them; a FILE holds its
#: operations, and four of them allocate: close frees the buffer, setvbuf
#: allocates one, and put and get allocate it on their first use
#: (``__bufio_buffer_allocate_locked()``, ``calloc()``; each of the three has a
#: static buffer in 6.1, so on the image as it boots none does). None runs
#: without a call of a stream function, and the guards fail the gate the day
#: the walk reaches one.
_OPERATIONS = {
    "__bufio_close": "close",
    "__wrap___bufio_setvbuf": "setvbuf",
    "__bufio_put": "put",
    "__bufio_get": "get",
}
CUTS: dict[tuple[str, str], Cut] = {
    (stream, operation): Cut(
        f"picolibc's {stream} FILE, which every task's TLS template points to:"
        f" its {_OPERATIONS[operation]} operation runs only from a C stream function"
        " called on it, which nothing the walk reaches calls",
        _STREAM_CALLS + _STREAM_IO,
    )
    for stream in ("__stdin", "__stdout", "__stderr")
    for operation in _OPERATIONS
}

#: TLSF's default walker prints each block (``printf()``); ``tlsf_walk_pool()``
#: uses it only when its caller passes no walker. The image's one caller,
#: ``multi_heap_get_info_impl()`` (``DIAGnostic:HEAP?``'s free and largest
#: block), passes its own, ``multi_heap_get_info_tlsf()``. Those that pass a
#: caller's walker, which may be null, are the guards.
CUTS[("tlsf_walk_pool", "default_walker")] = Cut(
    "TLSF's default walker, which tlsf_walk_pool() takes only for a null walker:"
    " its one caller in the image, multi_heap_get_info_impl(), passes its own",
    ("multi_heap_walk", "heap_caps_walk", "heap_caps_walk_all", "multi_heap_dump"),
)

#: The bounce buffer of a flash read: ESP-IDF's os-function table (below)
#: holds the functions that take it from the heap and free it, and
#: ``esp_flash_read()`` calls them only for a destination outside internal
#: RAM, which ``flash_log::port::read()`` refuses before the call
#: (``esp_ptr_in_dram()``, held in the tree by
#: ``tests/firmware/test_idf_allocation_lists.py``). A write copies through a
#: buffer on the stack and an erase moves no data, so neither calls them.
#: What would read the flash into a buffer the port's check never saw: ESP-IDF's
#: other readers, each of which hands ``esp_flash_read()`` its caller's buffer.
_FLASH_READS = (
    "esp_partition_read",
    "esp_partition_read_raw",
    "spi_flash_read",
    "esp_flash_read_encrypted",
    "bootloader_flash_read",
)

CUTS.update(
    {
        ("esp_flash_spi1_default_os_functions", operation): Cut(
            f"ESP-IDF's flash os-function table: its {what} runs only for a flash read"
            " into memory outside internal RAM, which flash_log::port::read() refuses"
            " (esp_ptr_in_dram)",
            _FLASH_READS,
        )
        for operation, what in (
            ("get_buffer_malloc", "bounce buffer's allocation"),
            ("release_buffer_malloc", "bounce buffer's free"),
        )
    }
)

#: Calls the image makes through a table that startup code installs, which
#: the relocations do not show as edges of the caller (blind spot 3). Every
#: ``esp_flash_*`` operation the firmware calls goes through three, which
#: ESP-IDF's startup sets on the default chip before ``app_main()``:
#:
#: * its ``os_func``, ``esp_flash_spi1_default_os_functions``
#:   (``esp_flash_app_enable_os_functions()``): the start, end, region check,
#:   yield and bounce buffer;
#: * its ``chip_drv``, one of ``default_registered_chips``, the list
#:   ``detect_spi_flash_chip()`` picks from (``spi_flash_chip_generic_*`` and
#:   the vendors' drivers);
#: * its host's ``driver``, ``esp_flash_default_host`` in
#:   ``memspi_host_driver.c`` (``memspi_host_init_pointers()``: the
#:   ``memspi_host_*`` and ``spi_flash_hal_*`` functions), a name ESP-IDF also
#:   gives the host instance in ``esp_flash_spi_init.c``, so the edge goes to
#:   every node of that name.
#:
#: Each is added as an edge from the caller to the table, by name, and an
#: image that lacks a name fails the gate as stale.
_FLASH_OPERATIONS = ("esp_flash_read", "esp_flash_write", "esp_flash_erase_region")
_FLASH_TABLES = {
    "esp_flash_spi1_default_os_functions": (
        "the default chip's os_func, installed by esp_flash_app_enable_os_functions()"
        " at startup: the operation calls through it"
    ),
    "default_registered_chips": (
        "the default chip's chip_drv, one of the drivers detect_spi_flash_chip() picks"
        " from this list at startup: the operation calls through it"
    ),
    "esp_flash_default_host": (
        "the default chip's host driver, installed by memspi_host_init_pointers() at"
        " startup: the chip driver calls through it"
    ),
}
RUNTIME_EDGES: dict[tuple[str, str], str] = {
    (operation, table): reason
    for operation in _FLASH_OPERATIONS
    for table, reason in _FLASH_TABLES.items()
}

#: Relocation types (``elf/xtensa.h``).
R_XTENSA_32 = 1
R_XTENSA_ASM_EXPAND = 11
R_XTENSA_ASM_SIMPLIFY = 12
R_XTENSA_SLOT0_OP = 20
#: The thread-local types, which carry an offset into the TLS block.
R_XTENSA_TLS = range(50, 57)

#: The section of the TLS template.
TLS_TEMPLATE = ".flash.tdata"


# --------------------------------------------------------------------------
# The image, as the gate reads it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """One symbol of a symbol table, as the gate needs it.

    :ivar name: The symbol's name.
    :ivar value: Its address (the image) or section offset (an object).
    :ivar size: Its size in bytes, 0 when the table gives none.
    :ivar kind: ``func``, ``object``, ``file``, ``section``, ``tls`` or ``other``.
    :ivar local: True for a local (``STB_LOCAL``) symbol.
    :ivar defined: True when the symbol is defined in a section of its file.
    :ivar absolute: True for an absolute (``SHN_ABS``) symbol.
    :ivar weak: True for a weak (``STB_WEAK``) symbol: an inline function or
        a template's instance, which any object that uses it defines.
    """

    name: str
    value: int
    size: int
    kind: str
    local: bool
    defined: bool
    absolute: bool
    weak: bool = False


@dataclass(frozen=True)
class Section:
    """One allocated section of the image.

    :ivar name: The section's name.
    :ivar address: Its load address.
    :ivar size: Its size in bytes.
    :ivar data: Its bytes, empty for a section with no contents (``NOBITS``).
    """

    name: str
    address: int
    size: int
    data: bytes


@dataclass(frozen=True)
class Relocation:
    """One relocation record that the image kept, at its final address.

    :ivar site: The address the relocation patches.
    :ivar type: Its Xtensa relocation type.
    :ivar symbol: The symbol it names (``None`` for none).
    :ivar addend: Its addend.
    """

    site: int
    type: int
    symbol: Symbol | None
    addend: int


@dataclass
class Image:
    """What the gate reads of the linked image.

    :ivar sections: The allocated sections.
    :ivar symbols: The symbol table, in its order (file symbols group the
        locals after them).
    :ivar relocations: Every relocation of an allocated section.
    """

    sections: list[Section]
    symbols: list[Symbol]
    relocations: list[Relocation]


@dataclass
class OwnObject:
    """What the gate reads of one object of the firmware's own component.

    :ivar path: The object file.
    :ivar file: Its file symbol (the source's name, as the image's symbol
        table groups the object's locals under it).
    :ivar globals: The functions it defines with external linkage.
    :ivar locals: The functions it defines with internal linkage.
    :ivar weak: Those of *globals* it defines weak, which other objects
        define too, so that one in the image says nothing of this object.
    """

    path: str
    file: str | None
    globals: set[str] = field(default_factory=set)
    locals: set[str] = field(default_factory=set)
    weak: set[str] = field(default_factory=set)


def _kind(symbol_type: str) -> str:
    return {
        "STT_FUNC": "func",
        "STT_OBJECT": "object",
        "STT_FILE": "file",
        "STT_SECTION": "section",
        "STT_TLS": "tls",
    }.get(symbol_type, "other")


def _symbols(elf: Any) -> list[Symbol]:  # pyelftools' ELFFile, which ships no types
    table = elf.get_section_by_name(".symtab")
    if table is None:
        return []
    return [
        Symbol(
            name=s.name,
            value=s["st_value"],
            size=s["st_size"],
            kind=_kind(s["st_info"]["type"]),
            local=s["st_info"]["bind"] == "STB_LOCAL",
            defined=s["st_shndx"] not in ("SHN_UNDEF", "SHN_ABS", "SHN_COMMON"),
            absolute=s["st_shndx"] == "SHN_ABS",
            weak=s["st_info"]["bind"] == "STB_WEAK",
        )
        for s in table.iter_symbols()
    ]


def read_image(path: Path) -> Image:
    """The sections, symbols and relocations of the linked image at *path* (pyelftools)."""
    from elftools.elf.elffile import ELFFile
    from elftools.elf.relocation import RelocationSection

    with path.open("rb") as stream:
        elf = ELFFile(stream)
        sections = [
            Section(
                s.name,
                s["sh_addr"],
                s["sh_size"],
                b"" if s["sh_type"] == "SHT_NOBITS" else s.data(),
            )
            for s in elf.iter_sections()
            if s["sh_flags"] & 0x2 and s["sh_size"]
        ]
        symbols = _symbols(elf)
        relocations = []
        for section in elf.iter_sections():
            if not isinstance(section, RelocationSection):
                continue
            patched = elf.get_section(section["sh_info"])
            if not patched["sh_flags"] & 0x2:
                continue  # debug information
            table: Any = elf.get_section(section["sh_link"])  # its symbol table
            for record in section.iter_relocations():
                index = record["r_info_sym"]
                named = table.get_symbol(index) if index else None
                relocations.append(
                    Relocation(
                        site=record["r_offset"],
                        type=record["r_info_type"],
                        symbol=None
                        if named is None
                        else Symbol(
                            named.name,
                            named["st_value"],
                            named["st_size"],
                            _kind(named["st_info"]["type"]),
                            named["st_info"]["bind"] == "STB_LOCAL",
                            named["st_shndx"] not in ("SHN_UNDEF", "SHN_ABS", "SHN_COMMON"),
                            named["st_shndx"] == "SHN_ABS",
                        ),
                        addend=record["r_addend"] if record.is_RELA() else 0,
                    )
                )
    return Image(sections, symbols, relocations)


def read_object(path: Path) -> OwnObject:
    """The file symbol and the defined functions of one own object (pyelftools)."""
    from elftools.elf.elffile import ELFFile

    with path.open("rb") as stream:
        symbols = _symbols(ELFFile(stream))
    own = OwnObject(str(path), next((s.name for s in symbols if s.kind == "file"), None))
    for s in symbols:
        if s.kind == "func" and s.defined:
            (own.locals if s.local else own.globals).add(s.name)
            if s.weak:
                own.weak.add(s.name)
    return own


# --------------------------------------------------------------------------
# Decoding the relocated instructions
# --------------------------------------------------------------------------


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decode_target(site: int, code: bytes) -> tuple[int, str] | None:
    """The address the Xtensa instruction at *site* refers to, and its kind.

    *code* holds the instruction's first three bytes. The kind is ``literal``
    for an ``l32r`` (the address of the literal-pool word it loads) and
    ``code`` for a call, a jump or a branch. ``None`` for an instruction with
    no PC-relative address.
    """
    b0, b1, b2 = code[0], code[1], code[2]
    op0 = b0 & 0xF
    n = (b0 >> 4) & 0x3
    offset18 = _signed((b0 >> 6) | (b1 << 2) | (b2 << 10), 18)
    if op0 == 0x1:  # L32R: a negative word offset from the next aligned word
        return ((site + 3) & ~3) + ((b1 | (b2 << 8)) - 0x10000) * 4, "literal"
    if op0 == 0x5:  # CALL0, CALL4, CALL8, CALL12
        return (site & ~3) + offset18 * 4 + 4, "code"
    if op0 == 0x6 and n == 0:  # J
        return site + 4 + offset18, "code"
    if op0 == 0x6 and n == 1:  # BZ (BEQZ, BNEZ, BLTZ, BGEZ): 12-bit offset
        return site + 4 + _signed((b1 >> 4) | (b2 << 4), 12), "code"
    if (op0 == 0x6 and n in (2, 3)) or op0 == 0x7:  # BI0, BI1, B: 8-bit offset
        return site + 4 + _signed(b2, 8), "code"
    if op0 == 0xC and b0 & 0x80:  # BEQZ.N, BNEZ.N: 6-bit unsigned offset
        return site + 4 + ((((b0 >> 4) & 0x3) << 4) | (b1 >> 4)), "code"
    return None


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------

#: A node: ("sym", start) a function or object, ("lit", address) a literal
#: word, ("region", start) an unnamed region, ("rom", address) a ROM function,
#: ("tls", 0) the TLS template.
Node = tuple[str, int]


@dataclass
class Graph:
    """The contracted call graph: named nodes and the named nodes each reaches.

    :ivar names: Every name of each named node (aliases share an address).
    :ivar edges: The named successors of each named node.
    :ivar own: The nodes of the firmware's own functions.
    :ivar unlinked: The own objects the link left out whole.
    """

    names: dict[Node, list[str]] = field(default_factory=dict)
    edges: dict[Node, set[Node]] = field(default_factory=lambda: defaultdict(set))
    own: set[Node] = field(default_factory=set)
    unlinked: list[str] = field(default_factory=list)

    def label(self, node: Node) -> str:
        """The name a report gives *node*: its first name, ``rom:`` for the ROM's."""
        names = self.names.get(node) or [f"{node[0]}@{node[1]:#x}"]
        return ("rom:" if node[0] == "rom" else "") + names[0]

    def has_name(self, node: Node, name: str) -> bool:
        """True when *name* is one of *node*'s names."""
        return name in self.names.get(node, ())


class _Layout:
    """Where the image put each symbol, for the lookups the graph needs."""

    def __init__(self, image: Image) -> None:
        self.sections = sorted(image.sections, key=lambda s: s.address)
        self._section_starts = [s.address for s in self.sections]
        spans: dict[int, int] = {}
        names: dict[int, list[str]] = defaultdict(list)
        boundaries = set()
        self.rom: dict[int, list[str]] = defaultdict(list)
        for s in image.symbols:
            if (
                s.absolute
                and s.kind in ("func", "object", "other")
                and s.name
                and ROM_CODE[0] <= s.value < ROM_CODE[1]
            ):
                self.rom[s.value].append(s.name)
            if not s.defined or s.kind not in ("func", "object", "other") or not s.name:
                continue
            if s.name.startswith("$") or not self.section_at(s.value):
                continue
            boundaries.add(s.value)
            if s.kind == "other":
                continue  # a label: bounds a region, names no node
            spans[s.value] = max(spans.get(s.value, 0), s.size)
            if s.name not in names[s.value]:
                names[s.value].append(s.name)
        for section in self.sections:
            boundaries.update((section.address, section.address + section.size))
        self._boundaries = sorted(boundaries)
        self.names = {start: sorted(n) for start, n in names.items()}
        self.intervals = sorted(
            (start, start + (size or self._next_boundary(start) - start))
            for start, size in spans.items()
        )
        self._starts = [start for start, _ in self.intervals]
        self._widest = max((end - start for start, end in self.intervals), default=0)

    def _next_boundary(self, address: int) -> int:
        i = bisect.bisect_right(self._boundaries, address)
        return self._boundaries[i] if i < len(self._boundaries) else address + 4

    def section_at(self, address: int) -> Section | None:
        """The section holding *address*, or None."""
        i = bisect.bisect_right(self._section_starts, address) - 1
        if i >= 0:
            section = self.sections[i]
            if section.address <= address < section.address + section.size:
                return section
        return None

    def word(self, address: int, length: int) -> bytes | None:
        """*length* bytes of the image at *address*, or None outside its contents."""
        section = self.section_at(address)
        if section is None or not section.data:
            return None
        offset = address - section.address
        chunk = section.data[offset : offset + length]
        return chunk if len(chunk) == length else None

    def container(self, address: int) -> int | None:
        """The start of the innermost named symbol that holds *address*, or None."""
        i = bisect.bisect_right(self._starts, address) - 1
        while i >= 0:
            start, end = self.intervals[i]
            if start <= address < end:
                return start
            if address - start >= self._widest:
                break  # no earlier symbol is wide enough to reach it
            i -= 1
        return None

    def region(self, address: int) -> int:
        """The start of the unnamed region that holds *address* (the last boundary)."""
        i = bisect.bisect_right(self._boundaries, address) - 1
        return self._boundaries[max(i, 0)]


def _target_node(layout: _Layout, address: int, kind: str) -> Node | None:
    if ROM_CODE[0] <= address < ROM_CODE[1]:
        return ("rom", address)
    start = layout.container(address)
    if start is not None:
        return ("sym", start)
    if layout.section_at(address) is None:
        return None  # a register block, ROM data or nothing: not an edge
    return ("lit", address & ~3) if kind == "literal" else ("region", layout.region(address))


def _site_nodes(layout: _Layout, site: int) -> list[Node]:
    start = layout.container(site)
    if start is not None:
        return [("sym", start)]
    return [("lit", site & ~3), ("region", layout.region(site))]


def raw_graph(image: Image) -> tuple[dict[Node, set[Node]], _Layout, list[str]]:
    """Every edge the image's relocations hold, before contraction, and any problem read."""
    layout = _Layout(image)
    edges: dict[Node, set[Node]] = defaultdict(set)
    problems = []
    tls = layout_tls = None
    for section in layout.sections:
        if section.name == TLS_TEMPLATE:
            layout_tls = section
    for reloc in image.relocations:
        target: tuple[int, str] | None = None
        if reloc.type == R_XTENSA_32:
            word = layout.word(reloc.site, 4)
            if word is not None:
                target = (struct.unpack("<I", word)[0], "data")
        elif reloc.type == R_XTENSA_SLOT0_OP:
            code = layout.word(reloc.site, 3)
            target = None if code is None else decode_target(reloc.site, code)
            if target is None and code is not None:
                problems.append(f"an instruction the gate cannot decode at {reloc.site:#x}")
        elif reloc.type == R_XTENSA_ASM_EXPAND:
            named = reloc.symbol
            if named is not None and named.kind in ("func", "section"):
                target = (named.value + reloc.addend, "code")
        elif reloc.type == R_XTENSA_ASM_SIMPLIFY:
            # A long call the linker relaxed to a direct one: a NOP where the
            # l32r was, then the call8, which carries no relocation of its own.
            code = layout.word(reloc.site + 3, 3)
            decoded = None if code is None else decode_target(reloc.site + 3, code)
            named = reloc.symbol
            if decoded is not None and decoded[1] == "code":
                target = decoded
            elif named is not None and named.kind in ("func", "section"):
                target = (named.value + reloc.addend, "code")
            else:
                problems.append(f"a relaxed call the gate cannot read at {reloc.site:#x}")
        elif reloc.type in R_XTENSA_TLS and layout_tls is not None:
            tls = ("tls", 0)
            for node in _site_nodes(layout, reloc.site):
                edges[node].add(tls)
            continue
        if target is None:
            continue
        to = _target_node(layout, *target)
        if to is None:
            continue
        for node in _site_nodes(layout, reloc.site):
            if node != to:
                edges[node].add(to)
    if layout_tls is not None:
        # The template's words: what a thread-local pointer can hold.
        for reloc in image.relocations:
            if layout_tls.address <= reloc.site < layout_tls.address + layout_tls.size:
                for node in _site_nodes(layout, reloc.site):
                    edges[("tls", 0)].update(edges.get(node, ()))
    return edges, layout, problems


def contract(edges: Mapping[Node, set[Node]], layout: _Layout) -> Graph:
    """The graph between named nodes: unnamed ones (literal words, regions, TLS) passed through."""
    graph = Graph()
    for start, names in layout.names.items():
        graph.names[("sym", start)] = names
    for address, names in layout.rom.items():
        graph.names[("rom", address)] = sorted(set(names))

    def named(node: Node) -> bool:
        return node[0] in ("sym", "rom")

    memo: dict[Node, set[Node]] = {}

    def through(node: Node) -> set[Node]:
        if node in memo:
            return memo[node]
        memo[node] = set()
        out: set[Node] = set()
        stack = [node]
        seen = {node}
        while stack:
            current = stack.pop()
            for nxt in edges.get(current, ()):
                if named(nxt):
                    out.add(nxt)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        memo[node] = out
        return out

    for node in list(edges):
        if named(node):
            for nxt in edges[node]:
                targets = {nxt} if named(nxt) else through(nxt)
                graph.edges[node].update(t for t in targets if t != node)
    for node in graph.edges:
        graph.names.setdefault(node, [f"{node[0]}@{node[1]:#x}"])
    return graph


def mark_own(graph: Graph, image: Image, objects: Iterable[OwnObject]) -> list[str]:
    """Marks the nodes of the own objects' functions in *graph*; returns any problem."""
    objects = list(objects)
    problems = []
    files: defaultdict[str, int] = defaultdict(int)
    for s in image.symbols:
        if s.kind == "file":
            files[s.name] += 1
    by_name: dict[str, list[int]] = defaultdict(list)
    current = None
    own_locals: dict[str, set[int]] = defaultdict(set)
    own_files = {o.file for o in objects if o.file}
    for s in image.symbols:
        if s.kind == "file":
            current = s.name
            continue
        if s.kind != "func" or not s.defined:
            continue
        if s.local:
            if current in own_files:
                own_locals[current].add(s.value)
                by_name[f"{current}:{s.name}"].append(s.value)
        else:
            by_name[s.name].append(s.value)
    for obj in objects:
        if obj.file is None:
            problems.append(f"{obj.path}: no file symbol, so its local functions cannot be found")
            continue
        if obj.locals and files.get(obj.file, 0) == 0:
            # No local of it survived the link. With none of its own
            # globals either (a weak one, an inline function every user
            # defines, may be another object's), none of its functions is in
            # the image; with one, its locals are there and cannot be found.
            if not any(name in by_name for name in obj.globals - obj.weak):
                graph.unlinked.append(obj.path)
                continue
            problems.append(f"{obj.path}: the image has no file symbol {obj.file}")
        elif files.get(obj.file, 0) > 1:
            problems.append(
                f"{obj.path}: {files[obj.file]} file symbols are named {obj.file}: its locals"
                " cannot be told from another source's"
            )
        for name in obj.globals:
            for start in by_name.get(name, ()):
                graph.own.add(("sym", start))
        for name in obj.locals:
            for start in by_name.get(f"{obj.file}:{name}", ()):
                graph.own.add(("sym", start))
    if not graph.own:
        problems.append("no function of the firmware's own objects is in the image")
    return problems


def add_runtime_edges(
    graph: Graph, runtime_edges: Mapping[tuple[str, str], str] = RUNTIME_EDGES
) -> list[str]:
    """Adds each edge of *runtime_edges* to *graph* by name; returns any problem."""
    problems = []
    for source, target in runtime_edges:
        sources, targets = (
            [node for node, names in graph.names.items() if name in names]
            for name in (source, target)
        )
        if not sources or not targets:
            problems.append(
                f"stale run-time edge {source} -> {target}: the image does not hold"
                f" {target if sources else source}"
            )
            continue
        for node in sources:
            graph.edges[node].update(targets)
    return problems


def build_graph(
    image: Image,
    objects: Iterable[OwnObject],
    runtime_edges: Mapping[tuple[str, str], str] = RUNTIME_EDGES,
) -> tuple[Graph, list[str]]:
    """The contracted graph of *image* with the own functions of *objects* marked,
    and the calls *runtime_edges* names through tables set at run time."""
    edges, layout, problems = raw_graph(image)
    graph = contract(edges, layout)
    problems += mark_own(graph, image, objects)
    problems += add_runtime_edges(graph, runtime_edges)
    return graph, problems


# --------------------------------------------------------------------------
# The walk and the verdict
# --------------------------------------------------------------------------


def _path(parent: Mapping[Node, Node | None], node: Node, graph: Graph) -> str:
    chain = [node]
    above = parent[node]
    while above is not None:
        chain.append(above)
        above = parent[above]
    return " -> ".join(graph.label(n) for n in reversed(chain))


def _is_sink(graph: Graph, node: Node) -> bool:
    return node[0] == "sym" and any(SINKS.match(n) for n in graph.names.get(node, ()))


def _cut(
    graph: Graph, cuts: Mapping[tuple[str, str], Cut], a: Node, b: Node
) -> tuple[str, str] | None:
    for source, target in cuts:
        if graph.has_name(a, source) and graph.has_name(b, target):
            return (source, target)
    return None


def verdict(
    graph: Graph,
    *,
    cuts: Mapping[tuple[str, str], Cut] = CUTS,
    rom_leaves: Mapping[str, str] = ROM_LEAVES,
    core_sinks: Iterable[str] = CORE_SINKS,
) -> tuple[list[str], list[str]]:
    """The report lines and the failures of the walk from every own function of *graph*."""
    failures = []
    present = {name for names in graph.names.values() for name in names}
    missing = [name for name in core_sinks if name not in present]
    if missing:
        failures.append(
            "the image holds none of " + ", ".join(missing) + ": an allocator renamed is not seen"
        )
    parent: dict[Node, Node | None] = dict.fromkeys(sorted(graph.own))
    queue = deque(sorted(graph.own))
    met: set[tuple[str, str]] = set()
    while queue:
        node = queue.popleft()
        if _is_sink(graph, node) or node[0] == "rom":
            continue
        for nxt in sorted(graph.edges.get(node, ())):
            crossed = _cut(graph, cuts, node, nxt)
            if crossed is not None:
                met.add(crossed)
                continue
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    reached = list(parent)
    roms = [n for n in reached if n[0] == "rom"]
    leaves_met = set()
    for node in sorted(reached):
        if _is_sink(graph, node):
            failures.append(f"an allocator is reachable: {_path(parent, node, graph)}")
        elif node[0] == "rom":
            known = [n for n in graph.names.get(node, ()) if n in rom_leaves]
            if not known:
                failures.append(
                    f"a ROM function no list names is reachable: {_path(parent, node, graph)}"
                )
            leaves_met.update(known)
    failures += [
        f"stale cut {source} -> {target}: the walk never meets it"
        for source, target in cuts
        if (source, target) not in met
    ]
    for (source, target), cut in cuts.items():
        failures += [
            f"cut {source} -> {target} no longer holds, {guard} is reachable:"
            f" {_path(parent, node, graph)}"
            for node in sorted(reached)
            for guard in cut.guards
            if graph.has_name(node, guard)
        ]
    failures += [
        f"stale ROM leaf {name}: the walk never reaches it"
        for name in rom_leaves
        if name not in leaves_met
    ]
    report = [
        f"{len(graph.own)} own function(s), the roots",
        f"{len(reached) - len(roms)} function(s) and object(s) reached, and"
        f" {len(roms)} ROM function(s)",
    ]
    report += [f"cut: {s} -> {t} ({cuts[(s, t)].reason})" for s, t in cuts if (s, t) in met]
    return report, failures


# --------------------------------------------------------------------------
# The self-test: images written here, each with one planted case
# --------------------------------------------------------------------------

#: Where the planted images put their code and data (outside the ROM's range).
_TEXT = 0x42000000
_RODATA = 0x3C000000
_ROM_MEMCPY = 0x400011E8
_ROM_UNKNOWN = 0x40001400

_STT = {"notype": 0, "object": 1, "func": 2, "section": 3, "file": 4}


def _elf(
    kind: int,
    sections: list[tuple[str, int, int, bytes, list[tuple[int, int, int]]]],
    symbols: list[tuple[str, int, int, str, bool, int]],
) -> bytes:
    """An ELF32 Xtensa file: *sections* (name, flags, address, bytes, relocations
    as (site, type, symbol index)) and *symbols* (name, value, size, type,
    local, section index or 0xFFF1 for absolute); locals must come first."""
    names = bytearray(b"\0")

    def name(text: str) -> int:
        offset = len(names)
        names.extend(text.encode() + b"\0")
        return offset

    strtab = bytearray(b"\0")
    symtab = bytearray(16)
    first_global = 1
    for i, (sym, value, size, kind_, local, shndx) in enumerate(symbols, start=1):
        offset = len(strtab)
        strtab.extend(sym.encode() + b"\0")
        bind = 0 if local else 1
        symtab += struct.pack("<IIIBBH", offset, value, size, (bind << 4) | _STT[kind_], 0, shndx)
        if local:
            first_global = i + 1
    headers = [struct.pack("<10I", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    body = bytearray()
    base = 52
    entries = []
    # Section indices: 1..n the given sections, then their relocations, then
    # .symtab, .strtab, .shstrtab.
    count = len(sections)
    relocated = [(i + 1, s) for i, s in enumerate(sections) if s[4]]
    symtab_index = count + len(relocated) + 1
    for title, flags, address, data, _ in sections:
        entries.append((name(title), 1, flags, address, bytes(data), 0, 0, 4, 0))
    for index, (title, _, _, _, relocs) in relocated:
        packed = b"".join(
            struct.pack("<IIi", site, (sym << 8) | rtype, 0) for site, rtype, sym in relocs
        )
        entries.append((name(".rela" + title), 4, 0x40, 0, packed, symtab_index, index, 4, 12))
    entries.append((name(".symtab"), 2, 0, 0, bytes(symtab), symtab_index + 1, first_global, 4, 16))
    entries.append((name(".strtab"), 3, 0, 0, bytes(strtab), 0, 0, 1, 0))
    shstr = name(".shstrtab")
    entries.append((shstr, 3, 0, 0, bytes(names), 0, 0, 1, 0))
    for sh_name, sh_type, flags, address, data, link, info, align, entsize in entries:
        offset = base + len(body)
        body += data
        while len(body) % 4:
            body.append(0)
        headers.append(
            struct.pack(
                "<10I",
                sh_name,
                sh_type,
                flags,
                address,
                offset,
                len(data),
                link,
                info,
                align,
                entsize,
            )
        )
    shoff = base + len(body)
    ident = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header = ident + struct.pack(
        "<HHIIIIIHHHHHH", kind, 94, 1, 0, 0, shoff, 0, 52, 0, 0, 40, len(headers), len(headers) - 1
    )
    return header + bytes(body) + b"".join(headers)


#: Xtensa's three-byte NOP (``or a1, a1, a1``'s encoding ``nop``), what the
#: linker leaves where it removed a long call's ``l32r``.
_NOP = bytes([0xF0, 0x20, 0x00])


def _call8(site: int, target: int) -> bytes:
    offset = (target - (site & ~3) - 4) >> 2
    return bytes([0x25 | ((offset & 0x3) << 6), (offset >> 2) & 0xFF, (offset >> 10) & 0xFF])


def _jump(site: int, target: int) -> bytes:
    offset = target - site - 4
    return bytes([0x06 | ((offset & 0x3) << 6), (offset >> 2) & 0xFF, (offset >> 10) & 0xFF])


def _l32r(site: int, literal: int) -> bytes:
    offset = ((literal - ((site + 3) & ~3)) >> 2) & 0xFFFF
    return bytes([0x81, offset & 0xFF, offset >> 8])


@dataclass
class _Plant:
    """A planted image under construction: a literal pool, then functions, and data."""

    literals: list[int] = field(default_factory=list)
    functions: list[tuple[str, list[tuple[str, str]], bool]] = field(default_factory=list)
    tables: list[tuple[str, list[str]]] = field(default_factory=list)

    def build(
        self,
        directory: Path,
        *,
        relocations: bool = True,
        files: tuple[str, ...] = ("own.cpp",),
    ) -> tuple[Any, ...]:
        pool = 0x40
        address = {}
        cursor = _TEXT + pool
        for fname, body, _ in self.functions:
            address[fname] = cursor
            slots = sum(2 if op == "relaxed" else 1 for op, _ in body)
            cursor += 4 * (slots + 1) + 4
        data_at = _RODATA
        for tname, entries in self.tables:
            address[tname] = data_at
            data_at += 4 * len(entries)
        text = bytearray(cursor - _TEXT)
        relocs_text: list[tuple[int, int, int]] = []
        relocs_data: list[tuple[int, int, int]] = []
        literal_at: dict[str, int] = {}

        def resolve(target: str) -> int:
            if target == "rom:memcpy":
                return _ROM_MEMCPY
            if target == "rom:unknown":
                return _ROM_UNKNOWN
            return address[target]

        def literal(target: str) -> int:
            if target not in literal_at:
                at = _TEXT + 4 * len(literal_at)
                literal_at[target] = at
                text[at - _TEXT : at - _TEXT + 4] = struct.pack("<I", resolve(target))
                relocs_text.append((at, R_XTENSA_32, 0))
            return literal_at[target]

        for fname, body, _ in self.functions:
            site = address[fname] + 3  # after a three-byte entry
            for op, target in body:
                if op == "relaxed":
                    # A long call the linker relaxed: a NOP where the l32r was,
                    # then a direct call8, with only R_XTENSA_ASM_SIMPLIFY left.
                    text[site - _TEXT : site - _TEXT + 3] = _NOP
                    call = _call8(site + 3, resolve(target))
                    text[site + 3 - _TEXT : site + 6 - _TEXT] = call
                    relocs_text.append((site, R_XTENSA_ASM_SIMPLIFY, 0))
                    site += 8
                    continue
                if op == "call":
                    code = _call8(site, resolve(target))
                elif op == "jump":
                    code = _jump(site, resolve(target))
                else:  # "load": the address through a literal, as a long call does
                    code = _l32r(site, literal(target))
                text[site - _TEXT : site - _TEXT + 3] = code
                relocs_text.append((site, R_XTENSA_SLOT0_OP, 0))
                site += 4
        rodata = bytearray()
        for _, entries in self.tables:
            for entry in entries:
                relocs_data.append((_RODATA + len(rodata), R_XTENSA_32, 0))
                rodata += struct.pack("<I", resolve(entry))
        sections = [
            (".flash.text", 0x6, _TEXT, bytes(text), relocs_text if relocations else []),
            (".flash.rodata", 0x2, _RODATA, bytes(rodata) or bytes(4), relocs_data),
        ]
        symbols: list[tuple[str, int, int, str, bool, int]] = []
        locals_ = [(f, b) for f, b, own_local in self.functions if own_local]
        symbols.extend((file_name, 0, 0, "file", True, 0xFFF1) for file_name in files)
        for fname, body in locals_:
            symbols.append((fname, address[fname], 4 * (len(body) + 1), "func", True, 1))
        symbols.append(("memcpy", _ROM_MEMCPY, 0, "func", False, 0xFFF1))
        symbols.append(("rom_unknown", _ROM_UNKNOWN, 0, "func", False, 0xFFF1))
        for fname, body, own_local in self.functions:
            if not own_local:
                symbols.append((fname, address[fname], 4 * (len(body) + 1), "func", False, 1))
        for tname, entries in self.tables:
            symbols.append((tname, address[tname], 4 * len(entries), "object", False, 2))
        elf = directory / "firmware.elf"
        elf.write_bytes(_elf(2, sections, symbols))
        own_text: list[tuple[str, int, int, bytes, list[Any]]] = [(".text", 0x6, 0, bytes(64), [])]
        obj_symbols = [("own.cpp", 0, 0, "file", True, 0xFFF1)]
        obj_symbols += [
            (f, 0, 4, "func", True, 1) for f, _, own_local in self.functions if own_local
        ]
        obj_symbols += [
            (f, 0, 4, "func", False, 1)
            for f, _, own_local in self.functions
            if f.startswith("own_") and not own_local
        ]
        obj = directory / "own.cpp.o"
        obj.write_bytes(_elf(1, own_text, obj_symbols))
        return elf, obj


def _clean_plant() -> _Plant:
    """A clean image: own code calling ESP-IDF code that allocates nothing,
    and the allocators present, as ESP-IDF's startup calls them."""
    plant = _Plant()
    plant.functions = [
        ("helper_local", [("call", "idf_leaf")], True),
        (
            "own_entry",
            [("call", "helper_local"), ("load", "idf_leaf"), ("load", "rom:memcpy")],
            False,
        ),
        ("idf_leaf", [], False),
        ("idf_startup", [("call", "malloc")], False),
        ("malloc", [("call", "multi_heap_malloc")], False),
        ("calloc", [("call", "multi_heap_malloc")], False),
        ("free", [], False),
        ("heap_caps_malloc", [("call", "multi_heap_malloc")], False),
        ("heap_caps_free", [], False),
        ("multi_heap_malloc", [], False),
    ]
    return plant


def _run(
    plant: _Plant,
    *,
    cuts: Mapping[tuple[str, str], Cut] | None = None,
    rom_leaves: Mapping[str, str] | None = None,
    **build: Any,
) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        elf, obj = plant.build(Path(tmp), **build)
        image = read_image(elf)
        graph, problems = build_graph(image, [read_object(obj)], runtime_edges={})
    _, failures = verdict(
        graph,
        cuts={} if cuts is None else cuts,
        rom_leaves={"memcpy": "copies"} if rom_leaves is None else rom_leaves,
    )
    if not image.relocations:
        failures.append("no relocations")
    return problems + failures


def selftest() -> int:
    """Plants each case in an image of its own; 0 when the gate judges every one right."""
    checks: list[tuple[str, bool]] = []

    def case(title: str, failures: list[str], expect: str | None) -> None:
        held = (not failures) if expect is None else any(expect in f for f in failures)
        checks.append((title, held))
        if not held:
            print(f"  {title}: {failures}")

    case("a clean image passes", _run(_clean_plant()), None)

    plant = _clean_plant()
    plant.functions[1] = ("own_entry", [("call", "malloc")], False)
    case("a direct call to an allocator fails", _run(plant), "own_entry -> malloc")

    plant = _clean_plant()
    plant.functions[1] = ("own_entry", [("load", "heap_caps_malloc")], False)
    case("a long call through a literal fails", _run(plant), "own_entry -> heap_caps_malloc")

    plant = _clean_plant()
    plant.functions[2] = ("idf_leaf", [("call", "idf_deeper")], False)
    plant.functions.append(("idf_deeper", [("load", "free")], False))
    case("an allocator two calls deep in ESP-IDF fails", _run(plant), "idf_deeper -> free")

    plant = _clean_plant()
    plant.functions[0] = ("helper_local", [("call", "multi_heap_malloc")], True)
    case("a local own function never called is a root", _run(plant), "helper_local")

    plant = _clean_plant()
    plant.functions[1] = ("own_entry", [("load", "kTable")], False)
    plant.tables.append(("kTable", ["idf_leaf", "malloc"]))
    case("an allocator in a table an own function loads fails", _run(plant), "kTable -> malloc")

    plant = _clean_plant()
    plant.functions[2] = ("idf_leaf", [("jump", "free")], False)
    case("a jump into an allocator fails", _run(plant), "idf_leaf -> free")

    plant = _clean_plant()
    plant.functions[2] = ("idf_leaf", [("relaxed", "free")], False)
    case(
        "a long call the linker relaxed into an allocator fails",
        _run(plant),
        "idf_leaf -> free",
    )

    plant = _clean_plant()
    plant.functions[1] = ("own_entry", [("load", "rom:unknown")], False)
    case("a ROM function no list names fails", _run(plant), "rom:rom_unknown")

    case(
        "a ROM leaf the walk never reaches is stale",
        _run(_clean_plant(), rom_leaves={"memcpy": "copies", "memset": "fills"}),
        "stale ROM leaf memset",
    )

    plant = _clean_plant()
    plant.functions[2] = ("idf_leaf", [("call", "idf_never")], False)
    plant.functions.append(("idf_never", [("call", "free")], False))
    case(
        "a cut edge is not followed",
        _run(plant, cuts={("idf_leaf", "idf_never"): Cut("never runs")}),
        None,
    )
    case(
        "a cut the walk never meets is stale",
        _run(_clean_plant(), cuts={("idf_leaf", "idf_never"): Cut("never runs")}),
        "stale cut idf_leaf -> idf_never",
    )

    plant = _clean_plant()
    plant.functions[2] = ("idf_leaf", [("call", "idf_never"), ("call", "fclose")], False)
    plant.functions += [("idf_never", [("call", "free")], False), ("fclose", [], False)]
    case(
        "a cut whose guard is reachable fails",
        _run(plant, cuts={("idf_leaf", "idf_never"): Cut("never runs", ("fclose",))}),
        "no longer holds, fclose is reachable",
    )

    plant = _clean_plant()
    plant.functions = [f for f in plant.functions if f[0] != "multi_heap_malloc"]
    plant.functions = [
        (n, [s for s in b if s[1] != "multi_heap_malloc"], o) for n, b, o in plant.functions
    ]
    case("an image without the allocator's names fails", _run(plant), "multi_heap_malloc")

    case(
        "an own source's name twice among the file symbols fails",
        _run(_clean_plant(), files=("own.cpp", "own.cpp")),
        "2 file symbols are named own.cpp",
    )
    case(
        "an image linked without --emit-relocs fails",
        _run(_clean_plant(), relocations=False),
        "no relocations",
    )

    dropped = OwnObject(
        "dropped.o", "dropped.cpp", {"dropped_entry", "own_entry"}, {"dropped_local"}, {"own_entry"}
    )
    with tempfile.TemporaryDirectory() as tmp:
        elf, obj = _clean_plant().build(Path(tmp))
        image = read_image(elf)
        graph, problems = build_graph(image, [read_object(obj), dropped], runtime_edges={})
        checks.append(
            (
                "an own object the link left out whole is not linked, and no failure",
                not problems and graph.unlinked == ["dropped.o"],
            )
        )
        # One of its globals in the image: its locals are there, unfound.
        half = OwnObject("half.o", "half.cpp", {"own_entry"}, {"half_local"})
        graph, problems = build_graph(image, [read_object(obj), half], runtime_edges={})
        checks.append(
            (
                "an own object linked without its file symbol fails",
                any("half.o: the image has no file symbol half.cpp" in p for p in problems)
                and not graph.unlinked,
            )
        )

    for site, code, expected in (
        (0x42000010, _call8(0x42000010, 0x42000100), (0x42000100, "code")),
        (0x42000101, _call8(0x42000101, 0x41FFF000), (0x41FFF000, "code")),
        (0x42000020, _jump(0x42000020, 0x42000009), (0x42000009, "code")),
        (0x42000021, _l32r(0x42000021, 0x42000000), (0x42000000, "literal")),
        (0x42000010, bytes([0x0C, 0x00, 0x00]), None),
    ):
        checks.append((f"decodes {code.hex()} at {site:#x}", decode_target(site, code) == expected))

    for title, held in checks:
        print(f"{'ok' if held else 'FAIL'}: {title}")
    return 0 if all(held for _, held in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--elf", type=Path, help="the linked image (firmware.elf)")
    parser.add_argument("--selftest", action="store_true", help="plant each case, no image needed")
    parser.add_argument("objects", nargs="*", type=Path, help="the own component's objects")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.elf is None or not args.objects:
        parser.error("an image and the own objects are needed (or --selftest)")
    image = read_image(args.elf)
    if not image.relocations:
        print(f"FAIL: {args.elf} holds no relocations: link it with -Wl,--emit-relocs")
        return 1
    graph, problems = build_graph(image, (read_object(path) for path in args.objects))
    report, failures = verdict(graph)
    for line in report:
        print(line)
    for path in graph.unlinked:
        print(f"not linked: {path}: none of its functions is in the image")
    for line in problems + failures:
        print(f"FAIL: {line}")
    print(f"{len(args.objects)} object(s), {len(problems) + len(failures)} failure(s)")
    return 1 if problems or failures else 0


if __name__ == "__main__":
    sys.exit(main())
