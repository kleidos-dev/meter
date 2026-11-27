// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pin_table.h
 * @ingroup pin_map
 * @brief The carrier board's pins, generated from `firmware/board/pins.toml` by
 *        `scripts/gen_pin_table.py`: never edited by hand.
 *
 * Every terminal of both screw rails (`docs/carrier-board.md` SS 2) and every
 * GPIO the module does not break out, each with its class and its reason
 * group; what each role needs of a pin; the default map, the rig as wired
 * (rev D); the bus's ranges; and kPinTableDigest, the CRC-32 of the table's
 * canonical text (the script says what it covers). `pinmap.h` holds the rules
 * that read it. The silk, the labels and the hover sentences are in the
 * panel's copy, `host/kleidos_meter/board_pins.json`, written from the same
 * file; `python3 scripts/gen_pin_table.py --check` fails when either differs.
 */
#ifndef KLEIDOS_METER_PIN_TABLE_H
#define KLEIDOS_METER_PIN_TABLE_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace kleidos::meter::pin_table {

/// @addtogroup pin_map
/// @{

/// Where a pin's terminal is.
enum class Rail : std::uint8_t {
    /// The left screw rail, the module's header P1.
    Left,
    /// The right screw rail, the module's header P2.
    Right,
    /// On neither rail: a GPIO the module does not break out.
    OffBoard,
};

/// What a role may do with a pin.
enum class PinClass : std::uint8_t {
    /// Any role whose limit it is below may take it.
    Free,
    /// A role may take it, at the cost its group states.
    Caution,
    /// No role may take it.
    Reserved,
};

/// Why a pin has its class (`pins.toml` [groups] gives each one's sentence).
enum class Group : std::uint8_t {
    /// Nothing on the chip or the board claims it.
    None,
    /// A supply terminal, not a GPIO.
    Power,
    /// Ground, not a GPIO.
    Ground,
    /// The chip's enable line, not a GPIO: pulling it low reboots the board.
    Reset,
    /// A strapping pin: its level at reset selects the boot mode, the JTAG source or the VDD_SPI voltage.
    Strapping,
    /// A line of the in-package SPI flash or of the octal PSRAM.
    Memory,
    /// The native USB pair, used by USB-Serial-JTAG by default.
    Usb,
    /// UART0, wired to the CH343P bridge: the console, which recovery needs.
    Uart0,
    /// A status-LED line: the firmware blanks it at boot and holds it low.
    Led,
    /// A JTAG pad: usable, but taking it costs an external JTAG probe.
    Jtag,
    /// In the VDD_SPI power domain: 3.3 V on this S3-N16R8 module, 1.8 V on an R8V part.
    VddSpi,
};

/// What a pin can be given to, in the order of PinMap's pins.
enum class Role : std::uint8_t {
    /// INA228 SDA
    Sda,
    /// INA228 SCL
    Scl,
    /// INA228 ALERT
    Alert,
    /// Relay 1
    Relay1,
    /// Relay 2
    Relay2,
};

/// The number of roles.
constexpr std::size_t kRoleCount = 5;

/// Each role's key, as the wire and the vectors spell it.
constexpr std::array<std::string_view, kRoleCount> kRoleWords = { {
    "sda",
    "scl",
    "alert",
    "relay1",
    "relay2",
} };

/// The first GPIO number each role cannot take, in Role's order (`pins.toml`
/// says why).
constexpr std::array<std::int32_t, kRoleCount> kRoleBelow = { {
    49,
    49,
    32,
    49,
    49,
} };

/// The highest GPIO number of the ESP32-S3, plus one.
constexpr std::int32_t kGpioCount = 49;

/// What a row's GPIO is when the terminal is not a GPIO, and a role's when it has
/// no pin.
constexpr std::int32_t kNoGpio = -1;

/// One terminal of a rail, or one GPIO the module does not break out.
struct Row {
    Rail         rail{};      ///< Where it is.
    std::uint8_t terminal{};  ///< 1 to 22 on a rail, 0 off the board.
    std::int32_t gpio{};      ///< Its GPIO, or kNoGpio.
    PinClass     pinClass{};  ///< What a role may do with it.
    Group        group{};     ///< Why.
};

/// The number of terminals, both rails.
constexpr std::size_t kTerminalCount = 44;

/// The number of rows: the terminals, then the GPIOs on neither rail.
constexpr std::size_t kRowCount = 53;

