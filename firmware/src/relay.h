// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file relay.h
 * @ingroup relay_bank
 * @brief The relays: the DUT battery lead on the HW-316 four-relay module, the
 *        shunt bypass on a MikroE Relay 2 Click, and two spares, driven from
 *        one table.
 *
 * **The lead (K1) and the spares (K3, K4) are HW-316 channels.** The module
 * is four SRD-05VDC-SL-C Form C relays (COM / NC / NO, 10 A at 30 VDC), each
 * behind a 1 kOhm input resistor, an IN LED, a PC817 opto and an S8050 driver
 * with a 1N4148 flyback diode, with the JD-VCC jumper fitted. VCC is the
 * carrier's 5 V terminal (left rail 21) and GND its `G` (22). Its inputs are
 * low-level triggered: with VCC at 5 V an input driven low sinks about 2 mA
 * through the IN LED and the opto LED in series and energizes its relay;
 * driven high at 3.3 V, or left floating, the relay is off, because the two
 * LEDs need about 3 V between them and 5 - 3.3 V cannot light them. On the
 * rig as wired (rev D, 2026-11-20) only IN1 is connected: K3 and K4 answer
 * their orders and move nothing (`docs/rig-wiring.md` SS 8.2).
 *
 * **The bypass (K2 on the wire) is a channel of the MikroE Relay 2 Click**
 * (`hardware/bom.md`, `docs/relay2-click-wiring.md`): RL2 on the bench since
 * 2026-11-20 (the schematic draws RL1, the same circuit), an IXYS LCA717
 * PhotoMOS relay (0.15 Ohm max, 3 ms max either way at 5 mA of LED current)
 * whose LED anode is that channel's control pin on GPIO15: **active high**.
 * The HW-316's own K2 bypassed the shunt until 2026-11-20, but its silver
 * contact does not close the ~10 mV dry circuit across the shunt and K1
 * (`docs/rig-wiring.md` SS 8); from 0.8.0 there is one image and it drives
 * the Click, as the `meter_relay2` image did from 0.3.0 on.
 *
 * So the safe state is the one a reset, a reflash or a dead meter leaves
 * behind -- every input at its released level, which is high for the HW-316
 * and low (or no current) for the Click -- and the wiring makes released also
 * the state that keeps the DUT powered through the shunt:
 *
 * | Channel | Map role | Default GPIO | Rail | Role | Part | Released |
 * |---|---|---|---|---|---|---|
 * | K1 | Relay 1 | 7, active low   | 7 | lead   | HW-316 IN1, NC between VIN- and the DUT's battery + | the lead conducts |
 * | K2 | Relay 2 | 15, active high | 8 | bypass | Relay 2 Click RL2, between VIN+ and the DUT's battery + | the bypass is open |
 * | K3 | -       | -               | - | spare  | HW-316 IN3, not wired | off |
 * | K4 | -       | -               | - | spare  | HW-316 IN4, not wired | off |
 *
 * The GPIOs and the active levels are the pin map's (pinmap.h), whose default
 * is the rig as wired; from 0.18.0 Bank::begin() takes them from the map the
 * boot loaded. The negatives are not switched.
 *
 * The DUT's battery + is reached through K1's NC contact (and the shunt) or
 * through the Click's PhotoMOS (around it), so the four combinations of the
 * two are four distinct circuits, named by Path below. The one this module
 * exists for is Path::Bypass: the DUT stays powered while the shunt carries
 * nothing, which is the offset null without hands.
 *
 * **Nothing senses a contact.** What the firmware commands is not evidence
 * that a contact moved: a module with no 5 V, a loose IN wire, a coil below
 * its 3.75 V pull-in or a Click with no supply all leave every relay where it
 * was while the console reports the order done. So `ROUTe:NULLpath ON` reads
 * the shunt before and after each step and refuses a null path it did not see
 * (Evidence below), and a null taken with K1 commanded open is not stored if
 * the shunt was plainly still carrying current (kNullResidualLimitPv).
 */
#ifndef KLEIDOS_METER_RELAY_H
#define KLEIDOS_METER_RELAY_H

#include "bits.h"
#include "text.h"

#include <driver/gpio.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace kleidos::meter::pinmap {
struct PinMap;  // pinmap.h, which includes this header for the Drive it names
}

