# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""scripts/heap_census.py: the census build's events, read, symbolized and tabulated.

The instrument's answers are the shapes ``DIAGnostic:HEAP:CENSus?`` and
``DIAGnostic:HEAP:CENSus:EVENt?`` give (``firmware/test/native/test_heap.cpp``
holds the firmware to them); the symboliser is a stand-in, and the command
line runs against a fake ``addr2line``, so no toolchain is needed.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import heap_census  # noqa: E402

SUMMARY = "capacity=384 frames=16 events=4 first=0"
EVENTS = [
    "n=0 op=alloc phase=pre size=344 caps=0x00001800 block=0x3fc9a000 "
    "callers=0x40375000,0x42001000",
    "n=1 op=alloc phase=main size=96 caps=0x00001800 block=0x3fc9b000 "
    "callers=0x42000100,0x40375000,0x42002000,0x42003000",
    "n=2 op=alloc phase=main size=32 caps=0x00001800 block=0x3fc9c000 "
    "callers=0x40375000,0x42002000,0x42003000",
    "n=3 op=free phase=main size=0 caps=0x00000000 block=0x3fc9b000 callers=-",
]
SYMBOLS = {
    0x40375000: ("heap_caps_malloc_base", "/idf/components/heap/heap_caps_base.c:175"),
    0x42000100: ("esp_heap_trace_alloc_hook", "/repo/firmware/src/heap_monitor.cpp:300"),
    0x42001000: ("esp_timer_init", "/idf/components/esp_timer/src/esp_timer.c:610"),
    0x42002000: ("uart_driver_install", "/idf/components/esp_driver_uart/src/uart.c:1700"),
    0x42003000: ("kleidos::meter::Console::begin()", "/repo/firmware/src/console.cpp:330"),
}


def test_an_event_line_parses_to_its_fields() -> None:
    event = heap_census.parse_event(EVENTS[1])
    assert event.index == 1
    assert event.allocation and not event.before_main
    assert (event.size, event.caps, event.block) == (96, 0x1800, 0x3FC9B000)
    assert event.callers == (0x42000100, 0x40375000, 0x42002000, 0x42003000)
    assert heap_census.parse_event(EVENTS[3]).callers == ()


def test_a_line_that_is_not_an_event_is_refused() -> None:
    with pytest.raises(ValueError, match="not a census event"):
        heap_census.parse_event("n=0 op=grow phase=pre size=1 caps=0x0 block=0x0 callers=-")
    with pytest.raises(KeyError):
        heap_census.parse_event("n=0 op=alloc")


def test_the_plain_image_has_no_census_to_read() -> None:
    with pytest.raises(ValueError, match="METER_HEAP_CENSUS"):
        heap_census.parse_summary("capacity=0 frames=16 events=0 first=0")


def test_the_site_climbs_over_the_allocator() -> None:
    event = heap_census.parse_event(EVENTS[1])
    where = heap_census.site_of(event, SYMBOLS)
    assert where.site == "uart_driver_install (components/esp_driver_uart/src/uart.c:1700)"
    assert where.via == ""
    assert where.owner == "Console::begin"
    assert where.chain[1] == "kleidos::meter::Console::begin() (firmware/src/console.cpp:330)"
    only_allocator = heap_census.Event(0, True, True, 8, 0, 0, (0x40375000,))
    nowhere = heap_census.site_of(only_allocator, SYMBOLS)
    assert nowhere.site.startswith("unknown")
    assert nowhere.owner == "ESP-IDF"
    # A queue's create is climbed over and named: the site is who asked for it.
    queue = (0x40376000, ("xQueueGenericCreate", "/idf/components/freertos/queue.c:545"))
    symbols = dict([*SYMBOLS.items(), queue])
    queued = heap_census.Event(0, True, False, 8, 0, 0, (0x40375000, 0x40376000, 0x42002000))
    where = heap_census.site_of(queued, symbols)
    assert (where.site.split(" ")[0], where.via) == ("uart_driver_install", "xQueueGenericCreate")
    # Nothing readable above the wrapper: the wrapper is the site.
    orphan = heap_census.Event(0, False, False, 0, 0, 0, (0x40375000, 0x40376000))
    assert heap_census.site_of(orphan, symbols).site.startswith("xQueueGenericCreate (")
    assert heap_census.is_wrapper("ExceptionlessAllocatable::operator new(unsigned int)")
    assert (
        heap_census.own_name("kleidos::meter::(anonymous namespace)::blankOne(int)") == "blankOne"
    )
    for name in ("malloc", "heap_caps_malloc", "kleidos::meter::heap::(anonymous namespace)::note"):
        assert heap_census.is_allocator(name)
    assert not heap_census.is_allocator("freertos_task_create")