/// Every row: the left rail's 22 terminals, the right rail's, then the GPIOs
/// the module does not break out.
constexpr std::array<Row, kRowCount> kRows = { {
    { Rail::Left, 1, kNoGpio, PinClass::Reserved, Group::Power },
    { Rail::Left, 2, kNoGpio, PinClass::Reserved, Group::Power },
    { Rail::Left, 3, kNoGpio, PinClass::Reserved, Group::Reset },
    { Rail::Left, 4, 4, PinClass::Free, Group::None },
    { Rail::Left, 5, 5, PinClass::Free, Group::None },
    { Rail::Left, 6, 6, PinClass::Free, Group::None },
    { Rail::Left, 7, 7, PinClass::Free, Group::None },
    { Rail::Left, 8, 15, PinClass::Free, Group::None },
    { Rail::Left, 9, 16, PinClass::Free, Group::None },
    { Rail::Left, 10, 17, PinClass::Free, Group::None },
    { Rail::Left, 11, 18, PinClass::Free, Group::None },
    { Rail::Left, 12, 8, PinClass::Free, Group::None },
    { Rail::Left, 13, 3, PinClass::Reserved, Group::Strapping },
    { Rail::Left, 14, 46, PinClass::Reserved, Group::Strapping },
    { Rail::Left, 15, 9, PinClass::Free, Group::None },
    { Rail::Left, 16, 10, PinClass::Free, Group::None },
    { Rail::Left, 17, 11, PinClass::Free, Group::None },
    { Rail::Left, 18, 12, PinClass::Free, Group::None },
    { Rail::Left, 19, 13, PinClass::Free, Group::None },
    { Rail::Left, 20, 14, PinClass::Free, Group::None },
    { Rail::Left, 21, kNoGpio, PinClass::Reserved, Group::Power },
    { Rail::Left, 22, kNoGpio, PinClass::Reserved, Group::Ground },
    { Rail::Right, 1, kNoGpio, PinClass::Reserved, Group::Ground },
    { Rail::Right, 2, 43, PinClass::Reserved, Group::Uart0 },
    { Rail::Right, 3, 44, PinClass::Reserved, Group::Uart0 },
    { Rail::Right, 4, 1, PinClass::Free, Group::None },
    { Rail::Right, 5, 2, PinClass::Free, Group::None },
    { Rail::Right, 6, 42, PinClass::Caution, Group::Jtag },
    { Rail::Right, 7, 41, PinClass::Caution, Group::Jtag },
    { Rail::Right, 8, 40, PinClass::Caution, Group::Jtag },
    { Rail::Right, 9, 39, PinClass::Caution, Group::Jtag },
    { Rail::Right, 10, 38, PinClass::Reserved, Group::Led },
    { Rail::Right, 11, 37, PinClass::Reserved, Group::Memory },
    { Rail::Right, 12, 36, PinClass::Reserved, Group::Memory },
    { Rail::Right, 13, 35, PinClass::Reserved, Group::Memory },
    { Rail::Right, 14, 0, PinClass::Reserved, Group::Strapping },
    { Rail::Right, 15, 45, PinClass::Reserved, Group::Strapping },
    { Rail::Right, 16, 48, PinClass::Reserved, Group::Led },
    { Rail::Right, 17, 47, PinClass::Caution, Group::VddSpi },
    { Rail::Right, 18, 21, PinClass::Free, Group::None },
    { Rail::Right, 19, 20, PinClass::Reserved, Group::Usb },
    { Rail::Right, 20, 19, PinClass::Reserved, Group::Usb },
    { Rail::Right, 21, kNoGpio, PinClass::Reserved, Group::Ground },
    { Rail::Right, 22, kNoGpio, PinClass::Reserved, Group::Ground },
    { Rail::OffBoard, 0, 26, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 27, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 28, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 29, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 30, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 31, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 32, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 33, PinClass::Reserved, Group::Memory },
    { Rail::OffBoard, 0, 34, PinClass::Reserved, Group::Memory },
} };

static_assert( kRows.size() == kTerminalCount + 9U,
               "the rows are the terminals and the GPIOs on neither rail" );

/// The first address of the INA228's strap space.
constexpr std::uint8_t kAddressFirst = 0x40;
/// The last address of the INA228's strap space.
constexpr std::uint8_t kAddressLast = 0x4F;
/// The slowest bus clock, in Hz.
constexpr std::uint32_t kClockMinHz = 50000;
/// The fastest bus clock, in Hz.
constexpr std::uint32_t kClockMaxHz = 1000000;

/// The default map's pin for each role, in Role's order: the rig as wired
/// (rev D).
constexpr std::array<std::int32_t, kRoleCount> kDefaultPins = { {
    4,
    5,
    6,
    7,
    15,
} };
/// The default bus clock at boot, in Hz.
constexpr std::uint32_t kDefaultSclHz = 400000;
/// Whether the default map pulls SDA up.
constexpr bool kDefaultPullSdaUp = true;
/// Whether the default map pulls SCL up.
constexpr bool kDefaultPullSclUp = true;
/// Whether the default map pulls ALERT up.
constexpr bool kDefaultPullAlertUp = true;
/// Whether the default map's Relay 1 is energized by a high level.
constexpr bool kDefaultRelay1ActiveHigh = false;
/// Whether the default map's Relay 2 is energized by a high level.
constexpr bool kDefaultRelay2ActiveHigh = true;

/// The CRC-32 of the table's canonical text (`scripts/gen_pin_table.py`).
constexpr std::uint32_t kPinTableDigest = 0x90072FE9U;

/// @}

}  // namespace kleidos::meter::pin_table

#endif  // KLEIDOS_METER_PIN_TABLE_H