namespace kleidos::meter::relay {

/// @addtogroup relay_bank
/// @{

/// Which level of a channel's input energizes it.
enum class Drive : std::uint8_t {
    ActiveLow,   ///< The HW-316: an input sinking current through its opto LED.
    ActiveHigh,  ///< The Relay 2 Click: a pin sourcing current into the LCA717's LED.
};

/// @brief The level that energizes a channel driven @p drive.
///
/// @param[in] drive  Which input level energizes the channel.
/// @return 1 for Drive::ActiveHigh, 0 for Drive::ActiveLow.
constexpr std::uint32_t energizeLevel( Drive drive ) noexcept {
    return ( drive == Drive::ActiveHigh ) ? 1U : 0U;
}

/// @brief The level that releases a channel driven @p drive, and the one its
/// pin is driven to at boot.
///
/// @param[in] drive  Which input level energizes the channel.
/// @return 0 for Drive::ActiveHigh, 1 for Drive::ActiveLow.
constexpr std::uint32_t releaseLevel( Drive drive ) noexcept {
    return ( drive == Drive::ActiveHigh ) ? 0U : 1U;
}

/// What a channel does on this rig.
enum class Role : std::uint8_t {
    Lead,    ///< K1, the map's Relay 1: in series with the shunt; released = connected.
    Bypass,  ///< K2, the map's Relay 2: across the shunt; released = open.
    Spare,   ///< Nothing wired.
};

/// One channel of the module: what it is, whichever GPIO drives it. The GPIO
/// and the level that energizes it are the pin map's (Bank::begin(), Wiring).
struct ChannelSpec {
    std::string_view name{};             ///< `k1` ... `k4`, the token key on the wire.
    Role             role{};             ///< What the channel does on this rig.
    std::string_view roleName{};         ///< `lead`, `bypass`, `spare`.
    std::string_view releasedWord{};     ///< What the circuit is with the coil off.
    std::string_view energizedWord{};    ///< What it is with the coil on.
    std::uint32_t    operateSettleMs{};  ///< Waited after it is energized.
    std::uint32_t    releaseSettleMs{};  ///< Waited after it is released.
    /// Whether the channel reaches a relay on this rig (0.13.0). An unwired
    /// one has no GPIO in the map and none is driven for it: its pad is not
    /// checked, its order is refused, and its token reads `unwired`.
    bool wired{};
};

/// Where one channel's input is and which level energizes it, as the pin map
/// Bank::begin() was given says.
struct Wiring {
    gpio_num_t pin   = GPIO_NUM_NC;       ///< The GPIO driving the module's input.
    Drive      drive = Drive::ActiveLow;  ///< Which level of it energizes the channel.
};

/// Number of relay channels on the module: K1 ... K4. Named apart from
/// kleidos::meter::kChannelCount, the measured channels, so the bare name
/// means one thing in every scope that sees both.
constexpr std::size_t kRelayCount = 4;

/// Index of the lead relay (K1) in kChannels.
constexpr std::size_t kLead = 0;

/// Index of the bypass relay (K2) in kChannels.
constexpr std::size_t kBypass = 1;

// --- Timings ---------------------------------------------------------------
// The relay's own figures bound how long a contact takes to move; nothing on
// the module senses whether it did, so every order waits them out before it
// answers. The SRD-05VDC-SL-C's operate time is 10 ms maximum (Songle's sheet,
// and LCSC's and JLCPCB's listings of part C35449). Its release time is quoted
// as 5 ms maximum in the Songle sheet and 10 ms in those listings; the larger
// is used.
//
// Both figures are for a bare coil. The module puts a 1N4148 across each one,
// and a diode alone lets the coil's current decay slowly through it, which
// holds the armature in and lengthens the release (TE Connectivity's note on
// coil suppression); no source gives the figure for this part. So the release
// carries an allowance of three times its bound. It is the wait
// make-before-break rests on when it opens the bypass: `ROUTe:NULLpath OFF` must not
// open K2 before K1's NC contact has reclosed.
//
// No source gives a bounce time either, so a margin equal to the operate bound
// is added to each -- a contact still bouncing when an order answers would put
// a few milliseconds of an interrupted circuit into a null.
//
// Measured on the rig with a 100 Ohm load on 2026-11-21
// (`docs/relay-timing.md`): K1 operates in 1.96 to 2.52 ms and releases, diode
// and bounce included, in 11.20 ms, well inside both waits, which stay.

/// The SRD-05VDC-SL-C's operate time, maximum, in ms.
constexpr std::uint32_t kOperateMaxMs = 10;
/// Its release time, maximum, in ms: the larger of the two quoted figures.
constexpr std::uint32_t kReleaseMaxMs = 10;
/// Added to the release for the flyback diode: three times its bound, in ms
/// (the release measured 11.20 ms in all, `docs/relay-timing.md`).
constexpr std::uint32_t kFlybackAllowanceMs = 3U * kReleaseMaxMs;
/// Added to each wait for contact bounce: the operate bound, in ms.
constexpr std::uint32_t kBounceMarginMs = 10;

/// Waited after a coil is energized, before the next step or the reply.
constexpr std::uint32_t kOperateSettleMs = kOperateMaxMs + kBounceMarginMs;

/// Waited after a coil is released.
constexpr std::uint32_t kReleaseSettleMs = kReleaseMaxMs + kFlybackAllowanceMs + kBounceMarginMs;

static_assert( kOperateSettleMs == 20U && kReleaseSettleMs == 50U,
               "The settle times are sized from the datasheet bounds, the flyback diode's "
               "allowance and the bounce margin" );
static_assert( kOperateSettleMs > kOperateMaxMs && kReleaseSettleMs > kReleaseMaxMs,
               "Each wait outlasts the bound it covers, or make-before-break is only hoped for" );

// The Relay 2 Click's LCA717 has no contact to bounce and no coil to decay:
// IXYS gives 3 ms maximum for both its turn-on and its turn-off at 5 mA of
// LED current. The Click feeds the LED from a 3.3 V pin through its own
// resistor, which no published figure states, and a smaller LED current
// lengthens the turn-on (IXYS's curve of turn-on time against LED current),
// so each wait is the bound plus a margin as large again plus a millisecond
// for the tick. The TRACE procedure of `docs/rig-wiring.md` SS 8.6 measured
// 0.56 to 1.12 ms on and 0.56 to 0.84 ms off on 2026-11-21, with a 100 Ohm
// load (`docs/relay-timing.md`): the waits stay.
/// The LCA717's turn-on and turn-off time, maximum, at 5 mA of LED current,
/// in ms.
constexpr std::uint32_t kSsrSwitchMaxMs = 3;
/// Waited after the SSR is turned on: the bound, a margin as large again and
/// a millisecond for the tick, in ms.
constexpr std::uint32_t kSsrOperateSettleMs = 2U * kSsrSwitchMaxMs + 4U;
/// Waited after the SSR is turned off, sized as kSsrOperateSettleMs, in ms.
constexpr std::uint32_t kSsrReleaseSettleMs = 2U * kSsrSwitchMaxMs + 4U;

static_assert( kSsrOperateSettleMs == 10U && kSsrReleaseSettleMs == 10U,
               "The SSR waits are sized from IXYS's 3 ms bound, doubled, plus a margin" );

/// @brief The part that closes the bypass, as `bypass_part=` reports it.
///
/// Always `relay2click` from 0.8.0, which has one image. The key stays on the
/// wire because it is a contract (`docs/scpi.md`); before 0.8.0 the `meter`
/// image reported `hw316` and the `meter_relay2` image `relay2click`.
constexpr const char* kBypassPart = "relay2click";

/// @brief The channels, in one table.
///
/// The GPIOs and the levels are not here: they are the pin map's (pinmap.h),
/// whose default is the rig as wired (rev D), K1 on GPIO7 active low and K2
/// on GPIO15 active high, pinned by the static_assert beside
/// pinmap::kDefaultMap.
///
/// The state words describe the **circuit**, not the coil: K1 is a series
/// contact, so its coil on means the lead is open; K2 is a parallel contact,
/// so its coil on means the bypass is closed.
///
/// K2 is the Relay 2 Click's channel (RL2 on the bench since 2026-11-20),
/// with the SSR's settle times (kSsrOperateSettleMs, kSsrReleaseSettleMs);
/// K1, K3 and K4 are HW-316 channels. The settle times are the parts' and no
/// map changes them.
constexpr std::array<ChannelSpec, kRelayCount> kChannels = { {
    { "k1", Role::Lead, "lead", "closed", "open", kOperateSettleMs, kReleaseSettleMs, true },
    { "k2", Role::Bypass, "bypass", "open", "closed", kSsrOperateSettleMs, kSsrReleaseSettleMs,
      true },
    // Only the HW-316's IN1 is wired on the rig (rev D): K3 and K4 reach
    // nothing and no map gives either a GPIO, so none is ever driven for them.
    { "k3", Role::Spare, "spare", "off", "on", kOperateSettleMs, kReleaseSettleMs, false },
    { "k4", Role::Spare, "spare", "off", "on", kOperateSettleMs, kReleaseSettleMs, false },
} };

static_assert( kChannels[kLead].role == Role::Lead, "kLead must index K1" );
static_assert( kChannels[kBypass].role == Role::Bypass, "kBypass must index K2" );
static_assert( kChannels[kLead].wired && kChannels[kBypass].wired,
               "The map's two relays are wired channels" );

// --- The parked pins -------------------------------------------------------------

/// @brief The broken-out carrier GPIOs the default map drives nothing on,
/// parked at boot as inputs with the internal pull-down (0.16.6).
///
/// The owner's ruling of 2026-11-21: the hardware the meter does not use is
/// parked. A pad left as reset made it floats, or keeps whatever weak pull its
/// reset state has; parked, it reads a defined low, and a wire that lands on it
/// later meets a pull-down, never a driven level. Bank::begin() parks
/// pinmap::parkedSet() of the map it is given, after it has released every
/// relay; this is that set for the default map, held to it pin for pin by a
/// static_assert of pinmap.h, so that a change of the board's table cannot
/// change what the rig parks without a change here.
///
/// `docs/carrier-board.md` SS 2 lists every terminal; these are the ones it
/// calls free, plus:
///
/// - **GPIO16, GPIO17** (left 9, 10): the unwired spares K3 and K4, which reach
///   nothing (the temporary load NW-00 wired to GPIO16 was removed on
///   2026-11-21). **Never wire a parked pin to an HW-316 input**, which is
///   active low: give it a role of the map first, so that begin() drives it
///   high instead.
/// - **GPIO39 to GPIO42** (right 6 to 9): JTAG's MTCK, MTDO, MTDI and MTMS.
///   The pads carry JTAG only when an eFuse selects them over the
///   USB-Serial-JTAG, and no eFuse is touched; this image has no JTAG at all
///   (`CONFIG_USJ_ENABLE_USB_SERIAL_JTAG` is off, `sdkconfig.defaults`).
/// - **GPIO47** (right 17), in the VDD_SPI domain: GPIO48, its pair as the
///   flash's differential clock, is a plain output here (the status LED's
///   line, `status_led.h`), so neither is a memory line on this module, and a
///   pull-down is safe at either domain voltage (`carrier-board.md` SS 9 q4).
///
/// Left alone, each for its reason: the strapping pins GPIO0, GPIO3, GPIO45
/// and GPIO46 (their level at reset selects the boot; GPIO0 is the BOOT
/// button); the in-package flash and octal PSRAM (GPIO26 to GPIO37); UART0,
/// GPIO43 and GPIO44 (the console); GPIO19 and GPIO20, the native USB pair,
/// whose pad ESP-IDF disables at startup with this image's options and whose
/// gpio_config() would write the USB-Serial-JTAG's registers with its clock
/// gated; and every pin in use: the default map's GPIO4/5/6 (the INA228's) and
/// GPIO7 and GPIO15 (the relays'), and the LED lines GPIO38 and GPIO48
/// (`status_led.h`, which holds them low).
constexpr std::array<gpio_num_t, 18> kParkedPins = { {
    GPIO_NUM_1,
    GPIO_NUM_2,
    GPIO_NUM_8,
    GPIO_NUM_9,
    GPIO_NUM_10,
    GPIO_NUM_11,
    GPIO_NUM_12,
    GPIO_NUM_13,
    GPIO_NUM_14,
    GPIO_NUM_16,
    GPIO_NUM_17,
    GPIO_NUM_18,
    GPIO_NUM_21,
    GPIO_NUM_39,
    GPIO_NUM_40,
    GPIO_NUM_41,
    GPIO_NUM_42,
    GPIO_NUM_47,
} };

/// @brief The pins the chip or this board reserves, never parked: the strapping
/// pins (GPIO0, 3, 45, 46), the in-package flash and the octal PSRAM (GPIO26
/// to GPIO37), the native USB pair (GPIO19, 20) and UART0 (GPIO43, 44)
/// (`docs/carrier-board.md` SS 5). Numbers, not gpio_num_t: most of them have
/// no name in the host tests' stand-in for ESP-IDF's header.
constexpr std::array<std::int32_t, 20> kReservedPins = { {
    0, 3, 19, 20, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 43, 44, 45, 46,
} };

namespace detail {

/// @brief Whether @p table holds @p pin.
///
/// @tparam Size   The table's length.
/// @param[in] table  The pins to look in, as numbers.
/// @param[in] pin    The pin to look for.
/// @return true when one entry of @p table is @p pin.
template <std::size_t Size>
constexpr bool holds( const std::array<std::int32_t, Size>& table, std::int32_t pin ) noexcept {
    for ( const std::int32_t entry : table ) {
        if ( entry == pin ) {
            return true;
        }
    }
    return false;
}

/// @brief Whether a parked-pin table keeps its two rules: no reserved pin, no
/// pin twice. (That it parks no pin in use, and leaves none floating, is
/// pinmap.h's static_assert: it is pinmap::parkedSet() of the default map.)
///
/// The tables are parameters so that the host tests show each refusal on a
/// planted table; the build checks kParkedPins with it (the static_assert
/// below).
///
/// @tparam Parked    The parked table's length.
/// @tparam Reserved  The reserved table's length.
/// @param[in] parked    The pins parked at boot.
/// @param[in] reserved  The pins the chip or the board reserves, as numbers.
/// @return true when the table is sound.
template <std::size_t Parked, std::size_t Reserved>
constexpr bool parkedPinsSound( const std::array<gpio_num_t, Parked>&     parked,
                                const std::array<std::int32_t, Reserved>& reserved ) noexcept {
    for ( std::size_t index = 0; index < parked.size(); ++index ) {
        if ( holds( reserved, static_cast<std::int32_t>( parked[index] ) ) ) {
            return false;
        }
        for ( std::size_t other = index + 1U; other < parked.size(); ++other ) {
            if ( parked[other] == parked[index] ) {
                return false;
            }
        }
    }
    return true;
}

}  // namespace detail

static_assert( detail::parkedPinsSound( kParkedPins, kReservedPins ),
               "kParkedPins parks a reserved pin or a pin twice" );

/// @brief The longest wait any channel takes after it is energized.
///
/// With maxReleaseSettleMs(), the figures `ROUTe:STATe?` reports as
/// `operate_settle_ms` and `release_settle_ms`.
///
/// @return the largest ChannelSpec::operateSettleMs in kChannels, in ms.
constexpr std::uint32_t maxOperateSettleMs() noexcept {
    std::uint32_t longest = 0;
    for ( const ChannelSpec& spec : kChannels ) {
        longest = ( spec.operateSettleMs > longest ) ? spec.operateSettleMs : longest;
    }
    return longest;
}

/// @brief The longest wait any channel takes after it is released.
///
/// @return the largest ChannelSpec::releaseSettleMs in kChannels, in ms.
constexpr std::uint32_t maxReleaseSettleMs() noexcept {
    std::uint32_t longest = 0;
    for ( const ChannelSpec& spec : kChannels ) {
        longest = ( spec.releaseSettleMs > longest ) ? spec.releaseSettleMs : longest;
    }
    return longest;
}

static_assert( maxOperateSettleMs() == kOperateSettleMs && maxReleaseSettleMs() == kReleaseSettleMs,
               "K1 is an HW-316 relay, so its waits are the longest" );

/// How long the relays may stand off the measuring circuit before the
/// instrument puts them back by itself, in seconds: the hold.
///
/// Every path but Path::Shunt is held: the null path, an open lead (the DUT
/// with no battery) and, from 0.16.0, a bypass across a closed lead. Holding
/// `path=bypass` has one exposure the orders cannot cover: a release nobody
/// commanded -- a meter reset, a panic, a port opened with the wrong DTR/RTS
/// order -- drops both coils at once, K2's NO contact can open before K1's NC
/// recloses, and the DUT loses its battery for up to a release time; an open
/// lead is that loss itself. A host that dies with the relays off the shunt
/// would leave them there indefinitely, so the hold is bounded and is the
/// instrument's teardown: armed by the order that left the path (an order that
/// changes nothing arms it again), re-armed whenever a capture or a
/// `CALibration:ZERO` window closes, and suspended only while a null window
/// samples, which is itself bounded. From 0.16.0 an open capture does not
/// suspend it: the release runs under it and is marked in it. Long enough for
/// a person at the console to type the next command.
constexpr std::uint32_t kNullPathHoldS = 120;

// --- The circuit -------------------------------------------------------------

/// A set of energized coils: bit n is channel n of kChannels.
using Mask = std::uint32_t;

/// @brief The Mask bit of one channel.
///
/// @param[in] channel  Index of the channel in kChannels.
/// @return a Mask with only bit @p channel set.
constexpr Mask bitOf( std::size_t channel ) noexcept {
    return 1U << channel;
}

/// Every coil released: the boot state, and the measuring circuit.
constexpr Mask kAllReleased = 0;

/// How the DUT's battery + is fed, which is a function of K1 and K2 alone.
enum class Path : std::uint8_t {
    Shunt,   ///< K1 closed, K2 open: through the shunt only. The measuring circuit.
    Bypass,  ///< K1 open, K2 closed: around the shunt, which carries nothing. The null.
    Both,    ///< K1 closed, K2 closed: the shunt is bypassed and reads a fraction of the current.
    None,    ///< K1 open, K2 open: the battery lead is disconnected.
};

/// @brief Whether what the shunt reads is what the DUT draws from the battery.
///
/// All of it on Path::Shunt, and on Path::None the zero a disconnected DUT
/// draws. Not on Path::Both, where the shunt reads a fraction, nor on
/// Path::Bypass, where it reads nothing of a DUT that is still running -- a
/// capture there would report a powered board as drawing zero.
///
/// @param[in] path  How the DUT's battery + is fed.
/// @retval true   @p path is Path::Shunt or Path::None.
/// @retval false  @p path is Path::Both or Path::Bypass.
constexpr bool shuntCarriesAll( Path path ) noexcept {
    return path == Path::Shunt || path == Path::None;
}

/// @brief The path a set of energized coils makes, from K1 and K2 alone.
///
/// @param[in] energized  The energized coils; the spares are ignored.
/// @return the Path K1's and K2's states make.
constexpr Path pathOf( Mask energized ) noexcept {
    const bool throughShunt  = !bits::hasBit( energized, bitOf( kLead ) );
    const bool throughBypass = bits::hasBit( energized, bitOf( kBypass ) );
    if ( throughShunt && throughBypass ) {
        return Path::Both;
    }
    if ( throughShunt ) {
        return Path::Shunt;
    }
    return throughBypass ? Path::Bypass : Path::None;
}

/// @brief The path's spelling on the wire: `shunt`, `bypass`, `both`, `none`.
///
/// @param[in] path  The path to spell.
/// @return a static string; `none` for any value outside the four.
const char* pathName( Path path ) noexcept;

static_assert( pathOf( kAllReleased ) == Path::Shunt, "Released must measure" );
static_assert( pathOf( bitOf( kLead ) ) == Path::None, "K1 alone disconnects the DUT" );
static_assert( pathOf( bitOf( kBypass ) ) == Path::Both, "K2 alone bypasses the shunt" );
static_assert( pathOf( bitOf( kLead ) | bitOf( kBypass ) ) == Path::Bypass,
               "Both coils is the null path" );
static_assert( pathOf( bitOf( 2 ) | bitOf( 3 ) ) == Path::Shunt, "The spares are not in the path" );
static_assert( shuntCarriesAll( Path::Shunt ) && shuntCarriesAll( Path::None )
                   && !shuntCarriesAll( Path::Both ) && !shuntCarriesAll( Path::Bypass ),
               "Only the shunt alone, or no battery at all, is a reading of the DUT" );

// --- Orders, planned before they are driven ----------------------------------

/// One coil change and the wait that follows it.
struct Step {
    std::uint8_t  channel  = 0;      ///< Index of the channel in kChannels.
    bool          energize = false;  ///< True to energize the coil, false to release it.
    std::uint32_t settleMs = 0;      ///< Waited after the change, in ms.
};

/// The steps one console order takes, in order. Two at most: `ROUTe:NULLpath`.
struct Plan {
    static constexpr std::size_t kMaxSteps = 2;  ///< The most steps one order takes.

