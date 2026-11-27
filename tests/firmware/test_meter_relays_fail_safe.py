# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench meter's relay inputs come up released, and stay on the ruled pins.

The HW-316 module on the meter's 5 V rail is low-level triggered: an input
driven low energizes its relay, and an input driven high at 3.3 V or left
floating leaves it off. The bypass is a MikroE Relay 2 Click channel, active
high: its LED with no current leaves the PhotoMOS open (``firmware/src/relay.h``).
K1 is the DUT battery lead in series with the shunt, so an input that is low
for an instant opens the lead and drops the board under test; K2 is the bypass
across the shunt, so one that is high corrupts every reading.  Released is
therefore the only safe state, and three things keep it:

1. ``app_main()`` releases the relays before anything else but **the read of
   the pin map**, which says where they are (``pin_boot::load()``, FW-04): no
   other step -- an LED frame, the calibration's read, a sensor that hangs --
   runs while the pins are still the floating inputs reset left, which both
   parts read as released. The map's read reaches the flash and no pin.
2. The bank writes each pin's output latch to its released level, the map's,
   **before** ``gpio_config()`` makes the pad an output, so the pad's first
   driven level is released.
3. The default map's pins are the rig's as wired (rev D: Relay 1 on GPIO7
   active low, Relay 2 on GPIO15 active high), and no map may give a relay a
   pin the INA228 uses (``pinmap::validate()``, ``Problem::Shared``).

Beyond the boot state, the console's interlocks are held here too: every
relay refusal carries its own SCPI error number from ``docs/scpi.md``, the
relay state every capture header and null report records, and the rule that a
relay null the shunt plainly still carried current through is never stored.

Host-only: C++ files read as text, plus one host compile of ``relay.h`` so
that its ``static_assert``s -- make-before-break, the four paths, the
null-path checks -- run in this suite and not only in the meter's own build,
which CI does not run.  No device.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

METER_SRC = REPO_ROOT / "firmware" / "src"

MAIN_CPP = METER_SRC / "main.cpp"
RELAY_H = METER_SRC / "relay.h"
RELAY_CPP = METER_SRC / "relay.cpp"
CONSOLE_CPP = METER_SRC / "console.cpp"
COMMANDS_CPP = METER_SRC / "console_commands.cpp"
SCPI_DOC = REPO_ROOT / "docs" / "scpi.md"

#: The relay refusals console.cpp holds as named constants, and the SCPI error
#: each is queued under (``docs/scpi.md``, "Errors and status").
REFUSAL_CONSTANTS = {
    "kRelaysUnavailable": "kRelaysUnavailable",
    "kOrderBypassesCapture": "kOrderBypasses",
    "kOrderDisconnects": "kOrderDisconnects",
}

#: The channels: name -> (role, wired).
RULED_CHANNELS = {
    "k1": ("lead", True),
    "k2": ("bypass", True),
    "k3": ("spare", False),
    "k4": ("spare", False),
}

#: The default map, rev D as wired: role -> GPIO.
RIG_PINS = {"sda": 4, "scl": 5, "alert": 6, "relay1": 7, "relay2": 15}

#: The board's table, the one description the default map is generated from.
PINS_TOML = REPO_ROOT / "firmware" / "board" / "pins.toml"


def _function_body(text: str, signature: str) -> str:
    """Return the brace-balanced body that follows *signature* in *text*."""
    start = text.index(signature)
    open_brace = text.index("{", start)
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def _strip_comments(text: str) -> str:
    """Drop ``//`` line comments so a call named in prose is not counted."""
    return re.sub(r"//[^\n]*", "", text)


def _string_constant(text: str, name: str) -> str:
    """Return the value of ``constexpr const char* <name> = "..." "...";``.

    Adjacent literals are joined, as the compiler joins them.
    """
    match = re.search(rf"constexpr const char\* {name}\s*=\s*((?:\"[^\"]*\"\s*)+);", text)
    assert match is not None, f"console.cpp defines no string constant {name}"
    return "".join(re.findall(r'"([^"]*)"', match.group(1)))


#: The calls ``app_main()`` makes before it releases the relays, in order: the
#: heap monitor's arming (FW-07's phase mark, which FW-17 made the start of
#: every count), one atomic store that splits the heap counts at this line,
#: and the pin map's read, which says where the relays are and reaches no pin.
BEFORE_THE_RELAYS = ("heap::arm", "pin_boot::load")


