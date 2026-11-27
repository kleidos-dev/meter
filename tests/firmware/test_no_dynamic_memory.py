# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware's own code allocates nothing: the source gate.

``docs/misra.md`` claims that ``firmware/src`` uses no dynamic memory: every
FreeRTOS object and task stack is created over static storage, the TRACE ring
is a static array in PSRAM, and there is no ``new``, ``delete``, C allocator,
``heap_caps`` allocator, smart pointer or standard container, and no C stream
function (FW-18). This guard reads the sources with their comments and
strings removed and fails on any of those tokens; its allow-list is empty,
and a planted call proves it still sees one.
It also holds the ring's declaration to the one shape ``.ext_ram.bss`` keeps:
no initializer, which that NOLOAD section would drop without a word.

It also fails a call of an ESP-IDF entry point that allocates or frees on the
device (:data:`IDF_ALLOCATING`, the deny list) from a source that neither its
allow-list (:data:`IDF_ALLOWED`, each entry a site of docs/misra.md's D1; empty
since FW-16) nor its made-safe list (:data:`IDF_MADE_SAFE`, each entry a call
the image's configuration takes the allocation out of, with the mechanism)
names, and an entry of either whose source no longer makes the call; a planted
call proves it sees one. The lists are the object gate's and the host
harness's too, which ``test_idf_allocation_lists.py`` holds equal.

The object gate (``scripts/check_heap_objects.py``, CI's firmware job) reads
the compiled objects, which catches what a header or the compiler adds; the
device's counts (``DIAGnostic:HEAP?``, the bench suite) are the evidence on
the device.

Host-only: the sources read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "firmware" / "src"

#: Each forbidden construct and the pattern that finds it in code.
FORBIDDEN = {
    "new-expression": r"(?<![\w.>:])new\b\s*[\w:(<\[]",
    "delete-expression": r"(?<![\w.>:])delete\b\s*[\w(\[]",
    "C allocator": r"\b(?:std::)?(?:malloc|calloc|realloc|free|aligned_alloc)\s*\(",
    "heap_caps allocator": (
        r"\bheap_caps_(?:malloc|calloc|realloc|free|aligned_alloc|malloc_prefer)\w*\s*\("
    ),
    "FreeRTOS allocator": r"\b(?:pvPortMalloc|vPortFree)\s*\(",
    "dynamic FreeRTOS create": (
        r"\b(?:xQueueCreate|xSemaphoreCreateBinary|xSemaphoreCreateMutex|"
        r"xSemaphoreCreateCounting|xSemaphoreCreateRecursiveMutex|xTaskCreate|"
        r"xTaskCreatePinnedToCore|xEventGroupCreate|xStreamBufferCreate|"
        r"xMessageBufferCreate|xTimerCreate)\s*\("
    ),
    "smart pointer": r"\bstd::(?:unique_ptr|shared_ptr|make_unique|make_shared|weak_ptr)\b",
    "allocating standard type": (
        r"\bstd::(?:string|wstring|vector|deque|list|forward_list|map|multimap|set|"
        r"multiset|unordered_\w+|function|any|stringstream|ostringstream)\b"
    ),
    "allocating standard header": (
        r"#\s*include\s*<(?:string|vector|deque|list|forward_list|map|set|"
        r"unordered_map|unordered_set|functional|memory|any|sstream|iostream)>"
    ),
    # FW-18: the C library's streams. picolibc's stdio takes a lock and a
    # buffer from the heap on a stream's first use, and a file needs the VFS,
    # which the image leaves out (sdkconfig.defaults). The console writes
    # into its own buffers (the bounded writer of text.h, FW-24 to FW-26)
    # and writes UART0's registers; app_main()'s fallback writes through the
    # ROM's esp_rom_output_putc(), with no buffer.
    "stdio stream": (
        r"\b(?:std::)?(?:printf|vprintf|fprintf|vfprintf|puts|fputs|putchar|fputc|"
        r"putc|fwrite|fflush|fopen|freopen|fdopen|setvbuf)\s*\("
    ),
}

#: Allowed (file, construct) pairs: none.
ALLOWED: set[tuple[str, str]] = set()

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


def _code(text: str) -> str:
    """The text with comments, string and character literals blanked."""
    out = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = re.sub(r"//[^\n]*", "", out)
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', out)


def _findings(name: str, text: str) -> list[str]:
    code = _code(text)
    found = []
    for construct, pattern in FORBIDDEN.items():
        if (name, construct) in ALLOWED:
            continue
        for match in re.finditer(pattern, code):
            line = code.count("\n", 0, match.start()) + 1
            found.append(f"{name}:{line}: {construct}: {match.group(0).strip()}")
    return found


def _idf_calls(name: str, text: str) -> list[tuple[str, int]]:
    """Each call of an entry point of :data:`IDF_ALLOCATING` in *text*: (entry, line)."""
    code = _code(text)
    pattern = r"\b(" + "|".join(IDF_ALLOCATING) + r")\s*\("
    return [
        (match.group(1), code.count("\n", 0, match.start()) + 1)
        for match in re.finditer(pattern, code)
    ]


def _idf_findings(name: str, text: str) -> list[str]:
    return [
        f"{name}:{line}: allocating ESP-IDF call outside the allow-list: {entry}"
        for entry, line in _idf_calls(name, text)
        if (name, entry) not in IDF_ALLOWED and (name, entry) not in IDF_MADE_SAFE
    ]


def test_the_firmware_calls_no_allocating_esp_idf_entry_point_outside_the_allow_list() -> None:
    sources = sorted(SRC.glob("*.cpp")) + sorted(SRC.glob("*.h"))
    found = [hit for path in sources for hit in _idf_findings(path.name, path.read_text("utf-8"))]
    assert not found, "allocating ESP-IDF call (docs/misra.md D1):\n" + "\n".join(found)


def test_every_allow_list_and_made_safe_entry_is_still_a_call_of_its_source() -> None:
    stale = [
        f"{source}: {entry}"
        for source, entry in [*IDF_ALLOWED, *IDF_MADE_SAFE]
        if entry not in {e for e, _ in _idf_calls(source, (SRC / source).read_text("utf-8"))}
    ]
    assert not stale, "list entries no source calls any more:\n" + "\n".join(stale)


def test_the_gate_sees_a_planted_allocating_esp_idf_call() -> None:
    planted = "void f() {\n    (void)esp_intr_alloc( 0, 0, nullptr, nullptr, nullptr );\n}\n"
    assert _idf_findings("sampler.cpp", planted) == [
        "sampler.cpp:2: allocating ESP-IDF call outside the allow-list: esp_intr_alloc"
    ]
    # Made safe in its own source, refused in another; a comment or a longer
    # name is no call. NVS left the allow-list in FW-16: a save there is a
    # finding again.
    write = (
        "err = esp_flash_write( c, b, a, n );\n// esp_flash_erase_region( c, a, n )\n"
        "esp_task_wdt_add_users( x );\n"
    )
    assert _idf_findings("flash_port.cpp", write) == []
    assert _idf_findings("console.cpp", write) == [
        "console.cpp:1: allocating ESP-IDF call outside the allow-list: esp_flash_write"
    ]
    assert _idf_findings("calibration.cpp", "err = nvs_set_blob( h, k, v, n );\n") == [
        "calibration.cpp:1: allocating ESP-IDF call outside the allow-list: nvs_set_blob"
    ]


def test_the_firmware_sources_allocate_nothing() -> None:
    sources = sorted(SRC.glob("*.cpp")) + sorted(SRC.glob("*.h"))
    assert len(sources) > 20
    found = [hit for path in sources for hit in _findings(path.name, path.read_text("utf-8"))]
    assert not found, "dynamic memory in firmware/src (docs/misra.md):\n" + "\n".join(found)


def test_the_gate_sees_each_planted_construct() -> None:
    planted = {
        "new-expression": "auto* p = new Sampler();",
        "delete-expression": "delete p;",
        "C allocator": "void* p = malloc( 4 );",
        "heap_caps allocator": "void* p = heap_caps_malloc( 4, 0 );",
        "FreeRTOS allocator": "void* p = pvPortMalloc( 4 );",
        "dynamic FreeRTOS create": "queue = xQueueCreate( 4, 4 );",
        "smart pointer": "std::unique_ptr<int> p;",
        "allocating standard type": "std::vector<int> v;",
        "allocating standard header": "#include <vector>",
        "stdio stream": 'std::printf( "%d", 4 );',
    }
    assert set(planted) == set(FORBIDDEN)
    for construct, line in planted.items():
        hits = _findings("planted.cpp", f"void f() {{\n    {line}\n}}\n")
        assert any(construct in hit for hit in hits), construct


def test_comments_strings_and_static_creates_do_not_count() -> None:
    clean = (
        "// a new ring; free( nothing )\n"
        '/* std::vector */ const char* s = "malloc( 4 ) new X";\n'
        "queue = xQueueCreateStatic( 4, 4, storage, &buffer );\n"
        'task = xTaskCreateStaticPinnedToCore( f, "t", 4096, this, 1, stack, &tcb, 0 );\n'
        "std::size_t free_internal = heap_caps_get_free_size( 0 );\n"
        "Sampler( const Sampler& ) = delete;\n"
        'n = std::snprintf( buffer, sizeof buffer, "%d", 4 );\n'
        'esp_rom_printf( "boot\\n" );\n'
        "void respond( const char* format, ... ) __attribute__( ( format( printf, 2, 3 ) ) );\n"
    )
    assert _findings("clean.cpp", clean) == []


def test_the_gate_sees_each_planted_stdio_call() -> None:
    """FW-18: every stream function the pattern names is a finding, with or
    without ``std::``; ``snprintf`` and the ROM's ``esp_rom_printf`` are not."""
    for call in ("puts( s )", "fwrite( b, 1, n, f )", "fopen( p, m )", "printf( s )"):
        for spelled in (call, f"std::{call}"):
            hits = _findings("planted.cpp", f"void f() {{\n    (void){spelled};\n}}\n")
            assert any("stdio stream" in hit for hit in hits), spelled


def test_the_ring_is_static_in_psram_and_has_no_initializer() -> None:
    main = _code((SRC / "main.cpp").read_text("utf-8"))
    declaration = re.search(r"EXT_RAM_BSS_ATTR\s+static\s+([^;]+);", main)
    assert declaration, "main.cpp no longer declares the ring EXT_RAM_BSS_ATTR static"
    assert re.fullmatch(r"std::array<std::int32_t,\s*kTraceSamples>\s+gRing", declaration.group(1))
    assert "gRing" in main.split(declaration.group(0), 1)[1], "the ring is not handed on"