    std::array<Step, kMaxSteps> steps = {};  ///< The steps, in order; the first #count are used.
    std::size_t                 count = 0;   ///< Number of steps in use.

    /// @brief Appends one coil change, with the channel's settle time for it.
    ///
    /// Ignored once the plan holds kMaxSteps steps.
    ///
    /// @param[in] channel   Index of the channel in kChannels.
    /// @param[in] energize  True to energize the coil, false to release it.
    constexpr void add( std::size_t channel, bool energize ) noexcept {
        if ( count < kMaxSteps ) {
            const ChannelSpec& spec = kChannels[channel];
            steps[count++] = Step{ static_cast<std::uint8_t>( channel ), energize,
                                   energize ? spec.operateSettleMs : spec.releaseSettleMs };
        }
    }
};

/// @brief Whether one channel's coil is energized in a state.
///
/// @param[in] state    The energized coils.
/// @param[in] channel  Index of the channel in kChannels.
/// @retval true   bit @p channel of @p state is set.
/// @retval false  it is clear.
constexpr bool isEnergized( Mask state, std::size_t channel ) noexcept {
    return bits::hasBit( state, bitOf( channel ) );
}

/// @brief The coil state one step leaves.
///
/// @param[in] state  The energized coils before the step.
/// @param[in] step   The coil change.
/// @return @p state with the step's channel energized or released.
constexpr Mask after( Mask state, const Step& step ) noexcept {
    return step.energize ? ( state | bitOf( step.channel ) ) : ( state & ~bitOf( step.channel ) );
}

/// @brief The coil state a whole plan leaves.
///
/// @param[in] state  The energized coils before the plan.
/// @param[in] plan   The steps, applied in order.
/// @return the energized coils after the last step.
constexpr Mask finalState( Mask state, const Plan& plan ) noexcept {
    for ( std::size_t i = 0; i < plan.count; ++i ) {
        state = after( state, plan.steps[i] );
    }
    return state;
}

/// @brief One coil to one state; empty when it is already there.
///
/// @param[in] state     The energized coils now.
/// @param[in] channel   Index of the channel in kChannels.
/// @param[in] energize  True to energize the coil, false to release it.
/// @return a plan of one step, or of none.
constexpr Plan planSet( Mask state, std::size_t channel, bool energize ) noexcept {
    Plan plan;
    if ( isEnergized( state, channel ) != energize ) {
        plan.add( channel, energize );
    }
    return plan;
}

/// @brief The null path in make-before-break order, from whatever state the
/// coils are in, so that no step ever disconnects a DUT that was connected.
///
/// ON closes the bypass **first** and opens the lead second: the DUT is fed by
/// both contacts for one settle time, then by K2 alone, and the shunt carries
/// nothing. OFF recloses the lead first and opens the bypass second.
///
/// @param[in] state  The energized coils now.
/// @param[in] on     True for ON (enter the null path), false for OFF.
/// @return the steps still needed, none, one or two; a coil already where the
///         order wants it is skipped.
constexpr Plan planNullPath( Mask state, bool on ) noexcept {
    Plan plan;
    if ( on ) {
        if ( !isEnergized( state, kBypass ) ) {
            plan.add( kBypass, true );
        }
        if ( !isEnergized( state, kLead ) ) {
            plan.add( kLead, true );
        }
    } else {
        if ( isEnergized( state, kLead ) ) {
            plan.add( kLead, false );
        }
        if ( isEnergized( state, kBypass ) ) {
            plan.add( kBypass, false );
        }
    }
    return plan;
}

/// @brief Whether a step of @p plan disconnects a DUT that was connected
/// before it.
///
/// A transition **into** Path::None, not a state that is None: a DUT already
/// disconnected (a cold power cycle in progress) is not disconnected again by
/// switching a spare, or by an order that changes nothing.
///
/// @param[in] state  The energized coils before the plan.
/// @param[in] plan   The steps, applied in order.
/// @retval true   some step moves the path from another Path into Path::None.
/// @retval false  no step does.
constexpr bool unpowersDut( Mask state, const Plan& plan ) noexcept {
    for ( std::size_t i = 0; i < plan.count; ++i ) {
        const Mask next = after( state, plan.steps[i] );
        if ( pathOf( next ) == Path::None && pathOf( state ) != Path::None ) {
            return true;
        }
        state = next;
    }
    return false;
}

/// @brief Whether the shunt stops carrying the DUT's whole current at the start
/// of @p plan or after any of its steps (shuntCarriesAll()).
///
/// When it does, a capture that spans the order has readings that are a
/// fraction of the current, or none of it.
///
/// Make-before-break makes every `ROUTe:NULLpath` order one of these: ON passes
/// through Path::Both on its way to Path::Bypass, and OFF starts there.
///
/// @param[in] state  The energized coils before the plan.
/// @param[in] plan   The steps, applied in order.
/// @retval true   the path at the start or after some step is Path::Both or
///                Path::Bypass.
/// @retval false  the shunt carries all of it throughout.
constexpr bool bypassesShunt( Mask state, const Plan& plan ) noexcept {
    if ( !shuntCarriesAll( pathOf( state ) ) ) {
        return true;
    }
    for ( std::size_t i = 0; i < plan.count; ++i ) {
        state = after( state, plan.steps[i] );
        if ( !shuntCarriesAll( pathOf( state ) ) ) {
            return true;
        }
    }
    return false;
}

/// @brief Fixtures for the compile-time checks in this header.
namespace detail {

/// K1 and K2 energized: the null path, Path::Bypass.
constexpr Mask kNullState = bitOf( kLead ) | bitOf( kBypass );

/// `ROUTe:NULLpath ON` from every coil released.
constexpr Plan kOnFromRest = planNullPath( kAllReleased, true );
/// `ROUTe:NULLpath OFF` from the null path.
constexpr Plan kOffFromNull = planNullPath( kNullState, false );
/// K1 opened from rest with the bypass open.
constexpr Plan kLeadOpenBare = planSet( kAllReleased, kLead, true );

}  // namespace detail

// The orders this module exists for, checked where they are defined.
static_assert( detail::kOnFromRest.count == 2U && detail::kOnFromRest.steps[0].channel == kBypass
                   && detail::kOnFromRest.steps[1].channel == kLead,
               "NULLPATH ON closes the bypass before it opens the lead" );
static_assert( !unpowersDut( kAllReleased, detail::kOnFromRest ),
               "NULLPATH ON never disconnects the DUT" );
static_assert( pathOf( finalState( kAllReleased, detail::kOnFromRest ) ) == Path::Bypass,
               "NULLPATH ON ends on the null path" );
static_assert( detail::kOffFromNull.count == 2U && detail::kOffFromNull.steps[0].channel == kLead
                   && detail::kOffFromNull.steps[1].channel == kBypass,
               "NULLPATH OFF recloses the lead before it opens the bypass" );
static_assert( !unpowersDut( detail::kNullState, detail::kOffFromNull ),
               "NULLPATH OFF never disconnects the DUT" );
static_assert( finalState( detail::kNullState, detail::kOffFromNull ) == kAllReleased,
               "NULLPATH OFF ends with every coil released" );
static_assert( unpowersDut( kAllReleased, detail::kLeadOpenBare ),
               "Opening the lead with the bypass open disconnects the DUT" );
static_assert( !unpowersDut( bitOf( kBypass ), planSet( bitOf( kBypass ), kLead, true ) ),
               "Opening the lead with the bypass closed keeps the DUT fed" );
static_assert( unpowersDut( detail::kNullState, planSet( detail::kNullState, kBypass, false ) ),
               "Opening the bypass with the lead open disconnects the DUT" );
static_assert( !unpowersDut( bitOf( kLead ), planSet( bitOf( kLead ), 2, true ) ),
               "A spare does not disconnect a DUT that is already disconnected" );
static_assert( planNullPath( kAllReleased, false ).count == 0U, "OFF from rest changes nothing" );
static_assert( bypassesShunt( kAllReleased, detail::kOnFromRest )
                   && bypassesShunt( detail::kNullState, detail::kOffFromNull ),
               "Both NULLPATH orders take the shunt off the DUT's current on the way" );
static_assert( bypassesShunt( kAllReleased, planSet( kAllReleased, kBypass, true ) ),
               "BYPASS ON from rest puts K2 across the shunt" );
static_assert( !bypassesShunt( kAllReleased, detail::kLeadOpenBare ),
               "LEAD OPEN from rest is a disconnection, not a bypass: the shunt reads the zero" );
static_assert( !bypassesShunt( kAllReleased, planSet( kAllReleased, 2, true ) ),
               "A spare never bypasses the shunt" );

// --- Checking a null path ----------------------------------------------------
// `ROUTe:NULLpath ON` reads the shunt three times when it can -- before it moves
// anything, after K2 closes and after K1 opens -- and judges each step by what
// the shunt did. Each reading's **share** is the reading less the stored null
// for the live range (0 when there is none yet), which is the part of it the
// DUT's current makes.
//
// The wiring makes the judgment possible. The bypass branch is K2's contact
// and two short wires (rig-wiring.md R10, R11); the shunt branch is 0.100 Ohm
// of shunt, one wire and K1's contact (R8). Even with K2's contact at its
// 100 mOhm maximum, K1's at nothing and 30 mOhm per wire, K2 takes about 45 %
// of the current once it closes, and as a rule more than half. With K1 open
// the shunt hangs from VIN+ with no return and carries none of it: what it
// reads is its own offset and nothing else.

/// The most a shunt may read once K1 is open, in picovolts: 50 uV.
///
/// About seven times the largest null this rig has measured (-6.85 uV in range
/// 0, 2026-10-27, the Kleidos power store's `qa/measurements/power/tdeck/`), so
/// a real offset never reaches it; and 500 uA through 0.100 Ohm, so a lead that
/// did not open -- which leaves the shunt its share of the DUT's current, all
/// of it if K2 did not close either -- is caught outright under a DUT drawing a
/// few milliamps.
/// It is not a statement about the part's `Vos` (+/-1 uV max, [DS] SS 6.5):
/// this rig nulls well outside that, and the limit is sized to the rig.
constexpr std::int64_t kNullResidualLimitPv = 50'000'000;

/// The share below which a contact's move cannot be seen, in picovolts:
/// 200 uV, which is 2 mA through 0.100 Ohm.
///
/// Below it the DUT draws too little for the steps to stand clear of the
/// offset's own uncertainty and of the DUT's variation between two readings,
/// so the order is carried out and reported `check=unverified` rather than
/// refused: only the residual limit is applied.
constexpr std::int64_t kCheckFloorPv = 200'000'000;

/// @brief The absolute value of a reading or share.
///
/// @param[in] value  A reading or share, in picovolts.
/// @return @p value without its sign, in picovolts.
constexpr std::int64_t magnitude( std::int64_t value ) noexcept {
    return ( value < 0 ) ? -value : value;
}

/// @brief Whether the first share is large enough for the steps to be judged.
///
/// @param[in] share  The reading before anything moved, less the stored null,
///                   in picovolts.
/// @retval true   its magnitude is at least kCheckFloorPv.
/// @retval false  it is below the floor: the order is `check=unverified`.
constexpr bool seesDutCurrent( std::int64_t share ) noexcept {
    return magnitude( share ) >= kCheckFloorPv;
}

/// @brief Whether the shunt lost a bypass's part of the DUT's current or
/// more: it keeps at most three quarters of its share.
///
/// A closed K2 leaves it 55 % at worst (above), so the twenty points between
/// the two are margin for the DUT's own variation between the two readings.
/// An open K1 leaves it nothing, which passes too: this alone cannot tell a
/// bypass that closed from a lead that opened (bypassTookCurrent()).
///
/// @param[in] shareBefore  The share before the move, in picovolts.
/// @param[in] shareAfter   The share after it, in picovolts.
/// @retval true   `|shareAfter|` is at most three quarters of `|shareBefore|`.
/// @retval false  the shunt kept more than that.
constexpr bool shareLeftShunt( std::int64_t shareBefore, std::int64_t shareAfter ) noexcept {
    return 4 * magnitude( shareAfter ) <= 3 * magnitude( shareBefore );
}

/// @brief Whether K2 took current: closing the bypass left the shunt at most
/// three quarters of its share, and at least a sixteenth of it.
///
/// The upper bound is shareLeftShunt(). The lower one tells a bypass that
/// closed from a lead that opened, which leaves the shunt with no return and a
/// share of nothing but the reading's noise: the two branches are of one
/// order, so a closed K2 leaves the shunt a part of the current, never none of
/// it. The bypass branch measured about 34 mOhm on 2026-11-20
/// (`docs/relay2-click-wiring.md`), against the shunt's 100 mOhm, its wire and
/// K1's contact: the shunt keeps 25 % with those two at nothing and 13 % with
/// them at 120 mOhm together, and a sixteenth, 6.25 %, is half of that. At the
/// 2 mA floor it is 12.5 uV, while a lead that opened leaves the offset alone:
/// the stored null takes it away, and with none stored the rig's largest,
/// -6.85 uV, is under it. Without this bound, relays swapped onto each other's
/// wires with matching levels pass the check: the first step opens the lead,
/// the shunt reads nothing, and the DUT is cut on every null path after it
/// (the review of 2026-11-26).
///
/// @param[in] shareBefore  The share before K2 closed, in picovolts.
/// @param[in] shareAfter   The share after K2 closed, in picovolts.
/// @retval true   `|shareAfter|` is between a sixteenth and three quarters of
///                `|shareBefore|`.
/// @retval false  the shunt kept more than that, or less.
constexpr bool bypassTookCurrent( std::int64_t shareBefore, std::int64_t shareAfter ) noexcept {
    return shareLeftShunt( shareBefore, shareAfter )
           && 16 * magnitude( shareAfter ) >= magnitude( shareBefore );
}

/// @brief Whether K1 left the shunt dead.
///
/// After the lead opened, the shunt reads no more than
/// the residual limit, and -- when the first share was large enough to judge --
/// at most half of what it read just before. A K1 that never moved reads the
/// same share twice, which the second test catches even under the limit.
///
/// @param[in] readingAfter  The shunt's reading after K1 opened, in picovolts.
/// @param[in] shareBefore   The share just before K1 opened, in picovolts.
/// @param[in] shareAfter    The share after K1 opened, in picovolts.
/// @param[in] judged        Whether the first share passed seesDutCurrent().
/// @retval true   within kNullResidualLimitPv and, when @p judged, at most half
///                the share before.
/// @retval false  either test failed.
constexpr bool leadLeftShuntDead( std::int64_t readingAfter, std::int64_t shareBefore,
                                  std::int64_t shareAfter, bool judged ) noexcept {
    if ( magnitude( readingAfter ) > kNullResidualLimitPv ) {
        return false;
    }
    return !judged || 2 * magnitude( shareAfter ) <= magnitude( shareBefore );
}

namespace detail {

constexpr std::int64_t kMicrovolt = 1'000'000;  ///< One microvolt, in picovolts.

// The T-Deck in deep sleep, 3,924.974 uA: 392 uV through the shunt, -6.85 uV of null.
constexpr std::int64_t kSleepNull  = -6'850'000;                ///< Its null, -6.85 uV, in pV.
constexpr std::int64_t kSleepShare = 392 * kMicrovolt;          ///< Its share, 392 uV, in pV.
constexpr std::int64_t kSleepRead0 = kSleepShare + kSleepNull;  ///< Its first reading, in pV.

}  // namespace detail

static_assert( kNullResidualLimitPv == 50 * detail::kMicrovolt
                   && kCheckFloorPv == 200 * detail::kMicrovolt,
               "The refusal wordings quote 50 uV and 2 mA" );
static_assert( seesDutCurrent( detail::kSleepShare ),
               "The T-Deck's lowest state is a share the steps can be judged against" );
static_assert( bypassTookCurrent( detail::kSleepShare, detail::kSleepShare * 4 / 10 ),
               "K2 taking 60 % of the current passes" );
static_assert( !bypassTookCurrent( detail::kSleepShare, detail::kSleepShare ),
               "Coils that never moved fail at the first step" );
static_assert( !bypassTookCurrent( detail::kSleepShare, detail::kSleepNull ),
               "A first step that opened the lead leaves only the offset, and fails" );
static_assert( shareLeftShunt( detail::kSleepShare, detail::kSleepNull ),
               "A lead that opened takes the whole share off the shunt" );
static_assert( leadLeftShuntDead( detail::kSleepNull, detail::kSleepShare * 4 / 10, 0, true ),
               "An open K1 leaves only the offset, and passes" );
static_assert( !leadLeftShuntDead( detail::kSleepShare * 4 / 10 + detail::kSleepNull,
                                   detail::kSleepShare * 4 / 10, detail::kSleepShare * 4 / 10,
                                   true ),
               "A K1 that did not open keeps K2's leftover share, and fails" );
static_assert( !leadLeftShuntDead( 20 * detail::kMicrovolt, 20 * detail::kMicrovolt,
                                   20 * detail::kMicrovolt, true ),
               "A stuck K1 under the residual limit still fails the half test" );
static_assert( leadLeftShuntDead( 30 * detail::kMicrovolt, 30 * detail::kMicrovolt,
                                  30 * detail::kMicrovolt, false ),
               "Below the floor only the limit applies: that is what check=unverified says" );
static_assert( detail::kSleepRead0 > kCheckFloorPv, "The scenario above is above the floor" );

/// What the instrument saw of the circuit as it stands.
enum class Evidence : std::uint8_t {
    None,        ///< `-`: not checked. Boot, and every order but `ROUTe:NULLpath ON`.
    Seen,        ///< `ok`: K2 took current and K1 left the shunt dead, both seen.
    Unverified,  ///< `unverified`: within the residual limit, but the DUT drew too little to judge.
    Skipped,     ///< `skipped`: entered under a capture, or with no INA228, so nothing was read.
};

/// @brief The evidence's spelling on the wire: `-`, `ok`, `unverified`,
/// `skipped`.
///
/// @param[in] evidence  The evidence to spell.
/// @return a static string; `-` for any value outside the four.
const char* evidenceName( Evidence evidence ) noexcept;

// --- What a reset leaves behind ------------------------------------------------

/// The coils this firmware last commanded, kept where a reset does not clear
/// it (0.16.0).
///
/// A reset releases every coil at once (Bank::begin()), so the relays always
/// come back to the measuring circuit; but with the null path held, K2 can
/// open before K1 recloses, and with the lead open the DUT was already
/// without its battery. The record lets the next boot say what the reset
/// released. On the device it lives in RTC slow memory (`RTC_NOINIT_ATTR`),
/// which a software reset, a panic and a watchdog reset leave as it was; a
/// power-on or an EN reset leaves noise, which the magic number and the
/// inverted copy refuse, and which Bank::begin() ignores whatever it reads.
struct ResetRecord {
    std::uint32_t magic;      ///< kResetRecordMagic while the record is whole.
    Mask          energized;  ///< The coils last commanded.
    Mask          inverse;    ///< `~energized`, so that noise is not read as a state.
};

/// The magic number of a whole ResetRecord.
constexpr std::uint32_t kResetRecordMagic = 0x4B4C4844U;  // "KLHD"

/// @brief Whether a record is whole: its magic number and its inverted copy
/// agree with its state.
///
/// @param[in] record  The record as RTC memory holds it.
/// @retval true   the record was written by Bank::drive() or Bank::begin().
/// @retval false  it is noise.
constexpr bool recordWhole( const ResetRecord& record ) noexcept {
    return record.magic == kResetRecordMagic && record.inverse == ~record.energized;
}

namespace detail {

/// @brief The one ResetRecord, in memory a reset does not clear.
///
/// A function's static, so that it is no global; the host tests write it to
/// stand for what a previous boot left.
///
/// @return the record.
ResetRecord& resetRecord() noexcept;

}  // namespace detail

// --- The driver ----------------------------------------------------------------

/// The four inputs, and the coil state this firmware last commanded.
///
/// There is no contact sense on the module, so the state reported is the
/// commanded one, after the settle time has elapsed. What *is* checked is the
/// pad: each pin is an input as well as an output, so a pin shorted against
/// the module's input reads back the wrong level and padMismatches() says so.
class Bank {
   public:
    Bank()                         = default;
    ~Bank()                        = default;
    Bank( const Bank& )            = delete;
    Bank& operator=( const Bank& ) = delete;
    Bank( Bank&& )                 = delete;
    Bank& operator=( Bank&& )      = delete;

