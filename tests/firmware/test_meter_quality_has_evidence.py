# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench meter's ``q=`` token is never answered from no evidence.

At 0.100 Ω the instrument's floor is the ±0.5 µA that ``dVos/dT`` moves over
±5 °C (``docs/architecture.md`` §3.7), so the die temperature is
the one quantity that says whether the offset null still stands.  Two ways that
can silently stop being true, both of which happened:

1. **A write-only field.**  The boundary temperatures a ``TRACE`` capture takes
   — the whole reason ``MODE = 4h`` is in the driver — were stored in a private
   member nothing read, while the token was judged against a different value
   that only the streaming profiles ever wrote.  After a ``TRACE`` capture the
   instrument reported ``q=cal die_mc=unknown`` for ever, however far the bench
   had drifted.  GCC does not warn on an unused private field, so the compiler
   cannot be the guard here.

2. **A sentinel used as a number.**  ``kDieMilliCelsiusUnknown`` is
   ``INT32_MIN``, and ``live - INT32_MIN`` is signed overflow.  ``ZERO`` can
   store that sentinel whenever its own temperature read failed, so *both*
   temperatures have to be checked, not just the live one.

Host-only: three C++ files, read as text.  No device, no build.

These are a backstop.  What the token does with a TRACE capture's boundary
temperatures is held by behavior, in the host tests
(``firmware/test/native/test_sampler_sides.cpp``: a capture that closes or
opens 6 °C warmer reads ``q=stale``); the text is read with its comments
removed, so a comment that names a call counts for nothing (the audit's mutant
F2b deleted the closing note and survived on a comment that named it).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

METER_SRC = REPO_ROOT / "firmware" / "src"

SAMPLER_CPP = METER_SRC / "sampler.cpp"
SAMPLER_H = METER_SRC / "sampler.h"
CALIBRATION_CPP = METER_SRC / "calibration.cpp"

#: The sentinel the quality test must never treat as a temperature.
SENTINEL = "kDieMilliCelsiusUnknown"

#: The one writer of the value ``q=`` is judged against.
NOTE_CALL = "noteDieMilliCelsius("