def test_app_main_releases_the_relays_first() -> None:
    """Nothing but the map's read precedes the relay bank's ``begin()``."""
    body = _strip_comments(_function_body(MAIN_CPP.read_text(encoding="utf-8"), "void app_main("))
    calls = re.findall(r"\b([A-Za-z_][\w:]*(?:\.[A-Za-z_]\w*)?)\s*\(", body)
    calls = [call for call in calls if call not in {"if", "for", "while", "switch", "return"}]
    assert calls, "app_main() makes no call at all"
    assert tuple(calls[: len(BEFORE_THE_RELAYS)]) == BEFORE_THE_RELAYS, (
        f"app_main() begins with {calls[: len(BEFORE_THE_RELAYS)]}, not {BEFORE_THE_RELAYS}: "
        "the heap monitor's phase mark is its first line, and the map's read the one step "
        "before the relays are released"
    )
    calls = calls[len(BEFORE_THE_RELAYS) :]
    assert calls[0] == "gRelays.begin", (
        f"app_main()'s first call is {calls[0]}(), not gRelays.begin(). Until the "
        f"relays are released, a pin reset left low or floating is a lead the DUT "
        f"may lose; nothing else may run first."
    )
    assert body.index("gRelays.begin") < body.index("led::blankAll"), (
        "The relays must be released before the LED is blanked."
    )
    assert "gRelays.begin( gPins.map )" in body, "the relays are released where the map puts them"


def test_the_map_is_read_without_touching_a_pin() -> None:
    """``pin_boot::load()``, all that runs before the relays, names no pin call.

    Its one call that leaves the module is ``pinmap_store::load()``, which
    reads the flash log and reaches no GPIO either; ``liveOf()`` makes none.
    """
    source = _strip_comments((METER_SRC / "pin_boot.cpp").read_text(encoding="utf-8"))
    load = _function_body(source, "Live load()")
    assert re.findall(r"\b([A-Za-z_][\w:]*)\s*\(", load) == ["liveOf", "pinmap_store::load"]
    live = _function_body(source, "Live liveOf(")
    assert re.findall(r"\b([A-Za-z_][\w:]*)\s*\(", live) == ["if"]
    store = _strip_comments((METER_SRC / "pinmap_store.cpp").read_text(encoding="utf-8"))
    assert "gpio_" not in store and "relay::Bank" not in store


def test_the_latch_is_written_high_before_the_pad_becomes_an_output() -> None:
    """``gpio_set_level(..., releaseLevel(...))`` precedes ``gpio_config()``."""
    body = _strip_comments(
        _function_body(RELAY_CPP.read_text(encoding="utf-8"), "void Bank::connect(")
    )
    latch = body.find("gpio_set_level( wired.pin, releaseLevel( wired.drive ) )")
    config = body.find("gpio_config(")
    assert latch != -1, "Bank::connect() never writes the release level to the latch"
    assert config != -1, "Bank::connect() never configures the pads"
    assert latch < config, (
        "Bank::connect() configures the pad as an output before writing its latch "
        "to the released level, so the pad can drive the register's reset value "
        "-- low, which energizes an active-low relay -- for an instant."
    )
    assert "GPIO_MODE_INPUT_OUTPUT" in body, (
        "The pads are input and output, so RELAY? can read back the level each one is driven to."
    )


def test_each_input_is_driven_at_the_level_its_part_is_built_for() -> None:
    """The HW-316 is low-level triggered; the Relay 2 Click's LED is active high.

    The default map says so (Relay 1 is the HW-316's K1, Relay 2 the Click's
    channel), and the bank takes the level from the map: released must be high
    for the first and low for the second, or a boot energizes the bypass.
    """
    header = RELAY_H.read_text(encoding="utf-8")
    assert re.search(r"return \( drive == Drive::ActiveHigh \) \? 1U : 0U;", header), (
        "energizeLevel()"
    )
    assert re.search(r"return \( drive == Drive::ActiveHigh \) \? 0U : 1U;", header), (
        "releaseLevel()"
    )
    assert "KLEIDOS_METER_BYPASS_RELAY2_CLICK" not in header, "one image: no bypass switch"
    assert re.search(r'kBypassPart\s*=\s*"relay2click"', header)
    default = tomllib.loads(PINS_TOML.read_text(encoding="utf-8"))["defaults"]
    assert (default["relay1_active"], default["relay2_active"]) == ("low", "high")
    source = RELAY_CPP.read_text(encoding="utf-8")
    for role in ("relay1", "relay2"):
        assert f"map.{role}Drive" in source, f"the bank does not take {role}'s level from the map"