    /// @brief Releases every coil on the GPIOs @p map gives the relays: each
    /// pin's output latch is written to that relay's released level
    /// **before** the pad is enabled as an output, so there is no instant at
    /// which it drives the other. app_main() does nothing before it that
    /// reaches the hardware but read the map (pin_boot.h); until then the pins
    /// are the floating inputs reset left, which both parts read as released.
    ///
    /// Once every pin is released it reads what the reset left of the commanded
    /// state (ResetRecord), keeps it as releasedByReset() unless the chip
    /// powered on, and records the released state in its place. Last, it parks
    /// pinmap::parkedSet() of @p map (0.16.6, park()). A pin that will not park
    /// stays as reset left it, as every pin did before 0.16.6, and does not
    /// touch ready().
    ///
    /// @param[in] map  A map pinmap::validate() accepts: Relay 1 is the lead
    ///                 (kLead), Relay 2 the bypass (kBypass).
    /// @return false if any relay pin could not be configured; every relay
    ///         order is then refused, and that pin stays the floating input
    ///         reset left, which the module reads as off.
    bool begin( const pinmap::PinMap& map ) noexcept;

    /// @brief Moves the relays, every coil released, to the GPIOs @p map gives
    /// them (`SYSTem:PINS:APPLy`, FW-05).
    ///
    /// Each pin is latched at its relay's released level before it is an
    /// output, as at the boot. A pin a relay leaves, and no role of @p map
    /// takes, is then parked toward the level that released that relay: an
    /// input with the pull-up for an active-low part and the pull-down for an
    /// active-high one, until the next boot parks it as the others. Nothing
    /// else is parked, and ready() is this map's.
    ///
    /// @param[in] map  A map pinmap::validate() accepts.
    void move( const pinmap::PinMap& map ) noexcept;

