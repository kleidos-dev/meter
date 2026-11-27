// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file console.cpp
 * @ingroup console_dispatch
 * @brief The UART0 reader, the dispatcher, the response buffer, the stream
 *        records, the status registers and the relay orders.
 *
 * The measurement, configuration, calibration and diagnostic handlers are in
 * console_commands.cpp; the grammar is scpi.h and the surface `docs/scpi.md`.
 *
 * Every response, error and record is composed with the bounded writer of
 * text.h (FW-25): typed appends on `std::to_chars`, with no format string, no
 * variadic call and nothing allocated, held byte for byte to the golden
 * transcript (`firmware/test/native/golden/console.transcript`).
 *
 * One append serves every value a stream record carries, because each is
 * carried in thousandths of its printed unit (text::Writer::thousandths()):
 * nanoamps for `i_ua`, microvolts for `v_mv`, microwatts for `p_mw`,
 * milli-degrees for `die_c`. So the digits on the wire are exactly the
 * integer the device computed -- no float, no rounding of its own, no locale.
 */
#include "console.h"

#include "bits.h"
#include "chars.h"
#include "crc32.h"
#include "heap_monitor.h"
#include "pin_apply.h"
#include "pin_boot.h"
#include "pin_table.h"
#include "pinmap.h"
#include "pinmap_store.h"
#include "rtos_ticks.h"
#include "scale.h"
#include "status_led.h"
#include "text.h"
#include "uart0.h"
#include "watchdog.h"

#include <esp_app_desc.h>
#include <esp_err.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <algorithm>
#include <array>
#include <span>
#include <string_view>

#include <sdkconfig.h>

