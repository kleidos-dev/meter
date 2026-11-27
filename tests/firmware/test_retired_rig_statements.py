# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""No tracked file states a retired calibration procedure or rig as current.

Three things the rig did once and does no more:

- **The shunt's two-point calibration from two known loads, 20 µA and 20 mA**,
  the plan of 0.1.0. It was never run: on 2026-11-20 the shunt was calibrated
  against a 100 Ω reference at one current on both ADC ranges, and that is the
  procedure (``calibration/README.md``, "The shunt, two points", and
  ``kleidos-meter calibrate``). A line that names both loads must say
  ``superseded``.
- **The bypass on the HW-316.** Until 2026-11-20 (rev C, the ``meter`` image
  until 0.7.0) the bypass was the module's K2 on ``IN2``; since rev D it is a
  channel of a MikroE Relay 2 Click, and of the HW-316 only K1 is wired. A
  line that names the HW-316 (or its ``IN2`` input, or its relay part) together
  with the bypass must also carry what dates it or names its successor: the
  Click, a date of the change, a version before 0.8.0, "spare", "unwired",
  "superseded" and the like (``CURRENT_RIG``).
- **The temporary 100 Ω load and its relay on GPIO16.** For the NW-00 night
  test of the meter-web plan a 100 Ω load hung on the Relay 2 Click's RL1,
  its control on GPIO16, and the relay timing of 2026-11-21 ran with it in
  the DUT's place (``docs/relay-timing.md``); it was removed on 2026-11-21
  and the rig is rev D again. A line that names the temporary load, NW-00's
  load relay or a load on GPIO16 must say ``removed`` or carry that date.

And no tracked file lists photographs still to take: the rig is drawn in
``hardware/rig.kicad_sch`` rev D and rendered as ``hardware/rig.svg``, and a
guide points there instead (the owner, 2026-11-24). What was read off a
photograph stays: those lines are sources, not a to-do.

The check reads a line at a time, which in this repository is a paragraph, a
list item or a table row (Markdown is never hard-wrapped). The records of
their time are left alone: the changelog, the frozen 0.1.0 design record,
the calibration records, and the schematic renders ``hardware/gen_rig.py``
writes, whose revision block keeps rev C's line (the generator itself is
read).

Host-only: ``git ls-files`` and the files read as text.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The records of their time, and this guard, which names what it looks for.
RECORDS = {
    "CHANGELOG.md",
    "docs/firmware-design.md",
    "hardware/rig.kicad_sch",
    "hardware/rig-calibration.kicad_sch",
    "hardware/rig.svg",
    "hardware/rig-calibration.svg",
    "tests/firmware/test_retired_rig_statements.py",
}

#: A directory of records.
RECORD_DIRS = ("calibration/records/",)

#: Both loads of the retired two-point procedure on one line, in either order.
TWO_POINT = re.compile(r"\b20\s*(?:µ|u)A\b.*\b20\s*mA\b|\b20\s*mA\b.*\b20\s*(?:µ|u)A\b")

#: The HW-316, its bypass input or its relay part.
HW316 = re.compile(r"HW-316|\bIN2\b|SRD-05VDC")

#: The bypass, as a word (not the `bypass_part` key, not the `BYPASS` net).
BYPASS = re.compile(r"\b[Bb]ypass(?:es|ed|ing)?\b(?!_)|\bK2\b")

#: What dates a line or names the rig that replaced it.
CURRENT_RIG = re.compile(
    r"Click|relay2click|LCA717|PhotoMOS|OptoMOS|\bSSR\b|\bspares?\b|unwired|not wired"
    r"|no-connect|never energi|no longer|only `?IN1|single-relay|keeps only K1"
    r"|(?:until|before|since) 2026-11-20|2026-10-\d\d|\brev C\b|before 0\.8\.0"
    r"|older than 0\.8\.0|\b0\.[1-7]\.0\b|superseded|out of the bypass",
    re.IGNORECASE,
)


#: The temporary load, NW-00's load relay, or a load on GPIO16.
TEMPORARY_LOAD = re.compile(
    r"\btemporary\s+(?:100\s*(?:Ω|Ohm|ohm)\s+)?load\b|\bload\s+relay\b"
    r"|\b100\s*(?:Ω|Ohm|ohm)\s+load\b"
    r"|NW-00[^.;]{0,60}\bload\b|\bload\b[^.;]{0,60}NW-00"
    r"|\bload\b[^.;]{0,60}\bGPIO\s?16\b|\bGPIO\s?16\b[^.;]{0,60}\bload\b",
    re.IGNORECASE,
)

#: What retires it: the word, or the day it went.
LOAD_REMOVED = re.compile(r"\bremoved\b|2026-11-21", re.IGNORECASE)

#: A list of photographs still to take.
PHOTO_TODO = re.compile(
    r"\bphoto(?:graph)?s?\s+(?:still\s+)?to\s+(?:take|shoot)\b"
    r"|\btake\b[^.;]{0,60}\bphoto(?:graph)?s?\b"
    r"|\bno\s+photo(?:graph)?s\s+yet\b"
    r"|\bphoto(?:graph)?s?\s+(?:still\s+)?(?:missing|wanted|needed|pending)\b",
    re.IGNORECASE,
)