    /// @brief Parks every GPIO of @p pins: an input with the internal
    /// pull-down, no pull-up, no interrupt and no peripheral driving it.
    ///
    /// One call per pin, so a pin that will not park leaves the others
    /// parked; it stays as it was.
    /// @param[in] pins  The GPIOs, a pinmap::PinSet of pins the chip has.
    static void park( std::uint64_t pins ) noexcept;

    /// @brief The coils the last reset found energized and released (0.16.0).
    ///
    /// @return a Mask over kChannels; kAllReleased after a power-on, or when
    ///         the reset found every coil released.
    Mask releasedByReset() const noexcept { return releasedByReset_; }

    /// @brief Whether begin(), or the last move(), configured every pin.
    /// @return true when relay orders are carried out, false when refused.
    bool ready() const noexcept { return ready_; }
    /// @brief The coil state this firmware last commanded.
    /// @return the energized coils, a Mask over kChannels.
    Mask energized() const noexcept { return energized_; }
    /// @brief The path the commanded coil state makes.
    /// @return pathOf() of energized().
    Path path() const noexcept { return pathOf( energized_ ); }

    /// @brief Whether the shunt does not carry the DUT's whole current: K2
    /// across a closed K1 (a fraction), or the DUT fed around an open K1 (none
    /// of it).
    ///
    /// @retval true   path() is Path::Both or Path::Bypass.
    /// @retval false  path() is Path::Shunt or Path::None.
    bool shuntBypassed() const noexcept { return !shuntCarriesAll( path() ); }