namespace kleidos::meter {

using namespace ina228;

namespace {

// --- The line ---------------------------------------------------------------
// UART0 through the CH343P bridge, on its own registers (uart0.h, FW-12). The
// native-USB port stays unplugged: a reset on a native-USB board tears down
// the host's descriptor and the capture dies with it
// (`docs/carrier-board.md` SS 7). A slow line blocks the console task and
// never the sampler -- the queue between them absorbs that, and raises
// `dropped=` when it cannot.

static_assert( kConsoleLineCapacity < uart0::kRxRingBytes,
               "the receive ring holds a whole program message" );

/// The console's core: Core 0, ESP-IDF's main task's (FW-17). The
/// latency-critical sampling loop gets a core to itself and everything that
/// blocks runs on the other.
constexpr std::uint32_t kConsoleCore = 0;
/// The console's priority, which run() raises the main task to from its 1.
constexpr UBaseType_t kConsolePriority = 5;

// The console is ESP-IDF's main task (FW-17): its stack and its core are the
// main task's options, which sdkconfig.defaults sets to the console's.
static_assert( static_cast<std::uint32_t>( CONFIG_ESP_MAIN_TASK_STACK_SIZE ) == kConsoleStackBytes,
               "the main task's stack is the console's (sdkconfig.defaults)" );
static_assert( static_cast<std::uint32_t>( CONFIG_ESP_MAIN_TASK_AFFINITY ) == kConsoleCore,
               "the main task is pinned to the console's core (sdkconfig.defaults)" );

/// How long a pass waits for input before going back to drain the stream
/// queue. At the fastest streamed cadence (STATE, 868 Sa/s) a pass of this
/// length accumulates about six samples against a queue 256 deep.
constexpr std::uint32_t kReadTimeoutMs = 5;

/// How often the console checks in with the watchdog at most (0.14.0):
/// often enough against its 5 s timeout, rarely enough that a dump's
/// per-line writes do not pay for a check-in each (watchdog.h).
constexpr std::int64_t kWatchdogFeedUs = 100000;

/// How long a running capture may go without a new sample before the console
/// says the sampler stalled (0.14.0): 2 s, thirty periods of the slowest
/// profile (`SLEEP`, 67.5 ms), so its pace probe is never taken for one.
constexpr std::int64_t kStallLimitUs = 2000000;

/// `DIAGnostic:WATChdog?`'s `panic`: 1 when a starved party reboots the
/// instrument. A constant of the build (watchdog.h), so it is decided here and
/// the report carries no branch that could only ever go one way.
constexpr std::uint32_t kWatchdogPanic = watchdog::kResetsWhenStarved ? 1U : 0U;

/// The longest `ROUTe:LEAD:CYCLe` (0.13.0): a minute without its battery is a
/// cold boot for any DUT on this rig.
constexpr std::uint32_t kMaxLeadCycleMs = 60000;

/// Printed in place of a value that does not exist yet, so that the token
/// still matches the `key=non-space` contract of every report.
constexpr const char* kAbsent = "-";

/// A pin map's role that has no GPIO, as `SYSTem:PINS?` and the vectors write it.
constexpr std::string_view kPinNone = "none";

/// A pin map's address that asks for a scan, as `SYSTem:PINS?` writes it.
constexpr std::string_view kAddressAutoWord = "auto";

/// A bus or alert pin with the internal pull-up, as a pin map's report writes it.
constexpr std::string_view kPullUp = "up";

/// A bus or alert pin with no internal pull, as a pin map's report writes it.
constexpr std::string_view kPullOff = "off";

/// A relay energized by a high level, as a pin map's report writes it.
constexpr std::string_view kActiveHigh = "high";

/// A relay energized by a low level, as a pin map's report writes it.
constexpr std::string_view kActiveLow = "low";

/// The highest number `SYSTem:PINS:STAGe` takes for a GPIO: a pin set's last
/// bit (pinmap::PinSet). validate() names a number the chip has no GPIO for.
constexpr std::uint32_t kStagedGpioMax = 63U;

/// Relay 1's index among the roles: the three before it are the bus's and
/// ALERT's, which take a pull, and the two from it are the relays, which take
/// an active level.
constexpr std::size_t kRelayRoleFirst = pinmap::roleIndex( pin_table::Role::Relay1 );

static_assert( kRelayRoleFirst == 3U
                   && pinmap::roleIndex( pin_table::Role::Relay2 ) == kRelayRoleFirst + 1U
                   && pin_table::kRoleCount == kRelayRoleFirst + 2U,
               "the roles are SDA, SCL and ALERT, then Relay 1 and Relay 2" );

/// The highest address `SYSTem:PINS:ADDRess` takes: a byte. validate() names
/// one outside the INA228's strap space.
constexpr std::uint32_t kStagedAddressMax = 255U;

/// @brief A pull as a pin map's report writes it.
/// @param[in] pull The pull.
/// @return kPullUp or kPullOff.
std::string_view pullWord( pinmap::Pull pull ) noexcept {
    return ( pull == pinmap::Pull::Up ) ? kPullUp : kPullOff;
}

/// @brief A relay's active level as a pin map's report writes it.
/// @param[in] drive The level that energizes the relay.
/// @return kActiveHigh or kActiveLow.
std::string_view driveWord( relay::Drive drive ) noexcept {
    return ( drive == relay::Drive::ActiveHigh ) ? kActiveHigh : kActiveLow;
}

/// @brief Appends @p map's address to @p w: `0x` and its two digits, or
/// kAddressAutoWord.
/// @param[in,out] w   The unit's or the error's writer.
/// @param[in]     map The map.
void writeMapAddress( text::Writer& w, const pinmap::PinMap& map ) noexcept {
    if ( std::uint32_t{ map.address } == std::uint32_t{ pinmap::kAddressAuto } ) {
        w.text( kAddressAutoWord );
    } else {
        w.text( "0x" );
        w.hex( map.address, 2U, text::LetterCase::Upper );
    }
}

/// @brief Appends the bus of a map that keeps the rules, as the errors name it:
/// `sda=4 scl=5 addr=auto`.
/// @param[in,out] w   The error's writer.
/// @param[in]     map The map.
void writeMapBus( text::Writer& w, const pinmap::PinMap& map ) noexcept {
    w.text( "sda=" );
    w.signedDecimal( map.pins[pinmap::roleIndex( pin_table::Role::Sda )] );
    w.text( " scl=" );
    w.signedDecimal( map.pins[pinmap::roleIndex( pin_table::Role::Scl )] );
    w.text( " addr=" );
    writeMapAddress( w, map );
}

/// @brief Appends @p role's `key=value` pair of @p map: its GPIO, or kPinNone.
/// @param[in,out] w    The writer.
/// @param[in]     map  The map.
/// @param[in]     role The role, whose word is the key.
void writeMapPin( text::Writer& w, const pinmap::PinMap& map, pin_table::Role role ) noexcept {
    const std::size_t index = pinmap::roleIndex( role );
    w.key( pin_table::kRoleWords[index] );
    if ( map.pins[index] == pin_table::kNoGpio ) {
        w.text( kPinNone );
    } else {
        w.signedDecimal( map.pins[index] );
    }
}

/// @brief Appends the board table's digest (pin_table::kPinTableDigest) as
/// `0x` and eight upper-case hex digits, as the vectors and the panel's copy
/// write it.
/// @param[in,out] w The writer.
void writeTableDigest( text::Writer& w ) noexcept {
    w.text( "0x" );
    w.hex( pin_table::kPinTableDigest, 8U, text::LetterCase::Upper );
}

// The self-test's bits, as `*TST?` and `SYSTem:PINS:RESult?` number them.
constexpr std::uint32_t kTstInstrument = 1U;   ///< No INA228 came up.
constexpr std::uint32_t kTstEngine     = 2U;   ///< The sampling engine is not running.
constexpr std::uint32_t kTstRelays     = 4U;   ///< A relay pin could not be configured.
constexpr std::uint32_t kTstIdentity   = 8U;   ///< The identity or SHUNT_CAL read back wrong.
constexpr std::uint32_t kTstTrim       = 16U;  ///< The trim memory's checksum (MEMSTAT).
constexpr std::uint32_t kTstAlert      = 32U;  ///< No ALERT edge per conversion.
constexpr std::uint32_t kTstPads       = 64U;  ///< A relay pad does not read its level.

/// What an apply's self-test says failed, and `SYSTem:PINS:RESult?`'s word for it.
struct PinsReason {
    std::uint32_t    bits = 0U;  ///< The self-test's bits that mean it.
    std::string_view word{};     ///< `reason`'s word.
};

/// The bus's core refused its half of the move, so the bus did not move.
constexpr std::string_view kReasonCore = "core";
/// Nothing acknowledges on the map's bus, or its transactions fail.
constexpr std::string_view kReasonNoChip = "no_chip";
/// What answers is not a sound INA228.
constexpr std::string_view kReasonChip = "chip";
/// The sampling engine is not running.
constexpr std::string_view kReasonEngine = "engine";
/// No ALERT edge reaches the map's pin.
constexpr std::string_view kReasonAlert = "alert";
/// A relay pad is not driven, or does not read its level.
constexpr std::string_view kReasonPads = "pads";
/// The flash did not take the record.
constexpr std::string_view kReasonSave = "save";
/// The relays, moved and at rest, took the DUT's current off the shunt: a
/// relay's active level, or its wire, is not the board's.
constexpr std::string_view kReasonRest = "rest";

/// The reasons read from the self-test, the first that holds being the answer.
constexpr std::array<PinsReason, 4> kPinsReasons = { {
    { kTstIdentity | kTstTrim, kReasonChip },
    { kTstEngine, kReasonEngine },
    { kTstAlert, kReasonAlert },
    { kTstRelays | kTstPads, kReasonPads },
} };

/// @brief Why a tried map failed.
/// @param[in] moved What the move did.
/// @param[in] tst   The self-test's bits after it.
/// @return The reason's word, or kAbsent when nothing failed.
std::string_view pinsReason( const pin_apply::Moved& moved, std::uint32_t tst ) noexcept {
    if ( !moved.made ) {
        return kReasonCore;
    }
    if ( pin_boot::busSilent( moved.bus ) ) {
        return kReasonNoChip;
    }
    if ( moved.bus != BringUpResult::Ok ) {
        return kReasonChip;
    }
    for ( const PinsReason& reason : kPinsReasons ) {
        if ( bits::hasBit( tst, reason.bits ) ) {
            return reason.word;
        }
    }
    return kAbsent;
}

/// Printed for a die temperature no conversion has produced yet.
constexpr std::string_view kDieUnknown = "unknown";

/// The longest a SCPI error response can be: the code, the standard text and
/// the queued detail, quoted (scpi::formatError()).
constexpr std::size_t kErrorResponseCapacity = 160;

/// Ticks a settle wait sleeps between two drains of the stream queue.
constexpr TickType_t kSettlePollTicks = 1;

/// The conversion a null-path check reads: shunt only, continuous, 1052 us
/// averaged sixteen times, whatever profile the last capture left.
constexpr std::uint16_t kCheckAdcConfig =
    adcConfigWord( kModeContinuousShunt, kCt50us, kCt1052us, kCt50us, kAvg16 );
/// How long that conversion takes: 1052 us times sixteen.
constexpr std::uint32_t kCheckConversionUs =
    static_cast<std::uint32_t>( kConversionTimeUs[kCt1052us] ) * kAverageCount[kAvg16];
/// How long a check waits for it: the conversion and 1 % more, in whole
/// milliseconds, plus 3 ms for the truncation and a tick.
constexpr std::uint32_t kCheckWaitMs =
    ( kCheckConversionUs + kCheckConversionUs / 100U ) / 1000U + 3U;

static_assert( kCheckConversionUs == 16832U, "1052 us x 16" );
static_assert( kCheckWaitMs * 1000U >= kCheckConversionUs + kCheckConversionUs / 100U + 2000U,
               "The wait must outlast one whole conversion with a tick to spare" );

// The refusals that keep a shunt which does not carry the DUT's whole current
// out of a measurement (relay::shuntCarriesAll()). The rig's documents quote
// them, and a host test holds them to the firmware.
/// The refusal of every relay order when the relay inputs failed at boot.
constexpr const char* kRelaysUnavailable = "relays unavailable (gpio config failed at boot)";
/// The refusal of an order that would bypass a running capture's shunt.
constexpr const char* kOrderBypassesCapture =
    "order bypasses the shunt under a capture (add FORCe: the capture becomes q=invalid)";
/// The refusal of an order that would leave the DUT with no supply.
constexpr const char* kOrderDisconnects = "order disconnects the dut (k1 open, k2 open: add FORCe)";

/// Status register bits (SCPI-1999 vol. 1 SS 9.4, 9.5).
constexpr std::uint32_t kOperCalibrating = 1U << 0;
constexpr std::uint32_t kOperMeasuring   = 1U << 4;  ///< OPERation bit 4: a capture runs.
constexpr std::uint32_t kQuesCurrent     = 1U << 1;  ///< QUEStionable bit 1: MATHOF or a limit.
/// QUEStionable bit 2, TIME (0.12.0): the open or last capture's cadence was
/// not the one its header promised -- the pace fell back to the timer, a read
/// failed, or the achieved period missed the nominal one past its limit.
constexpr std::uint32_t kQuesTime = 1U << 2;
/// QUEStionable bit 9, device-defined (0.12.0): samples of the open or last
/// capture did not reach the host (`dropped=`).
constexpr std::uint32_t kQuesDataLost = 1U << 9;
/// QUEStionable bit 10, device-defined (0.16.0): the hold put the relays back
/// to rest under the open or last capture while it sampled, so the circuit
/// changed under it (`hold_teardowns=`).
constexpr std::uint32_t kQuesHold = 1U << 10;
/// The mask a status register's 15 bits fill.
constexpr std::uint32_t kStatusMask = 0x7FFF;
/// The fewest free bytes a task's stack may have kept for `*TST?` to pass.
constexpr std::uint32_t kStackFloorBytes = 512;
/// QUEStionable bit 8, CALibration: the live quality is not `cal`.
constexpr std::uint32_t kQuesCalibration = 1U << 8;

/// @brief A space or a tab: what may stand around a header.
/// @param[in] c The character.
/// @retval true  A space or a tab.
/// @retval false Anything else.
constexpr bool isBlank( char c ) noexcept {
    return c == ' ' || c == '\t';
}

/// @brief What ends a program message.
/// @param[in] c The character.
/// @retval true  A newline or a carriage return.
/// @retval false Anything else.
constexpr bool isNewline( char c ) noexcept {
    return c == '\n' || c == '\r';
}

/// @brief A character no program message may hold: SCPI is 7-bit ASCII,
/// so a control character other than a tab, DEL, or a byte above 0x7F.
/// @param[in] c The character received.
/// @retval true  The line is refused with -101.
/// @retval false The character may stand in a message.
constexpr bool isInvalidByte( char c ) noexcept {
    return !chars::isPrintable( c ) && ( c != '\t' );
}

/// @brief The text @p held holds, or kAbsent when it is empty, read with the
///        array's size (chars::untilNul()).
/// @tparam N The array's size.
/// @param[in] held The array: a tag, a mark's label, the serial.
/// @return Text that always matches a report's `key=non-space` contract.
template <std::size_t N>
std::string_view heldOrAbsent( const char ( &held )[N] ) noexcept {
    const std::string_view text = chars::untilNul( held );
    return text.empty() ? std::string_view( kAbsent ) : text;
}

/// How many bytes of the image's ELF SHA-256 `SYSTem:INFormation?` names as
/// `elf=`: the first 8, written as 16 lower-case hex digits.
constexpr std::size_t kElfHashBytes = 8;

/// @brief Appends the first kElfHashBytes of the running image's ELF SHA-256
/// as lower-case hex to @p w, or kAbsent when the image has no description.
///
/// The digest is the one esptool's elf2image writes into
/// `esp_app_desc_t.app_elf_sha256`, read from the description rather than
/// through `esp_app_get_elf_sha256()`, whose string holds only
/// `CONFIG_APP_RETRIEVE_LEN_ELF_SHA` (9 by default) digits.
/// @param[in,out] w           The writer the digits are appended to.
/// @param[in]     description The running image's description, or null.
void writeElfHash( text::Writer& w, const esp_app_desc_t* description ) noexcept {
    if ( description == nullptr ) {
        w.text( kAbsent );
        return;
    }
    const std::span<const std::uint8_t> digest( description->app_elf_sha256 );
    for ( const std::uint8_t byte : digest.first( kElfHashBytes ) ) {
        w.hex( byte, 2U, text::LetterCase::Lower );
    }
}

/// The most readings a relay order's check takes: one before its first step
/// and one after each (Console::executeRelayOrder()).
constexpr std::size_t kMaxReadings = relay::Plan::kMaxSteps + 1U;

/// The longest one reading is written: `INT64_MIN`'s 19 digits and its sign,
/// and the comma before it.
constexpr std::size_t kReadingWidth = 21U;

/// @brief Appends @p readings comma-separated to @p w, or kAbsent for none.
/// @param[in,out] w        The writer the readings are appended to.
/// @param[in]     readings The readings, picovolts.
void writeReadings( text::Writer& w, std::span<const std::int64_t> readings ) noexcept {
    if ( readings.empty() ) {
        w.text( kAbsent );
        return;
    }
    for ( std::size_t i = 0; i < readings.size(); ++i ) {
        w.text( ( i == 0U ) ? "" : "," );
        w.signedDecimal( readings[i] );
    }
}

/// esp_reset_reason()'s names, indexed by its value; "unknown" past the table.
constexpr std::array<const char*, 16> kResetReasons = {
    "unknown",  "poweron", "external",   "software",  "panic", "int_wdt",
    "task_wdt", "wdt",     "deepsleep",  "brownout",  "sdio",  "usb",
    "jtag",     "efuse",   "pwr_glitch", "cpu_lockup" };

/// @brief The name of the reason the chip last reset for.
/// @return A static string from kResetReasons.
const char* resetReasonName() noexcept {
    const auto reason = static_cast<std::size_t>( esp_reset_reason() );
    return ( reason < kResetReasons.size() ) ? kResetReasons[reason] : kResetReasons[0];
}

}  // namespace

std::int16_t samplerRefusalCode( std::string_view error ) noexcept {
    /// A refusal's class.
    struct Class {
        std::string_view prefix = {};  ///< The first words of its texts.
        std::int16_t     code   = 0;   ///< The error they are queued as.
    };
    static constexpr std::array<Class, 4> kClasses = { {
        { "sampler engine", scpi::err::kSamplerFailed },
        { "i2c", scpi::err::kHardware },
        { "no device", scpi::err::kHardwareMissing },
        { "use ", scpi::err::kDataOutOfRange },
    } };
    const auto                            isOf     = [error]( const Class& known ) noexcept {
        return error.starts_with( known.prefix );
    };
    const auto found = std::find_if( kClasses.begin(), kClasses.end(), isOf );
    return ( found == kClasses.end() ) ? scpi::err::kSettingsConflict : found->code;
}

// --- Lifecycle --------------------------------------------------------------

bool Console::begin( Ina228Driver& device, Sampler& sampler, CalibrationStore& calibration,
                     relay::Bank& relays, pin_boot::Live& pins ) noexcept {
    device_      = &device;
    sampler_     = &sampler;
    calibration_ = &calibration;
    relays_      = &relays;
    pins_        = &pins;
    pinsPending_ = pins.map;
    // The rig's own relays are the ones its documents measured.
    relaysVerified_ = pinmap::sameRelays( pins.map, pinmap::kDefaultMap );
    (void)identity::readSerial( serial_ );

    // UART0 and its interrupt, on this core (Core 0, app_main()'s), where the
    // console runs: nothing allocated (uart0.h).
    return uart0::begin( kConsoleBaud ) == ESP_OK;
}

void Console::announce( BringUpResult result, bool samplerReady ) noexcept {
    bringUp_ = result;
    esr_ |= scpi::esr::kPowerOn;

    if ( pins_->bus != pin_boot::Bus::Sound ) {
        failStoredBus();
    }
    // A bus held to its pins has said so, and that there is no INA228, already.
    if ( result != BringUpResult::Ok && pins_->bus != pin_boot::Bus::Held ) {
        failText( scpi::err::kHardwareMissing, bringUpReason( result ) );
    }
    if ( !samplerReady ) {
        failText( scpi::err::kSamplerFailed, "sampler engine failed to start (reboot)" );
    }
    if ( !relays_->ready() ) {
        // The inputs that failed stay the floating pins reset left them, which
        // the module reads as off: the DUT is still on the shunt.
        failText( scpi::err::kRelaysUnavailable, kRelaysUnavailable );
    }
    // 0.16.0: a reset is the one release no order makes. Bank::begin() put
    // every coil back to rest at once; what it found energized is told here,
    // since the capture it fell in is gone with it.
    const relay::Mask released = relays_->releasedByReset();
    if ( released != relay::kAllReleased ) {
        text::Writer w = openError();
        w.text( "a reset (" );
        w.text( resetReasonName() );
        w.text( ") released " );
        relay::writeChannels( w, released );
        w.text( " (path=" );
        w.text( relay::pathName( relay::pathOf( released ) ) );
        w.text( "); the dut is back on its shunt" );
        failText( scpi::err::kResetReleased, w.view() );
    }
    // Anything received before the announcement is discarded, a loss in it
    // included: the receive ring is emptied and its overrun taken.
    const std::span<char> inbox( inbox_ );
    std::size_t           taken = 0;
    do {
        taken = uart0::read( inbox.data(), inbox.size(), 0 );
    } while ( taken > 0U );
    (void)uart0::takeOverrun();
    inboxHead_ = 0;
    inboxTail_ = 0;
}

void Console::failStoredBus() noexcept {
    text::Writer w = openError();
    w.text( "the stored bus (" );
    writeMapBus( w, pins_->stored );
    w.text( ( pins_->bus == pin_boot::Bus::FellBack )
                ? ") did not answer: the default bus is back (the record is kept)"
                : ") did not answer and a role holds the default bus pins: no INA228" );
    failText( scpi::err::kPinMapReverted, w.view() );
}

void Console::run() noexcept {
    // ESP-IDF's main task, which app_main() runs on, starts at priority 1;
    // the console's is 5 (FW-17: the console is that task, created by
    // ESP-IDF before app_main() and never ended, so no stack of its own).
    task_ = xTaskGetCurrentTaskHandle();
    vTaskPrioritySet( nullptr, kConsolePriority );
    // Subscribed from its own task (0.14.0): a console that stops coming back
    // to this loop, or to a long handler's check-in, is reset by the
    // watchdog (watchdog.h), and SYSTem:HEALth? then says reset=task_wdt.
    (void)watchdog::subscribe( watchdog::Party::Console );
    while ( true ) {
        receive( rtos::ticksFromMs( readWaitMs() ) );
        feedWatchdog();
        noteOverrun();

        // The head is read afresh for every byte: a line it runs may read more
        // into the inbox (an acquisition looking for ABORt), and may move what
        // is still unread to its front.
        while ( inboxHead_ < inboxTail_ ) {
            acceptByte( inbox_[inboxHead_++] );
        }

        pumpStream();
        superviseSampler();
        serviceLeadCycle();
        serviceHold();
        serviceOperationComplete();
        latchStatus();
    }
}

void Console::feedWatchdog() noexcept {
    const std::int64_t now = esp_timer_get_time();
    if ( now - lastWatchdogFeedUs_ < kWatchdogFeedUs ) {
        return;
    }
    lastWatchdogFeedUs_ = now;
    watchdog::checkIn( watchdog::Party::Console );
    // One tick for Core 0's idle task, whose check-in the watchdog waits for
    // too: work that never blocks (a full ring's counting pass formats a
    // million rows, about 5.6 s on the meter) would starve it otherwise. It
    // costs a millisecond in a hundred.
    vTaskDelay( 1 );
}

void Console::superviseSampler() noexcept {
    // The sampler task has its own watchdog check-in, which catches a
    // task that stops running; this catches one that runs and takes nothing
    // (a bus that answers nothing, a pace that never comes), which the
    // watchdog cannot see.
    if ( !sampler_->running() ) {
        stallWatching_ = false;
        return;
    }
    const std::uint32_t count = sampler_->sampleCount();
    const std::int64_t  now   = esp_timer_get_time();
    if ( !stallWatching_ || count != stallCount_ ) {
        stallWatching_ = true;
        stallCount_    = count;
        stallSinceUs_  = now;
        return;
    }
    if ( samplerStalled_ || now - stallSinceUs_ < kStallLimitUs ) {
        return;
    }
    samplerStalled_ = true;
    ++samplerStalls_;
    text::Writer w = openError();
    w.text( "the sampler took no sample for " );
    w.signedDecimal( ( now - stallSinceUs_ ) / 1000 );
    w.text( " ms (capture running)" );
    failText( scpi::err::kSamplerFailed, w.view() );
}

void Console::noteCaptureStart() noexcept {
    paceUnmet_      = false;
    samplerStalled_ = false;
    stallWatching_  = false;
    holdTeardowns_  = 0;
}

void Console::cmdWatchdogQuery( Call& ) noexcept {
    text::Writer w = openUnit();
    w.field( "console", watchdog::subscribed( watchdog::Party::Console ) ? 1U : 0U );
    w.field( "sampler", watchdog::subscribed( watchdog::Party::Sampler ) ? 1U : 0U );
    w.field( "timeout_s", watchdog::kTimeoutSeconds );
    w.field( "panic", kWatchdogPanic );
    w.field( "stalls", samplerStalls_ );
    respondText( w.view() );
}

void Console::cmdWatchdogTest( Call& ) noexcept {
    // The console stops checking in and waits: within the watchdog's timeout
    // the timer resets the chip, the relays release to the measuring circuit,
    // and SYSTem:HEALth? names the reset. It blocks rather than spins, so
    // IDLE0 still checks in and the reset is the task's alone.
    while ( true ) {
        vTaskDelay( rtos::kWaitForever );
    }
}

std::uint32_t Console::readWaitMs() const noexcept {
    // The loop waits for input at most kReadTimeoutMs, and no longer than a
    // lead cycle has left: its reclose is then late by the loop's own work
    // only.
    if ( !leadCycleArmed_ ) {
        return kReadTimeoutMs;
    }
    const std::int64_t left = ( leadCycleDueUs_ - esp_timer_get_time() ) / 1000;
    return ( left < 0 ) ? 0U
                        : ( ( left < static_cast<std::int64_t>( kReadTimeoutMs ) )
                                ? static_cast<std::uint32_t>( left )
                                : kReadTimeoutMs );
}

void Console::serviceLeadCycle() noexcept {
    if ( !leadCycleArmed_ || esp_timer_get_time() < leadCycleDueUs_ ) {
        return;
    }
    leadCycleArmed_ = false;
    // The DUT's battery comes back whatever runs: a capture that started
    // since the order is marked, as a LIVE move is, rather than refused.
    executeRelayOrder( relay::planSet( relays_->energized(), relay::kLead, false ), true,
                       sampler_->running(), false );
}

void Console::cmdLeadCycle( Call& call ) noexcept {
    constexpr const char* kUsage = "use ROUTe:LEAD:CYCLe <ms>,FORCe[,LIVE] (1..60000 ms)";
    std::uint32_t         ms     = 0;
    if ( !call.params.unsignedInteger( 0, ms ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( ms < 1U || ms > kMaxLeadCycleMs ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    bool force = false;
    bool live  = false;
    if ( !relayOptions( call, ForceRule::Always, force, live, kUsage ) ) {
        return;
    }
    if ( relays_->path() != relay::Path::Shunt ) {
        text::Writer w = openError();
        w.text( "a lead cycle starts from path=shunt (path=" );
        w.text( relay::pathName( relays_->path() ) );
        w.character( ')' );
        failText( scpi::err::kSettingsConflict, w.view() );
        return;
    }
    // Opened as ROUTe:LEAD OPEN opens it, with every interlock: FORCe is
    // needed, because the DUT loses its battery for the cycle. The time runs
    // from the drive, not from the end of its settle: the reclose's drive
    // follows the opening's by the cycle (bench, 2026-11-20: timed from the
    // settle's end, a 200 ms cycle held the lead open 220 ms).
    const std::int64_t drivenUs = esp_timer_get_time();
    executeRelayOrder( relay::planSet( relays_->energized(), relay::kLead, true ), force, live,
                       false );
    if ( relays_->path() == relay::Path::None ) {
        leadCycleArmed_ = true;
        leadCycleDueUs_ = drivenUs + static_cast<std::int64_t>( ms ) * 1000;
    }
}

void Console::serviceOperationComplete() noexcept {
    if ( opcPending_ && !sampler_->running() ) {
        opcPending_ = false;
        esr_ |= scpi::esr::kOperationComplete;
    }
}

void Console::receive( TickType_t wait ) noexcept {
    const std::span<char> inbox( inbox_ );
    if ( inboxHead_ == inboxTail_ ) {
        inboxHead_ = 0;
        inboxTail_ = 0;
    } else if ( inboxHead_ > 0U ) {
        const std::span<const char> pending = inbox.subspan( inboxHead_, inboxTail_ - inboxHead_ );
        (void)std::copy( pending.begin(), pending.end(), inbox.begin() );
        inboxTail_ -= inboxHead_;
        inboxHead_ = 0;
    } else {
        // The inbox already starts at its first byte: nothing to move.
    }
    const std::span<char> room = inbox.subspan( inboxTail_ );
    if ( room.empty() ) {
        return;  // the rest waits in the receive ring and the FIFO, in order
    }
    inboxTail_ += uart0::read( room.data(), room.size(), ( inboxTail_ == 0U ) ? wait : 0U );
}

void Console::acceptByte( char c ) noexcept {
    if ( isNewline( c ) ) {
        line_[lineLength_] = '\0';
        // The line's state is taken and cleared before its message runs: a
        // loss a long handler sees while it runs (noteOverrun()) belongs to
        // the next line, and must still mark it when the handler returns.
        const bool        lost     = lineLost_;
        const bool        invalid  = lineInvalid_;
        const bool        overflow = lineOverflow_;
        const std::size_t length   = lineLength_;
        lineLength_                = 0;
        lineOverflow_              = false;
        lineInvalid_               = false;
        lineLost_                  = false;
        if ( lost ) {
            // -363 was queued when the loss was seen; nothing of it runs.
        } else if ( invalid ) {
            text::Writer w = openError();
            w.text( "byte 0x" );
            w.hex( lineInvalidByte_, 2U, text::LetterCase::Upper );
            w.text( " is not printable ASCII (program message discarded)" );
            failText( scpi::err::kInvalidCharacter, w.view() );
        } else if ( overflow ) {
            text::Writer w = openError();
            w.text( "program message longer than " );
            w.unsignedDecimal( kConsoleLineCapacity - 1U );
            w.text( " bytes" );
            failText( scpi::err::kInputOverrun, w.view() );
        } else {
            // A whole line runs; an empty one runs nothing.
            if ( length > 0U ) {
                dispatch( std::span<char>( line_ ).data() );
            }
        }
        return;
    }
    // Never stored, so it cannot come back inside an error's text.
    if ( isInvalidByte( c ) ) {
        if ( !lineInvalid_ ) {
            lineInvalid_     = true;
            lineInvalidByte_ = chars::byteOf( c );
        }
        return;
    }
    if ( lineLength_ + 1U < kConsoleLineCapacity ) {
        line_[lineLength_++] = c;
    } else {
        lineOverflow_ = true;
    }
}

void Console::noteOverrun() noexcept {
    // A full receive ring holds the rest in the hardware FIFO; only the FIFO's
    // own overflow throws bytes away, and the UART latches it (uart0.h). One
    // error per message the loss falls in: a burst that overflows the FIFO
    // again before the discarded message has ended (a dump reads nothing for
    // seconds, and looks here every row) adds nothing to it.
    const bool lost = uart0::takeOverrun();
    if ( lost && !lineLost_ ) {
        failText( scpi::err::kInputOverrun,
                  "receive overrun: input lost while the console was busy (message discarded)" );
        lineLost_ = true;
    }
}

bool Console::abortRequested() noexcept {
    noteOverrun();
    receive( 0 );

    // Each complete line from the head: is its first unit ABORt?
    std::size_t start = inboxHead_;
    while ( start < inboxTail_ ) {
        std::size_t end = start;
        while ( end < inboxTail_ && !isNewline( inbox_[end] ) ) {
            ++end;
        }
        if ( end == inboxTail_ ) {
            return false;  // a partial line: not yet a message
        }
        std::size_t cursor = start;
        while ( cursor < end && isBlank( inbox_[cursor] ) ) {
            ++cursor;
        }
        if ( cursor < end && inbox_[cursor] == ':' ) {
            ++cursor;
        }
        // Room for `ABORT` and a byte more, so a longer header never matches.
        std::array<char, 8> header = {};
        std::size_t         length = 0;
        while ( cursor < end && length + 1U < header.size() && !isBlank( inbox_[cursor] )
                && inbox_[cursor] != ';' ) {
            header[length++] = inbox_[cursor++];
        }
        std::size_t after = cursor;
        while ( after < end && isBlank( inbox_[after] ) ) {
            ++after;
        }
        scpi::Suffix suffix  = {};
        const bool   alone   = after == end || inbox_[after] == ';';
        bool         isAbort = false;
        if ( alone ) {
            isAbort = scpi::matches( "ABORt", std::string_view( header.data(), length ), suffix );
        }
        if ( isAbort ) {
            // Take the unit out: through its `;` when more units follow, which
            // then run as their own message; through the newline when not.
            const std::size_t           cut = ( after == end ) ? ( end + 1U ) : ( after + 1U );
            const std::span<char>       all( inbox_ );
            const std::span<const char> rest = all.subspan( cut, inboxTail_ - cut );
            (void)std::copy( rest.begin(), rest.end(), all.subspan( start ).begin() );
            inboxTail_ -= cut - start;
            return true;
        }
        start = end + 1U;
    }
    return false;
}

void Console::dispatch( char* line ) noexcept {
    resolver_.reset();
    scpi::Splitter splitter( line );
    scpi::Unit     unit;
    while ( splitter.next( unit ) ) {
        if ( unit.headerLength == 0U ) {
            continue;  // an empty unit: Splitter leaves a header on any other (scpi.h)
        }
        std::array<char, scpi::kHeaderCapacity> header = {};
        if ( !resolver_.resolve( unit, header.data(), header.size() ) ) {
            failText( scpi::err::kUndefinedHeader, "header too long" );
            continue;
        }
        execute( header.data(), unit.parameters );
    }
    endMessage();
}

std::span<const Console::Command> Console::commands() noexcept {
    // One letter per flag, the letters of docs/scpi.md's Flags column, so that
    // the table below reads as that page does, and N where no flag applies and
    // no parameter is taken. An entry that names no count takes none either.
    // NOLINTBEGIN(readability-identifier-naming): the letters of the Flags column
    constexpr std::uint32_t D = kNeedsDevice;
    constexpr std::uint32_t R = kRefusedOpen;
    constexpr std::uint32_t C = kRefusedWhileOpen;
    constexpr std::uint32_t N = kNoParameters;
    // NOLINTEND(readability-identifier-naming)

    static_assert( scpi::kMaxParameters <= ( kParametersMask >> kParametersShift ),
                   "every count a unit can carry fits its field" );

    // A lookup table rather than a chain of comparisons, so the command
    // surface is data: scripts/scpi_table.py reads it here, and the host
    // tests' sweep through this view. Order matters only where two patterns
    // could match the same header, and none do.
    static constexpr Command kCommands[] = {
        // IEEE 488.2 common commands
        { "*IDN?", N, &Console::cmdIdn },
        { "*RST", N, &Console::cmdReset },
        { "*CLS", N, &Console::cmdClear },
        { "*ESE", takes( 1U ), &Console::cmdEseSet },
        { "*ESE?", N, &Console::cmdEseQuery },
        { "*ESR?", N, &Console::cmdEsrQuery },
        { "*SRE", takes( 1U ), &Console::cmdSreSet },
        { "*SRE?", N, &Console::cmdSreQuery },
        { "*STB?", N, &Console::cmdStbQuery },
        { "*OPC", N, &Console::cmdOpc },
        { "*OPC?", N, &Console::cmdOpcQuery },
        { "*WAI", N, &Console::cmdWait },
        { "*TST?", R | C, &Console::cmdTest },
        // SYSTem
        { "SYSTem:ERRor[:NEXT]?", N, &Console::cmdErrorNext },
        { "SYSTem:ERRor:COUNt?", N, &Console::cmdErrorCount },
        { "SYSTem:ERRor:ALL?", N, &Console::cmdErrorAll },
        { "SYSTem:VERSion?", N, &Console::cmdVersion },
        { "SYSTem:INFormation?", N, &Console::cmdInformation },
        { "SYSTem:STATus?", N, &Console::cmdStatus },
        { "SYSTem:HEALth?", N, &Console::cmdHealth },
        { "SYSTem:COMMunicate:I2C:FREQuency", D | R | takes( 1U ), &Console::cmdBusSet },
        { "SYSTem:COMMunicate:I2C:FREQuency?", N, &Console::cmdBusQuery },
        { "SYSTem:LED?", N, &Console::cmdLed },
        { "SYSTem:PINS?", N, &Console::cmdPinsQuery },
        { "SYSTem:PINS:PENDing?", N, &Console::cmdPinsPending },
        { "SYSTem:PINS:STAGe", takes( 3U ), &Console::cmdPinsStage },
        { "SYSTem:PINS:ADDRess", takes( 1U ), &Console::cmdPinsAddress },
        { "SYSTem:PINS:CLOCk", takes( 1U ), &Console::cmdPinsClock },
        { "SYSTem:PINS:CLEar", N, &Console::cmdPinsClear },
        { "SYSTem:PINS:APPLy", R | C, &Console::cmdPinsApply },
        { "SYSTem:PINS:DEFault", R | C, &Console::cmdPinsDefault },
        { "SYSTem:PINS:RESult?", N, &Console::cmdPinsResult },
        // STATus
        { "STATus:OPERation[:EVENt]?", N, &Console::cmdOperEvent },
        { "STATus:OPERation:CONDition?", N, &Console::cmdOperCondition },
        { "STATus:OPERation:ENABle", takes( 1U ), &Console::cmdOperEnable },
        { "STATus:OPERation:ENABle?", N, &Console::cmdOperEnableQuery },
        { "STATus:QUEStionable[:EVENt]?", N, &Console::cmdQuesEvent },
        { "STATus:QUEStionable:CONDition?", N, &Console::cmdQuesCondition },
        { "STATus:QUEStionable:ENABle", takes( 1U ), &Console::cmdQuesEnable },
        { "STATus:QUEStionable:ENABle?", N, &Console::cmdQuesEnableQuery },
        { "STATus:PRESet", N, &Console::cmdStatusPreset },
        { "STATus:QUEStionable:PTRansition", takes( 1U ), &Console::cmdQuesPtrSet },
        { "STATus:QUEStionable:PTRansition?", N, &Console::cmdQuesPtrQuery },
        { "STATus:QUEStionable:NTRansition", takes( 1U ), &Console::cmdQuesNtrSet },
        { "STATus:QUEStionable:NTRansition?", N, &Console::cmdQuesNtrQuery },
        { "STATus:OPERation:PTRansition", takes( 1U ), &Console::cmdOperPtrSet },
        { "STATus:OPERation:PTRansition?", N, &Console::cmdOperPtrQuery },
        { "STATus:OPERation:NTRansition", takes( 1U ), &Console::cmdOperNtrSet },
        { "STATus:OPERation:NTRansition?", N, &Console::cmdOperNtrQuery },
        // SENSe
        { "SENSe:PROFile", D | R | C | takes( 1U ), &Console::cmdProfileSet },
        { "SENSe:PROFile?", N, &Console::cmdProfileQuery },
        { "SENSe:PROFile:REPort?", N, &Console::cmdProfileReport },
        { "SENSe:CURRent:RANGe[:UPPer]", D | R | C | takes( 1U ), &Console::cmdRangeSet },
        { "SENSe:CURRent:RANGe[:UPPer]?", N, &Console::cmdRangeQuery },
        { "SENSe:CURRent:RANGe:INDex", D | R | C | takes( 1U ), &Console::cmdRangeIndexSet },
        { "SENSe:CURRent:RANGe:INDex?", N, &Console::cmdRangeIndexQuery },
        { "SENSe:CURRent:RANGe:REPort?", N, &Console::cmdRangeReport },
        { "SENSe:CURRent:SHUNt", R | takes( 1U ), &Console::cmdShuntSet },
        { "SENSe:CURRent:SHUNt?", N, &Console::cmdShuntQuery },
        { "SENSe:CURRent:SHUNt:RANGe#", R | takes( 1U ), &Console::cmdShuntRangeSet },
        { "SENSe:CURRent:SHUNt:RANGe#?", N, &Console::cmdShuntRangeQuery },
        { "SENSe:CURRent:APERture", D | R | C | takes( 1U ), &Console::cmdCurrentApertureSet },
        { "SENSe:CURRent:APERture?", N, &Console::cmdCurrentAperture },
        { "SENSe:VOLTage:APERture?", N, &Console::cmdVoltageAperture },
        { "SENSe:TEMPerature:APERture?", N, &Console::cmdTemperatureAperture },
        { "SENSe:AVERage:COUNt?", N, &Console::cmdAverageCount },
        { "SENSe:AVERage:COUNt", D | R | C | takes( 1U ), &Console::cmdAverageCountSet },
        { "SENSe:AVERage[:STATe]", D | R | C | takes( 1U ), &Console::cmdAverageSet },
        { "SENSe:AVERage[:STATe]?", N, &Console::cmdAverageQuery },
        { "SENSe:FUNCtion:ON", R | takes( 4U ), &Console::cmdFunctionOn },
        { "SENSe:FUNCtion:ON?", N, &Console::cmdFunctionOnQuery },
        { "SENSe:FUNCtion:ON:DEFault", R, &Console::cmdFunctionDefault },
        { "SENSe:CHARge?", D | R | C, &Console::cmdCharge },
        { "SENSe:CHARge:RESet", D | R | C, &Console::cmdChargeReset },
        { "SENSe:CAPTure", R | takes( 1U ), &Console::cmdCaptureSet },
        { "SENSe:CAPTure?", N, &Console::cmdCaptureQuery },
        { "SENSe:CAPTure:LIMit", R | takes( 1U ), &Console::cmdCaptureLimitSet },
        { "SENSe:CAPTure:LIMit?", N, &Console::cmdCaptureLimitQuery },
        { "SENSe:CURRent:THReshold:MARK", R | takes( 1U ), &Console::cmdThresholdMarkSet },
        { "SENSe:CURRent:THReshold:MARK?", N, &Console::cmdThresholdMarkQuery },
        { "SENSe:CURRent:THReshold", R | takes( 1U ), &Console::cmdThresholdSet },
        { "SENSe:CURRent:THReshold?", N, &Console::cmdThresholdQuery },
        { "SENSe:CURRent:THReshold:HYSTeresis", R | takes( 1U ), &Console::cmdHysteresisSet },
        { "SENSe:CURRent:THReshold:HYSTeresis?", N, &Console::cmdHysteresisQuery },
        // SAMPle, FORMat, TRIGger
        { "SAMPle:TIMer?", N, &Console::cmdSampleTimer },
        { "SAMPle:COUNt", R | takes( 1U ), &Console::cmdSampleCountSet },
        { "SAMPle:COUNt?", N, &Console::cmdSampleCountQuery },
        { "FORMat[:DATA]", R | takes( 1U ), &Console::cmdFormatSet },
        { "FORMat[:DATA]?", N, &Console::cmdFormatQuery },
        { "TRIGger:SOURce", R | takes( 1U ), &Console::cmdTriggerSourceSet },
        { "TRIGger:SOURce?", N, &Console::cmdTriggerSourceQuery },
        { "TRIGger:LEVel", R | takes( 1U ), &Console::cmdTriggerLevelSet },
        { "TRIGger:LEVel?", N, &Console::cmdTriggerLevelQuery },
        { "TRIGger:SLOPe", R | takes( 1U ), &Console::cmdTriggerSlopeSet },
        { "TRIGger:SLOPe?", N, &Console::cmdTriggerSlopeQuery },
        { "TRIGger:PRETrigger", R | takes( 1U ), &Console::cmdPretriggerSet },
        { "TRIGger:PRETrigger?", N, &Console::cmdPretriggerQuery },
        // CONFigure: only the current takes a range
        { "CONFigure[:SCALar]:CURRent[:DC]", D | R | C | takes( 1U ), &Console::cmdConfCurrent },
        { "CONFigure[:SCALar]:VOLTage[:DC]", D | R, &Console::cmdConfVoltage },
        { "CONFigure[:SCALar]:POWer[:DC]", D | R, &Console::cmdConfPower },
        { "CONFigure[:SCALar]:ENERgy", D | R, &Console::cmdConfEnergy },
        { "CONFigure[:SCALar]:TEMPerature", D | R, &Console::cmdConfTemperature },
        { "CONFigure?", N, &Console::cmdConfQuery },
        // MEASure: here too only the current takes a range
        { "MEASure[:SCALar]:CURRent[:DC]?", D | R | takes( 1U ), &Console::cmdMeasCurrent },
        { "MEASure[:SCALar]:VOLTage[:DC]?", D | R, &Console::cmdMeasVoltage },
        { "MEASure[:SCALar]:POWer[:DC]?", D | R, &Console::cmdMeasPower },
        { "MEASure[:SCALar]:ENERgy?", D | R, &Console::cmdMeasEnergy },
        { "MEASure[:SCALar]:TEMPerature?", D | R, &Console::cmdMeasTemperature },
        // READ
        { "READ?", D | R, &Console::cmdRead },
        { "READ[:SCALar]:CURRent[:DC]?", D | R, &Console::cmdReadCurrent },
        { "READ[:SCALar]:VOLTage[:DC]?", D | R, &Console::cmdReadVoltage },
        { "READ[:SCALar]:POWer[:DC]?", D | R, &Console::cmdReadPower },
        { "READ[:SCALar]:ENERgy?", D | R, &Console::cmdReadEnergy },
        { "READ[:SCALar]:TEMPerature?", D | R, &Console::cmdReadTemperature },
        // FETCh
        { "FETCh?", N, &Console::cmdFetch },
        { "FETCh[:SCALar]:CURRent[:DC]?", N, &Console::cmdFetchCurrent },
        { "FETCh[:SCALar]:VOLTage[:DC]?", N, &Console::cmdFetchVoltage },
        { "FETCh[:SCALar]:POWer[:DC]?", N, &Console::cmdFetchPower },
        { "FETCh[:SCALar]:ENERgy?", N, &Console::cmdFetchEnergy },
        { "FETCh[:SCALar]:CHARge?", N, &Console::cmdFetchCharge },
        { "FETCh[:SCALar]:TEMPerature?", N, &Console::cmdFetchTemperature },
        { "FETCh:STATistics?", N, &Console::cmdFetchStatistics },
        { "FETCh:STATistics:BATCh?", N, &Console::cmdFetchBatches },
        { "FETCh:HEADer?", N, &Console::cmdFetchHeader },
        // INITiate, ABORt
        { "INITiate[:IMMediate]", D | R, &Console::cmdInitiate },
        { "INITiate:FORCe", D | R, &Console::cmdInitiateForce },
        { "ABORt", D, &Console::cmdAbort },
        // TRACe
        { "TRACe:MARK", takes( 1U ), &Console::cmdMark },
        { "TRACe:DATA?", R | takes( 3U ), &Console::cmdTraceData },
        { "TRACe:POINts?", N, &Console::cmdTracePoints },
        { "TRACe:POINts:MAXimum?", N, &Console::cmdTracePointsMax },
        // CALibration
        { "CALibration:ZERO", D | R | takes( 1U ), &Console::cmdZeroStart },
        { "CALibration:ZERO:RESult?", N, &Console::cmdZeroResult },
        { "CALibration:TAG", R | takes( 1U ), &Console::cmdTagSet },
        { "CALibration:TAG?", N, &Console::cmdTagQuery },
        { "CALibration:DATE", R | takes( 3U ), &Console::cmdDateSet },
        { "CALibration:DATE?", N, &Console::cmdDateQuery },
        { "CALibration:VBUSside", R | takes( 1U ), &Console::cmdVbusSideSet },
        { "CALibration:VBUSside?", N, &Console::cmdVbusSideQuery },
        { "CALibration:DATA?", N, &Console::cmdCalData },
        { "CALibration:SHUNt:REFerence", R | takes( 1U ), &Console::cmdShuntReferenceSet },
        { "CALibration:SHUNt:REFerence?", N, &Console::cmdShuntReferenceQuery },
        { "CALibration:SHUNt[:MEASure]", D | R, &Console::cmdShuntMeasure },
        { "CALibration:SHUNt:RESult?", N, &Console::cmdShuntResult },
        // DIAGnostic
        { "DIAGnostic:ALERt?", D | R | C, &Console::cmdDiagAlert },
        { "DIAGnostic:I2C:SCAN?", R, &Console::cmdDiagScan },
        { "DIAGnostic:REGister?", D | R | C | takes( 1U ), &Console::cmdDiagRegister },
        { "DIAGnostic:HEAP?", N, &Console::cmdDiagHeap },
        { "DIAGnostic:WATChdog?", N, &Console::cmdWatchdogQuery },
        { "DIAGnostic:WATChdog:TEST", R | C, &Console::cmdWatchdogTest },
        // ROUTe: the relays need no INA228, and refuse a move under a capture
        // themselves, because `LIVE` is what lets one through.
        { "ROUTe:LEAD", takes( 3U ), &Console::cmdLead },
        { "ROUTe:LEAD:CYCLe", takes( 3U ), &Console::cmdLeadCycle },
        { "ROUTe:BYPass", takes( 3U ), &Console::cmdBypass },
        { "ROUTe:NULLpath", takes( 3U ), &Console::cmdNullPath },
        { "ROUTe:RELay#", takes( 2U ), &Console::cmdRelay },
        { "ROUTe:STATe?", N, &Console::cmdRouteState },
        { "ROUTe:RESult?", N, &Console::cmdRouteResult },
    };
    return kCommands;
}

void Console::execute( const char* header, char* parameters ) noexcept {
    // The census build's two queries (heap_monitor.h), which no other image
    // answers: a table of their own, tried only when the census is built, so
    // that the plain image's command set is kCommands alone and a census
    // header is `-113` there like any other unknown one.
    static constexpr Command kCensusCommands[] = {
        { "DIAGnostic:HEAP:CENSus?", kNoParameters, &Console::cmdDiagHeapCensus },
        { "DIAGnostic:HEAP:CENSus:EVENt?", takes( 1U ), &Console::cmdDiagHeapCensusEvent },
    };

    scpi::Suffix   suffix = {};
    const Command* found  = findCommand( commands(), header, suffix );
    const bool     census = heap::censusBuilt();
    if ( found == nullptr && census ) {
        found = findCommand( kCensusCommands, header, suffix );
    }
    if ( found == nullptr ) {
        failText( scpi::err::kUndefinedHeader, header );
        return;
    }
    if ( bits::hasBit( found->flags, kNeedsDevice ) && !device_->ready() ) {
        failText( scpi::err::kHardwareMissing, bringUpReason( bringUp_ ) );
        return;
    }
    if ( bits::hasBit( found->flags, kRefusedOpen ) && sampler_->running() ) {
        if ( sampler_->zeroOpen() ) {
            failText( scpi::err::kWindowRunning, "null window running (ABORt first)" );
        } else {
            failText( scpi::err::kSettingsConflict, "capture running (ABORt first)" );
        }
        return;
    }
    if ( bits::hasBit( found->flags, kRefusedWhileOpen ) && sampler_->captureOpen() ) {
        // Finished but not closed: its closing reads (ABORt) are still to come.
        if ( sampler_->zeroOpen() ) {
            failText( scpi::err::kWindowRunning, "null window ended, not closed (ABORt first)" );
        } else {
            failText( scpi::err::kSettingsConflict, "capture ended, not closed (ABORt first)" );
        }
        return;
    }

    scpi::Parameters params;
    if ( !params.parse( parameters ) ) {
        failText( params.error(), header );
        return;
    }
    const std::uint32_t most = found->mostParameters();
    if ( params.count() > most ) {
        text::Writer w = openError();
        w.text( header );
        if ( most == 0U ) {
            w.text( " takes no parameter" );
        } else {
            w.text( " takes at most " );
            w.unsignedDecimal( most );
            w.text( ( most == 1U ) ? " parameter" : " parameters" );
        }
        failText( scpi::err::kParameterNotAllowed, w.view() );
        return;
    }
    Call call{ params, suffix };
    ( this->*found->handler )( call );
}

const Console::Command* Console::findCommand( std::span<const Command> table,
                                              std::string_view         header,
                                              scpi::Suffix&            suffix ) noexcept {
    const auto found =
        std::find_if( table.begin(), table.end(), [header, &suffix]( const Command& command ) {
            return scpi::matches( command.pattern, header, suffix );
        } );
    return ( found == table.end() ) ? nullptr : &*found;
}

// --- Output -----------------------------------------------------------------

void Console::write( std::span<const char> bytes ) noexcept {
    if ( crcRunning_ ) {
        crc_ = crc32::update( crc_, bytes.data(), bytes.size() );
        crcBytes_ += bytes.size();
    }
    // A dump or a stream is the console's longest work; a dump's counting
    // pass is all formatting and never blocks, so it checks in too.
    feedWatchdog();
    if ( counting_ ) {
        countedBytes_ += bytes.size();
        return;
    }
    const std::size_t placed = uart0::write( bytes.data(), bytes.size() );

    // Use what the write returned and count what it could not place (team
    // note *a short write is the default failure*): a line that took nothing
    // for uart0::kTxStallMicroseconds. A lost line is a sample the host did
    // not get, which is what `dropped=` counts; the counter is reset at every
    // capture start, so a stray count never reaches a report.
    if ( placed < bytes.size() ) {
        sampler_->noteDropped( 1 );
    }
}

text::Writer Console::openRecord() noexcept {
    // The last byte is the newline's, so a record of any length still ends in
    // one within the line's budget.
    return text::Writer( std::span<char>( output_ ).first( sizeof( output_ ) - 1U ) );
}

void Console::endRecord( const text::Writer& record ) noexcept {
    const std::size_t length = record.size();
    output_[length]          = '\n';
    write( std::span<const char>( output_ ).first( length + 1U ) );
}

void Console::appendResponse( std::string_view text ) noexcept {
    const std::span<char> units = std::span<char>( response_ ).first( kResponseUnitsCapacity );
    std::size_t           room  = units.size() - responseLength_;
    while ( text.size() > room ) {
        (void)std::copy_n( text.begin(), room, units.subspan( responseLength_ ).begin() );
        responseLength_ += room;
        text.remove_prefix( room );
        flushResponse();
        room = units.size();
    }
    (void)std::copy_n( text.begin(), text.size(), units.subspan( responseLength_ ).begin() );
    responseLength_ += text.size();
}

text::Writer Console::openError() noexcept {
    return text::Writer( errorDetail_ );
}

text::Writer Console::openUnit() noexcept {
    return text::Writer( scratch_ );
}

void Console::respondText( std::string_view unit ) noexcept {
    if ( responseUnits_ > 0U ) {
        appendResponse( ";" );
    }
    ++responseUnits_;
    appendResponse( unit );
}

void Console::respondNumber( std::uint64_t value ) noexcept {
    text::Writer w = openUnit();
    w.unsignedDecimal( value );
    respondText( w.view() );
}

void Console::respondScaled( std::int64_t value, std::uint32_t decimals ) noexcept {
    text::Writer w = openUnit();
    w.scaled( value, decimals );
    respondText( w.view() );
}

void Console::respondMoreText( std::string_view more ) noexcept {
    appendResponse( more );
}

void Console::flushResponse() noexcept {
    if ( responseLength_ > 0U ) {
        write( std::span<const char>( response_ ).first( responseLength_ ) );
        responseLength_ = 0;
    }
    wirePartial_ = true;
}

void Console::endMessage() noexcept {
    if ( responseUnits_ == 0U ) {
        return;
    }
    // appendResponse() fills at most kResponseUnitsCapacity, a byte short of
    // the buffer: the newline's.
    response_[responseLength_++] = '\n';
    write( std::span<const char>( response_ ).first( responseLength_ ) );
    responseLength_ = 0;
    responseUnits_  = 0;
    wirePartial_    = false;
}

void Console::failText( std::int16_t code, std::string_view detail ) noexcept {
    errors_.push( code, detail );
    esr_ |= scpi::esrBitFor( code );
}

void Console::failI2c( esp_err_t error, std::string_view context ) noexcept {
    // Its own buffer, not openError()'s: @p context may be a view of that one.
    ErrorDetail  detail = {};
    text::Writer w( detail );
    w.text( "i2c " );
    w.text( esp_err_to_name( error ) );
    w.text( context );
    failText( scpi::err::kHardware, w.view() );
}

void Console::failParameter( const scpi::Parameters& params, const char* usage ) noexcept {
    const std::int16_t code =
        ( params.error() != scpi::err::kNone ) ? params.error() : scpi::err::kIllegalParameter;
    failText( code, usage );
}

void Console::failSampler( std::string_view error ) noexcept {
    failText( samplerRefusalCode( error ), error );
}

std::string_view Console::firmwareVersion() noexcept {
    const esp_app_desc_t* description = esp_app_get_description();
    return ( description != nullptr ) ? heldOrAbsent( description->version )
                                      : std::string_view( kAbsent );
}

Quality Console::liveQuality() const noexcept {
    // K2 closed across a closed K1 puts the bypass contact in parallel with
    // the shunt, so whatever the shunt reads is a fraction of the current; K1
    // open with K2 closed feeds a running DUT around it, so the shunt reads
    // none of it.
    if ( captureBypassed_ || relays_->shuntBypassed() ) {
        return Quality::Invalid;
    }
    const bool mathOverflow = ( sampler_->flags() & kDiagMathOf ) != 0U;
    return calibration_->quality( device_->range(), sampler_->lastDieMilliCelsius(), mathOverflow );
}

std::uint32_t Console::streamMask() const noexcept {
    return ( chosenMask_ != 0U ) ? chosenMask_ : profileSpec( sampler_->profile() ).defaultChannels;
}

// --- Stream -----------------------------------------------------------------

void Console::pumpStream() noexcept {
    if ( wirePartial_ ) {
        return;  // a response is half on the wire: the samples wait in the queue
    }
    Sample sample = {};
    for ( std::size_t i = 0; i < kStreamQueueDepth; ++i ) {
        if ( !sampler_->nextStreamSample( sample, 0 ) ) {
            return;
        }
        if ( sample.kind != RecordKind::Conversion ) {
            printRecord( sample );
            continue;
        }
        consume( sample );
        printSample( sample );
    }
}

void Console::printRecord( const Sample& record ) noexcept {
    Mark mark        = {};
    mark.index       = record.index;
    mark.timestampUs = record.timestampUs;
    if ( record.kind == RecordKind::Mark ) {
        mark = sampler_->markAt( record.markSlot );
    } else {
        text::Writer label( mark.label );
        label.text( ( record.kind == RecordKind::ThresholdRise ) ? std::string_view( "thr_rise" )
                                                                 : std::string_view( "thr_fall" ) );
    }
    printMark( mark );
}

void Console::emitMark( const Mark& mark ) noexcept {
    // A streamed capture's samples up to this mark's index are already in the
    // queue (the sampler counts a sample after queuing it), so a mark queued
    // behind them reaches the line after them. Anything else prints at once,
    // and so does a mark the full queue has no room for: a line that cannot
    // keep up is dropping samples anyway, and says so in `dropped=`.
    if ( sampler_->streaming() ) {
        Sample record      = {};
        record.kind        = RecordKind::Mark;
        record.index       = mark.index;
        record.timestampUs = mark.timestampUs;
        record.markSlot    = static_cast<std::uint8_t>( sampler_->markCount() - 1U );
        if ( sampler_->queueRecord( record ) ) {
            return;
        }
    }
    printMark( mark );
}

void Console::consume( const Sample& sample ) noexcept {
    // An acquisition reads the queue itself and prints nothing: a mark record
    // in it carries no conversion.
    if ( sample.kind != RecordKind::Conversion ) {
        return;
    }
    // The shunt calibration's two sums. The means of the DUT's voltage, power
    // and temperature are the sampler's (CaptureStats), over every sample
    // taken, streamed or not.
    if ( sample.busValid ) {
        const ShuntRange   range = sampler_->captureRange();
        const std::int64_t shuntPv =
            scale::shuntPicovolts( sample.shuntCounts, shuntLsbPicovolts( range ) );
        const std::int64_t corrected = shuntPv - sampler_->captureOffsetPicovolts();
        const std::int64_t busPv     = scale::busPicovolts( sample.busCounts, kBusLsbPv );
        ++accumulator_.busSamples;
        accumulator_.sumBusUv += busPv / scale::kPicoPerMicro;
        accumulator_.sumCorrectedPv += corrected;
    }
}

void Console::printSample( const Sample& sample ) noexcept {
    const std::uint32_t mask  = sampler_->channelMask();
    const ShuntRange    range = sampler_->captureRange();

    const std::int64_t shuntPv =
        scale::shuntPicovolts( sample.shuntCounts, shuntLsbPicovolts( range ) );
    const std::int64_t currentNa        = sampler_->currentNanoamps( sample.shuntCounts );
    const std::int64_t correctedShuntPv = shuntPv - sampler_->captureOffsetPicovolts();
    const std::int64_t busPv            = scale::busPicovolts( sample.busCounts, kBusLsbPv );
    const std::int64_t dutPv            = scale::dutPicovolts( busPv, correctedShuntPv );

    // A channel whose input this profile did not convert is left out rather
    // than written as a zero that would read as a measurement.
    const bool wantI = bits::hasBit( mask, channelBit( Channel::CurrentMicroamps ) );
    const bool wantV =
        sample.busValid && bits::hasBit( mask, channelBit( Channel::VoltageMillivolts ) );
    const bool wantP =
        sample.busValid && bits::hasBit( mask, channelBit( Channel::PowerMilliwatts ) );
    const bool wantT = sample.dieValid && bits::hasBit( mask, channelBit( Channel::DieCelsius ) );

    const std::int64_t voltageUv = dutPv / scale::kPicoPerMicro;
    const std::int64_t powerUw   = scale::powerMicrowatts( dutPv, currentNa );
    const std::int64_t dieMc     = scale::dieMilliCelsius( sample.dieCounts );

    if ( format_ == StreamFormat::Csv ) {
        // A channel left out is an empty field, so every record has six.
        text::Writer w = openRecord();
        w.text( "S," );
        w.unsignedDecimal( sample.timestampUs );
        w.character( ',' );
        if ( wantI ) {
            w.thousandths( currentNa );
        }
        w.character( ',' );
        if ( wantV ) {
            w.thousandths( voltageUv );
        }
        w.character( ',' );
        if ( wantP ) {
            w.thousandths( powerUw );
        }
        w.character( ',' );
        if ( wantT ) {
            w.thousandths( dieMc );
        }
        endRecord( w );
        return;
    }
    const auto line = [this, &sample]( Channel channel, std::int64_t thousandths ) noexcept {
        text::Writer w = openRecord();
        w.text( "METER t=" );
        w.unsignedDecimal( sample.timestampUs );
        w.field( "ch", kChannelNames[static_cast<std::size_t>( channel )] );
        w.key( "v" );
        w.thousandths( thousandths );
        endRecord( w );
    };
    if ( wantI ) {
        line( Channel::CurrentMicroamps, currentNa );
    }
    if ( wantV ) {
        line( Channel::VoltageMillivolts, voltageUv );
    }
    if ( wantP ) {
        line( Channel::PowerMilliwatts, powerUw );
    }
    if ( wantT ) {
        line( Channel::DieCelsius, dieMc );
    }
}

void Console::printMark( const Mark& mark ) noexcept {
    if ( wirePartial_ ) {
        return;  // kept in the capture, replayed by TRACe:DATA?
    }
    text::Writer w = openRecord();
    if ( format_ == StreamFormat::Csv ) {
        w.text( "K," );
        w.unsignedDecimal( mark.index );
        w.character( ',' );
        w.unsignedDecimal( mark.timestampUs );
        w.character( ',' );
        w.text( heldOrAbsent( mark.label ) );
    } else {
        w.text( "METER_MARK mark=" );
        w.text( heldOrAbsent( mark.label ) );
        w.field( "n", mark.index );
        w.field( "t", mark.timestampUs );
    }
    endRecord( w );
}

// --- Status -----------------------------------------------------------------

std::uint32_t Console::operationCondition() const noexcept {
    std::uint32_t condition = 0;
    if ( sampler_->zeroOpen() ) {
        condition = kOperCalibrating;
    } else if ( sampler_->running() ) {
        condition = kOperMeasuring;
    } else {
        // Neither a null nor a capture is open: no bit of the condition.
    }
    return condition;
}

std::uint32_t Console::questionableCondition() const noexcept {
    std::uint32_t condition = 0;
    if ( bits::hasBit( sampler_->flags(), kDiagMathOf | kDiagShntOl | kDiagShntUl ) ) {
        condition = kQuesCurrent;
    }
    if ( liveQuality() != Quality::Cal ) {
        condition |= kQuesCalibration;
    }
    // The sampler's own counters describe the open or last capture until the
    // next starts; a missed period is known only once a capture has closed.
    if ( sampler_->paceFellBack() || sampler_->readFailures() > 0U || paceUnmet_
         || samplerStalled_ ) {
        condition |= kQuesTime;
    }
    if ( sampler_->dropped() > 0U ) {
        condition |= kQuesDataLost;
    }
    if ( holdTeardowns_ > 0U ) {
        condition |= kQuesHold;
    }
    return condition;
}

void Console::latchStatus() noexcept {
    const std::uint32_t oper = operationCondition();
    const std::uint32_t ques = questionableCondition();
    // A rise reaches the event register through PTR, a fall through NTR.
    const auto edges = []( std::uint32_t now, std::uint32_t last, std::uint32_t ptr,
                           std::uint32_t ntr ) noexcept {
        return ( now & ~last & ptr ) | ( ~now & last & ntr );
    };
    operEvent_ |= edges( oper, operLast_, operPtr_, operNtr_ );
    quesEvent_ |= edges( ques, quesLast_, quesPtr_, quesNtr_ );
    operLast_ = oper;
    quesLast_ = ques;
}

std::uint32_t Console::statusByte() noexcept {
    latchStatus();
    std::uint32_t status = 0;
    if ( errors_.count() > 0U ) {
        status = scpi::stb::kErrorAvailable;
    }
    if ( bits::hasBit( quesEvent_, quesEnable_ ) ) {
        status |= scpi::stb::kQuestionable;
    }
    if ( bits::hasBit( esr_, ese_ ) ) {
        status |= scpi::stb::kEventSummary;
    }
    if ( bits::hasBit( operEvent_, operEnable_ ) ) {
        status |= scpi::stb::kOperationSummary;
    }
    if ( bits::hasBit( status & sre_, ~scpi::stb::kMasterSummary ) ) {
        status |= scpi::stb::kMasterSummary;
    }
    return status;
}

// --- Common commands --------------------------------------------------------

void Console::cmdIdn( Call& ) noexcept {
    // Manufacturer, model, serial, firmware (IEEE 488.2 SS 10.14). The serial
    // is the digest of the factory MAC (identity.h), the instrument's public
    // name in the calibration registry; `0` when the eFuse read failed.
    text::Writer w = openUnit();
    w.text( "KLEIDOS,KLEIDOS-METER," );
    w.text( chars::untilNul( serial_ ) );
    w.character( ',' );
    w.text( firmwareVersion() );
    respondText( w.view() );
}

void Console::cmdReset( Call& ) noexcept {
    // Every window is closed, nothing of it stored: a reset is not an ABORt.
    if ( sampler_->zeroOpen() ) {
        ZeroResult discarded = {};
        (void)sampler_->finishZero( discarded );
    } else if ( sampler_->captureOpen() ) {
        CaptureStats stats = {};
        if ( sampler_->stop( stats ) ) {
            lastStats_         = stats;
            lastHoldTeardowns_ = holdTeardowns_;
            paceUnmet_         = stats.paceUnmet;
            haveStats_         = true;
        }
    } else {
        // Nothing is open: nothing to close.
    }
    captureBypassed_ = false;

    const char* error = nullptr;
    (void)sampler_->setProfile( Profile::Trace, error );
    sampler_->resetAveraging();
    if ( device_->ready() ) {
        (void)selectRange( ShuntRange::Wide163mV );
        // A rewrite of the bus's timing registers, made only when the speed
        // is not the map's boot clock already: a reset at it reaches no
        // register (FW-14; until then the change re-created ESP-IDF's device
        // handle, an allocation).
        if ( device_->busSpeedHz() != pins_->map.sclHz ) {
            (void)device_->setBusSpeed( pins_->map.sclHz );
        }
    }
    format_                 = StreamFormat::Text;
    function_               = Function::Current;
    shuntReferenceMicroOhm_ = calmath::kDefaultReferenceMicroOhm;
    chosenMask_             = 0;
    sampleCount_            = 100;
    statisticsOnly_         = false;
    triggerSource_          = TriggerSource::Immediate;
    triggerLevelNa_         = 0;
    triggerRising_          = true;
    pretrigger_             = 0;
    captureLimited_         = false;
    opcPending_             = false;
    (void)sampler_->setThresholdMarks( false, error );
    (void)sampler_->setThreshold( false, 0, 0, error );

    // The relays go back to rest, make-before-break: K1's NC recloses before
    // K2's NO opens. The spares are not wired, so nothing else moves.
    leadCycleArmed_ = false;
    if ( relays_->ready() ) {
        releaseNullPath();
        evidence_ = relay::Evidence::None;
        updateHold();
    }
}

void Console::cmdClear( Call& ) noexcept {
    errors_.clear();
    opcPending_ = false;
    esr_        = 0;
    operEvent_  = 0;
    quesEvent_  = 0;
}

void Console::cmdEseSet( Call& call ) noexcept {
    constexpr const char* kUsage = "use *ESE <0..255>";
    std::uint32_t         value  = 0;
    if ( !call.params.unsignedInteger( 0, value ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( value > 255U ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    ese_ = value;
}

void Console::cmdEseQuery( Call& ) noexcept {
    respondNumber( ese_ );
}

void Console::cmdEsrQuery( Call& ) noexcept {
    respondNumber( esr_ );
    esr_ = 0;
}

void Console::cmdSreSet( Call& call ) noexcept {
    constexpr const char* kUsage = "use *SRE <0..255>";
    std::uint32_t         value  = 0;
    if ( !call.params.unsignedInteger( 0, value ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( value > 255U ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    sre_ = value;
}

void Console::cmdSreQuery( Call& ) noexcept {
    respondNumber( sre_ );
}

void Console::cmdStbQuery( Call& ) noexcept {
    respondNumber( statusByte() );
}

void Console::cmdOpc( Call& ) noexcept {
    // A window that ends by itself (a ring, a triggered ring, a limited
    // capture, a null) is the one pending operation this instrument has: the
    // bit is set when it ends (serviceOperationComplete()). Anything else has
    // completed before the next command is read, and the bit is set now.
    if ( sampler_->running() && sampler_->endsItself() ) {
        opcPending_ = true;
        return;
    }
    esr_ |= scpi::esr::kOperationComplete;
}

void Console::cmdOpcQuery( Call& ) noexcept {
    respondText( "1" );
}

void Console::cmdWait( Call& ) noexcept {}

std::uint32_t Console::partsTest() noexcept {
    // 1 the INA228, 2 the sampling engine, 4 the relay inputs (the bring-up's
    // three); from 0.12.0, 8 the INA228's identity or SHUNT_CAL read back
    // wrong, 16 its trim memory (MEMSTAT), 32 the ALERT wire, 64 a relay pad
    // that does not read its level.
    std::uint32_t result = 0;
    if ( !device_->ready() ) {
        result |= kTstInstrument;
    }
    if ( !sampler_->engineReady() ) {
        result |= kTstEngine;
    }
    if ( !relays_->ready() ) {
        result |= kTstRelays;
    } else if ( relays_->padMismatches() != 0U ) {
        result |= kTstPads;
    } else {
        // The bank is up and its pads agree: no bit.
    }
    if ( device_->ready() ) {
        std::uint16_t manufacturer = 0;
        std::uint16_t identity     = 0;
        std::uint16_t shuntCal     = 0;
        // Each register is read only while the ones before it were.
        esp_err_t status = device_->read16( kRegManufacturerId, manufacturer );
        if ( status == ESP_OK ) {
            status = device_->read16( kRegDeviceId, identity );
        }
        if ( status == ESP_OK ) {
            status = device_->read16( kRegShuntCal, shuntCal );
        }
        const bool read = status == ESP_OK;
        if ( !read || static_cast<std::uint32_t>( manufacturer ) != device_->manufacturerId()
             || bits::field( identity, kDieIdMask, kDieIdShift ) != device_->dieId()
             || static_cast<std::uint32_t>( shuntCal ) != device_->shuntCal() ) {
            result |= kTstIdentity;
        }
        std::uint16_t diag = 0;
        if ( device_->readDiagAlrt( diag ) != ESP_OK || !bits::hasBit( diag, kDiagMemStat ) ) {
            result |= kTstTrim;
        }
        // The wire is probed by the sampling engine; with none (bit 2) there
        // is nothing to probe it with.
        if ( sampler_->engineReady() ) {
            if ( !sampler_->probeAlert() ) {
                result |= kTstAlert;
            }
        }
    }
    return result;
}

std::uint32_t Console::selfTest() noexcept {
    // 0 is a pass; otherwise one bit per part that failed (docs/scpi.md):
    // partsTest()'s, and from 0.12.0 128 the stored calibration and 256 a
    // task's stack below its floor.
    std::uint32_t result = partsTest();
    if ( !calibration_->storedReadable() ) {
        result |= 128U;
    }
    const auto console = static_cast<std::uint32_t>( uxTaskGetStackHighWaterMark( task_ ) );
    const bool samplerLow =
        sampler_->engineReady() && sampler_->stackFreeBytes() < kStackFloorBytes;
    if ( console < kStackFloorBytes || samplerLow ) {
        result |= 256U;
    }
    return result;
}

void Console::cmdTest( Call& ) noexcept {
    respondNumber( selfTest() );
}

// --- SYSTem -----------------------------------------------------------------

void Console::cmdErrorNext( Call& ) noexcept {
    std::array<char, kErrorResponseCapacity> text   = {};
    const scpi::ErrorEntry                   entry  = errors_.pop();
    const std::size_t                        length = scpi::formatError( entry, text );
    respondText( std::string_view( text.data(), length ) );
}

void Console::cmdErrorCount( Call& ) noexcept {
    respondNumber( errors_.count() );
}

void Console::cmdErrorAll( Call& ) noexcept {
    std::array<char, kErrorResponseCapacity> text = {};
    if ( errors_.count() == 0U ) {
        const std::size_t length = scpi::formatError( scpi::ErrorEntry{}, text );
        respondText( std::string_view( text.data(), length ) );
        return;
    }
    bool first = true;
    while ( errors_.count() > 0U ) {
        const std::size_t      length = scpi::formatError( errors_.pop(), text );
        const std::string_view error( text.data(), length );
        if ( first ) {
            respondText( error );
            first = false;
        } else {
            respondMoreText( "," );
            respondMoreText( error );
        }
    }
}

void Console::cmdVersion( Call& ) noexcept {
    respondText( "1999.0" );
}

void Console::cmdHealth( Call& ) noexcept {
    // A health report reads and counts; it touches no register, so it is safe
    // under any capture. The self-test is `*TST?`, which this does not run.
    const heap::Counts counts = heap::counts();
    text::Writer       w      = openUnit();
    w.field( "reset", resetReasonName() );
    w.field( "uptime_us", static_cast<std::uint64_t>( esp_timer_get_time() ) );
    w.key( "elf" );
    writeElfHash( w, esp_app_get_description() );
    w.field( "fw", firmwareVersion() );
    w.field( "stack_console", uxTaskGetStackHighWaterMark( task_ ) );
    w.field( "stack_sampler", sampler_->stackFreeBytes() );
    w.field( "allocs_after_init", counts.allocsAfterInit );
    w.field( "frees_after_init", counts.freesAfterInit );
    w.field( "hold_released", holdReleases_ );
    w.field( "errors", errors_.count() );
    // 0.16.0: the coils the last reset found energized (`-` for none).
    w.key( "reset_released" );
    relay::writeChannels( w, relays_->releasedByReset() );
    // 0.18.0: the pin map's source and the board table it was checked against.
    w.field( "pins_src", pin_boot::sourceWord( pins_->source ) );
    w.key( "pins_table" );
    writeTableDigest( w );
    respondText( w.view() );
}

void Console::cmdInformation( Call& ) noexcept {
    const Calibration& calibration = calibration_->value();
    // A key keeps its place: a new one is appended (docs/scpi.md, CHANGELOG.md).
    text::Writer w = openUnit();
    w.field( "model", "kleidos-meter" );
    w.field( "fw", firmwareVersion() );
    w.field( "chip", device_->ready() ? "ina228" : "none" );
    w.key( "addr" );
    w.text( "0x" );
    w.hex( device_->address(), 2U, text::LetterCase::Upper );
    w.key( "mfg" );
    w.text( "0x" );
    w.hex( device_->manufacturerId(), 4U, text::LetterCase::Upper );
    w.key( "die" );
    w.text( "0x" );
    w.hex( device_->dieId(), 3U, text::LetterCase::Upper );
    w.key( "rev" );
    w.text( "0x" );
    w.hex( device_->revisionId(), 1U, text::LetterCase::Upper );
    w.field( "shunt_uohm", calibration.shuntMicroOhm );
    w.field( "cal_tag", heldOrAbsent( calibration.tag ) );
    w.field( "q", qualityName( liveQuality() ) );
    w.field( "relays", relays_->ready() ? 1U : 0U );
    w.field( "bypass_part", relay::kBypassPart );
    w.field( "serial", chars::untilNul( serial_ ) );
    w.field( "image", identity::kImageName );
    w.key( "elf" );
    writeElfHash( w, esp_app_get_description() );
    // 0.18.0: the pin map's source and the board table it was checked against.
    w.field( "pins_src", pin_boot::sourceWord( pins_->source ) );
    w.key( "pins_table" );
    writeTableDigest( w );
    respondText( w.view() );
}

void Console::cmdStatus( Call& ) noexcept {
    // The device is not read here: TRACE's affordability rests on the register
    // pointer being retained between reads, so what is reported is the last
    // conversion the sampler itself saw. `vbus_uv` is the source side of the
    // shunt, where the stream's `v_mv` is the voltage the DUT sees.
    const bool         running   = sampler_->running();
    const std::int64_t currentNa = sampler_->currentNanoamps( sampler_->lastShuntCounts() );
    const std::int64_t busPv     = scale::busPicovolts( sampler_->lastBusCounts(), kBusLsbPv );
    const std::int32_t dieMc     = sampler_->lastDieMilliCelsius();

    text::Writer w = openUnit();
    w.field( "running", running ? 1U : 0U );
    w.field( "sink", sampler_->sinkName() );
    w.field( "profile", profileSpec( sampler_->profile() ).name );
    w.field( "period_us", running ? sampler_->periodUs() : 0U );
    w.field( "pace", ( sampler_->effectivePace() == Pace::Alert ) ? std::string_view( "alert" )
                                                                  : std::string_view( "timer" ) );
    w.field( "pace_fallback", sampler_->paceFellBack() ? 1U : 0U );
    w.field( "range", static_cast<std::uint32_t>( device_->range() ) );
    w.field( "samples", sampler_->sampleCount() );
    w.field( "dropped", sampler_->dropped() );
    w.field( "read_err", sampler_->readFailures() );
    w.key( "i_na" );
    w.signedDecimal( currentNa );
    w.key( "vbus_uv" );
    w.signedDecimal( busPv / scale::kPicoPerMicro );
    w.key( "die_mc" );
    if ( dieMc == kDieMilliCelsiusUnknown ) {
        w.text( kDieUnknown );
    } else {
        w.signedDecimal( dieMc );
    }
    w.field( "die_age_us", sampler_->dieAgeUs() );
    w.key( "flags" );
    w.text( "0x" );
    w.hex( sampler_->flags(), 4U, text::LetterCase::Upper );
    w.field( "scl_hz", device_->busSpeedHz() );
    w.field( "q", qualityName( liveQuality() ) );
    // 0.11.0: whether the open capture's level trigger waits or has fired.
    std::string_view trigger = kAbsent;
    if ( sampler_->captureOpen() && captureInfo_.triggerSource == TriggerSource::Level ) {
        trigger =
            sampler_->triggerFired() ? std::string_view( "fired" ) : std::string_view( "armed" );
    }
    w.field( "trig", trigger );
    respondText( w.view() );
}

void Console::cmdBusSet( Call& call ) noexcept {
    /// The window a bus speed may be set to: standard mode's 100 kHz halved,
    /// to fast-mode plus, the own I2C master's range (i2c_bus.h).
    constexpr std::uint32_t kMinSclHz = Ina228Driver::kMinBusSpeedHz;
    constexpr std::uint32_t kMaxSclHz = Ina228Driver::kMaxBusSpeedHz;

    std::uint32_t hz = 0;
    if ( !call.params.unsignedInteger( 0, hz ) || hz < kMinSclHz || hz > kMaxSclHz ) {
        if ( call.params.error() == scpi::err::kNone ) {
            text::Writer w = openError();
            w.text( "use SYSTem:COMMunicate:I2C:FREQuency <" );
            w.unsignedDecimal( kMinSclHz );
            w.text( ".." );
            w.unsignedDecimal( kMaxSclHz );
            w.character( '>' );
            failText( scpi::err::kDataOutOfRange, w.view() );
        } else {
            failParameter( call.params, "use SYSTem:COMMunicate:I2C:FREQuency <hz>" );
        }
        return;
    }
    const esp_err_t err = device_->setBusSpeed( hz );
    if ( err != ESP_OK ) {
        failI2c( err, "" );
    }
}

void Console::cmdBusQuery( Call& ) noexcept {
    respondNumber( device_->busSpeedHz() );
}

void Console::cmdLed( Call& ) noexcept {
    // The list ahead of the report is never empty, so it needs no `-`.
    static_assert( !led::kLedDataGpios.empty(), "the LED blanks at least one line" );
    text::Writer w = openUnit();
    w.text( "blanked=" );
    led::writeBlankedList( w );
    w.field( "pixels", led::kLedBlankPixels );
    w.field( "held", "low" );
    w.character( ' ' );
    led::writeReport( w );
    respondText( w.view() );
}

// --- SYSTem:PINS ------------------------------------------------------------

void Console::writePinMap( text::Writer& w, const pinmap::PinMap& map ) noexcept {
    writeMapPin( w, map, pin_table::Role::Sda );
    writeMapPin( w, map, pin_table::Role::Scl );
    writeMapPin( w, map, pin_table::Role::Alert );
    w.key( pinmap::kAddressKey );
    writeMapAddress( w, map );
    w.field( pinmap::kClockKey, map.sclHz );
    w.field( "pull_sda", pullWord( map.pullSda ) );
    w.field( "pull_scl", pullWord( map.pullScl ) );
    w.field( "pull_alert", pullWord( map.pullAlert ) );
    writeMapPin( w, map, pin_table::Role::Relay1 );
    w.field( "relay1_active", driveWord( map.relay1Drive ) );
    writeMapPin( w, map, pin_table::Role::Relay2 );
    w.field( "relay2_active", driveWord( map.relay2Drive ) );
}

void Console::cmdPinsQuery( Call& ) noexcept {
    text::Writer w = openUnit();
    writePinMap( w, pins_->map );
    w.field( "src", pin_boot::sourceWord( pins_->source ) );
    w.field( "verified", relaysVerified_ ? 1U : 0U );
    w.key( "table" );
    writeTableDigest( w );
    respondText( w.view() );
}

void Console::cmdPinsPending( Call& ) noexcept {
    const pinmap::Verdict verdict = pinmap::validate( pinsPending_ );
    const bool            valid   = verdict.problem == pinmap::Problem::None;
    text::Writer          w       = openUnit();
    writePinMap( w, pinsPending_ );
    w.field( "valid", valid ? 1U : 0U );
    w.field( "problem",
             valid ? std::string_view( kAbsent ) : pinmap::problemWord( verdict.problem ) );
    w.field( "at", valid ? std::string_view( kAbsent ) : verdict.at );
    respondText( w.view() );
}

void Console::cmdPinsStage( Call& call ) noexcept {
    constexpr const char* kUsage =
        "use SYSTem:PINS:STAGe SDA|SCL|ALERt|RELay1|RELay2,<gpio>|NONE[,HIGH|LOW|UP|OFF]";
    // The roles as the parameter names them, REL1 and REL2 being the
    // relays' short forms; kStagedRoles gives each its role.
    static constexpr std::array<std::string_view, 7> kRoleNames = {
        "SDA", "SCL", "ALERt", "RELAY1", "RELAY2", "REL1", "REL2",
    };
    static constexpr std::array<pin_table::Role, 7> kStagedRoles = {
        pin_table::Role::Sda,    pin_table::Role::Scl,    pin_table::Role::Alert,
        pin_table::Role::Relay1, pin_table::Role::Relay2, pin_table::Role::Relay1,
        pin_table::Role::Relay2,
    };
    static constexpr std::array<std::string_view, 1> kNone   = { "NONE" };
    static constexpr std::array<std::string_view, 2> kDrives = { "HIGH", "LOW" };
    static constexpr std::array<std::string_view, 2> kPulls  = { "UP", "OFF" };

    std::size_t which = 0;
    if ( !call.params.choice( 0, kRoleNames, which ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    const pin_table::Role role = kStagedRoles[which];
    const bool relay = ( role == pin_table::Role::Relay1 ) || ( role == pin_table::Role::Relay2 );

    std::int32_t gpio  = pin_table::kNoGpio;
    const char*  given = call.params.raw( 1 );
    if ( ( given != nullptr ) && chars::isLetter( given[0] ) ) {
        std::size_t none = 0;
        if ( !call.params.choice( 1, kNone, none ) ) {
            failParameter( call.params, kUsage );
            return;
        }
    } else {
        std::uint32_t number = 0;
        if ( !call.params.unsignedInteger( 1, number ) ) {
            failParameter( call.params, kUsage );
            return;
        }
        if ( number > kStagedGpioMax ) {
            failText( scpi::err::kDataOutOfRange, kUsage );
            return;
        }
        gpio = static_cast<std::int32_t>( number );
    }

    // The option, when given: a relay's active level, a bus or alert pin's
    // pull. Read before anything is staged, so a refused unit stages nothing.
    const bool  hasOption = call.params.count() > 2U;
    std::size_t option    = 0;
    if ( hasOption ) {
        const std::span<const std::string_view> options =
            relay ? std::span<const std::string_view>( kDrives )
                  : std::span<const std::string_view>( kPulls );
        if ( !call.params.choice( 2, options, option ) ) {
            failParameter( call.params, kUsage );
            return;
        }
    }

    pinsPending_ = pinmap::withPin( pinsPending_, role, gpio );
    if ( !hasOption ) {
        return;
    }
    // A relay's level and a bus or alert pin's pull, each role's own field.
    static constexpr std::array<pinmap::Pull pinmap::PinMap::*, kRelayRoleFirst> kPullFields = {
        &pinmap::PinMap::pullSda,
        &pinmap::PinMap::pullScl,
        &pinmap::PinMap::pullAlert,
    };
    static constexpr std::array<relay::Drive pinmap::PinMap::*, 2> kDriveFields = {
        &pinmap::PinMap::relay1Drive,
        &pinmap::PinMap::relay2Drive,
    };
    const std::size_t index = pinmap::roleIndex( role );
    if ( relay ) {
        pinsPending_.*kDriveFields[index - kRelayRoleFirst] =
            ( option == 0U ) ? relay::Drive::ActiveHigh : relay::Drive::ActiveLow;
    } else {
        pinsPending_.*kPullFields[index] = ( option == 0U ) ? pinmap::Pull::Up : pinmap::Pull::Off;
    }
}

void Console::cmdPinsAddress( Call& call ) noexcept {
    constexpr const char* kUsage =
        "use SYSTem:PINS:ADDRess AUTO|<1..255> (the INA228 answers at #H40..#H4F)";
    const char* given = call.params.raw( 0 );
    if ( ( given != nullptr ) && chars::isLetter( given[0] ) ) {
        static constexpr std::array<std::string_view, 1> kAuto = { "AUTO" };
        std::size_t                                      which = 0;
        if ( !call.params.choice( 0, kAuto, which ) ) {
            failParameter( call.params, kUsage );
            return;
        }
        pinsPending_.address = pinmap::kAddressAuto;
        return;
    }
    std::uint32_t address = 0;
    if ( !call.params.unsignedInteger( 0, address ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( ( address == 0U ) || ( address > kStagedAddressMax ) ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    pinsPending_.address = static_cast<std::uint8_t>( address );
}

void Console::cmdPinsClock( Call& call ) noexcept {
    std::uint32_t hz = 0;
    if ( !call.params.unsignedInteger( 0, hz ) ) {
        failParameter( call.params, "use SYSTem:PINS:CLOCk <hz>" );
        return;
    }
    pinsPending_.sclHz = hz;
}

void Console::cmdPinsClear( Call& ) noexcept {
    pinsPending_ = pins_->map;
}

bool Console::refusePins( const pinmap::PinMap& target ) noexcept {
    pinsOutcome_        = {};
    pinsOutcome_.result = "refused";
    if ( relays_->path() != relay::Path::Shunt ) {
        pinsOutcome_.reason = "relays";
        const bool   held   = relays_->path() == relay::Path::Bypass;
        text::Writer w      = openError();
        w.text( held ? "a null path is held (path=" : "the relays are not at rest (path=" );
        w.text( relay::pathName( relays_->path() ) );
        w.text( held ? "): ROUTe:NULLpath OFF first" : "): put them back first" );
        failText( scpi::err::kSettingsConflict, w.view() );
        return true;
    }
    const pinmap::Verdict verdict = pinmap::validate( target );
    if ( verdict.problem != pinmap::Problem::None ) {
        pinsOutcome_.reason = "invalid";
        text::Writer w      = openError();
        w.text( "the staged map breaks a rule: " );
        w.text( pinmap::problemWord( verdict.problem ) );
        w.text( " at " );
        w.text( verdict.at );
        w.text( " (SYSTem:PINS:PENDing?)" );
        failText( scpi::err::kSettingsConflict, w.view() );
        return true;
    }
    return false;
}

Console::PinsTrial Console::tryPins( const pinmap::PinMap& from, const pinmap::PinMap& to,
                                     ShuntRange range ) noexcept {
    // A scan of a bus nothing answers on and a probe that times out are each
    // well under the watchdog's period, and not together.
    feedWatchdog();
    const pin_apply::Parts parts = { *device_, *sampler_, *relays_ };
    PinsTrial              trial;
    trial.moved = pin_apply::moveTo( parts, from, to );
    if ( device_->ready() ) {
        // The bring-up reset the device: its range and its accumulators with
        // it. The range it reports is the one it took.
        (void)device_->setRange( range );
        chargeWindowStartUs_ = static_cast<std::uint64_t>( esp_timer_get_time() );
    }
    feedWatchdog();
    trial.tst = partsTest();
    if ( trial.moved.bus != BringUpResult::Ok ) {
        trial.tst |= kTstInstrument;
    }
    trial.reason = pinsReason( trial.moved, trial.tst );
    return trial;
}

void Console::failReverted( const pinmap::PinMap& target, const PinsTrial& trial,
                            const PinsTrial& back ) noexcept {
    text::Writer w = openError();
    if ( trial.reason == kReasonNoChip ) {
        w.text( "the INA228 did not answer on " );
        writeMapBus( w, target );
    } else if ( trial.reason == kReasonAlert ) {
        w.text( "no ALERT edge on GPIO" );
        w.signedDecimal( target.pins[pinmap::roleIndex( pin_table::Role::Alert )] );
    } else if ( trial.reason == kReasonRest ) {
        w.text( "the relays at rest took the dut's current off the shunt (vs_pv=" );
        writeReadings( w, trial.rest );
        w.character( ')' );
    } else {
        w.text( "the map failed its proof (" );
        w.text( trial.reason );
        w.character( ')' );
    }
    w.text( ": the last good map is back" );
    if ( back.tst != 0U ) {
        w.text( "; it fails too (tst=" );
        w.unsignedDecimal( back.tst );
        w.character( ')' );
    }
    failText( scpi::err::kPinMapReverted, w.view() );
}

void Console::applyPins( const pinmap::PinMap& target, bool defaults ) noexcept {
    if ( refusePins( target ) ) {
        return;
    }
    const pinmap::PinMap lastGood  = pins_->map;
    const std::uint32_t  runningHz = device_->busSpeedHz();
    const ShuntRange     range     = device_->range();

    // A relay's pad proves nothing of its part, but the DUT's current does:
    // relays that moved and rest as they should leave the shunt its share. A
    // wrong active level energizes the part at rest, which opens the lead or
    // closes the bypass, and keeps doing so across every boot after a save.
    // The shares are judged as ROUTe:NULLpath's are, from the 2 mA floor; a
    // reading that fails is a share of nothing.
    const bool   relaysMove = !pinmap::sameRelays( target, lastGood ) && device_->ready();
    std::int64_t reference  = 0;
    if ( calibration_->nulled( range ) ) {
        reference = calibration_->offsetPicovolts( range );
    }
    std::int64_t restBefore = reference;
    if ( relaysMove ) {
        (void)readShuntPicovolts( restBefore );
    }
    const bool restJudged = relaysMove && relay::seesDutCurrent( restBefore - reference );

    PinsTrial trial = tryPins( lastGood, target, range );
    if ( restJudged && trial.reason == kAbsent ) {
        // The parts' slowest move, before the shunt is read again.
        settle( relay::kReleaseSettleMs );
        std::int64_t restAfter = reference;
        (void)readShuntPicovolts( restAfter );
        trial.rest = { restBefore, restAfter };
        if ( relay::shareLeftShunt( restBefore - reference, restAfter - reference ) ) {
            trial.reason = kReasonRest;
        }
    }
    // The defaults are kept whatever the proof finds, since they are the way
    // back with no INA228; nothing is, when the bus did not move.
    bool keep  = trial.reason == kAbsent || ( defaults && trial.moved.made );
    bool saved = false;
    if ( keep ) {
        // Written and read back only now, with the map proved.
        saved = pinmap_store::save( target ) == pinmap_store::Saved::Ok;
        if ( !saved ) {
            trial.reason = kReasonSave;
            keep         = defaults;
        }
    }

    pinsOutcome_.result     = keep ? "ok" : "reverted";
    pinsOutcome_.reason     = trial.reason;
    pinsOutcome_.identified = !pin_boot::busSilent( trial.moved.bus );
    pinsOutcome_.tested     = true;
    pinsOutcome_.saved      = saved;
    pinsOutcome_.mfg        = device_->manufacturerId();
    pinsOutcome_.die        = device_->dieId();
    pinsOutcome_.tst        = trial.tst;

    if ( keep ) {
        relaysVerified_ = relaysVerified_ && pinmap::sameRelays( target, lastGood );
        *pins_          = pin_boot::liveOf( target );
        pinsPending_    = target;
        bringUp_        = trial.moved.bus;
        if ( !saved ) {
            failText( scpi::err::kHardware,
                      "the default map is live and was not saved (a reboot loads the stored one)" );
        }
        return;
    }
    const PinsTrial back = tryPins( target, lastGood, range );
    if ( runningHz != lastGood.sclHz ) {
        // A running clock SYSTem:COMMunicate:I2C:FREQuency had set.
        (void)device_->setBusSpeed( runningHz );
    }
    bringUp_ = back.moved.bus;
    failReverted( target, trial, back );
}

void Console::cmdPinsApply( Call& ) noexcept {
    applyPins( pinsPending_, false );
}

void Console::cmdPinsDefault( Call& ) noexcept {
    applyPins( pinmap::kDefaultMap, true );
}

void Console::cmdPinsResult( Call& ) noexcept {
    const PinsOutcome& last = pinsOutcome_;
    text::Writer       w    = openUnit();
    w.field( "result", last.result );
    w.field( "reason", last.reason );
    w.key( "mfg" );
    if ( last.identified ) {
        w.text( "0x" );
        w.hex( last.mfg, 4U, text::LetterCase::Upper );
    } else {
        w.text( kAbsent );
    }
    w.key( "die" );
    if ( last.identified ) {
        w.text( "0x" );
        w.hex( last.die, 3U, text::LetterCase::Upper );
    } else {
        w.text( kAbsent );
    }
    w.key( "tst" );
    if ( last.tested ) {
        w.unsignedDecimal( last.tst );
    } else {
        w.text( kAbsent );
    }
    w.field( "saved", last.saved ? 1U : 0U );
    respondText( w.view() );
}

// --- STATus -----------------------------------------------------------------

void Console::cmdOperEvent( Call& ) noexcept {
    latchStatus();
    respondNumber( operEvent_ );
    operEvent_ = 0;
}

void Console::cmdOperCondition( Call& ) noexcept {
    respondNumber( operationCondition() );
}

void Console::cmdOperEnable( Call& call ) noexcept {
    constexpr const char* kUsage = "use STATus:OPERation:ENABle <0..32767>";
    std::uint32_t         value  = 0;
    if ( !call.params.unsignedInteger( 0, value ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( value > 32767U ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    operEnable_ = value;
}

void Console::cmdOperEnableQuery( Call& ) noexcept {
    respondNumber( operEnable_ );
}

void Console::cmdQuesEvent( Call& ) noexcept {
    latchStatus();
    respondNumber( quesEvent_ );
    quesEvent_ = 0;
}

void Console::cmdQuesCondition( Call& ) noexcept {
    respondNumber( questionableCondition() );
}

void Console::cmdQuesEnable( Call& call ) noexcept {
    constexpr const char* kUsage = "use STATus:QUEStionable:ENABle <0..32767>";
    std::uint32_t         value  = 0;
    if ( !call.params.unsignedInteger( 0, value ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( value > 32767U ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    quesEnable_ = value;
}

void Console::cmdQuesEnableQuery( Call& ) noexcept {
    respondNumber( quesEnable_ );
}

void Console::cmdStatusPreset( Call& ) noexcept {
    operEnable_ = 0;
    quesEnable_ = 0;
    operPtr_    = kStatusMask;
    operNtr_    = 0;
    quesPtr_    = kStatusMask;
    quesNtr_    = 0;
}

bool Console::maskParameter( Call& call, std::uint32_t& mask, const char* usage ) noexcept {
    std::uint32_t value = 0;
    if ( !call.params.unsignedInteger( 0, value ) ) {
        failParameter( call.params, usage );
        return false;
    }
    if ( value > kStatusMask ) {
        text::Writer w = openError();
        w.text( usage );
        w.text( " (0..32767)" );
        failText( scpi::err::kDataOutOfRange, w.view() );
        return false;
    }
    mask = value;
    return true;
}

void Console::cmdQuesPtrSet( Call& call ) noexcept {
    (void)maskParameter( call, quesPtr_, "use STATus:QUEStionable:PTRansition <mask>" );
}

void Console::cmdQuesPtrQuery( Call& ) noexcept {
    respondNumber( quesPtr_ );
}

void Console::cmdQuesNtrSet( Call& call ) noexcept {
    (void)maskParameter( call, quesNtr_, "use STATus:QUEStionable:NTRansition <mask>" );
}

void Console::cmdQuesNtrQuery( Call& ) noexcept {
    respondNumber( quesNtr_ );
}

void Console::cmdOperPtrSet( Call& call ) noexcept {
    (void)maskParameter( call, operPtr_, "use STATus:OPERation:PTRansition <mask>" );
}

void Console::cmdOperPtrQuery( Call& ) noexcept {
    respondNumber( operPtr_ );
}

void Console::cmdOperNtrSet( Call& call ) noexcept {
    (void)maskParameter( call, operNtr_, "use STATus:OPERation:NTRansition <mask>" );
}

void Console::cmdOperNtrQuery( Call& ) noexcept {
    respondNumber( operNtr_ );
}

// --- Relays -----------------------------------------------------------------

std::string_view Console::relayTokens( relay::Mask state, relay::Evidence evidence ) noexcept {
    text::Writer w( relayText_ );
    relay::writeState( w, state, evidence );
    return w.view();
}

std::string_view Console::holdText() noexcept {
    text::Writer w( holdText_ );
    if ( !holdArmed_ ) {
        w.text( kAbsent );
        return w.view();
    }
    const std::int64_t left    = holdDeadlineUs_ - esp_timer_get_time();
    const std::int64_t seconds = ( left <= 0 ) ? 0 : ( ( left + 999999 ) / 1000000 );
    w.signedDecimal( seconds );
    return w.view();
}

void Console::settle( std::uint32_t milliseconds ) noexcept {
    const std::int64_t deadline =
        esp_timer_get_time() + static_cast<std::int64_t>( milliseconds ) * 1000;
    while ( true ) {
        pumpStream();
        feedWatchdog();
        if ( esp_timer_get_time() >= deadline ) {
            return;
        }
        vTaskDelay( kSettlePollTicks );
    }
}

bool Console::readShuntPicovolts( std::int64_t& picovolts ) noexcept {
    // The profile's word is written back whatever happens: SYSTem:STATus?
    // and DIAGnostic:ALERt? must not find the check's.
    const std::uint16_t restore = device_->adcConfig();
    bool                ok      = device_->applyAdcConfig( kCheckAdcConfig ) == ESP_OK;
    std::uint32_t       raw     = 0;
    if ( ok ) {
        vTaskDelay( rtos::ticksFromMs( kCheckWaitMs ) );
        ok = device_->read24( kRegVShunt, raw ) == ESP_OK;
    }
    const bool restored = device_->applyAdcConfig( restore ) == ESP_OK;
    if ( !ok || !restored ) {
        return false;
    }
    picovolts =
        scale::shuntPicovolts( scale::decode20( raw ), shuntLsbPicovolts( device_->range() ) );
    return true;
}

void Console::updateHold() noexcept {
    // A null path left standing, from 0.13.0 a lead left open (the DUT with
    // no battery) and from 0.16.0 a bypass across a closed lead: every path
    // but the measuring circuit is put back 120 s after the order that left
    // it, or after the last window closed. With the relays down nothing is
    // energized and the path is the shunt, so the hold stays unarmed.
    holdArmed_ = relays_->path() != relay::Path::Shunt;
    if ( holdArmed_ ) {
        holdDeadlineUs_ =
            esp_timer_get_time() + static_cast<std::int64_t>( relay::kNullPathHoldS ) * 1000000;
    }
}

void Console::releaseNullPath() noexcept {
    // planNullPath( ..., false ) is make-before-break from any state: K1's NC
    // recloses and is waited out before K2's NO opens.
    const relay::Plan plan = relay::planNullPath( relays_->energized(), false );
    for ( std::size_t i = 0; i < plan.count; ++i ) {
        relays_->drive( plan.steps[i].channel, plan.steps[i].energize );
        settle( plan.steps[i].settleMs );
    }
    evidence_ = relay::Evidence::None;
    updateHold();
}

void Console::serviceHold() noexcept {
    if ( !holdArmed_ ) {
        return;
    }
    // A null window that samples holds the circuit it nulls, and is bounded
    // (600 s at most): the hold waits, and gets its whole time again from the
    // moment the window stops sampling, by itself or by ABORt. Nothing else
    // suspends it (0.16.0): a capture, open or finished, no longer does, so
    // a host that died under one leaves the relays off the shunt 120 s, not
    // for ever.
    const std::int64_t now = esp_timer_get_time();
    if ( sampler_->zeroOpen() && sampler_->running() ) {
        holdDeadlineUs_ = now + static_cast<std::int64_t>( relay::kNullPathHoldS ) * 1000000;
        return;
    }
    if ( now < holdDeadlineUs_ ) {
        return;
    }
    releaseHeld();
}

namespace {

/// What the hold's release of a path queues: its error, what it did and the
/// order that takes the path again.
struct HoldRelease {
    std::int16_t code;    ///< The asynchronous error queued.
    const char*  done;    ///< What the release did, for the detail.
    const char*  retake;  ///< The order that takes the path again.
};

/// The releases, indexed by the relay::Path held. Path::Shunt is never held.
constexpr std::array<HoldRelease, 4> kHoldReleases = { {
    { scpi::err::kNullPathReleased, "", "" },  // Path::Shunt: never held
    { scpi::err::kNullPathReleased, "the hold released the null path", "ROUTe:NULLpath ON" },
    { scpi::err::kBypassOpened, "the hold opened the bypass", "ROUTe:BYPass ON" },
    { scpi::err::kLeadReclosed, "the hold reclosed the lead", "ROUTe:LEAD OPEN" },
} };

static_assert( static_cast<std::size_t>( relay::Path::Bypass ) == 1U
                   && static_cast<std::size_t>( relay::Path::Both ) == 2U
                   && static_cast<std::size_t>( relay::Path::None ) == 3U,
               "kHoldReleases is indexed by relay::Path" );

}  // namespace

void Console::releaseHeld() noexcept {
    const relay::Path  held    = relays_->path();
    const HoldRelease& release = kHoldReleases[static_cast<std::size_t>( held )];
    // The circuit changes under a capture that is sampling, so each move is
    // marked in it the way a LIVE move is, in slots TRACe:MARK cannot fill;
    // a capture that has ended (a ring, a limit) holds no sample after it.
    const bool marking = sampler_->running();

    // planNullPath( ..., false ) is make-before-break from any state: K1's NC
    // recloses and is waited out before K2's NO opens; from path=none it is
    // K1 alone, from path=both K2 alone.
    const relay::Plan plan = relay::planNullPath( relays_->energized(), false );
    for ( std::size_t i = 0; i < plan.count; ++i ) {
        const relay::Step&        step = plan.steps[i];
        const relay::ChannelSpec& spec = relay::kChannels[step.channel];
        relays_->drive( step.channel, step.energize );
        if ( marking ) {
            std::array<char, kMarkLabelCapacity + 1> label = {};
            text::Writer                             w( label );
            w.text( "hold:" );
            w.text( spec.name );
            w.character( ':' );
            w.text( spec.releasedWord );
            Mark stored = {};
            if ( sampler_->addHoldMark( label.data(), stored ) ) {
                emitMark( stored );
            }
        }
        settle( step.settleMs );
    }
    leadCycleArmed_ = false;  // the lead is closed: nothing left to reclose
    evidence_       = relay::Evidence::None;
    updateHold();
    ++holdReleases_;
    if ( marking ) {
        ++holdTeardowns_;
    }
    // Asynchronous, so it reaches the host the one way it can: the queue
    // (0.12.0), and with it the ESR's device-dependent bit.
    text::Writer w = openError();
    w.text( release.done );
    w.text( " after " );
    w.unsignedDecimal( relay::kNullPathHoldS );
    w.text( " s" );
    w.text( marking ? " under a capture, marked hold:" : "" );
    w.text( " (" );
    w.text( release.retake );
    w.text( " again)" );
    failText( release.code, w.view() );
}

bool Console::relayOptions( Call& call, ForceRule rule, bool& force, bool& live,
                            const char* usage ) noexcept {
    static constexpr std::array<std::string_view, 2> kOptions = { "FORCe", "LIVE" };
    force                                                     = false;
    live                                                      = false;
    for ( std::size_t i = 1; i < call.params.count(); ++i ) {
        std::size_t which = 0;
        if ( !call.params.choice( i, kOptions, which ) ) {
            failParameter( call.params, usage );
            return false;
        }
        if ( which == 0U ) {
            force = true;
        } else {
            live = true;
        }
    }
    // No NULLpath plan disconnects the DUT, so the one interlock FORCe can
    // override there is a capture's shunt, which only LIVE reaches.
    if ( force && !live && rule == ForceRule::WithLive ) {
        failText( scpi::err::kIllegalParameter, usage );
        return false;
    }
    return true;
}

void Console::executeRelayOrder( const relay::Plan& plan, bool force, bool live,
                                 bool checked ) noexcept {
    haveOrder_ = false;  // a refused order leaves no result standing
    if ( !relays_->ready() ) {
        failText( scpi::err::kRelaysUnavailable, kRelaysUnavailable );
        return;
    }

    const relay::Mask before    = relays_->energized();
    const bool        capturing = sampler_->running();

    // An order that changes nothing is answered with the state whatever is
    // running; the interlocks are about moving a contact.
    if ( plan.count > 0U && capturing ) {
        // A null assumes one circuit for its whole window, so not even LIVE
        // moves a contact under one.
        if ( sampler_->zeroOpen() ) {
            failText( scpi::err::kWindowRunning, "null window running (ABORt first)" );
            return;
        }
        if ( !live ) {
            failText( scpi::err::kSettingsConflict, "capture running (ABORt first, or add LIVE)" );
            return;
        }
        // The header has already been fixed with its `q=`, so a capture the
        // order would take off the DUT's current needs FORCe as well.
        if ( !force && !captureBypassed_ && relay::bypassesShunt( before, plan ) ) {
            failText( scpi::err::kOrderBypasses, kOrderBypassesCapture );
            return;
        }
    }
    if ( !force && relay::unpowersDut( before, plan ) ) {
        failText( scpi::err::kOrderDisconnects, kOrderDisconnects );
        return;
    }

    // The checks read the shunt between the steps, which takes the device
    // away from any capture: only with none open, and only with a device.
    const bool reading = checked && plan.count > 0U && !sampler_->captureOpen() && device_->ready();

    std::array<std::int64_t, kMaxReadings> readings  = {};
    std::size_t                            readCount = 0;
    std::int64_t                           reference = 0;
    bool                                   judged    = false;
    bool                                   stuckLead = false;
    if ( reading ) {
        const ShuntRange range = device_->range();
        if ( calibration_->nulled( range ) ) {
            reference = calibration_->offsetPicovolts( range );
        }
        if ( !readShuntPicovolts( readings[0] ) ) {
            failI2c( device_->lastError(), " (null-path check: nothing moved)" );
            return;
        }
        readCount = 1;
        judged    = relay::seesDutCurrent( readings[0] - reference );
        // An open K1 leaves the shunt with no return: the DUT's current seen
        // through it says K1 did not open, and no step of this order moves
        // K1 again to judge it by.
        stuckLead = judged && relay::isEnergized( before, relay::kLead );
    }

    // Every reading a check can take fits its list, so neither the refusal's
    // nor ROUTe:RESult?'s `vs_pv=` is ever cut.
    static_assert( kMaxReadings * kReadingWidth < sizeof( orderReadings_ ),
                   "every reading of a check fits vs_pv=" );
    std::array<char, sizeof( orderMoved_ )> moved = {};
    text::Writer                            movedWriter( moved );
    std::uint32_t                           marked = 0;

    const std::int64_t began = esp_timer_get_time();
    for ( std::size_t i = 0; i < plan.count; ++i ) {
        const relay::Step&        step = plan.steps[i];
        const relay::ChannelSpec& spec = relay::kChannels[step.channel];

        relays_->drive( step.channel, step.energize );

        if ( capturing ) {
            // The capture's circuit has just changed, so the sample index it
            // changed at is recorded and emitted as a mark record, and a shunt
            // off the DUT's current at any moment marks the capture invalid.
            if ( relays_->shuntBypassed() ) {
                captureBypassed_ = true;
            }
            std::array<char, kMarkLabelCapacity + 1> label = {};
            text::Writer                             w( label );
            w.text( spec.name );
            w.character( ':' );
            w.text( step.energize ? spec.energizedWord : spec.releasedWord );
            Mark stored = {};
            if ( sampler_->addMark( label.data(), stored ) ) {
                ++marked;
                emitMark( stored );
            }
        }

        movedWriter.text( ( i == 0U ) ? "" : "," );
        movedWriter.text( spec.name );

        settle( step.settleMs );

        if ( !reading ) {
            continue;
        }
        std::int64_t now = 0;
        if ( !readShuntPicovolts( now ) ) {
            releaseNullPath();
            text::Writer w = openError();
            w.text( " (null-path check: released to path=" );
            w.text( relay::pathName( relays_->path() ) );
            w.character( ')' );
            failI2c( device_->lastError(), w.view() );
            return;
        }
        readings[readCount]            = now;
        const std::int64_t shareBefore = readings[readCount - 1U] - reference;
        const std::int64_t shareAfter  = now - reference;
        ++readCount;

        // A checked plan is NULLpath ON, whose steps only ever energize: K2
        // closes the bypass, K1 opens the lead (relay.h).
        const bool bypassFailed = step.channel == relay::kBypass && judged
                                  && !relay::bypassTookCurrent( shareBefore, shareAfter );
        // A share that fell to nothing is a lead that opened, not a bypass
        // that closed: Relay 2 is on Relay 1's wire.
        const bool shareLost  = bypassFailed && relay::shareLeftShunt( shareBefore, shareAfter );
        bool       leadFailed = false;
        if ( relays_->path() == relay::Path::Bypass ) {
            leadFailed =
                stuckLead || !relay::leadLeftShuntDead( now, shareBefore, shareAfter, judged );
        }
        if ( !bypassFailed && !leadFailed ) {
            continue;
        }

        // The path was not seen to form, so it is undone, make-before-break,
        // and the refusal carries every reading it rests on.
        relaysVerified_ = false;
        releaseNullPath();
        std::string_view why = "bypass check failed (k2 took no current: vs_pv=";
        if ( shareLost ) {
            why = "bypass check failed (the shunt lost all of the dut's current: vs_pv=";
        }
        // A lead known not to have opened is the cause whatever K2 took.
        if ( leadFailed ) {
            why = "lead check failed (the shunt still conducts: vs_pv=";
        }
        text::Writer w = openError();
        w.text( why );
        writeReadings( w, std::span<const std::int64_t>( readings ).first( readCount ) );
        w.text( "; released to path=" );
        w.text( relay::pathName( relays_->path() ) );
        w.character( ')' );
        failText( leadFailed ? scpi::err::kLeadCheckFailed : scpi::err::kBypassCheckFailed,
                  w.view() );
        return;
    }
    const auto settledUs = static_cast<std::uint64_t>( esp_timer_get_time() - began );

    if ( plan.count > 0U ) {
        if ( !checked ) {
            evidence_ = relay::Evidence::None;
        } else if ( !reading ) {
            evidence_ = relay::Evidence::Skipped;
        } else {
            evidence_       = judged ? relay::Evidence::Seen : relay::Evidence::Unverified;
            relaysVerified_ = relaysVerified_ || judged;
        }
    }
    updateHold();

    haveOrder_ = true;
    text::Writer movedText( orderMoved_ );
    movedText.text( ( plan.count > 0U ) ? movedWriter.view() : kAbsent );
    orderSettleUs_ = settledUs;
    orderLive_     = ( capturing && plan.count > 0U ) ? 1U : 0U;
    orderMarked_   = marked;
    text::Writer readingsText( orderReadings_ );
    writeReadings( readingsText, std::span<const std::int64_t>( readings ).first( readCount ) );
}

void Console::cmdLead( Call& call ) noexcept {
    constexpr const char* kUsage = "use ROUTe:LEAD OPEN|CLOSe[,FORCe][,LIVE]";
    static constexpr std::array<std::string_view, 2> kActions = { "OPEN", "CLOSe" };

    std::size_t action = 0;
    bool        force  = false;
    bool        live   = false;
    if ( !call.params.choice( 0, kActions, action ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( !relayOptions( call, ForceRule::Always, force, live, kUsage ) ) {
        return;
    }
    leadCycleArmed_ = false;  // an explicit order replaces a pending cycle
    // K1 is a series contact: energizing it opens the lead.
    executeRelayOrder( relay::planSet( relays_->energized(), relay::kLead, action == 0U ), force,
                       live, false );
}

void Console::cmdBypass( Call& call ) noexcept {
    constexpr const char* kUsage = "use ROUTe:BYPass ON|OFF[,FORCe][,LIVE]";
    bool                  on     = false;
    bool                  force  = false;
    bool                  live   = false;
    if ( !call.params.boolean( 0, on ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( !relayOptions( call, ForceRule::Always, force, live, kUsage ) ) {
        return;
    }
    // K2 is a parallel contact: energizing it closes the bypass.
    executeRelayOrder( relay::planSet( relays_->energized(), relay::kBypass, on ), force, live,
                       false );
}

void Console::cmdNullPath( Call& call ) noexcept {
    constexpr const char* kUsage = "use ROUTe:NULLpath ON|OFF[,LIVE[,FORCe]]";
    bool                  on     = false;
    bool                  force  = false;
    bool                  live   = false;
    if ( !call.params.boolean( 0, on ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( !relayOptions( call, ForceRule::WithLive, force, live, kUsage ) ) {
        return;
    }
    // ON is checked: it is the order whose result a null is taken through.
    // OFF is not, because both of its steps are spring releases.
    executeRelayOrder( relay::planNullPath( relays_->energized(), on ), force, live, on );
}

void Console::cmdRelay( Call& call ) noexcept {
    constexpr const char* kUsage = "use ROUTe:RELay3|RELay4 ON|OFF[,LIVE]";
    const std::int32_t    number = call.suffix.value;

    // One spelling per role, so that every move of K1 or K2 goes through the
    // interlocks written for it.
    if ( number == static_cast<std::int32_t>( relay::kLead ) + 1 ) {
        failText( scpi::err::kSuffixOutOfRange, "k1 is the lead (use ROUTe:LEAD OPEN|CLOSe)" );
        return;
    }
    if ( number == static_cast<std::int32_t>( relay::kBypass ) + 1 ) {
        failText( scpi::err::kSuffixOutOfRange, "k2 is the bypass (use ROUTe:BYPass ON|OFF)" );
        return;
    }
    if ( number < 1 || number > static_cast<std::int32_t>( relay::kRelayCount ) ) {
        failText( scpi::err::kSuffixOutOfRange, kUsage );
        return;
    }
    // What the order asks is read before the rig's wiring refuses it: an
    // order that names no state is a missing parameter on any rig.
    bool on = false;
    if ( !call.params.boolean( 0, on ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    // 0.13.0: the rig wires neither spare (relay.h, ChannelSpec::wired), so
    // the order is refused by name; before, it answered and no relay moved.
    text::Writer w = openError();
    w.text( relay::kChannels[static_cast<std::size_t>( number - 1 )].name );
    w.text( " is not wired on this rig (docs/rig-wiring.md)" );
    failText( scpi::err::kHardwareMissing, w.view() );
}

void Console::cmdRouteState( Call& ) noexcept {
    if ( !relays_->ready() ) {
        failText( scpi::err::kRelaysUnavailable, kRelaysUnavailable );
        return;
    }
    // `pads=` is the one thing about the module this firmware can check at any
    // time: each pin read back against the level it is driven to.
    const relay::Mask mismatched = relays_->padMismatches();
    text::Writer      w          = openUnit();
    relay::writeState( w, relays_->energized(), evidence_ );
    w.key( "pads" );
    if ( mismatched == 0U ) {
        w.text( "ok" );
    } else {
        relay::writeChannels( w, mismatched );
    }
    w.field( "operate_settle_ms", relay::maxOperateSettleMs() );
    w.field( "release_settle_ms", relay::maxReleaseSettleMs() );
    w.field( "hold_s", holdText() );
    w.field( "hold_released", holdReleases_ );
    w.field( "bypass_part", relay::kBypassPart );
    // 0.18.0: the live map's relay pins and the level that energizes each.
    const pinmap::PinMap& map = pins_->map;
    w.field( "relay1_gpio",
             static_cast<std::uint32_t>( map.pins[pinmap::roleIndex( pin_table::Role::Relay1 )] ) );
    w.field( "relay1_active", driveWord( map.relay1Drive ) );
    w.field( "relay2_gpio",
             static_cast<std::uint32_t>( map.pins[pinmap::roleIndex( pin_table::Role::Relay2 )] ) );
    w.field( "relay2_active", driveWord( map.relay2Drive ) );
    respondText( w.view() );
}

void Console::cmdRouteResult( Call& ) noexcept {
    if ( !relays_->ready() ) {
        failText( scpi::err::kRelaysUnavailable, kRelaysUnavailable );
        return;
    }
    // The circuit as it stands now, and what the last completed order did:
    // `settle_us` measured, `live=1` when a capture ran through it, `marked`
    // how many of its moves the capture recorded, `vs_pv` its check readings.
    text::Writer w = openUnit();
    relay::writeState( w, relays_->energized(), evidence_ );
    w.field( "moved", haveOrder_ ? chars::untilNul( orderMoved_ ) : std::string_view( kAbsent ) );
    w.field( "settle_us", haveOrder_ ? orderSettleUs_ : 0U );
    w.field( "live", haveOrder_ ? orderLive_ : 0U );
    w.field( "marked", haveOrder_ ? orderMarked_ : 0U );
    w.field( "vs_pv",
             haveOrder_ ? chars::untilNul( orderReadings_ ) : std::string_view( kAbsent ) );
    w.field( "hold_s", holdText() );
    w.field( "q", qualityName( liveQuality() ) );
    respondText( w.view() );
}

}  // namespace kleidos::meter
