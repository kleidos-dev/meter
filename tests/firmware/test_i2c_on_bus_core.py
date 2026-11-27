# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every call into the I2C master is made where the driver routes it.

From 0.16.5 every transaction runs on the core that opened the bus
(``docs/architecture.md`` §2.2). ``Ina228Driver`` holds the rule: its
transaction entries make the call in place on the bus's core and hand it
there through ``esp_ipc_call_blocking()`` from the other one. From FW-14 the
bus is the own I2C master (``firmware/src/i2c_bus.cpp``, §2.4), which also
refuses every call from a core other than the one its ``begin()`` ran on, so
a call that bypasses the routing fails at run time with
``ESP_ERR_INVALID_STATE``; the host harness sees only the paths a test runs.
This guard reads the sources and holds every call into ``i2c_bus`` to the
places the routing owns, so a call written anywhere else fails here whatever
the tests run:

* ``i2c_bus::begin()`` in ``Ina228Driver::openBus()``, which records the core
  it ran on as the bus's;
* the transfers, the probe and the clock change in
  ``Ina228Driver::makeCall()``, the body of the routing and its IPC callback;
* in the three IRAM entries of the sampler's path (``readRegister()``,
  ``setPointer()``, ``readRetained()``), the in-place call only as the true
  branch of ``onBusCoreNow() ?``.

No other source of ``firmware/src`` calls ``i2c_bus``, and none calls
ESP-IDF's I2C master at all: FW-14 retired it (``scripts/
check_drivers_absent.py`` holds it out of the image). A planted call proves
the guard still sees one.

``i2c_bus.cpp`` is the only source that may reach I2C0's registers (a call of
``hal/i2c_ll.h`` or the ``I2C0`` block): a register write from anywhere else
would bypass its core check.

Host-only: the sources read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "firmware" / "src"

#: A call into the own I2C master: ``i2c_bus::<function>`` followed by its
#: argument list (its constants are not calls).
CALL = re.compile(r"\bi2c_bus::(\w+)\s*\(")

#: A call into ESP-IDF's I2C master, retired by FW-14: allowed nowhere.
RETIRED = re.compile(r"\b(i2c_master_\w+|i2c_new_master_bus|i2c_del_master_bus)\s*\(")

#: The module itself, which defines the functions.
BUS_MODULE = {"i2c_bus.cpp", "i2c_bus.h"}

#: The calls each driver function may make, by its name.
ALLOWED = {
    "openBus": {"begin"},
    "makeCall": {"probe", "write", "read", "writeRead", "setClock"},
    "readRegister": {"writeRead"},
    "setPointer": {"write"},
    "readRetained": {"read"},
}

#: The entries whose in-place call must be the true branch of the core check.
GUARDED = {"readRegister", "setPointer", "readRetained"}

#: A definition of a driver member function, at file scope.
DEFINITION = re.compile(r"\bIna228Driver::(\w+)\s*\(")


def _code(text: str) -> str:
    """The text with comments, string and character literals blanked."""
    out = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = re.sub(r"//[^\n]*", "", out)
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', out)


def _enclosing(code: str) -> list[str | None]:
    """For each character of @p code, the driver function whose body holds it.

    A namespace's braces do not count as a body: a definition inside one is at
    file scope.
    """
    owner: list[str | None] = [None] * len(code)
    braces: list[bool] = []  # one per open brace: whether it opened a namespace
    candidate: str | None = None
    current: str | None = None
    statement = 0  # where the text since the last ';', '{' or '}' starts
    for index, char in enumerate(code):
        depth = braces.count(False)
        if depth == 0:
            match = DEFINITION.match(code, index)
            if match:
                candidate = match.group(1)
            elif char == ";":
                candidate = None
        if char == "{":
            namespace = depth == 0 and re.search(r"\bnamespace\b[^{]*$", code[statement:index])
            braces.append(bool(namespace))
            if depth == 0 and not namespace:
                current = candidate
        elif char == "}" and braces:
            closed_namespace = braces.pop()
            if not closed_namespace and braces.count(False) == 0:
                current = None
                candidate = None
        owner[index] = current
        if char in ";{}":
            statement = index + 1
    return owner


def _statement_before(code: str, position: int) -> str:
    """The text of the statement that holds @p position, up to it."""
    start = max(code.rfind(";", 0, position), code.rfind("{", 0, position))
    return code[start + 1 : position]


def _findings(name: str, text: str) -> list[str]:
    code = _code(text)
    owner = _enclosing(code)
    found = [
        f"{name}:{code.count(chr(10), 0, m.start()) + 1}: {m.group(1)}() of the retired driver"
        for m in RETIRED.finditer(code)
    ]
    if name in BUS_MODULE:
        return found
    for match in CALL.finditer(code):
        line = code.count("\n", 0, match.start()) + 1
        call = match.group(1)
        where = f"{name}:{line}: i2c_bus::{call}()"
        if name != "ina228.cpp":
            found.append(f"{where} outside the driver")
            continue
        function = owner[match.start()]
        if call not in ALLOWED.get(function or "", set()):
            found.append(f"{where} in {function or 'file scope'}, which does not route it")
            continue
        if function in GUARDED and not re.search(
            r"onBusCoreNow\(\s*\)\s*\?\s*$", _statement_before(code, match.start())
        ):
            found.append(f"{where} in {function}, not the true branch of onBusCoreNow() ?")
    return found