def test_the_channels_are_the_ruled_ones_and_the_default_map_is_the_rig() -> None:
    """K1 and K2 are wired, K3 and K4 not; the defaults are rev D's pins, all apart."""
    header = RELAY_H.read_text(encoding="utf-8")
    rows = re.findall(r'\{\s*"(k\d)",\s*Role::\w+,\s*"(\w+)",[^}]*?(true|false)\s*\}', header)
    table = {name: (role, wired == "true") for name, role, wired in rows}
    assert table == RULED_CHANNELS, (
        f"relay.h's kChannels is {table}; the ruled channels are {RULED_CHANNELS}."
    )
    default = tomllib.loads(PINS_TOML.read_text(encoding="utf-8"))["defaults"]
    pins = {role: default[role] for role in RIG_PINS}
    assert pins == RIG_PINS, f"the default map is {pins}; the rig as wired is {RIG_PINS}"
    for source in (MAIN_CPP, RELAY_H, RELAY_CPP, METER_SRC / "pin_boot.cpp"):
        named = re.findall(r"GPIO_NUM_\d+", _strip_comments(source.read_text(encoding="utf-8")))
        parked = 18 if source == RELAY_H else 0  # kParkedPins, the default map's
        assert len(named) == parked, f"{source.name} names GPIOs of its own: {named}"


#: Pins never parked (``relay.h`` ``kReservedPins``, ``docs/carrier-board.md``
#: SS 5): strapping, in-package flash and octal PSRAM, the native USB pair, UART0.
RESERVED_PINS = {0, 3, 19, 20, *range(26, 38), 43, 44, 45, 46}


def test_the_unused_pins_are_parked_pulled_down_after_the_relays() -> None:
    """Every pin no role holds is parked low (0.16.6), and none that is in use.

    A parked pin reads a defined low instead of floating (GPIO16 and GPIO17 are
    the unwired spares'). ``kParkedPins`` is the default map's set, held to
    ``pinmap::parkedSet()`` by the build; the bank parks the live map's (a pin
    a relay leaves while the instrument runs is parked toward that relay's
    released level instead, until the next boot: ``Bank::move()``). The parking
    runs after the relays are released, and it never touches a strapping,
    memory, USB or UART0 pin, the LED's or a pin a role holds.
    """
    header = RELAY_H.read_text(encoding="utf-8")
    table = re.search(r"kParkedPins = \{ \{(.*?)\} \};", header, re.S)
    assert table is not None, "relay.h has no kParkedPins table"
    listed = [int(pin) for pin in re.findall(r"GPIO_NUM_(\d+)", table.group(1))]
    parked = set(listed)
    assert len(listed) == len(parked), f"a pin is parked twice: {sorted(listed)}"
    assert {16, 17} <= parked, "the unwired spares' pins are not parked"
    reserved = re.search(r"kReservedPins = \{ \{(.*?)\} \};", header, re.S)
    assert reserved is not None
    assert {int(pin) for pin in re.findall(r"\d+", reserved.group(1))} == RESERVED_PINS
    assert not parked & RESERVED_PINS, f"a reserved pin is parked: {sorted(parked & RESERVED_PINS)}"

    led = (METER_SRC / "status_led.h").read_text(encoding="utf-8")
    led_table = re.search(r"kLedDataGpios = \{(.*?)\};", led)
    assert led_table is not None
    led_pins = {int(g) for g in re.findall(r"GPIO_NUM_(\d+)", led_table.group(1))}
    for name, pins in (("default map", set(RIG_PINS.values())), ("LED", led_pins)):
        assert pins and not parked & pins, f"a parked pin is the {name}'s: {sorted(parked & pins)}"

    source = _strip_comments(RELAY_CPP.read_text(encoding="utf-8"))
    park = _function_body(source, "void Bank::park(")
    assert "parkPin( pin, false )" in park and "gpio_" not in park
    one = _function_body(source, "void parkPin(")
    assert "GPIO_MODE_INPUT;" in one and "gpio_set_level" not in one
    assert "high ? GPIO_PULLDOWN_DISABLE : GPIO_PULLDOWN_ENABLE" in one
    assert "high ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE" in one
    begin = _function_body(source, "bool Bank::begin(")
    assert begin.index("connect( map )") < begin.index("park( pinmap")
    assert "park( pinmap::parkedSet( map ) )" in begin
    assert begin.index("record( kAllReleased )") < begin.index("park( pinmap"), (
        "the unused pins are parked before every relay is released"
    )


def _host_compiler() -> str | None:
    """Return a host C++ compiler, or None."""
    for name in ("c++", "clang++", "g++"):
        path = shutil.which(name)
        if path is not None:
            return path
    return None


