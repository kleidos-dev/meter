#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The object gate: the firmware's compiled objects call no allocator.

    python3 scripts/check_heap_objects.py --nm <xtensa-esp32s3-elf-nm> \\
        --elf firmware/.pio/build/meter/firmware.elf firmware/.pio/build/meter/src/*.o

The source gate (tests/firmware/test_no_dynamic_memory.py) reads what the
firmware's code says; this reads what the compiler made of it, so a call a
header or the compiler inserted is seen too. Every object of the firmware's
own component (``src/``) is listed with ``nm --undefined-only``, and any
undefined reference to an allocator fails the gate: the C allocator, ESP-IDF's
``heap_caps_*`` allocators, FreeRTOS's ``pvPortMalloc`` / ``vPortFree``, the
dynamic FreeRTOS creates (``xQueueGenericCreate``, ``xTaskCreatePinnedToCore``,
...), and every replaceable ``operator new`` / ``operator delete``.

It also fails a reference to an ESP-IDF entry point that allocates or frees on
the device (:data:`IDF_ALLOCATING`, the deny list: a driver's install, NVS's
writes, the task watchdog's subscription, the interrupt allocator, ...) from
an object whose source is neither allowed to call it (:data:`IDF_ALLOWED`,
each entry a site of D1 in docs/misra.md; empty since FW-16) nor made safe to
(:data:`IDF_MADE_SAFE`, a call the image's configuration takes the allocation
out of), and an entry of either list the object of its source no longer
references. The lists are the same as the source gate's and the host
harness's (``firmware/test/native/harness/idf_ledger.h``), which
``tests/firmware/test_idf_allocation_lists.py`` holds equal, each allowed call
to its source and deviation, and the history to an allow-list that only
shrinks and a deny list that only grows.

With ``--elf`` it also reads the linked image (FW-16) and fails on any symbol
of the allocating C++ runtime or of NVS in it, whoever pulled it in: every
``operator new`` and ``operator delete``, the C++ ABI's ``__cxa_*`` functions
(the exception and guard runtime, whose objects allocate: ``eh_globals``
takes a thread key from the heap at startup) and ``eh_globals`` itself, and
NVS's functions and classes, and (FW-18) PSA's and mbedTLS's functions. Two
symbols are exempt, :data:`ELF_EXEMPT`: the empty functions ESP-IDF's
``cxx`` and ``mbedtls`` components force into every image.

It prints how many objects it read and how many forbidden references each
holds (zero), the count CI writes to the job summary, and each allowed ESP-IDF
call with its deviation. docs/misra.md says what it holds.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Exact names that are an allocation, or a dynamic FreeRTOS create.
FORBIDDEN_NAMES = {
    "malloc",
    "calloc",
    "realloc",
    "free",
    "aligned_alloc",
    "posix_memalign",
    "memalign",
    "pvPortMalloc",
    "vPortFree",
    "xQueueGenericCreate",
    "xQueueCreateMutex",
    "xQueueCreateCountingSemaphore",
    "xTaskCreatePinnedToCore",
    "xTaskCreate",
    "xEventGroupCreate",
    "xStreamBufferGenericCreate",
    "xTimerCreate",
}

#: Patterns: the heap_caps allocators, and operator new / delete in every form
#: (``_Znwj``, ``_Znaj``, ``_ZdlPv``, ``_ZdaPvj``, the aligned and nothrow ones).
FORBIDDEN_PATTERNS = (
    re.compile(r"^heap_caps_(malloc|calloc|realloc|free|aligned_alloc|aligned_calloc)\w*$"),
    re.compile(r"^_Zn[wa][jm]"),
    re.compile(r"^_Zd[la]Pv"),
)


#: The deny list: ESP-IDF 6.0.3 entry points that allocate or free on the heap
#: when the firmware calls them (directly, through the partition iterator a
#: flash write's region check takes, or through a lazily created C library
#: lock). Not on it, because they take only what an entry on it made: NVS's
#: reads and ``nvs_commit()`` (NVS's lock, which ``nvs_flash_init()``
#: creates; the commit is a no-op in 6.0.3), ``gpio_isr_handler_add()`` (the
#: service's table), the I2C and UART transfers, and
#: ``esp_ipc_call_blocking()`` (static semaphores). It only grows.
IDF_ALLOCATING = (
    # I2C master
    "i2c_new_master_bus",
    "i2c_del_master_bus",
    "i2c_master_bus_add_device",
    "i2c_master_bus_rm_device",
    # UART
    "uart_param_config",
    "uart_driver_install",
    "uart_driver_delete",
    # GPIO interrupts
    "gpio_install_isr_service",
    "gpio_uninstall_isr_service",
    "gpio_isr_register",
    # RMT
    "rmt_new_tx_channel",
    "rmt_new_rx_channel",
    "rmt_del_channel",
    "rmt_new_bytes_encoder",
    "rmt_new_copy_encoder",
    "rmt_new_simple_encoder",
    "rmt_del_encoder",
    # NVS
    "nvs_flash_init",
    "nvs_flash_init_partition",
    "nvs_flash_deinit",
    "nvs_flash_erase",
    "nvs_flash_erase_partition",
    "nvs_open",
    "nvs_open_from_partition",
    "nvs_close",
    "nvs_set_blob",
    "nvs_set_str",
    "nvs_set_u8",
    "nvs_set_u16",
    "nvs_set_u32",
    "nvs_set_u64",
    "nvs_set_i8",
    "nvs_set_i16",
    "nvs_set_i32",
    "nvs_set_i64",
    "nvs_erase_key",
    "nvs_erase_all",
    # Task watchdog
    "esp_task_wdt_init",
    "esp_task_wdt_deinit",
    "esp_task_wdt_add",
    "esp_task_wdt_add_user",
    "esp_task_wdt_delete",
    "esp_task_wdt_delete_user",
    # Interrupt allocator
    "esp_intr_alloc",
    "esp_intr_alloc_intrstatus",
    "esp_intr_alloc_bind",
    "esp_intr_alloc_intrstatus_bind",
    "esp_intr_free",
    # Partitions and flash
    "esp_partition_find",
    "esp_partition_find_first",
    "esp_partition_iterator_release",
    "esp_partition_mmap",
    "esp_partition_write",
    "esp_partition_write_raw",
    "esp_partition_erase_range",
    "esp_flash_read",
    "esp_flash_write",
    "esp_flash_erase_region",
    "spi_flash_mmap",
    "spi_flash_munmap",
    # Timers, ring buffers, FreeRTOS objects with capabilities, threads, PSA
    "esp_timer_create",
    "esp_timer_delete",
    "xRingbufferCreate",
    "vRingbufferDelete",
    "xQueueCreateWithCaps",
    "vQueueDeleteWithCaps",
    "xSemaphoreCreateBinaryWithCaps",
    "xSemaphoreCreateMutexWithCaps",
    "xSemaphoreCreateCountingWithCaps",
    "vSemaphoreDeleteWithCaps",
    "xTaskCreatePinnedToCoreWithCaps",
    "vTaskDeleteWithCaps",
    "pthread_create",
    "psa_crypto_init",
)

#: The allow-list: each (source, entry point) of the firmware that still calls
#: an entry of :data:`IDF_ALLOCATING`, and the deviations of docs/misra.md it
#: is a site of (D1: at initialization). It only shrinks: the zero-allocation
#: work packages took entries off it, and FW-16 the last (NVS's four).
IDF_ALLOWED: dict[tuple[str, str], tuple[str, ...]] = {}

#: The made-safe list (FW-16): each (source, entry point) of the firmware that
#: calls an entry of :data:`IDF_ALLOCATING` the image's configuration takes the
#: allocation out of, and how. Such an entry stays on the deny list, which
#: only grows: it allocates in ESP-IDF's default configuration, and from any
#: other source. ``test_idf_allocation_lists.py`` holds each mechanism in the
#: tree.
IDF_MADE_SAFE: dict[tuple[str, str], str] = {
    ("flash_port.cpp", "esp_flash_read"): (
        "the destination is internal RAM: flash_log::port::read() refuses any other "
        "(esp_ptr_in_dram), for which ESP-IDF would read through a bounce buffer it "
        "takes from the heap (get_temp_buffer)"
    ),
    ("flash_port.cpp", "esp_flash_write"): (
        "the region check is wrapped onto a static table "
        "(-Wl,--wrap=esp_partition_is_flash_region_writable), "
        "CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED leaves out the other"
    ),
    ("flash_port.cpp", "esp_flash_erase_region"): (
        "the region check is wrapped onto a static table "
        "(-Wl,--wrap=esp_partition_is_flash_region_writable), and "
        "CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED leaves out the other"
    ),
}

#: What the linked image may not define (``--elf``): each kind and the pattern
#: of its symbols, as nm lists them (mangled).
ELF_FORBIDDEN = {
    "operator new": re.compile(r"^_Zn[wa]"),
    "operator delete": re.compile(r"^_Zd[la]"),
    "C++ ABI runtime": re.compile(r"__cxa_"),
    "eh_globals": re.compile(r"eh_globals"),
    "NVS": re.compile(r"^nvs_|^_ZN3nvs|NVSHandle|NVSPartition"),
    # FW-18: PSA and the rest of mbedTLS. Nothing in the image uses them, and
    # psa_crypto_init(), which took PSA's key slots and two mutexes from the
    # heap at startup, is linked wrapped onto an answer that initializes
    # nothing (src/startup_memory.h), so a psa_ or mbedtls_ function in the
    # image is either the wrap undone or a new user.
    "PSA": re.compile(r"^psa_|^mbedtls_"),
}

#: The one symbol of :data:`ELF_FORBIDDEN`'s kinds the image keeps:
#: ``__cxa_guard_dummy``, an empty function ESP-IDF 6.0.3's ``cxx`` component
#: makes every image link (``-u __cxa_guard_dummy`` in its CMakeLists.txt) so
#: that its own static-local guards are taken over libstdc++'s. Nothing calls
#: it, and the guards themselves (``__cxa_guard_acquire``, which creates its
#: semaphores on the heap) are not linked: the firmware has no guarded static.
#: And ``mbedtls_psa_crypto_init_include_impl``, an empty function mbedTLS's
#: component makes every image link (``-u`` in its CMakeLists.txt) so that its
#: startup hook, which calls the wrapped ``psa_crypto_init()``, is kept.
ELF_EXEMPT = frozenset({"__cxa_guard_dummy", "mbedtls_psa_crypto_init_include_impl"})


def source_of(obj: Path) -> str:
    """The source file an object of the component was compiled from (``ina228.cpp.o``)."""
    return obj.name.removesuffix(".o")


def unlisted_idf(source: str, symbols: list[str]) -> list[str]:
    """The allocating ESP-IDF entry points *source* references and may not."""
    return sorted(
        {
            s
            for s in symbols
            if s in IDF_ALLOCATING
            and (source, s) not in IDF_ALLOWED
            and (source, s) not in IDF_MADE_SAFE
        }
    )


def allowed_idf(source: str, symbols: list[str]) -> list[str]:
    """The allocating ESP-IDF entry points *source* references and may, with their deviations."""
    return sorted(
        f"{s} ({', '.join(IDF_ALLOWED[(source, s)])})"
        for s in set(symbols)
        if (source, s) in IDF_ALLOWED
    )


def stale_idf(source: str, symbols: list[str]) -> list[str]:
    """The allow-list entries of *source* its object no longer references.

    An entry the code stopped needing is taken off the list in the same
    change, so that the list says only what the image still does.
    """
    present = set(symbols)
    return sorted(
        e for (s, e) in [*IDF_ALLOWED, *IDF_MADE_SAFE] if s == source and e not in present
    )


def forbidden(symbol: str) -> bool:
    """True when an undefined reference to *symbol* is an allocation."""
    return symbol in FORBIDDEN_NAMES or any(p.match(symbol) for p in FORBIDDEN_PATTERNS)


def elf_forbidden(symbols: list[str]) -> list[str]:
    """The symbols of the linked image that :data:`ELF_FORBIDDEN` names, with their kind."""
    return sorted(
        f"{symbol} ({kind})"
        for symbol in set(symbols) - ELF_EXEMPT
        for kind, pattern in ELF_FORBIDDEN.items()
        if pattern.search(symbol)
    )


def image_symbols(nm: str, elf: Path) -> list[str]:
    """Every symbol of the linked image, defined or not, as nm lists them."""
    listed = subprocess.run(
        [nm, "--format=posix", str(elf)], capture_output=True, text=True, check=True
    ).stdout
    return [line.split()[0] for line in listed.splitlines() if line.strip()]


def undefined_symbols(nm: str, obj: Path) -> list[str]:
    """The undefined symbols of one object, as nm lists them."""
    listed = subprocess.run(
        [nm, "--undefined-only", "--format=posix", str(obj)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.split()[0] for line in listed.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nm", default="nm", help="the nm of the toolchain that built the objects")
    parser.add_argument(
        "--elf",
        type=Path,
        help="the linked image, read for the C++ runtime, NVS (FW-16) and PSA (FW-18)",
    )
    parser.add_argument("objects", nargs="+", type=Path)
    args = parser.parse_args(argv)

    failures = 0
    unlisted = 0
    for obj in sorted(args.objects):
        symbols = undefined_symbols(args.nm, obj)
        hits = sorted({s for s in symbols if forbidden(s)})
        idf = unlisted_idf(source_of(obj), symbols)
        listed = (": " + ", ".join(hits)) if hits else ""
        print(f"{obj.name}: {len(hits)} allocator reference(s){listed}")
        if idf:
            print(f"{obj.name}: {len(idf)} unlisted ESP-IDF allocating call(s): {', '.join(idf)}")
        allowed = allowed_idf(source_of(obj), symbols)
        if allowed:
            print(f"{obj.name}: allowed ESP-IDF allocating call(s): {', '.join(allowed)}")
        stale = stale_idf(source_of(obj), symbols)
        if stale:
            print(f"{obj.name}: allow-list entries it no longer calls: {', '.join(stale)}")
        failures += len(hits)
        unlisted += len(idf) + len(stale)
    image = 0
    if args.elf is not None:
        found = elf_forbidden(image_symbols(args.nm, args.elf))
        listed = (": " + ", ".join(found)) if found else ""
        print(f"{args.elf.name}: {len(found)} C++ runtime, NVS or PSA symbol(s){listed}")
        image = len(found)
    print(
        f"{len(args.objects)} object(s), {failures} allocator reference(s), "
        f"{unlisted} unlisted ESP-IDF allocating call(s)"
        + (f", {image} forbidden image symbol(s)" if args.elf is not None else "")
    )
    return 1 if failures or unlisted or image else 0


if __name__ == "__main__":
    sys.exit(main())