def code_only(text: str) -> str:
    """Returns C++ source with its comments blanked out.

    ``//`` and ``/* */`` comments become spaces (a block comment keeps its
    newlines), while string and character literals are kept as written, so a
    ``//`` inside a literal is not taken for a comment. An apostrophe after a
    letter or a digit is a digit separator (``1'000``), not a literal.

    :param text: The source.
    :returns: The same text with every comment replaced by blanks.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"' or (c == "'" and not (i > 0 and text[i - 1].isalnum())):
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def test_comments_count_for_nothing() -> None:
    """The backstop reads code: a call named in a comment is not a call."""
    source = (
        "int a = f(); // noteDieMilliCelsius( x );\n"
        "/* noteDieMilliCelsius( y );\n */ int b;\n"
        'const char* s = "// noteDieMilliCelsius(";\n'
        "int c = 1'000; // noteDieMilliCelsius( z );\n"
        "char q = '\\''; // noteDieMilliCelsius( w );\n"
    )
    code = code_only(source)
    assert code.count(NOTE_CALL) == 1  # the one inside the string literal
    assert "int a = f();" in code and "int b;" in code
    assert "int c = 1'000;" in code and "char q = '\\'';" in code
    assert code.count("\n") == source.count("\n")


def test_the_sampler_has_no_second_die_temperature() -> None:
    """One value, not two.

    A second die-temperature member is how the defect arose: three writes into
    a field nothing read, beside an atomic only one code path stored into.
    """
    sources = {path: path.read_text(encoding="utf-8") for path in (SAMPLER_H, SAMPLER_CPP)}
    for path, text in sources.items():
        assert "dieMilliCelsiusLast_" not in text, (
            f"{path.name} carries a second die-temperature member. The value the "
            f"quality token is judged against is lastDieMilliCelsius_, and every "
            f"writer must store into it."
        )


def test_every_die_temperature_reaches_the_judged_value() -> None:
    """Each of the three readers notes what it read.

    The three are the opening capture boundary, the closing one, and a streamed
    sample's own ``DIETEMP``.  ``ZERO`` adds a fourth.  Fewer than three means a
    reading that the token cannot see.
    """
    text = code_only(SAMPLER_CPP.read_text(encoding="utf-8"))
    notes = text.count(NOTE_CALL) - text.count(f"void Sampler::{NOTE_CALL}")

    assert notes >= 3, (
        f"sampler.cpp calls {NOTE_CALL} {notes} time(s); the capture boundaries and "
        f"the sampled DIETEMP must each note what they read, or `q=` falls through "
        f"to `cal` on evidence it never saw."
    )


def test_the_judged_value_is_an_atomic_the_console_reads() -> None:
    """The console reads it across cores, so it is an atomic with an accessor."""
    header = SAMPLER_H.read_text(encoding="utf-8")
    assert re.search(r"std::atomic<std::int32_t>\s+lastDieMilliCelsius_", header), (
        "lastDieMilliCelsius_ must stay an atomic: the sampler writes it, the console reads it."
    )
    assert "std::int32_t lastDieMilliCelsius() const noexcept" in header


def test_what_both_tasks_read_mid_capture_is_atomic_or_handed_over() -> None:
    """The sampler's fields the console reads while a window is open.

    The profile, the pace, the sink and the ring's fill are read by both tasks
    while a capture runs, so they are atomics. The loop's sums and extremes
    are not read mid-capture at all: the task keeps its own copy and hands it
    over by value (``totals_``) just before the acknowledgement the console
    takes in ``halt()``. A plain field read across the cores is undefined in the
    language and invisible to the host tests, whose scheduler runs one task at
    a time.
    """
    header = SAMPLER_H.read_text(encoding="utf-8")
    for declaration in (
        r"std::atomic<Profile>\s+profile_",
        r"std::atomic<Pace>\s+pace_",
        r"std::atomic<Sink>\s+sink_",
        r"std::atomic<std::uint32_t>\s+ringCount_",
    ):
        assert re.search(declaration, header), f"sampler.h must declare {declaration}"
    for plain in ("sumNanoamps_", "minNanoamps_", "maxNanoamps_", "captureEndUs_"):
        assert plain not in header, f"{plain} is read across tasks: keep it in LoopTotals"
    source = SAMPLER_CPP.read_text(encoding="utf-8")
    loop_end = source.index("totals_ = totals;")
    assert source.index("xSemaphoreGive( doneSem_ )", loop_end) > loop_end, (
        "the totals must cross before the acknowledgement that orders them"
    )
    halt = source[source.index("bool Sampler::halt()") :]
    halt = halt[: halt.index("\n}\n")]
    assert "xSemaphoreTake( doneSem_" in halt, "halt() must take the acknowledgement"


def test_a_boundary_reading_is_noted_unconditionally() -> None:
    """``START`` refreshes it, with the sentinel when the read failed.

    This is what stops a reading outliving the capture that took it: without
    it, a temperature from a run days ago still passed as live evidence.
    """
    text = code_only(SAMPLER_CPP.read_text(encoding="utf-8"))
    assert f"{NOTE_CALL} info.dieMilliCelsiusStart )" in text, (
        "beginCapture() must note the opening boundary temperature unconditionally, "
        "sentinel included, so a stale reading cannot survive into a new capture."
    )


def test_the_staleness_test_guards_both_temperatures() -> None:
    """Neither side of the subtraction may be the sentinel.

    ``ZERO`` stores whatever its temperature read returned, sentinel included,
    so guarding only the live value leaves ``live - INT32_MIN`` — signed
    overflow, and a verdict unrelated to the temperature.
    """
    text = CALIBRATION_CPP.read_text(encoding="utf-8")
    start = text.index("Quality CalibrationStore::quality(")
    body = text[start:]

    guards = body.count(f"!= {SENTINEL}")
    assert guards >= 2, (
        f"quality() checks the {SENTINEL} sentinel {guards} time(s); both the live "
        f"temperature and the one the null was taken at must be checked."
    )

    assert "std::int64_t drift" in body, (
        "The spread must be formed in int64, so that neither the subtraction nor "
        "its negation can overflow on a sentinel that slipped through."
    )


def test_uncal_is_keyed_off_a_flag_not_off_the_nominal_value() -> None:
    """A shunt that measures exactly 100.000 mΩ is still a measurement.

    Keying ``uncal`` off ``shuntMicroOhm == kNominalShuntMicroOhm`` made the
    single most likely outcome of the two-point calibration on an R100 part
    indistinguishable from never having calibrated at all.
    """
    text = CALIBRATION_CPP.read_text(encoding="utf-8")
    start = text.index("Quality CalibrationStore::quality(")
    body = text[start:]

    assert "kNominalShuntMicroOhm" not in body, (
        "quality() must not compare the stored resistance against the nominal "
        "value: that makes the nominal value a sentinel for 'unmeasured'."
    )
    assert "shuntMeasured" in body