def test_the_table_groups_by_phase_op_and_site_with_their_totals() -> None:
    census = heap_census.parse_saved([SUMMARY, *EVENTS])
    rows = heap_census.tabulate(census, SYMBOLS)
    assert [(r.phase, r.op, r.count, r.bytes) for r in rows] == [
        ("pre_main", "alloc", 1, 344),
        ("since_main", "alloc", 2, 128),
        ("since_main", "free", 1, 0),
    ]
    text = heap_census.render(census, rows, chains=True)
    assert "- before app_main: 1 allocation(s), 0 free(s)" in text
    assert "- since app_main: 2 allocation(s), 1 free(s)" in text
    assert "| since_main | alloc | 2 | 128 | uart_driver_install" in text
    assert "console.cpp:330) | - | Console::begin |" in text
    assert "| pre_main | ESP-IDF | 1 | 0 | 344 |" in text
    assert "| since_main | Console::begin | 2 | 0 | 128 |" in text
    assert "| since_main | ESP-IDF | 0 | 1 | 0 |" in text
    assert "<- kleidos::meter::Console::begin()" in text
    assert "overwritten" not in text


def test_a_census_that_lost_events_says_so() -> None:
    census = heap_census.parse_saved(["capacity=2 frames=16 events=4 first=2", *EVENTS[2:]])
    text = heap_census.render(census, heap_census.tabulate(census, SYMBOLS))
    assert "**2 event(s) were overwritten" in text


def test_read_census_asks_for_every_event_held() -> None:
    answers = {"DIAGnostic:HEAP:CENSus?": "capacity=384 frames=16 events=6 first=2"}
    for i, line in enumerate(EVENTS):
        answers[f"DIAGnostic:HEAP:CENSus:EVENt? {i + 2}"] = line.replace(f"n={i}", f"n={i + 2}", 1)
    asked: list[str] = []

    def query(text: str) -> str:
        asked.append(text)
        return answers[text]

    census, lines = heap_census.read_census(query)
    assert asked[1:] == [f"DIAGnostic:HEAP:CENSus:EVENt? {i}" for i in range(2, 6)]
    assert [event.index for event in census.held] == [2, 3, 4, 5]
    assert lines[0] == answers["DIAGnostic:HEAP:CENSus?"]
    assert heap_census.parse_saved(lines).held == census.held


def fake_addr2line(tmp_path: Path) -> Path:
    """An ``addr2line`` that names each address from :data:`SYMBOLS`."""
    tool = tmp_path / "addr2line"
    table = {f"0x{a:08x}": symbol for a, symbol in SYMBOLS.items()}
    tool.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"table = {table!r}\n"
        "args = sys.argv[1:]\n"
        "assert args[:3] == ['-f', '-C', '-e'], args\n"
        "for address in args[4:]:\n"
        "    function, location = table.get(address, ('??', '??:0'))\n"
        "    print(function)\n"
        "    print(location)\n",
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def test_the_command_line_reads_a_saved_census(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saved = tmp_path / "census.txt"
    saved.write_text("\n".join([SUMMARY, *EVENTS]) + "\n", encoding="utf-8")
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"\x7fELF")
    tool = fake_addr2line(tmp_path)
    code = heap_census.main(["--from", str(saved), "--elf", str(elf), "--addr2line", str(tool)])
    out = capsys.readouterr().out
    assert code == 0
    assert (
        "| pre_main | alloc | 1 | 344 | esp_timer_init (components/esp_timer/src/esp_timer.c:610) "
        "| - | ESP-IDF |" in out
    )


def test_the_command_line_fails_a_census_that_lost_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saved = tmp_path / "census.txt"
    saved.write_text("\n".join(["capacity=2 frames=16 events=4 first=2", *EVENTS[2:]]), "utf-8")
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"\x7fELF")
    args = ["--from", str(saved), "--elf", str(elf), "--addr2line", str(fake_addr2line(tmp_path))]
    assert heap_census.main(args) == 1
    assert "overwritten" in capsys.readouterr().out


def test_a_symboliser_that_answers_short_is_an_error(tmp_path: Path) -> None:
    tool = tmp_path / "addr2line"
    tool.write_text(f"#!{sys.executable}\nprint('one')\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    symbolize = heap_census.addr2line_symboliser(tmp_path / "firmware.elf", tool)
    assert symbolize([]) == {}
    with pytest.raises(RuntimeError, match="1 lines for 2 addresses"):
        symbolize([1, 2])