def test_relay_h_static_asserts_hold_on_the_host(tmp_path: Path) -> None:
    """``relay.h`` compiles on the host, so every ``static_assert`` in it holds.

    The meter's build is not part of CI, and the orders this module exists
    for -- make-before-break both ways, no step into ``path=none`` without
    FORCE, the paths a capture may not run on, the null-path verdicts -- are
    ``static_assert``s.  A stub ``driver/gpio.h`` stands in for ESP-IDF's: the
    header only names ``gpio_num_t`` and its values.
    """
    compiler = _host_compiler()
    if compiler is None:
        pytest.skip("no host C++ compiler on PATH: relay.h's static_asserts were NOT checked")
    stub = tmp_path / "driver"
    stub.mkdir()
    (stub / "gpio.h").write_text(
        "#pragma once\n"
        "typedef enum { GPIO_NUM_NC = -1, "
        + ", ".join(f"GPIO_NUM_{pin} = {pin}" for pin in range(49))
        + " } gpio_num_t;\n",
        encoding="utf-8",
    )
    unit = tmp_path / "relay_check.cpp"
    unit.write_text('#include "relay.h"\nint main() { return 0; }\n', encoding="utf-8")
    result = subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-fsyntax-only",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{tmp_path}",
            f"-I{METER_SRC}",
            str(unit),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        "relay.h does not compile on the host, so one of its static_asserts fails or the "
        "header no longer stands alone:\n" + result.stderr
    )


def test_every_relay_refusal_is_queued_under_its_documented_number() -> None:
    """Each named refusal goes out under its own device-specific error."""
    console = _strip_comments(CONSOLE_CPP.read_text(encoding="utf-8"))
    doc = SCPI_DOC.read_text(encoding="utf-8")
    for constant, code in REFUSAL_CONSTANTS.items():
        text = _string_constant(console, constant)
        assert re.search(rf"failText\(\s*scpi::err::{code},\s*{constant}\s*\)", console), (
            f"{constant} ({text!r}) is not queued under scpi::err::{code}"
        )
    numbers = re.findall(
        r"constexpr std::int16_t (k\w+)\s*=\s*(\d+);", (METER_SRC / "scpi.h").read_text()
    )
    for name, number in numbers:
        assert re.search(rf"^\| {number} \|", doc, re.M), (
            f"scpi::err::{name} ({number}) is not in docs/scpi.md's error table"
        )


def test_every_record_names_the_relay_circuit_it_was_taken_in() -> None:
    """The identity advertises the relays; the header and both null reports carry the tokens."""
    console = _strip_comments(CONSOLE_CPP.read_text(encoding="utf-8"))
    commands = _strip_comments(COMMANDS_CPP.read_text(encoding="utf-8"))
    info = _function_body(console, "void Console::cmdInformation(")
    assert re.search(r'w\.field\(\s*"relays",', info), (
        "SYSTem:INFormation? no longer advertises relays="
    )
    header = _function_body(commands, "void Console::cmdFetchHeader(")
    assert re.search(
        r'w\.field\(\s*"relays",.*?relayTokens\( captureRelays_, captureEvidence_ \)', header, re.S
    ), "the capture header no longer records the relay state INITiate found"
    result = _function_body(commands, "void Console::cmdZeroResult(")
    assert "relayTokens( zeroRelays_, zeroEvidence_ )" in result, (
        "the running null report no longer carries the circuit the window was opened in"
    )
    assert "relayTokens( lastZeroRelays_, lastZeroEvidence_ )" in result, (
        "the null result no longer carries the circuit the window was opened in"
    )


def test_zero_has_no_override_for_a_bypassed_shunt() -> None:
    """``CALibration:ZERO`` refuses ``path=both`` outright: nothing makes that a null."""
    body = _strip_comments(
        _function_body(COMMANDS_CPP.read_text(encoding="utf-8"), "void Console::cmdZeroStart(")
    )
    assert "relay::Path::Both" in body and "kShuntBypassed" in body, (
        "CALibration:ZERO no longer refuses a shunt with K2 across a closed K1"
    )
    assert "FORC" not in body, (
        "CALibration:ZERO takes an override again; a forced null would be stored"
    )


def test_a_relay_null_beyond_the_limit_is_never_stored() -> None:
    """The not-stored refusal returns before the null reaches RAM or the flash."""
    body = _strip_comments(
        _function_body(COMMANDS_CPP.read_text(encoding="utf-8"), "void Console::finishZeroRun(")
    )
    refusal = body.find("kNullNotStored")
    assert refusal != -1, "finishZeroRun() no longer bounds a relay null"
    assert "kNullResidualLimitPv" in body and "relay::kLead" in body
    assert refusal < body.find("calibration_->setOffset("), (
        "a relay null beyond the residual limit reaches setOffset() before it is refused"
    )
    assert refusal < body.find("saveCalibration()"), (
        "a relay null beyond the residual limit reaches the flash before it is refused"
    )
