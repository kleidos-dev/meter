# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The object gate knows every allocator, and CI runs it on the image.

``scripts/check_heap_objects.py`` reads ``nm --undefined-only`` of each object
of the firmware's own component and fails on a reference to an allocator.
These tests hold its list of names (the C allocator, ``heap_caps_*``,
``pvPortMalloc``, the dynamic FreeRTOS creates, every ``operator new`` and
``operator delete``) against the static creates and the heap queries the
firmware does use, run it against a stand-in ``nm`` both ways, show that an
allocating ESP-IDF call passes only from the source its made-safe entry names
and that a planted one fails, that its ``--elf`` leg fails an image holding
the allocating C++ runtime or NVS (FW-16), and check that CI's firmware job
runs it on the image's objects and on the image. ``test_idf_allocation_lists.py``
holds its ESP-IDF lists to the source gate's and the harness's.

Host-only: a temporary stand-in for nm, written in Python.
"""

from __future__ import annotations

import ast
import importlib.util
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_heap_objects.py"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_heap_objects", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_allocators_are_forbidden() -> None:
    gate = _module()
    for symbol in (
        "malloc",
        "free",
        "heap_caps_malloc",
        "heap_caps_calloc_prefer",
        "heap_caps_free",
        "pvPortMalloc",
        "xQueueGenericCreate",
        "xTaskCreatePinnedToCore",
        "_Znwj",
        "_ZnajRKSt9nothrow_t",
        "_ZdlPv",
        "_ZdaPvj",
        "_ZnwjSt11align_val_t",
    ):
        assert gate.forbidden(symbol), symbol


#: Every exact name the gate forbids, written out: the C allocator, FreeRTOS's
#: heap, and each dynamic FreeRTOS create. The gate may forbid more, never less.
EXPECTED_FORBIDDEN_NAMES = frozenset(
    {
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
)


@pytest.mark.parametrize("symbol", sorted(EXPECTED_FORBIDDEN_NAMES))
def test_every_allocator_and_dynamic_create_is_forbidden(symbol: str) -> None:
    gate = _module()
    assert symbol in gate.FORBIDDEN_NAMES, f"{symbol} left FORBIDDEN_NAMES"
    assert gate.forbidden(symbol), symbol


def forbidden_names_in(text: str) -> set[str] | None:
    """``FORBIDDEN_NAMES`` as *text*, one version of the script, assigns it."""
    for node in ast.walk(ast.parse(text)):
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and getattr(node.targets[0] if isinstance(node, ast.Assign) else node.target, "id", "")
            == "FORBIDDEN_NAMES"
            and node.value is not None
        ):
            return set(ast.literal_eval(node.value))
    return None


def test_the_forbidden_names_only_grow() -> None:
    """No commit of the script's history forbade a name the script does not forbid now."""
    current = _module().FORBIDDEN_NAMES
    listing = subprocess.run(
        ["git", "log", "--format=%H", "--", str(SCRIPT.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    dropped = []
    for commit in listing:
        shown = subprocess.run(
            ["git", "show", f"{commit}:{SCRIPT.relative_to(ROOT).as_posix()}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        old = forbidden_names_in(shown.stdout) if shown.returncode == 0 else None
        dropped += [f"{commit[:10]}: {name}" for name in sorted((old or set()) - current)]
    assert not dropped, "names that left FORBIDDEN_NAMES:\n" + "\n".join(dropped)


def test_what_the_firmware_uses_is_allowed() -> None:
    gate = _module()
    for symbol in (
        "xQueueGenericCreateStatic",
        "xTaskCreateStaticPinnedToCore",
        "heap_caps_get_free_size",
        "heap_caps_get_minimum_free_size",
        "heap_caps_get_largest_free_block",
        "snprintf",
        "esp_flash_write",
        "nvs_flash_init",
    ):
        assert not gate.forbidden(symbol), symbol


def _fake_nm(tmp_path: Path, symbols: list[str]) -> Path:
    nm = tmp_path / "nm"
    lines = "".join(f"{s} U\n" for s in symbols)
    nm.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({lines!r})\n", "utf-8")
    nm.chmod(nm.stat().st_mode | stat.S_IEXEC)
    return nm


def test_the_gate_passes_clean_objects_and_fails_a_planted_one(tmp_path: Path) -> None:
    gate = _module()
    obj = tmp_path / "relay.cpp.o"
    obj.write_bytes(b"")
    clean = _fake_nm(tmp_path, ["xQueueGenericCreateStatic", "snprintf"])
    assert gate.main(["--nm", str(clean), str(obj)]) == 0
    planted = _fake_nm(tmp_path, ["xQueueGenericCreateStatic", "heap_caps_malloc"])
    assert gate.main(["--nm", str(planted), str(obj)]) == 1


def test_a_made_safe_esp_idf_call_passes_only_from_its_source(tmp_path: Path) -> None:
    gate = _module()
    port = tmp_path / "flash_port.cpp.o"
    console = tmp_path / "console.cpp.o"
    for obj in (port, console):
        obj.write_bytes(b"")
    log = _fake_nm(tmp_path, ["esp_flash_read", "esp_flash_write", "esp_flash_erase_region"])
    assert gate.main(["--nm", str(log), str(port)]) == 0
    assert gate.main(["--nm", str(log), str(console)]) == 1
    assert gate.unlisted_idf("console.cpp", ["esp_flash_write"]) == ["esp_flash_write"]
    assert gate.unlisted_idf("console.cpp", ["uart_driver_install"]) == ["uart_driver_install"]
    # FW-16 took NVS off the allow-list: a save from the store is unlisted,
    # and so is an init from main.cpp.
    assert gate.IDF_ALLOWED == {}
    assert gate.unlisted_idf("calibration.cpp", ["nvs_open", "nvs_set_blob"]) == [
        "nvs_open",
        "nvs_set_blob",
    ]
    assert gate.unlisted_idf("main.cpp", ["nvs_flash_init"]) == ["nvs_flash_init"]
    assert gate.allowed_idf("main.cpp", ["nvs_flash_init"]) == []
    # FW-14 took the I2C master's entry points off the list: the INA228's
    # driver allocates nothing, and a call from it is unlisted.
    assert gate.unlisted_idf("ina228.cpp", ["i2c_master_bus_add_device"]) == [
        "i2c_master_bus_add_device"
    ]


def test_a_made_safe_entry_its_object_no_longer_calls_fails_the_gate(tmp_path: Path) -> None:
    gate = _module()
    obj = tmp_path / "flash_port.cpp.o"
    obj.write_bytes(b"")
    whole = _fake_nm(tmp_path, ["esp_flash_read", "esp_flash_write", "esp_flash_erase_region"])
    assert gate.main(["--nm", str(whole), str(obj)]) == 0
    without_erase = _fake_nm(tmp_path, ["esp_flash_read", "esp_flash_write"])
    assert gate.main(["--nm", str(without_erase), str(obj)]) == 1
    assert gate.stale_idf("flash_port.cpp", []) == [
        "esp_flash_erase_region",
        "esp_flash_read",
        "esp_flash_write",
    ]


def test_a_planted_allocating_esp_idf_call_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = _module()
    obj = tmp_path / "flash_port.cpp.o"
    obj.write_bytes(b"")
    made_safe = ["esp_flash_read", "esp_flash_write", "esp_flash_erase_region"]
    planted = _fake_nm(tmp_path, [*made_safe, "esp_intr_alloc"])
    assert gate.main(["--nm", str(planted), str(obj)]) == 1
    out = capsys.readouterr().out
    assert "flash_port.cpp.o: 1 unlisted ESP-IDF allocating call(s): esp_intr_alloc" in out
    assert out.splitlines()[-1] == (
        "1 object(s), 0 allocator reference(s), 1 unlisted ESP-IDF allocating call(s)"
    )


def _fake_image_nm(tmp_path: Path, symbols: list[str]) -> Path:
    nm = tmp_path / "nm_image"
    lines = "".join(f"{s} T 4200{i:04x} 4\n" for i, s in enumerate(symbols))
    nm.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({lines!r})\n", "utf-8")
    nm.chmod(nm.stat().st_mode | stat.S_IEXEC)
    return nm


def test_the_image_leg_fails_the_cxx_runtime_and_nvs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FW-16: no operator new or delete, no __cxa_* (but the forced dummy),
    no eh_globals and no NVS in firmware.elf, whoever pulled them in; FW-18:
    no PSA or mbedTLS function but mbedTLS's forced empty hook."""
    gate = _module()
    obj = tmp_path / "relay.cpp.o"
    obj.write_bytes(b"")
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"")
    clean = [
        "app_main",
        "__cxa_guard_dummy",
        "esp_flash_default_chip",
        "_ZN7kleidos5meter4heap3armEv",
        # FW-18: the wrap and mbedTLS's forced hook are not PSA's code.
        "__wrap_psa_crypto_init",
        "mbedtls_psa_crypto_init_include_impl",
        "__esp_system_init_fn_mbedtls_psa_crypto_init_fn",
    ]
    nm = _fake_image_nm(tmp_path, clean)
    assert gate.main(["--nm", str(nm), "--elf", str(elf), str(obj)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].endswith(", 0 forbidden image symbol(s)")
    for planted in (
        "_Znwj",
        "_ZnajRKSt9nothrow_t",
        "_ZdlPv",
        "_ZdaPv",
        "__cxa_begin_catch",
        "__cxa_guard_acquire",
        "__wrap___cxa_throw",
        "_ZN12_GLOBAL__N_110eh_globalsE",
        "nvs_flash_init",
        "_ZN3nvs19NVSPartitionManager11open_handleEPKcS2_15nvs_open_mode_tPPNS_15NVSHandleSimpleE",
        "_ZN14NVSHandleEntry17s_nvs_next_handleE",
        "psa_crypto_init",
        "psa_initialize_key_slots",
        "mbedtls_mutex_lock",
    ):
        nm = _fake_image_nm(tmp_path, [*clean, planted])
        assert gate.main(["--nm", str(nm), "--elf", str(elf), str(obj)]) == 1, planted
        assert f"firmware.elf: 1 C++ runtime, NVS or PSA symbol(s): {planted} (" in (
            capsys.readouterr().out
        )
    # Without --elf the image is not read.
    nm = _fake_image_nm(tmp_path, ["_Znwj"])
    assert gate.main(["--nm", str(nm), str(obj)]) == 1  # an object referencing new fails anyway


def test_the_task_watchdog_subscription_is_no_longer_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FW-11: the console and the sampler check in with the firmware's own
    watchdog, so ESP-IDF's task watchdog subscription is unlisted in both."""
    gate = _module()
    for source in ("console.cpp", "sampler.cpp"):
        assert (source, "esp_task_wdt_add") not in gate.IDF_ALLOWED
    obj = tmp_path / "sampler.cpp.o"
    obj.write_bytes(b"")
    planted = _fake_nm(tmp_path, ["esp_task_wdt_add"])
    assert gate.main(["--nm", str(planted), str(obj)]) == 1
    out = capsys.readouterr().out
    assert "sampler.cpp.o: 1 unlisted ESP-IDF allocating call(s): esp_task_wdt_add" in out


def test_ci_runs_the_gate_on_the_image() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check_heap_objects.py" in text
    assert "firmware/.pio/build/meter/src" in text
    assert "--elf firmware/.pio/build/meter/firmware.elf" in text
