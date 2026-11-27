# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host's zero-allocation session gives every command of the table.

``docs/misra.md`` claims ``allocs_since_main=0 frees_since_main=0`` for every
operation the SCPI table offers (FW-19). On the host that claim is
``firmware/test/native/test_heap.cpp``'s every-command session
(``kEveryCommand``), which requires both counts at 0 after each message; this
holds the session to the table: every header of ``kCommands`` in
``firmware/src/console.cpp`` must be the header of at least one unit of the
session, matched as the console matches it (long or short form of each node,
optional nodes, numeric suffixes, any case). A command added to the table
without a message in the session fails here.

Host-only: both files read as text, the table by ``scripts/scpi_table.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import scpi_table  # noqa: E402

SESSION = ROOT / "firmware" / "test" / "native" / "test_heap.cpp"

#: A C++ string literal, plain or raw (``R"(...)"``).
LITERAL = re.compile(r'R"\((.*?)\)"|"((?:[^"\\]|\\.)*)"', re.S)


def _initializer(text: str, name: str) -> str:
    found = re.search(rf"\b{name}\[\]\s*=\s*\{{(.*?)\n\s*\}};", text, re.S)
    assert found, f"no array {name}"
    return re.sub(r"//[^\n]*", "", found.group(1))


def table_headers() -> list[str]:
    """The headers of ``kCommands``, in its order."""
    return [entry.pattern for entry in scpi_table.commands()]


#: The headers a table pattern matches, as a regular expression.
pattern = scpi_table.header_pattern


#: The arrays of the every-command session.
ARRAYS = ("kEveryCommand", "kLastWord")


def session_messages(text: str, arrays: tuple[str, ...] = ARRAYS) -> list[str]:
    """The messages of the session's arrays: the first literal of each element."""
    body = "".join(_initializer(text, name) for name in arrays)
    messages = []
    for element in re.findall(r"\{\s*(R\"\(.*?\)\"|\"(?:[^\"\\]|\\.)*\")\s*,", body, re.S):
        literal = LITERAL.fullmatch(element)
        assert literal, element
        raw, plain = literal.groups()
        messages.append(raw if raw is not None else bytes(plain, "utf-8").decode("unicode_escape"))
    return messages


def unit_headers(message: str) -> list[str]:
    """The header of each unit of a program message whose units all start at the root."""
    headers = []
    for raw in message.split(";"):
        unit = raw.strip()
        if unit:
            headers.append(unit.lstrip(":").split()[0])
    return headers


def session_units(text: str) -> list[str]:
    """The header of every unit of the session, in its order."""
    return [header for message in session_messages(text) for header in unit_headers(message)]


def test_every_header_of_the_table_is_in_the_session() -> None:
    headers = table_headers()
    units = session_units(SESSION.read_text(encoding="utf-8"))
    assert len(headers) > 140, len(headers)
    missing = [h for h in headers if not any(pattern(h).fullmatch(u) for u in units)]
    assert not missing, "headers of kCommands the every-command session never gives:\n" + "\n".join(
        missing
    )


def test_every_unit_of_the_session_is_a_command_of_the_table() -> None:
    """A unit the table does not have would test nothing (a typo, a retired command)."""
    headers = table_headers()
    units = session_units(SESSION.read_text(encoding="utf-8"))
    stray = [u for u in units if not any(pattern(h).fullmatch(u) for h in headers)]
    assert not stray, f"units no command of the table matches: {stray}"


def test_the_matcher_reads_each_form() -> None:
    assert pattern("SENSe:CURRent:RANGe[:UPPer]?").fullmatch("sens:curr:rang?")
    assert pattern("SENSe:CURRent:RANGe[:UPPer]?").fullmatch("SENSE:CURRENT:RANGE:UPPER?")
    assert not pattern("SENSe:CURRent:RANGe[:UPPer]?").fullmatch("SENS:CURR:RANG")
    assert pattern("SENSe:CURRent:SHUNt:RANGe#").fullmatch("SENS:CURR:SHUN:RANG1")
    assert pattern("ROUTe:RELay#").fullmatch("ROUT:REL3")
    assert not pattern("ROUTe:LEAD").fullmatch("ROUT:LEAD:CYCL")
    assert pattern("SYSTem:COMMunicate:I2C:FREQuency?").fullmatch("SYST:COMM:I2C:FREQ?")
    assert pattern("*IDN?").fullmatch("*idn?")
    assert not pattern("*RST").fullmatch("*RST?")
    assert unit_headers(":SENS:PROF STATE;:INIT;*OPC?") == ["SENS:PROF", "INIT", "*OPC?"]
    planted = (
        'x;\nconstexpr SessionStep kEveryCommand[] = {\n    { "*IDN?", 1 },\n'
        '    { R"(A "b")", 2 },\n};\n'
    )
    assert session_messages(planted, ("kEveryCommand",)) == ["*IDN?", 'A "b"']


def test_a_header_left_out_of_the_session_is_found() -> None:
    """The guard fails on a planted table with a command the session lacks."""
    units = session_units(SESSION.read_text(encoding="utf-8"))
    assert not any(pattern("DIAGnostic:PLANted?").fullmatch(u) for u in units)