def _tracked_text() -> list[tuple[str, str]]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.split(b"\0")
    files = []
    for raw in listed:
        name = raw.decode()
        if not name or name in RECORDS or name.startswith(RECORD_DIRS):
            continue
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            files.append((name, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return files


def two_point_as_current(line: str) -> bool:
    """Whether ``line`` states the 20 µA and 20 mA procedure without retiring it."""
    return bool(TWO_POINT.search(line)) and "superseded" not in line.lower()


def hw316_bypass_as_current(line: str) -> bool:
    """Whether ``line`` gives the bypass to the HW-316 with nothing that dates it."""
    return bool(HW316.search(line) and BYPASS.search(line) and not CURRENT_RIG.search(line))


def temporary_load_as_current(line: str) -> bool:
    """Whether ``line`` names the temporary load without saying it is gone."""
    return bool(TEMPORARY_LOAD.search(line)) and not LOAD_REMOVED.search(line)


def photo_todo(line: str) -> bool:
    """Whether ``line`` lists a photograph still to take."""
    return bool(PHOTO_TODO.search(line))


def _hits(predicate: Callable[[str], bool]) -> list[str]:
    return [
        f"{name}:{number}: {line.strip()[:160]}"
        for name, text in _tracked_text()
        for number, line in enumerate(text.splitlines(), start=1)
        if predicate(line)
    ]


def test_no_file_states_the_two_point_procedure_as_current() -> None:
    found = _hits(two_point_as_current)
    assert not found, (
        "the 20 µA and 20 mA calibration was superseded on 2026-11-20 "
        "(calibration/README.md, 'The shunt, two points'):\n" + "\n".join(found)
    )


def test_no_file_states_an_hw316_bypass_as_current() -> None:
    found = _hits(hw316_bypass_as_current)
    assert not found, (
        "the bypass is the Relay 2 Click's since 2026-11-20 (rev D); date the HW-316's, "
        "or name its successor:\n" + "\n".join(found)
    )


def test_the_two_point_check_sees_the_procedure_and_its_retirement() -> None:
    assert two_point_as_current("from two points, a known 20 µA and a known 20 mA")
    assert two_point_as_current("two known loads (~20 mA and ~20 uA)")
    assert not two_point_as_current("20 µA and 20 mA, superseded on 2026-11-20")
    assert not two_point_as_current("a known 20 µA load")


def test_the_bypass_check_sees_the_hw316_bypass_and_lets_its_history_pass() -> None:
    stated = [
        "An HW-316 module lets the instrument open the lead and bypass the shunt.",
        "| K2 | `IN2` | 15 | 8 | `bypass` | cell + | — | DUT battery + |",
        "The bypass is the SRD-05VDC-SL-C's NO contact.",
    ]
    for line in stated:
        assert hw316_bypass_as_current(line), line
    dated = [
        "the HW-316's K2 bypassed the shunt until 2026-11-20",
        "K2, the bypass on the Relay 2 Click; the HW-316 keeps K1",
        "an image older than 0.8.0 drove the HW-316 bypass",
        "| K2 | `IN2` | bypass until 2026-11-20 |",
        "HW-316 K2 | `IN2` unwired | spare",
        "`bypass_part=hw316` on the HW-316 image",
    ]
    for line in dated:
        assert not hw316_bypass_as_current(line), line


def test_no_file_states_the_temporary_load_as_fitted() -> None:
    found = _hits(temporary_load_as_current)
    assert not found, (
        "the temporary 100 Ω load and its relay on GPIO16 were removed on 2026-11-21 "
        "(the rig is rev D); say so on the line:\n" + "\n".join(found)
    )


def test_no_file_lists_photographs_to_take() -> None:
    found = _hits(photo_todo)
    assert not found, (
        "the rig is drawn in hardware/rig.kicad_sch rev D (hardware/rig.svg); "
        "point at the drawing instead of a photograph to take:\n" + "\n".join(found)
    )


def test_the_load_check_sees_the_load_and_lets_its_removal_pass() -> None:
    stated = [
        "the 100 Ω load on GPIO16",
        "The temporary load stays on the Click's RL1 until the next run.",
        "NW-00's load relay is wired to left 9.",
        "| 9 | `16` | GPIO16 | the load relay of the night test |",
        "GPIO16 switches a load for the night test",
        "a temporary 100 Ohm load in the DUT's place",
    ]
    for line in stated:
        assert temporary_load_as_current(line), line
    retired = [
        "the temporary load NW-00 wired to GPIO16 was removed on 2026-11-21",
        "## The record of 2026-11-21, with a 100 Ω load",
        "Measured on the rig with a 100 Ohm load on 2026-11-21",
        "the load relay on GPIO16, Removed",
        "GPIO16 (T9) and GPIO17 (T10) reach nothing",
        "a temporary build looped each frame",
        "the load current of the DUT",
    ]
    for line in retired:
        assert not temporary_load_as_current(line), line


def test_the_photo_check_sees_a_todo_and_lets_a_source_pass() -> None:
    todo = [
        "## Photos to take",
        "The guide has no photographs yet.",
        "The next time the Click is on the bench, take these four photos",
        "photographs still to take: the header, the Click",
        "Photo needed: the Click wired",
    ]
    for line in todo:
        assert photo_todo(line), line
    sources = [
        "Read off the photo: the shield is marked `S3-N16R8`",
        "Settled from the photographs, and nothing more:",
        "the owner's bench photograph of the ina228-board (not in the repository)",
        "an LCA717 PhotoMOS",
        "The drawing: `hardware/rig.svg`, rendered from `hardware/rig.kicad_sch` rev D",
    ]
    for line in sources:
        assert not photo_todo(line), line