def test_every_i2c_call_is_where_the_driver_routes_it() -> None:
    sources = sorted(SRC.glob("*.cpp")) + sorted(SRC.glob("*.h"))
    assert len(sources) > 20
    found = [hit for path in sources for hit in _findings(path.name, path.read_text("utf-8"))]
    assert not found, (
        "a call into the I2C master the bus-core routing does not own "
        "(docs/architecture.md §2.2):\n" + "\n".join(found)
    )


def test_the_driver_still_makes_each_allowed_call() -> None:
    # The allow-list describes the driver as it is: a call it no longer makes
    # is a stale entry, and a guard that names nothing would pass anything.
    code = _code((SRC / "ina228.cpp").read_text("utf-8"))
    owner = _enclosing(code)
    made: dict[str, set[str]] = {}
    for match in CALL.finditer(code):
        made.setdefault(owner[match.start()] or "", set()).add(match.group(1))
    assert made == ALLOWED
    # And i2c_bus defines each of them, so a renamed function is not an
    # allow-list that names nothing.
    header = _code((SRC / "i2c_bus.h").read_text("utf-8"))
    for call in set().union(*ALLOWED.values()):
        assert re.search(rf"\besp_err_t\s+{call}\s*\(", header), call


def test_the_guard_sees_each_planted_call() -> None:
    planted = {
        "a call in a new driver function": (
            "ina228.cpp",
            "esp_err_t Ina228Driver::peek( std::uint8_t reg ) noexcept {\n"
            "    return i2c_bus::write( address_, &reg, 1 );\n}\n",
        ),
        "an in-place call without the core check": (
            "ina228.cpp",
            "IRAM_ATTR esp_err_t Ina228Driver::setPointer( std::uint8_t reg ) noexcept {\n"
            "    return note( i2c_bus::write( address_, &reg, 1 ) );\n}\n",
        ),
        "the far core's branch made in place": (
            "ina228.cpp",
            "IRAM_ATTR esp_err_t Ina228Driver::setPointer( std::uint8_t reg ) noexcept {\n"
            "    return note( onBusCoreNow() ? transferOnBusCore( BusCall::Transmit, &reg, 1,"
            " nullptr, 0 ) : i2c_bus::write( address_, &reg, 1 ) );\n}\n",
        ),
        "a call outside the driver": (
            "console.cpp",
            "void Console::cmdPeek() noexcept {\n    (void)i2c_bus::probe( 0x40 );\n}\n",
        ),
        "the clock changed off the routing": (
            "ina228.cpp",
            "esp_err_t Ina228Driver::setBusSpeed( std::uint32_t hz ) noexcept {\n"
            "    return i2c_bus::setClock( hz );\n}\n",
        ),
        "the retired driver called again": (
            "ina228.cpp",
            "void Ina228Driver::makeCall( void* context ) noexcept {\n"
            "    (void)i2c_master_probe( bus, 0x40, 20 );\n}\n",
        ),
    }
    for case, (name, text) in planted.items():
        assert _findings(name, text), case


def test_comments_strings_and_constants_do_not_count() -> None:
    clean = (
        "// i2c_bus::write( address_, frame, 3 )\n"
        '/* i2c_master_probe( bus_ ) */ const char* s = "i2c_bus::read( x )";\n'
        "constexpr std::uint32_t kMin = i2c_bus::kMinClockHz;\n"
    )
    assert _findings("console.cpp", clean) == []


#: A reach into I2C0's registers: a call of its low-level layer, or the block.
REGISTERS = re.compile(r"\bi2c_ll_\w+|\bI2C0\b")

#: The one source that may reach them.
REGISTER_OWNER = "i2c_bus.cpp"


def _register_findings(name: str, text: str) -> list[str]:
    if name == REGISTER_OWNER:
        return []
    code = _code(text)
    return [
        f"{name}:{code.count(chr(10), 0, match.start()) + 1}: {match.group(0)}"
        for match in REGISTERS.finditer(code)
    ]


def test_only_the_own_master_reaches_i2c0s_registers() -> None:
    sources = sorted(SRC.glob("*.cpp")) + sorted(SRC.glob("*.h"))
    found = [
        hit for path in sources for hit in _register_findings(path.name, path.read_text("utf-8"))
    ]
    assert not found, (
        "I2C0's registers reached outside firmware/src/i2c_bus.cpp, past its core check "
        "(docs/architecture.md §2.4):\n" + "\n".join(found)
    )
    # The owner does reach them: a guard that names nothing would pass anything.
    assert REGISTERS.search(_code((SRC / REGISTER_OWNER).read_text("utf-8")))


def test_the_register_guard_sees_a_planted_reach() -> None:
    planted = {
        "an LL call": "void x() { i2c_ll_start_trans( &I2C0 ); }\n",
        "the register block": "auto* hw = &I2C0;\n",
    }
    for case, text in planted.items():
        assert _register_findings("ina228.cpp", text), case
    assert _register_findings("ina228.cpp", "// i2c_ll_start_trans( &I2C0 )\n") == []