    /// @brief Drives one input. No wait: the caller owns the settle time.
    ///
    /// Does nothing before a successful begin() or for a channel out of range.
    /// The new state is written to the ResetRecord as it is driven.
    ///
    /// @param[in] channel   Index of the channel in kChannels.
    /// @param[in] energize  True to energize the coil, false to release it.
    void drive( std::size_t channel, bool energize ) noexcept;

    /// @brief The channels whose pad does not read back the level it is driven
    /// to.
    ///
    /// @return a Mask with a bit set for each mismatched channel; 0 when every
    ///         pad agrees.
    Mask padMismatches() const noexcept;

   private:
    /// @brief Takes @p map's wiring and drives each relay's pin at its
    /// released level, latch first; ready() is whether every pin took it.
    /// @param[in] map  A map pinmap::validate() accepts.
    void connect( const pinmap::PinMap& map ) noexcept;

    /// Each channel's, from begin() or the last move().
    std::array<Wiring, kRelayCount> wiring_ = {};

    Mask energized_       = kAllReleased;  ///< The coils driven energized.
    Mask releasedByReset_ = kAllReleased;  ///< The coils the last reset left energized.
    bool ready_           = false;         ///< Every relay pin was configured.
};

/// @brief Appends `k1=lead:closed k2=bypass:open k3=spare:off k4=spare:off
/// path=shunt check=-` for @p state and @p evidence to @p w.
///
/// These are the relay tokens every
/// reply and report carries (`ROUTe:STATe?`, `ROUTe:RESult?`, the capture
/// header, `CALibration:ZERO:RESult?`), so all of them spell a state one way.
/// Each token is a report's `key=value` pair (text::Writer::key()), so the
/// first is preceded by a space when @p w already holds text.
///
/// @param[in,out] w         The writer the tokens are appended to.
/// @param[in]     state     The energized coils to report.
/// @param[in]     evidence  What the instrument saw of the circuit, as `check=`.
void writeState( text::Writer& w, Mask state, Evidence evidence ) noexcept;

/// @brief Appends the comma-separated channel names in @p channels (`k1,k3`),
/// or `-` when it is empty, to @p w.
///
/// @param[in,out] w        The writer the names are appended to.
/// @param[in]     channels The channels to name, a Mask over kChannels.
void writeChannels( text::Writer& w, Mask channels ) noexcept;

/// @}

}  // namespace kleidos::meter::relay

#endif  // KLEIDOS_METER_RELAY_H
