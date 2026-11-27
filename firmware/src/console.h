// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file console.h
 * @ingroup console_dispatch
 * @brief The UART0 command surface: SCPI-1999 commands over the CH343 line.
 *
 * The grammar, every command and every response is `docs/scpi.md`; the
 * parser is scpi.h. This class owns the line, the command table, the response
 * buffer, the error queue and the status registers, and turns the sample
 * stream into records.
 *
 * Rules this file keeps without exception:
 *
 *  1. A query produces exactly one response unit; the units of one program
 *     message go out as one line, joined by `;`, terminated by `\n`. A
 *     command produces no output. Nothing else is printed but the stream
 *     records of an open capture, each on its own line, never inside a
 *     response.
 *  2. No echo, no prompt, no line editing, no boot banner: a reboot shows as
 *     the power-on bit of `*ESR?` and an error queue that may carry the
 *     bring-up's refusals.
 *  3. A refused command queues one error, with the reason as its
 *     device-dependent text, and changes nothing. Every later unit of the
 *     message still runs, so a host that ends each message with
 *     `SYSTem:ERRor:ALL?` always gets its answer.
 *  4. Anything that reconfigures the device is refused while a capture or a
 *     null window is open, naming the command that releases it (`ABORt`). So
 *     is anything that would move the device's register pointer, because
 *     TRACE's 280 us cadence rests on that pointer being retained.
 *  5. A relay order that would move a coil while a capture runs is refused
 *     unless it says `LIVE`, and one that would take the shunt off the DUT's
 *     current under that capture, or disconnect the DUT, is refused unless it
 *     also says `FORCe` (relay.h). A `LIVE` move is marked in the capture and
 *     each mark is emitted as a stream record.
 */
#ifndef KLEIDOS_METER_CONSOLE_H
#define KLEIDOS_METER_CONSOLE_H

#include "bits.h"
#include "calibration.h"
#include "calmath.h"
#include "identity.h"
#include "ina228.h"
#include "pin_apply.h"
#include "pin_boot.h"
#include "pinmap.h"
#include "relay.h"
#include "sampler.h"
#include "scpi.h"
#include "text.h"

#include <freertos/FreeRTOS.h>

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace kleidos::meter {

/// @addtogroup console_dispatch
/// @{

/// The console's stack: ESP-IDF's main task's (FW-17), on Core 0, where
/// everything that blocks belongs. `CONFIG_ESP_MAIN_TASK_STACK_SIZE` is set to
/// it in `sdkconfig.defaults`, and console.cpp fails the build when the two
/// differ.
constexpr std::uint32_t kConsoleStackBytes = 8192;

/// Longest program message accepted. Anything longer is discarded to its
/// newline and answered with `-363`.
constexpr std::size_t kConsoleLineCapacity = 256;

/// The bytes the console holds between the receive ring and the line: what a
/// pass reads, and what an acquisition reads while it looks for `ABORt`
/// (Console::abortRequested()), kept in order for the lines after it.
constexpr std::size_t kInboxCapacity = 1024;

/// The longest window one `MEASure`, `READ` or `CALibration:SHUNt`
/// acquisition may ask for: `SAMPle:COUNt` times the profile's period. A
/// longer one is refused (`-222`) instead of holding the console for hours;
/// a long record is a streamed capture (`INITiate`).
constexpr std::uint64_t kMaxAcquisitionUs = 600ULL * 1000000ULL;

/// Longest single formatted line (a stream record, one `TRACe:DATA?` row).
///
/// The binding row is a full `R` row of a raw dump: the prefix `R n=<u32> c=`
/// and sixteen counts of at most seven characters, comma-separated.
constexpr std::size_t kConsoleOutputCapacity = 384;

/// The response buffer: the units of one program message, joined. Sized for
/// the longest unit, `FETCh:HEADer?`, plus `SYSTem:ERRor:ALL?` on a full
/// queue; a message whose responses outgrow it is written out in pieces.
constexpr std::size_t kResponseCapacity = 2560;

/// What the units of a message fill of it: all but the newline's byte.
constexpr std::size_t kResponseUnitsCapacity = kResponseCapacity - 1U;

/// How a stream record is written (`FORMat:DATA`).
enum class StreamFormat : std::uint8_t {
    Text,  ///< `METER t=<us> ch=<channel> v=<value>`, one line per channel.
    Csv,   ///< `S,<t_us>,<i_ua>,<v_mv>,<p_mw>,<die_c>`, one line per sample.
};

/// What a measurement query reports (`CONFigure`, `MEASure`, `READ`, `FETCh`).
enum class Function : std::uint8_t {
    Current,      ///< Mean current, from the sampler's statistics, in amps.
    Voltage,      ///< Mean delivered voltage, in volts.
    Power,        ///< Mean delivered power, in watts.
    Energy,       ///< Mean power times the window, in joules.
    Charge,       ///< The CHARGE accumulator's change, in coulombs.
    Temperature,  ///< Mean die temperature, in degrees Celsius.
};

/// @brief The error a refused sampler call is queued as, by how its reason
/// begins.
/// @param[in] error The sampler's refusal text (sampler.h).
/// @return The engine's (`107`), the bus's (`-240`), no device's (`-241`) or a
///         value out of range's (`-222`) code, and the settings conflict's
///         (`-221`) for any other.
std::int16_t samplerRefusalCode( std::string_view error ) noexcept;

/// Sums over the samples a capture streamed (or a measurement took), for the
/// voltage, power and energy a FETCh reports. The current comes from the
/// sampler's own statistics, which see every sample, streamed or not.
struct Accumulator {
    std::uint32_t busSamples = 0;      ///< Samples with a bus conversion summed.
    bool          valid      = false;  ///< True once an acquisition has closed.

    /// The two sums the shunt calibration divides (calmath.h), over the same
    /// bus-converted samples: the bus channel's own voltage, wherever its lead
    /// is, and the offset-corrected shunt voltage.
    std::int64_t sumBusUv       = 0;
    std::int64_t sumCorrectedPv = 0;  ///< Offset-corrected shunt voltage, picovolts.
};

/// The UART0 reader, the command table and every line the instrument prints.
class Console {  // NOLINT(clang-analyzer-optin.performance.Padding): grouped by role
   public:
    Console()                            = default;
    ~Console()                           = default;
    Console( const Console& )            = delete;
    Console& operator=( const Console& ) = delete;
    Console( Console&& )                 = delete;
    Console& operator=( Console&& )      = delete;

    /// @brief Sets UART0 up on the CH343 port's pins (uart0::begin(), its
    /// interrupt on this core).
    ///
    /// Creates no task: the console runs on the task that calls run(), which
    /// is `app_main()`'s, ESP-IDF's main task (FW-17). Nothing is read until
    /// then.
    ///
    /// @p relays is the bank app_main() has already released; the console is
    /// its only user from here on, so no order can interleave with another.
    /// @param[in,out] device      The INA228, kept for the console's commands.
    /// @param[in,out] sampler     The sampling engine.
    /// @param[in,out] calibration The calibration store.
    /// @param[in,out] relays      The relay bank, the console's alone from here.
    /// @param[in]     pins        The map the boot runs, kept for the reports;
    ///                            the staged map starts as it.
    /// @retval true  The UART is set up.
    /// @retval false uart0::begin() refused it.
    bool begin( Ina228Driver& device, Sampler& sampler, CalibrationStore& calibration,
                relay::Bank& relays, pin_boot::Live& pins ) noexcept;

    /// @brief Records the power-on state: the `PON` bit of `*ESR?`, and one
    /// queued error for each of the four things that can be wrong -- the
    /// stored pin map's bus, the INA228 bring-up, the sampling engine, the
    /// relay inputs. A stored bus that did not answer and could not fall back
    /// is one error, the bus's, which says there is no INA228.
    ///
    /// Prints nothing, and discards whatever the port received since begin()
    /// (a host's open, line noise at reset): run() reads from here on.
    /// @param[in] result       The INA228 bring-up result.
    /// @param[in] samplerReady Whether the sampling engine started.
    void announce( BringUpResult result, bool samplerReady ) noexcept;

    /// @brief The console's loop, run on the calling task for good: reads the
    /// line, runs each program message, and services the stream, the status,
    /// the relay hold, the lead cycle and the watchdog between reads.
    ///
    /// Called by `app_main()` after announce(), so the console is ESP-IDF's
    /// main task (FW-17): that task never ends, and so nothing frees its
    /// stack, and no task or stack of the console's own is created. It first
    /// raises the task's priority from the main task's 1 to the console's 5
    /// and subscribes it to the watchdog. Never returns.
    [[noreturn]] void run() noexcept;

    // --- Command table ------------------------------------------------------
    //
    // Public because it is read from outside, and only read: the host tests'
    // sweep (firmware/test/native/test_sweep.cpp) runs every entry of it.

    /// One unit's decoded call.
    struct Call {
        scpi::Parameters& params;  ///< The unit's parameters, decoded on demand.
        scpi::Suffix      suffix;  ///< The numeric suffixes the header carried.
    };

    /// A command's handler: a member that reads @c Call and responds or fails.
    using Handler = void ( Console::* )( Call& call ) noexcept;

    /// Flags a command entry carries.
    static constexpr std::uint32_t kNeedsDevice = 0x01U;  ///< Refused with the bring-up reason.
    static constexpr std::uint32_t kRefusedOpen = 0x02U;  ///< Refused while a window is open.

    /// Refused while any window is open, running or finished and awaiting
    /// `ABORt`: the commands that change what an open capture's closing reads
    /// rest on (the range, the profile, the CHARGE accumulator, the latched
    /// DIAG_ALRT flags). kRefusedOpen alone lets a command through once a
    /// ring has filled, which is what `TRACe:DATA?` and the reads that only
    /// move the register pointer need.
    static constexpr std::uint32_t kRefusedWhileOpen = 0x08U;

    /// The field of an entry's flags that holds the most parameters its
    /// command takes; a unit that gives more is refused (-108) before its
    /// handler runs. Zero, none at all, is what an entry that names no count
    /// gets.
    static constexpr std::uint32_t kParametersMask  = 0xF0U;
    static constexpr std::uint32_t kParametersShift = 4U;  ///< That field's lowest bit.
    static constexpr std::uint32_t kNoParameters    = 0U;  ///< Refuses any parameter (-108).

    /// @brief The flags of a command that takes at most @p most parameters.
    /// @param[in] most The count, at most @ref scpi::kMaxParameters.
    /// @return The count, in its field of an entry's flags.
    [[nodiscard]] static consteval std::uint32_t takes( std::uint32_t most ) noexcept {
        return most << kParametersShift;
    }

    /// One row of the command table: a header pattern and what runs it.
    struct Command {
        std::string_view pattern = {};       ///< The SCPI header, short and long forms (scpi.h).
        std::uint32_t    flags   = 0;        ///< The refusals that apply (kNeedsDevice ...).
        Handler          handler = nullptr;  ///< The member that runs the command.

        /// @brief The most parameters the command takes.
        /// @return The count its flags carry, 0 for none.
        [[nodiscard]] constexpr std::uint32_t mostParameters() const noexcept {
            return bits::field( flags, kParametersMask, kParametersShift );
        }
    };

    /// @brief The command table: every header the instrument answers, less
    /// the census build's two (heap_monitor.h).
    /// @return A read-only view of the table, in the order it is searched.
    static std::span<const Command> commands() noexcept;

    /// @brief The first entry of @p table whose pattern matches @p header.
    /// @param[in]  table  The entries, in the order they are tried.
    /// @param[in]  header The resolved, absolute header.
    /// @param[out] suffix The numeric suffixes the header carried, for the
    ///                    entry found.
    /// @return The entry, or nullptr when none matches.
    static const Command* findCommand( std::span<const Command> table, std::string_view header,
                                       scpi::Suffix& suffix ) noexcept;

   private:
    /// @brief Reads what the receive ring holds into the inbox
    /// (uart0::read()), waiting at most @p wait when the inbox is empty and
    /// has room.
    /// @param[in] wait The longest wait for the first byte, in ticks.
    void receive( TickType_t wait ) noexcept;

    /// @brief Takes one received byte into the line: a newline ends it and
    /// runs it, a byte that is not printable ASCII marks it for `-101`, an
    /// overlong one for `-363`, a receive overrun for discarding.
    /// @param[in] c The byte.
    void acceptByte( char c ) noexcept;

    /// Queues `-363` for input the UART lost (a hardware FIFO overflow,
    /// uart0::takeOverrun()), marking the line being assembled for
    /// discarding; a loss inside a line already marked queues nothing more.
    /// Run by every pass and inside the long handlers.
    void noteOverrun() noexcept;

    /// @brief Reads the line while an acquisition runs and looks in the inbox
    /// for a program message whose first unit is `ABORt`. Takes that unit out
    /// of the inbox (the rest of its message stays queued, as does everything
    /// else) and returns true.
    /// @retval true  An `ABORt` was taken out of the inbox.
    /// @retval false None has come.
    bool abortRequested() noexcept;

    /// @brief Runs one program message: splits it into units, resolves each
    /// header against the previous one's path and executes it, then ends the
    /// response line.
    /// @param[in,out] line The message, without its newline; split in place.
    void dispatch( char* line ) noexcept;
    /// @brief Looks @p header up in the command table (and, in a census
    /// build, the census's two queries), applies the entry's refusals and
    /// runs its handler; `-113` when no entry matches.
    /// @param[in]     header     The resolved, absolute header.
    /// @param[in,out] parameters The unit's parameter text, decoded in place.
    void execute( const char* header, char* parameters ) noexcept;

    // --- Output -------------------------------------------------------------
    //
    // Every response, error and record the console writes is composed with
    // the bounded writer of text.h, typed appends with no format string
    // (FW-25, FW-26): a unit through openUnit() and respondText(), a number
    // through respondNumber() and respondScaled(), a record through
    // openRecord() and endRecord(), an error through failText() and
    // failI2c().

    /// The buffer one error's device-dependent text is composed in, as long
    /// as the queue keeps it (scpi::kErrorDetailCapacity), for failText().
    using ErrorDetail = std::array<char, scpi::kErrorDetailCapacity>;

    /// @brief Queues the one error of a stored pin map whose bus did not answer
    /// at boot (pin_boot::Bus): which bus, and what took its place.
    void failStoredBus() noexcept;

    /// @brief A writer over the buffer an error's device-dependent text is
    /// composed in (errorDetail_), for a detail of more than one piece;
    /// failText() then queues what it holds. Nothing else writes that buffer
    /// between the two, and a member rather than a local keeps it out of
    /// every handler's frame (-Wstack-usage=1024).
    /// @return The writer, over an empty string.
    text::Writer openError() noexcept;

    /// @brief Writes @p bytes to the UART (uart0::write()), adding them to the
    /// dump's CRC while one runs, and counts a short write as a dropped
    /// sample; while counting, only adds them to countedBytes_.
    /// @param[in] bytes The bytes.
    void write( std::span<const char> bytes ) noexcept;

    /// @brief A writer over the response unit's own buffer (scratch_), for a
    /// unit composed of more than one piece; respondText() then appends what
    /// it holds. Nothing else writes that buffer between the two.
    /// @return The writer, over an empty string.
    text::Writer openUnit() noexcept;

    /// @brief Appends one response unit, `;`-separated from the previous one.
    /// @param[in] unit The unit's text, a view of openUnit()'s writer or any
    ///                 other text.
    void respondText( std::string_view unit ) noexcept;

    /// @brief Appends one response unit that is @p value in decimal.
    /// @param[in] value The value.
    void respondNumber( std::uint64_t value ) noexcept;

    /// @brief Appends one response unit that is @p value written in a SCPI
    /// base unit, as text::Writer::scaled() writes it.
    /// @param[in] value    The value, in units of 10^-@p decimals of the base
    ///                     unit.
    /// @param[in] decimals The decimals that unit carries.
    void respondScaled( std::int64_t value, std::uint32_t decimals ) noexcept;

    /// @brief Appends text to the current unit without a separator.
    /// @param[in] more The text.
    void respondMoreText( std::string_view more ) noexcept;

    /// @brief A writer over the stream record's buffer (output_), less the
    /// byte endRecord() puts the newline in, so a record keeps the line
    /// budget kConsoleOutputCapacity sets: it is cut, never overrun.
    /// @return The writer, over an empty string.
    text::Writer openRecord() noexcept;

    /// @brief Ends the record @p record holds with its newline and writes it.
    /// @param[in] record The writer openRecord() returned.
    void endRecord( const text::Writer& record ) noexcept;

    /// @brief Queues an error and sets its ESR bit.
    /// @param[in] code   The SCPI error code (scpi::err).
    /// @param[in] detail The device-dependent text: the reason, and the remedy
    ///                   where there is one; the queue keeps what fits.
    void failText( std::int16_t code, std::string_view detail ) noexcept;

    /// @brief Queues `-240` for a bus transaction that failed: `i2c`, the
    /// error's name and @p context, which says what the failure left (empty
    /// for nothing more).
    /// @param[in] error   The transaction's error.
    /// @param[in] context Text appended to the error's name, a space first;
    ///                    it may be a view of openError()'s buffer.
    void failI2c( esp_err_t error, std::string_view context ) noexcept;

    /// Writes the buffered units and the terminating newline.
    void endMessage() noexcept;

    /// Writes whatever the response buffer holds, leaving the line open.
    void flushResponse() noexcept;

    /// @brief Appends raw text to the response, flushing the buffer each time
    /// it fills, so a text of any length goes out whole.
    ///
    /// It fills at most @ref kResponseUnitsCapacity, so endMessage()'s newline
    /// always fits. Every caller has counted the unit the text belongs to
    /// (responseUnits_), so a flush is always inside a counted unit: a
    /// response that is part on the wire has one.
    ///
    /// @param[in] text The text.
    void appendResponse( std::string_view text ) noexcept;

    /// @brief Queues the parser's error for a parameter that did not decode.
    /// @param[in] params The unit's parameters, holding the parser's error.
    /// @param[in] usage  The usage text the error carries.
    void failParameter( const scpi::Parameters& params, const char* usage ) noexcept;

    /// @brief Queues the error a refused sampler call left, mapped to its
    /// class.
    /// @param[in] error The sampler's refusal text; a refused call always
    ///                  names its reason (sampler.h).
    void failSampler( std::string_view error ) noexcept;

    // --- Stream -------------------------------------------------------------

    /// @brief Drains the sampler's stream queue: each conversion is summed and
    /// printed, each mark record printed. Waits while a response is half on
    /// the wire. An acquisition (acquire()) consumes its own samples and never
    /// comes here.
    void pumpStream() noexcept;
    /// @brief Adds one conversion to the accumulator's sums.
    /// @param[in] sample The sample; a record of another kind is ignored.
    void consume( const Sample& sample ) noexcept;
    /// @brief Writes one sample as a stream record, in the chosen format, with
    /// only the channels the profile converted.
    /// @param[in] sample The sample.
    void printSample( const Sample& sample ) noexcept;
    /// @brief Writes one mark as a stream record, unless a response is half
    /// on the wire (`TRACe:DATA?` still replays it).
    /// @param[in] mark The mark.
    void printMark( const Mark& mark ) noexcept;

    // --- Status -------------------------------------------------------------

    /// @brief The OPERation condition: calibrating while a null window is
    /// open, measuring while a capture runs.
    /// @return The condition register.
    std::uint32_t operationCondition() const noexcept;
    /// @brief The QUEStionable condition: current, time, calibration and the
    /// other bits of docs/scpi.md, as they stand.
    /// @return The condition register.
    std::uint32_t questionableCondition() const noexcept;
    /// @brief Moves each condition's edges, through its transition filters,
    /// into its event register.
    void latchStatus() noexcept;
    /// @brief The Status Byte, after latching the conditions.
    /// @return The register `*STB?` answers.
    std::uint32_t statusByte() noexcept;

    // --- Common and system commands ----------------------------------------

    /// @brief `*IDN?`: maker, model, serial and firmware version.
    /// @param[in,out] call The parsed unit.
    void cmdIdn( Call& call ) noexcept;
    /// @brief `*RST`: closes any window unstored and sets every setting to its
    /// reset value (docs/scpi.md).
    /// @param[in,out] call The parsed unit.
    void cmdReset( Call& call ) noexcept;
    /// @brief `*CLS`: clears the error queue, the ESR and both event registers.
    /// @param[in,out] call The parsed unit.
    void cmdClear( Call& call ) noexcept;
    /// @brief `*ESE <n>`: the Standard Event enable register.
    /// @param[in,out] call The parsed unit.
    void cmdEseSet( Call& call ) noexcept;
    /// @brief `*ESE?`.
    /// @param[in,out] call The parsed unit.
    void cmdEseQuery( Call& call ) noexcept;
    /// @brief `*ESR?`: reads and clears the Standard Event register.
    /// @param[in,out] call The parsed unit.
    void cmdEsrQuery( Call& call ) noexcept;
    /// @brief `*SRE <n>`: the service request enable register.
    /// @param[in,out] call The parsed unit.
    void cmdSreSet( Call& call ) noexcept;
    /// @brief `*SRE?`.
    /// @param[in,out] call The parsed unit.
    void cmdSreQuery( Call& call ) noexcept;
    /// @brief `*STB?`: the Status Byte.
    /// @param[in,out] call The parsed unit.
    void cmdStbQuery( Call& call ) noexcept;
    /// @brief `*OPC`: sets the ESR's operation-complete bit now, or when the
    /// window that ends by itself has ended.
    /// @param[in,out] call The parsed unit.
    void cmdOpc( Call& call ) noexcept;
    /// @brief `*OPC?`: answers `1`.
    /// @param[in,out] call The parsed unit.
    void cmdOpcQuery( Call& call ) noexcept;
    /// @brief `*WAI`: accepted, and immediate.
    /// @param[in,out] call The parsed unit.
    void cmdWait( Call& call ) noexcept;
    /// @brief `*TST?`: the self-test's verdict (selfTest()).
    /// @param[in,out] call The parsed unit.
    void cmdTest( Call& call ) noexcept;

    /// @brief `SYSTem:ERRor[:NEXT]?`: takes the oldest error from the queue.
    /// @param[in,out] call The parsed unit.
    void cmdErrorNext( Call& call ) noexcept;
    /// @brief `SYSTem:ERRor:COUNt?`: how many errors are queued.
    /// @param[in,out] call The parsed unit.
    void cmdErrorCount( Call& call ) noexcept;
    /// @brief `SYSTem:ERRor:ALL?`: takes every queued error, oldest first.
    /// @param[in,out] call The parsed unit.
    void cmdErrorAll( Call& call ) noexcept;
    /// @brief `SYSTem:VERSion?`: the SCPI version, `1999.0`.
    /// @param[in,out] call The parsed unit.
    void cmdVersion( Call& call ) noexcept;
    /// @brief `SYSTem:INFormation?`: the instrument's identity report.
    /// @param[in,out] call The parsed unit.
    void cmdInformation( Call& call ) noexcept;
    /// @brief `SYSTem:STATus?`: the sampler's live report; reads no register.
    /// @param[in,out] call The parsed unit.
    void cmdStatus( Call& call ) noexcept;
    /// @brief `SYSTem:COMMunicate:I2C:FREQuency <hz>`: the INA228 bus clock.
    /// @param[in,out] call The parsed unit.
    void cmdBusSet( Call& call ) noexcept;
    /// @brief `SYSTem:COMMunicate:I2C:FREQuency?`.
    /// @param[in,out] call The parsed unit.
    void cmdBusQuery( Call& call ) noexcept;
    /// @brief `SYSTem:LED?`: the carrier's LED, held dark, and its bring-up.
    /// @param[in,out] call The parsed unit.
    void cmdLed( Call& call ) noexcept;

    // --- SYSTem:PINS: the pin map (0.18.0, docs/scpi.md "The pin map") ----------

    /// @brief Appends @p map's twelve keys, in `SYSTem:PINS?`'s order: each
    /// role's GPIO (or `none`), the address (or `auto`), the boot clock, the
    /// three pulls and the two relays' active levels.
    /// @param[in,out] w   The unit's writer.
    /// @param[in]     map The map.
    static void writePinMap( text::Writer& w, const pinmap::PinMap& map ) noexcept;
    /// @brief `SYSTem:PINS?`: the live map, where it came from (`src`),
    /// whether its relays' wiring was checked (`verified`, relaysVerified_)
    /// and the board table's digest (`table`).
    /// @param[in,out] call The parsed unit.
    void cmdPinsQuery( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:PENDing?`: the staged map and validate()'s
    /// verdict on it (`valid`, `problem`, `at`).
    /// @param[in,out] call The parsed unit.
    void cmdPinsPending( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:STAGe <role>,<gpio>|NONE[,<option>]`: one role of
    /// the staged map; nothing on the board changes.
    /// @param[in,out] call The parsed unit.
    void cmdPinsStage( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:ADDRess AUTO|<address>`: the staged INA228 address.
    /// @param[in,out] call The parsed unit.
    void cmdPinsAddress( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:CLOCk <hz>`: the staged boot clock.
    /// @param[in,out] call The parsed unit.
    void cmdPinsClock( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:CLEar`: the staged map becomes the live one again.
    /// @param[in,out] call The parsed unit.
    void cmdPinsClear( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:APPLy`: the staged map, applied (applyPins()).
    /// @param[in,out] call The parsed unit.
    void cmdPinsApply( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:DEFault`: the default map, applied (applyPins()),
    /// and kept whatever its proof finds.
    /// @param[in,out] call The parsed unit.
    void cmdPinsDefault( Call& call ) noexcept;
    /// @brief `SYSTem:PINS:RESult?`: the last apply's outcome since the boot.
    /// @param[in,out] call The parsed unit.
    void cmdPinsResult( Call& call ) noexcept;

    /// What the last apply came to, as `SYSTem:PINS:RESult?` reports it.
    struct PinsOutcome {
        std::string_view result     = "none";  ///< `none`, `ok`, `reverted` or `refused`.
        std::string_view reason     = "-";     ///< What failed, or `-`.
        bool             identified = false;   ///< The INA228 answered: mfg and die are its.
        bool             tested     = false;   ///< The map was tried: tst is its proof's.
        bool             saved      = false;   ///< The map is the newest record.
        std::uint16_t    mfg        = 0;       ///< MANUFACTURER_ID as read on the tried map.
        std::uint16_t    die        = 0;       ///< DIEID, likewise.
        std::uint32_t    tst        = 0;       ///< partsTest() on the tried map.
    };

    /// What trying one map found.
    struct PinsTrial {
        pin_apply::Moved moved;         ///< What the move did.
        std::uint32_t    tst    = 0;    ///< partsTest() after it, bit 1 when the bring-up failed.
        std::string_view reason = "-";  ///< What failed, or `-`.
        /// The shunt's readings before and after a relay move, in picovolts,
        /// when the DUT's share was judged (`rest`'s `vs_pv=`).
        std::array<std::int64_t, 2> rest = {};
    };

    /// @brief Moves the instrument from one map to another and proves it
    /// there: the bring-up and partsTest(). The shunt range the device lost
    /// with its reset is put back.
    /// @param[in] from  The map it is on.
    /// @param[in] to    The map to move to.
    /// @param[in] range The shunt range.
    /// @return What it found.
    PinsTrial tryPins( const pinmap::PinMap& from, const pinmap::PinMap& to,
                       ina228::ShuntRange range ) noexcept;
    /// @brief Whether @p target may not be applied now: a relay that is not at
    /// rest, or a rule of validate() it breaks. Queues the one error and
    /// records the refusal.
    /// @param[in] target The map.
    /// @return true when it is refused.
    bool refusePins( const pinmap::PinMap& target ) noexcept;
    /// @brief Applies @p target: tries it, saves it once it is proved, and on
    /// any failure puts the last good map back, with one error.
    /// @param[in] target   The map.
    /// @param[in] defaults Kept whatever the proof finds, and whether or not
    ///                     the flash takes it: the way back
    ///                     (`SYSTem:PINS:DEFault`).
    void applyPins( const pinmap::PinMap& target, bool defaults ) noexcept;
    /// @brief Queues the one `117` of a map that was put back.
    /// @param[in] target The map that failed.
    /// @param[in] trial  What trying it found.
    /// @param[in] back   What the last good map's proof found.
    void failReverted( const pinmap::PinMap& target, const PinsTrial& trial,
                       const PinsTrial& back ) noexcept;

    /// @brief `STATus:OPERation[:EVENt]?`: reads and clears the event register.
    /// @param[in,out] call The parsed unit.
    void cmdOperEvent( Call& call ) noexcept;
    /// @brief `STATus:OPERation:CONDition?`.
    /// @param[in,out] call The parsed unit.
    void cmdOperCondition( Call& call ) noexcept;
    /// @brief `STATus:OPERation:ENABle <mask>`.
    /// @param[in,out] call The parsed unit.
    void cmdOperEnable( Call& call ) noexcept;
    /// @brief `STATus:OPERation:ENABle?`.
    /// @param[in,out] call The parsed unit.
    void cmdOperEnableQuery( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable[:EVENt]?`: reads and clears the event
    /// register.
    /// @param[in,out] call The parsed unit.
    void cmdQuesEvent( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable:CONDition?`.
    /// @param[in,out] call The parsed unit.
    void cmdQuesCondition( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable:ENABle <mask>`.
    /// @param[in,out] call The parsed unit.
    void cmdQuesEnable( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable:ENABle?`.
    /// @param[in,out] call The parsed unit.
    void cmdQuesEnableQuery( Call& call ) noexcept;
    /// @brief `STATus:PRESet`: SCPI's preset of the enable and transition
    /// registers.
    /// @param[in,out] call The parsed unit.
    void cmdStatusPreset( Call& call ) noexcept;

    // --- SENSe, SAMPle, FORMat, TRIGger -------------------------------------

    /// @brief `SENSe:PROFile TRACe|STATe|SLEep|BURSt`: the sampling profile.
    /// @param[in,out] call The parsed unit.
    void cmdProfileSet( Call& call ) noexcept;
    /// @brief `SENSe:PROFile?`.
    /// @param[in,out] call The parsed unit.
    void cmdProfileQuery( Call& call ) noexcept;
    /// @brief `SENSe:PROFile:REPort?`: the live profile's settings.
    /// @param[in,out] call The parsed unit.
    void cmdProfileReport( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:RANGe[:UPPer] <amps>|MIN|MAX|DEF`: the smallest
    /// range that holds the value.
    /// @param[in,out] call The parsed unit.
    void cmdRangeSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:RANGe[:UPPer]?`: the range's full scale, in amps.
    /// @param[in,out] call The parsed unit.
    void cmdRangeQuery( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:RANGe:INDex 0|1`: the INA228's `ADCRANGE`.
    /// @param[in,out] call The parsed unit.
    void cmdRangeIndexSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:RANGe:INDex?`.
    /// @param[in,out] call The parsed unit.
    void cmdRangeIndexQuery( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:RANGe:REPort?`: the range and its last switch.
    /// @param[in,out] call The parsed unit.
    void cmdRangeReport( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:SHUNt <ohms>`: the shunt of both ranges, stored.
    /// @param[in,out] call The parsed unit.
    void cmdShuntSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:SHUNt?`: the common shunt value, in ohms.
    /// @param[in,out] call The parsed unit.
    void cmdShuntQuery( Call& call ) noexcept;
    /// @brief Reads the range a `SENSe:CURRent:SHUNt:RANGe<n>` header names;
    /// queues -114 and returns false when it names none, or no range there is.
    /// @param[in]  call  The parsed unit, its suffix the range.
    /// @param[out] range The range named.
    /// @retval true  @p range holds the range.
    /// @retval false The suffix was refused (an error is queued).
    bool shuntRangeSuffix( const Call& call, ina228::ShuntRange& range ) noexcept;
    /// @brief `SENSe:CURRent:SHUNt:RANGe<n> <ohms>`: range `n`'s own shunt.
    /// @param[in,out] call The parsed unit.
    void cmdShuntRangeSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:SHUNt:RANGe<n>?`: the shunt range `n` converts
    /// with.
    /// @param[in,out] call The parsed unit.
    void cmdShuntRangeQuery( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:APERture?`: the shunt conversion time, seconds.
    /// @param[in,out] call The parsed unit.
    void cmdCurrentAperture( Call& call ) noexcept;
    /// @brief `SENSe:VOLTage:APERture?`: the bus conversion time, seconds.
    /// @param[in,out] call The parsed unit.
    void cmdVoltageAperture( Call& call ) noexcept;
    /// @brief `SENSe:TEMPerature:APERture?`: the die conversion time, seconds.
    /// @param[in,out] call The parsed unit.
    void cmdTemperatureAperture( Call& call ) noexcept;
    /// @brief `SENSe:AVERage:COUNt?`: the hardware averaging it converts with.
    /// @param[in,out] call The parsed unit.
    void cmdAverageCount( Call& call ) noexcept;
    /// @brief Sets the live profile's averaging override, or queues the
    /// sampler's refusal.
    /// @param[in] wanted The override.
    void setAveraging( const Averaging& wanted ) noexcept;
    /// @brief `SENSe:AVERage[:STATe] ON|OFF`: the live profile's averaging
    /// override on or off.
    /// @param[in,out] call The parsed unit.
    void cmdAverageSet( Call& call ) noexcept;
    /// @brief `SENSe:AVERage[:STATe]?`: whether the override is on.
    /// @param[in,out] call The parsed unit.
    void cmdAverageQuery( Call& call ) noexcept;
    /// @brief `SENSe:AVERage:COUNt <n>`: the override's averaging count, one
    /// of the INA228's eight.
    /// @param[in,out] call The parsed unit.
    void cmdAverageCountSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:APERture <seconds>`: the override's shunt
    /// conversion time, one of the INA228's eight.
    /// @param[in,out] call The parsed unit.
    void cmdCurrentApertureSet( Call& call ) noexcept;
    /// @brief `SENSe:FUNCtion:ON "CURR"[,...]`: the channels `INITiate`
    /// streams.
    /// @param[in,out] call The parsed unit.
    void cmdFunctionOn( Call& call ) noexcept;
    /// @brief `SENSe:FUNCtion:ON?`.
    /// @param[in,out] call The parsed unit.
    void cmdFunctionOnQuery( Call& call ) noexcept;
    /// @brief `SENSe:FUNCtion:ON:DEFault`: back to the profile's channels.
    /// @param[in,out] call The parsed unit.
    void cmdFunctionDefault( Call& call ) noexcept;
    /// @brief `SENSe:CHARge?`: the hardware charge accumulator's report.
    /// @param[in,out] call The parsed unit.
    void cmdCharge( Call& call ) noexcept;
    /// @brief `SENSe:CHARge:RESet`: clears the accumulators, starts a window.
    /// @param[in,out] call The parsed unit.
    void cmdChargeReset( Call& call ) noexcept;
    /// @brief `SAMPle:TIMer?`: the profile's sample period, seconds.
    /// @param[in,out] call The parsed unit.
    void cmdSampleTimer( Call& call ) noexcept;
    /// @brief `SAMPle:COUNt <n>`: the samples of one acquisition.
    /// @param[in,out] call The parsed unit.
    void cmdSampleCountSet( Call& call ) noexcept;
    /// @brief `SAMPle:COUNt?`.
    /// @param[in,out] call The parsed unit.
    void cmdSampleCountQuery( Call& call ) noexcept;
    /// @brief `FORMat[:DATA] TEXT|CSV`: the stream record format.
    /// @param[in,out] call The parsed unit.
    void cmdFormatSet( Call& call ) noexcept;
    /// @brief `FORMat[:DATA]?`.
    /// @param[in,out] call The parsed unit.
    void cmdFormatQuery( Call& call ) noexcept;
    /// @brief `TRIGger:SOURce IMMediate|LEVel`: what starts a ring's record.
    /// @param[in,out] call The parsed unit.
    void cmdTriggerSourceSet( Call& call ) noexcept;
    /// @brief `TRIGger:SOURce?`.
    /// @param[in,out] call The parsed unit.
    void cmdTriggerSourceQuery( Call& call ) noexcept;

    // --- CONFigure, MEASure, READ, FETCh ------------------------------------

    /// @brief `CONFigure[:SCALar]:CURRent[:DC] [<range>]`.
    /// @param[in,out] call The parsed unit.
    void cmdConfCurrent( Call& call ) noexcept;
    /// @brief `CONFigure[:SCALar]:VOLTage[:DC]`.
    /// @param[in,out] call The parsed unit.
    void cmdConfVoltage( Call& call ) noexcept;
    /// @brief `CONFigure[:SCALar]:POWer[:DC]`.
    /// @param[in,out] call The parsed unit.
    void cmdConfPower( Call& call ) noexcept;
    /// @brief `CONFigure[:SCALar]:ENERgy`.
    /// @param[in,out] call The parsed unit.
    void cmdConfEnergy( Call& call ) noexcept;
    /// @brief `CONFigure[:SCALar]:TEMPerature`.
    /// @param[in,out] call The parsed unit.
    void cmdConfTemperature( Call& call ) noexcept;
    /// @brief `CONFigure?`: the configured function.
    /// @param[in,out] call The parsed unit.
    void cmdConfQuery( Call& call ) noexcept;
    /// @brief `MEASure[:SCALar]:CURRent[:DC]? [<range>]`.
    /// @param[in,out] call The parsed unit.
    void cmdMeasCurrent( Call& call ) noexcept;
    /// @brief `MEASure[:SCALar]:VOLTage[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdMeasVoltage( Call& call ) noexcept;
    /// @brief `MEASure[:SCALar]:POWer[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdMeasPower( Call& call ) noexcept;
    /// @brief `MEASure[:SCALar]:ENERgy?`.
    /// @param[in,out] call The parsed unit.
    void cmdMeasEnergy( Call& call ) noexcept;
    /// @brief `MEASure[:SCALar]:TEMPerature?`.
    /// @param[in,out] call The parsed unit.
    void cmdMeasTemperature( Call& call ) noexcept;
    /// @brief `READ?`: one acquisition, then the configured function.
    /// @param[in,out] call The parsed unit.
    void cmdRead( Call& call ) noexcept;
    /// @brief `READ[:SCALar]:CURRent[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdReadCurrent( Call& call ) noexcept;
    /// @brief `READ[:SCALar]:VOLTage[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdReadVoltage( Call& call ) noexcept;
    /// @brief `READ[:SCALar]:POWer[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdReadPower( Call& call ) noexcept;
    /// @brief `READ[:SCALar]:ENERgy?`.
    /// @param[in,out] call The parsed unit.
    void cmdReadEnergy( Call& call ) noexcept;
    /// @brief `READ[:SCALar]:TEMPerature?`.
    /// @param[in,out] call The parsed unit.
    void cmdReadTemperature( Call& call ) noexcept;
    /// @brief `FETCh?`: the configured function over the last acquisition.
    /// @param[in,out] call The parsed unit.
    void cmdFetch( Call& call ) noexcept;
    /// @brief `FETCh[:SCALar]:CURRent[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdFetchCurrent( Call& call ) noexcept;
    /// @brief `FETCh[:SCALar]:VOLTage[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdFetchVoltage( Call& call ) noexcept;
    /// @brief `FETCh[:SCALar]:POWer[:DC]?`.
    /// @param[in,out] call The parsed unit.
    void cmdFetchPower( Call& call ) noexcept;
    /// @brief `FETCh[:SCALar]:ENERgy?`.
    /// @param[in,out] call The parsed unit.
    void cmdFetchEnergy( Call& call ) noexcept;
    /// @brief `FETCh[:SCALar]:CHARge?`.
    /// @param[in,out] call The parsed unit.
    void cmdFetchCharge( Call& call ) noexcept;
    /// @brief `FETCh[:SCALar]:TEMPerature?`.
    /// @param[in,out] call The parsed unit.
    void cmdFetchTemperature( Call& call ) noexcept;
    /// @brief `FETCh:STATistics?`: the last closed capture's report.
    /// @param[in,out] call The parsed unit.
    void cmdFetchStatistics( Call& call ) noexcept;
    /// @brief `FETCh:STATistics:BATCh?`: the last closed capture's batch means.
    /// @param[in,out] call The parsed unit.
    void cmdFetchBatches( Call& call ) noexcept;
    /// @brief `ROUTe:LEAD:CYCLe <ms>,FORCe[,LIVE]`: K1 opened, held @p ms and
    /// reclosed by the instrument (0.13.0).
    /// @param[in,out] call The parsed unit.
    void cmdLeadCycle( Call& call ) noexcept;
    /// @brief Recloses the lead once a cycle's time is up.
    void serviceLeadCycle() noexcept;
    /// @brief Checks the console in with the watchdog (watchdog.h), at most
    /// every 100 ms (0.14.0), and then blocks for one tick so that Core 0's
    /// idle task, which the watchdog also waits for, runs even under work
    /// that never blocks (FW-11).
    void feedWatchdog() noexcept;
    /// @brief Queues 107 once when a running capture takes no sample for 2 s
    /// (0.14.0).
    void superviseSampler() noexcept;
    /// @brief What every capture's start clears: the last capture's missed
    /// period and a stall.
    void noteCaptureStart() noexcept;
    /// @brief `DIAGnostic:WATChdog?`: the watchdog as the instrument runs it
    /// (0.14.0; the own timer of watchdog.h from FW-11).
    /// @param[in,out] call The parsed unit.
    void cmdWatchdogQuery( Call& call ) noexcept;
    /// @brief `DIAGnostic:WATChdog:TEST`: stops checking in with the
    /// watchdog, which resets the instrument (0.14.0).
    /// @param[in,out] call The parsed unit.
    [[noreturn]] void cmdWatchdogTest( Call& call ) noexcept;
    /// @brief How long the loop may wait for input: kReadTimeoutMs, or less
    /// when a lead cycle is due sooner.
    /// @return The wait, in milliseconds.
    std::uint32_t readWaitMs() const noexcept;
    /// @brief `SYSTem:HEALth?`: what tells a healthy instrument from one that
    /// rebooted, lost a wire or runs another image (0.12.0).
    /// @param[in,out] call The parsed unit.
    void cmdHealth( Call& call ) noexcept;
    /// @brief The self-test's verdict, one bit per part that failed (`*TST?`).
    /// @return 0 when every part passed.
    std::uint32_t selfTest() noexcept;
    /// @brief The self-test's parts a pin map reaches: the INA228 (1, 8, 16),
    /// the sampling engine (2), the relay inputs (4, 64) and the ALERT wire
    /// (32).
    /// @return 0, or one bit per part that failed, as `*TST?` numbers them.
    std::uint32_t partsTest() noexcept;
    /// @brief `STATus:QUEStionable:PTRansition <mask>` and the others of the
    /// four transition filters: which edges of a condition bit reach its event
    /// register (0.12.0).
    /// @param[in,out] call The parsed unit.
    void cmdQuesPtrSet( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable:PTRansition?`.
    /// @param[in,out] call The parsed unit.
    void cmdQuesPtrQuery( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable:NTRansition <mask>`.
    /// @param[in,out] call The parsed unit.
    void cmdQuesNtrSet( Call& call ) noexcept;
    /// @brief `STATus:QUEStionable:NTRansition?`.
    /// @param[in,out] call The parsed unit.
    void cmdQuesNtrQuery( Call& call ) noexcept;
    /// @brief `STATus:OPERation:PTRansition <mask>`.
    /// @param[in,out] call The parsed unit.
    void cmdOperPtrSet( Call& call ) noexcept;
    /// @brief `STATus:OPERation:PTRansition?`.
    /// @param[in,out] call The parsed unit.
    void cmdOperPtrQuery( Call& call ) noexcept;
    /// @brief `STATus:OPERation:NTRansition <mask>`.
    /// @param[in,out] call The parsed unit.
    void cmdOperNtrSet( Call& call ) noexcept;
    /// @brief `STATus:OPERation:NTRansition?`.
    /// @param[in,out] call The parsed unit.
    void cmdOperNtrQuery( Call& call ) noexcept;
    /// @brief Reads a 16-bit register mask parameter into @p mask.
    /// @param[in,out] call  The parsed unit.
    /// @param[out]    mask  The mask, 0..32767.
    /// @param[in]     usage The usage text of a refusal.
    /// @retval true  @p mask holds the value.
    /// @retval false It was refused (an error is queued).
    bool maskParameter( Call& call, std::uint32_t& mask, const char* usage ) noexcept;
    /// @brief `TRIGger:LEVel <amps>`.
    /// @param[in,out] call The parsed unit.
    void cmdTriggerLevelSet( Call& call ) noexcept;
    /// @brief `TRIGger:LEVel?`.
    /// @param[in,out] call The parsed unit.
    void cmdTriggerLevelQuery( Call& call ) noexcept;
    /// @brief `TRIGger:SLOPe POSitive|NEGative`.
    /// @param[in,out] call The parsed unit.
    void cmdTriggerSlopeSet( Call& call ) noexcept;
    /// @brief `TRIGger:SLOPe?`.
    /// @param[in,out] call The parsed unit.
    void cmdTriggerSlopeQuery( Call& call ) noexcept;
    /// @brief `TRIGger:PRETrigger <samples>`.
    /// @param[in,out] call The parsed unit.
    void cmdPretriggerSet( Call& call ) noexcept;
    /// @brief `TRIGger:PRETrigger?`.
    /// @param[in,out] call The parsed unit.
    void cmdPretriggerQuery( Call& call ) noexcept;
    /// @brief `SENSe:CAPTure:LIMit ON|OFF`: whether `INITiate` ends itself
    /// after `SAMPle:COUNt` samples.
    /// @param[in,out] call The parsed unit.
    void cmdCaptureLimitSet( Call& call ) noexcept;
    /// @brief `SENSe:CAPTure:LIMit?`.
    /// @param[in,out] call The parsed unit.
    void cmdCaptureLimitQuery( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:THReshold:MARK ON|OFF`.
    /// @param[in,out] call The parsed unit.
    void cmdThresholdMarkSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:THReshold:MARK?`.
    /// @param[in,out] call The parsed unit.
    void cmdThresholdMarkQuery( Call& call ) noexcept;
    /// @brief Writes a mark recorded on the open capture: through the stream
    /// queue behind the samples already queued when the capture streams, so
    /// it never precedes a sample of a lower index; at once otherwise.
    /// @param[in] mark The mark as recorded (Sampler::addMark()).
    void emitMark( const Mark& mark ) noexcept;
    /// @brief Writes a queued mark record of a streamed capture.
    /// @param[in] record The record (RecordKind other than Conversion).
    void printRecord( const Sample& record ) noexcept;
    /// @brief Sets `*OPC`'s bit once the window it waits for has ended.
    void serviceOperationComplete() noexcept;
    /// @brief Appends the 0.10.0 keys of `FETCh:STATistics?` to its unit.
    /// @param[in,out] w     The writer composing the unit.
    /// @param[in]     stats The closed capture's statistics.
    void appendStatistics( text::Writer& w, const CaptureStats& stats ) noexcept;
    /// @brief `SENSe:CAPTure DATA|STATistics`: what `INITiate` keeps.
    /// @param[in,out] call The parsed unit.
    void cmdCaptureSet( Call& call ) noexcept;
    /// @brief `SENSe:CAPTure?`.
    /// @param[in,out] call The parsed unit.
    void cmdCaptureQuery( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:THReshold <amps>|OFF`: the level the statistics
    /// count against.
    /// @param[in,out] call The parsed unit.
    void cmdThresholdSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:THReshold?`.
    /// @param[in,out] call The parsed unit.
    void cmdThresholdQuery( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:THReshold:HYSTeresis <amps>`.
    /// @param[in,out] call The parsed unit.
    void cmdHysteresisSet( Call& call ) noexcept;
    /// @brief `SENSe:CURRent:THReshold:HYSTeresis?`.
    /// @param[in,out] call The parsed unit.
    void cmdHysteresisQuery( Call& call ) noexcept;
    /// @brief `FETCh:HEADer?`: the last started capture's facts as one report
    /// unit, in the key order docs/scpi.md gives; `-230` before the first
    /// capture since boot.
    /// @param[in,out] call The parsed unit (it takes no parameter).
    void cmdFetchHeader( Call& call ) noexcept;

    /// @brief `CONFigure:<function> [<range>]`: the function `READ?` and
    /// `FETCh?` report, the stream channels it implies, and the range when one
    /// is given.
    /// @param[in] function The function the header names.
    /// @param[in,out] call The parsed unit; its optional parameter is the range.
    void configure( Function function, Call& call ) noexcept;
    /// @brief Selects STATE when @p function needs the bus voltage and the
    /// live profile does not convert it (`CONFigure`, `MEASure`; 0.12.0).
    /// @param[in] function The function being configured.
    void selectBusProfile( Function function ) noexcept;

    /// @brief MEASure (@p configureFirst) and READ: one acquisition of
    /// SAMPle:COUNt samples on the current profile, then the mean of
    /// @p function.
    /// @param[in]     function       The function to report.
    /// @param[in,out] call           The parsed unit; MEASure:CURRent's
    ///                               optional parameter is the range.
    /// @param[in]     configureFirst True for MEASure: configure, then read.
    void measure( Function function, Call& call, bool configureFirst ) noexcept;

    /// @brief The figure of @p function over the last acquisition, written as
    /// a response unit.
    /// @param[in] function The function to report.
    /// @return false (and an error queued) when it has none.
    bool reportFunction( Function function ) noexcept;

    /// Runs one acquisition without streaming it. @return false with an
    /// error queued when the sampler refused or the samples never came.
    bool acquire() noexcept;

    /// @brief Applies a range parameter (amps, MIN, MAX, DEF) at @p index, if
    /// present.
    /// @param[in,out] call  The parsed unit.
    /// @param[in]     index The parameter's position.
    /// @retval true  No parameter there, or the range it names is selected.
    /// @retval false It was refused or the switch failed (an error is queued).
    bool applyRangeParameter( Call& call, std::size_t index ) noexcept;

    /// @brief Selects @p range, with the discard and CNVRF check of a real
    /// switch.
    /// @param[in] range The range.
    /// @retval true  The range is selected.
    /// @retval false The I2C write failed (an error is queued).
    bool selectRange( ina228::ShuntRange range ) noexcept;

    // --- INITiate, ABORt ------------------------------------------------------

    /// @brief `INITiate[:IMMediate]`: starts a capture on the current profile.
    /// @param[in,out] call The parsed unit.
    void cmdInitiate( Call& call ) noexcept;
    /// @brief `INITiate:FORCe`: the same on a bypassed shunt, `q=invalid`.
    /// @param[in,out] call The parsed unit.
    void cmdInitiateForce( Call& call ) noexcept;
    /// @brief `ABORt`: closes the open window, the one way any window closes.
    /// @param[in,out] call The parsed unit.
    void cmdAbort( Call& call ) noexcept;
    /// @brief Starts a capture: refuses a bypassed shunt unless @p force,
    /// records the header facts and the circuit, and starts the sampler.
    /// @param[in] force True for `INITiate:FORCe`.
    void startCapture( bool force ) noexcept;

    /// Closes an open null window: computes the null, stores it, keeps the
    /// result for CALibration:ZERO:RESult?.
    void finishZeroRun() noexcept;

    // --- TRACe ----------------------------------------------------------------

    /// @brief `TRACe:MARK "<label>"`: marks the running capture's current
    /// sample.
    /// @param[in,out] call The parsed unit.
    void cmdMark( Call& call ) noexcept;
    /// @brief `TRACe:DATA? [SCALed|RAW][,<from>[,<count>]]`: the ring as a
    /// definite-length block.
    /// @param[in,out] call The parsed unit.
    void cmdTraceData( Call& call ) noexcept;
    /// @brief `TRACe:POINts?`: the samples in the ring.
    /// @param[in,out] call The parsed unit.
    void cmdTracePoints( Call& call ) noexcept;
    /// @brief `TRACe:POINts:MAXimum?`: the ring's capacity.
    /// @param[in,out] call The parsed unit.
    void cmdTracePointsMax( Call& call ) noexcept;

    /// @brief Writes the dump rows of [@p from, @p from + @p count). Run twice
    /// by cmdTraceData(): once counting bytes for the block header, once
    /// writing.
    /// @param[in] raw   True for `RAW` rows, false for scaled ones.
    /// @param[in] from  The first sample, as an offset into the ring.
    /// @param[in] count How many samples.
    void writeDump( bool raw, std::uint32_t from, std::uint32_t count ) noexcept;

    // --- CALibration ----------------------------------------------------------

    /// @brief `CALibration:ZERO <seconds>`: opens a null window.
    /// @param[in,out] call The parsed unit.
    void cmdZeroStart( Call& call ) noexcept;
    /// @brief `CALibration:ZERO:RESult?`: the open window, or the last null.
    /// @param[in,out] call The parsed unit.
    void cmdZeroResult( Call& call ) noexcept;
    /// @brief `CALibration:TAG "<text>"`: the calibration tag, stored.
    /// @param[in,out] call The parsed unit.
    void cmdTagSet( Call& call ) noexcept;
    /// @brief `CALibration:TAG?`.
    /// @param[in,out] call The parsed unit.
    void cmdTagQuery( Call& call ) noexcept;
    /// @brief `CALibration:DATE <year>,<month>,<day>`: the calibration's
    /// date, stored.
    /// @param[in,out] call The parsed unit.
    void cmdDateSet( Call& call ) noexcept;
    /// @brief `CALibration:DATE?`.
    /// @param[in,out] call The parsed unit.
    void cmdDateQuery( Call& call ) noexcept;
    /// @brief `CALibration:VBUSside SOURce|UNKNown`: where `VBUS` is wired.
    /// @param[in,out] call The parsed unit.
    void cmdVbusSideSet( Call& call ) noexcept;
    /// @brief `CALibration:VBUSside?`.
    /// @param[in,out] call The parsed unit.
    void cmdVbusSideQuery( Call& call ) noexcept;
    /// @brief `CALibration:DATA?`: the stored calibration's report.
    /// @param[in,out] call The parsed unit.
    void cmdCalData( Call& call ) noexcept;
    /// @brief `CALibration:SHUNt:REFerence <ohms>`: the shunt calibration's
    /// reference resistor.
    /// @param[in,out] call The parsed unit.
    void cmdShuntReferenceSet( Call& call ) noexcept;
    /// @brief `CALibration:SHUNt:REFerence?`.
    /// @param[in,out] call The parsed unit.
    void cmdShuntReferenceQuery( Call& call ) noexcept;
    /// @brief `CALibration:SHUNt[:MEASure]`: one acquisition against the
    /// reference resistor.
    /// @param[in,out] call The parsed unit.
    void cmdShuntMeasure( Call& call ) noexcept;
    /// @brief `CALibration:SHUNt:RESult?`: the last shunt point.
    /// @param[in,out] call The parsed unit.
    void cmdShuntResult( Call& call ) noexcept;
    /// @brief Writes the calibration to flash; queues `106` when the write
    /// fails (the value stays live until reboot).
    void saveCalibration() noexcept;

    // --- DIAGnostic -----------------------------------------------------------

    /// @brief `DIAGnostic:ALERt?`: the INA228's `DIAG_ALRT`, decoded.
    /// @param[in,out] call The parsed unit.
    void cmdDiagAlert( Call& call ) noexcept;
    /// @brief `DIAGnostic:I2C:SCAN?`: the addresses of 0x40 to 0x4F that
    /// acknowledge.
    /// @param[in,out] call The parsed unit.
    void cmdDiagScan( Call& call ) noexcept;
    /// @brief `DIAGnostic:REGister? <addr>`: one INA228 register.
    /// @param[in,out] call The parsed unit.
    void cmdDiagRegister( Call& call ) noexcept;
    /// @brief `DIAGnostic:HEAP?`: the heap monitor's counts and the heap.
    /// @param[in,out] call The parsed unit.
    void cmdDiagHeap( Call& call ) noexcept;
    /// @brief `DIAGnostic:HEAP:CENSus?`, a census build's alone: the ring's
    /// size and how much of it is held (heap::censusSummary()).
    /// @param[in,out] call The parsed unit.
    void cmdDiagHeapCensus( Call& call ) noexcept;
    /// @brief `DIAGnostic:HEAP:CENSus:EVENt? <n>`, a census build's alone:
    /// one census event, `-222` when it is not held.
    /// @param[in,out] call The parsed unit.
    void cmdDiagHeapCensusEvent( Call& call ) noexcept;

    // --- ROUTe ----------------------------------------------------------------

    /// @brief `ROUTe:LEAD OPEN|CLOSe[,FORCe][,LIVE]`: K1.
    /// @param[in,out] call The parsed unit.
    void cmdLead( Call& call ) noexcept;
    /// @brief `ROUTe:BYPass ON|OFF[,FORCe][,LIVE]`: K2.
    /// @param[in,out] call The parsed unit.
    void cmdBypass( Call& call ) noexcept;
    /// @brief `ROUTe:NULLpath ON|OFF[,LIVE[,FORCe]]`: K2 and K1,
    /// make-before-break, checked.
    /// @param[in,out] call The parsed unit.
    void cmdNullPath( Call& call ) noexcept;
    /// @brief `ROUTe:RELay3|RELay4 ON|OFF[,LIVE]`: the spares.
    /// @param[in,out] call The parsed unit.
    void cmdRelay( Call& call ) noexcept;
    /// @brief `ROUTe:STATe?`: the circuit, the hold and the relay parts.
    /// @param[in,out] call The parsed unit.
    void cmdRouteState( Call& call ) noexcept;
    /// @brief `ROUTe:RESult?`: the circuit and the last completed order.
    /// @param[in,out] call The parsed unit.
    void cmdRouteResult( Call& call ) noexcept;

    /// Which orders take `FORCe`, the override of an interlock.
    enum class ForceRule : std::uint8_t {
        Always,    ///< K1 and K2: disconnecting the DUT, or bypassing a capture's shunt.
        WithLive,  ///< NULLpath: no plan of it disconnects, so only a capture's shunt.
    };

    /// @brief Reads the `FORCe` / `LIVE` options from parameter 1 on.
    /// @param[in,out] call  The parsed unit.
    /// @param[in]     rule  Which orders take `FORCe`.
    /// @param[out]    force Whether `FORCe` was given.
    /// @param[out]    live  Whether `LIVE` was given.
    /// @param[in]     usage The usage text of a refusal.
    /// @retval true  The options are read.
    /// @retval false One was refused (an error is queued).
    bool relayOptions( Call& call, ForceRule rule, bool& force, bool& live,
                       const char* usage ) noexcept;

    /// @brief Runs @p plan: the interlocks, then each step and its settle
    /// time -- and, when @p checked, a shunt reading after each one (relay.h
    /// "Checking a null path") -- then keeps the result for ROUTe:RESult?.
    /// @param[in] plan    The steps.
    /// @param[in] force   `FORCe` was given.
    /// @param[in] live    `LIVE` was given.
    /// @param[in] checked Read the shunt before and after each step.
    void executeRelayOrder( const relay::Plan& plan, bool force, bool live, bool checked ) noexcept;

    /// @brief Puts the measuring circuit back from any state, make-before-break,
    /// and forgets the null path's evidence.
    void releaseNullPath() noexcept;
    /// @brief One fast shunt reading for a null path's check, the profile's
    /// ADC configuration written back after it.
    /// @param[out] picovolts The shunt voltage, in picovolts.
    /// @retval true  @p picovolts holds the reading.
    /// @retval false An I2C transaction failed.
    bool readShuntPicovolts( std::int64_t& picovolts ) noexcept;
    /// @brief Arms the relay hold when the circuit is not the measuring one,
    /// with its whole time from now; disarms it otherwise.
    void updateHold() noexcept;
    /// @brief Releases the circuit once the hold's time is up; a null window
    /// that samples gives it its whole time again.
    void serviceHold() noexcept;
    /// The hold's teardown (0.16.0): every coil back to rest,
    /// make-before-break, each move marked in a capture that is sampling,
    /// and the release queued as its error.
    void releaseHeld() noexcept;
    /// @brief The seconds left on the hold, or `-` when it is not armed.
    /// @return The text, a view of holdText_.
    std::string_view holdText() noexcept;
    /// @brief Waits out a relay's settle time, pumping the stream and feeding
    /// the watchdog meanwhile.
    /// @param[in] milliseconds The settle time.
    void settle( std::uint32_t milliseconds ) noexcept;
    /// @brief The relay tokens of a report (relay::formatState()).
    /// @param[in] state    The energized coils.
    /// @param[in] evidence What the null path's check saw.
    /// @return The text, a view of relayText_.
    std::string_view relayTokens( relay::Mask state, relay::Evidence evidence ) noexcept;

    /// @brief The live quality token, judged against the last die temperature
    /// seen.
    /// @return `invalid` on a bypassed shunt, else the calibration's verdict.
    Quality liveQuality() const noexcept;

    /// @brief The firmware version, read back from the image's own description
    /// so that firmware/version.txt stays the single source.
    /// @return The version, or `-` when the image carries none.
    static std::string_view firmwareVersion() noexcept;

    /// @brief The channel mask INITiate streams: SENSe:FUNCtion:ON's, or the
    /// profile's default when none was chosen.
    /// @return The mask (Channel bits).
    std::uint32_t streamMask() const noexcept;

    // --- State ----------------------------------------------------------------

    Ina228Driver*     device_      = nullptr;  ///< The INA228, from begin().
    Sampler*          sampler_     = nullptr;  ///< The sampling engine, from begin().
    CalibrationStore* calibration_ = nullptr;  ///< The calibration store, from begin().
    relay::Bank*      relays_      = nullptr;  ///< The relay bank, the console's alone.

    pin_boot::Live* pins_ = nullptr;  ///< The pin map the instrument runs.


    /// The instrument's serial (identity.h), read once by begin(); `0` until
    /// then, and when the eFuse read fails.
    char serial_[identity::kSerialLength + 1] = "0";

    scpi::ErrorQueue   errors_;    ///< The error queue (`SYSTem:ERRor`).
    scpi::PathResolver resolver_;  ///< The header path of the message being run.

    std::uint32_t esr_        = 0;  ///< The Standard Event register (`*ESR?`).
    std::uint32_t ese_        = 0;  ///< Its enable (`*ESE`).
    std::uint32_t sre_        = 0;  ///< The service request enable (`*SRE`).
    std::uint32_t operEvent_  = 0;  ///< The OPERation event register.
    std::uint32_t operEnable_ = 0;  ///< Its enable.
    std::uint32_t operLast_   = 0;  ///< The OPERation condition last latched.
    std::uint32_t quesEvent_  = 0;  ///< The QUEStionable event register.
    std::uint32_t quesEnable_ = 0;  ///< Its enable.
    std::uint32_t quesLast_   = 0;  ///< The QUEStionable condition last latched.

    /// The transition filters (0.12.0): a condition bit's rise reaches its
    /// event register through PTR, its fall through NTR. SCPI's preset: every
    /// rise, no fall.
    std::uint32_t operPtr_ = 0x7FFF;
    std::uint32_t operNtr_ = 0;       ///< OPERation's fall filter.
    std::uint32_t quesPtr_ = 0x7FFF;  ///< QUEStionable's rise filter.
    std::uint32_t quesNtr_ = 0;       ///< QUEStionable's fall filter.

    /// When the console last checked in with the watchdog, esp_timer
    /// microseconds (0.14.0).
    std::int64_t lastWatchdogFeedUs_ = 0;

    /// The sampler's supervision (0.14.0): the count last seen and since when,
    /// whether the open or last capture stalled, and how many stalls since boot.
    bool          stallWatching_  = false;
    std::uint32_t stallCount_     = 0;      ///< The sample count last seen.
    std::int64_t  stallSinceUs_   = 0;      ///< Since when it has not moved.
    bool          samplerStalled_ = false;  ///< The open or last capture stalled.
    std::uint32_t samplerStalls_  = 0;      ///< Stalls since boot.

    /// A lead cycle is pending, and when it recloses (0.13.0).
    bool         leadCycleArmed_ = false;
    std::int64_t leadCycleDueUs_ = 0;  ///< When it recloses, esp_timer microseconds.

    /// The last closed capture missed its period (`pace_unmet=1`), until the
    /// next capture starts: QUEStionable TIME (0.12.0).
    bool paceUnmet_ = false;

    /// The task run() runs on (ESP-IDF's main task), for its stack's
    /// high-water mark; null until run() begins.
    TaskHandle_t task_ = nullptr;

    StreamFormat  format_      = StreamFormat::Text;  ///< `FORMat:DATA`.
    Function      function_    = Function::Current;   ///< `CONFigure`'s function.
    std::uint32_t chosenMask_  = 0;                   ///< 0: the profile's default.
    std::uint32_t sampleCount_ = 100;                 ///< `SAMPle:COUNt`.

    /// `SENSe:CAPTure STATistics`: `INITiate` keeps the statistics only, no
    /// stream and no ring (Sampler::startStatistics()).
    bool statisticsOnly_ = false;

    /// `TRIGger`: the source, level, slope and pre-trigger the next ring
    /// capture starts with (0.11.0).
    TriggerSource triggerSource_  = TriggerSource::Immediate;
    std::int64_t  triggerLevelNa_ = 0;     ///< `TRIGger:LEVel`, nanoamps.
    bool          triggerRising_  = true;  ///< `TRIGger:SLOPe POSitive`.
    std::uint32_t pretrigger_     = 0;     ///< `TRIGger:PRETrigger`, samples.

    /// `SENSe:CAPTure:LIMit ON`: `INITiate` ends itself after `SAMPle:COUNt`.
    bool captureLimited_ = false;

    /// `*OPC` waits for the open window to end by itself.
    bool opcPending_ = false;

    /// The ring dump's CRC-32 over its rows before `METER_DUMP_END`, and the
    /// bytes it covers: running while `crcRunning_`.
    bool          crcRunning_ = false;
    std::uint32_t crc_        = 0;  ///< The running CRC-32.
    std::size_t   crcBytes_   = 0;  ///< The bytes it covers.

    /// Whether the last started capture was statistics-only, for its header.
    bool captureStatisticsOnly_ = false;

    /// The open (or last) capture: its header facts, its circuit, its sums.
    bool            haveCapture_            = false;
    CaptureInfo     captureInfo_            = {};                     ///< Its header facts.
    relay::Mask     captureRelays_          = relay::kAllReleased;    ///< Its coils at start.
    relay::Evidence captureEvidence_        = relay::Evidence::None;  ///< Its null-path check.
    bool            captureBypassedAtStart_ = false;                  ///< Bypassed when it started.

    /// True when the shunt did not carry the DUT's whole current
    /// (relay::shuntCarriesAll()) at the capture's start or after a `LIVE`
    /// order during it: its readings are then a fraction of the current, or
    /// none of it, and every report on it says `q=invalid`.
    bool captureBypassed_ = false;

    /// The last closed capture's statistics, for FETCh.
    bool         haveStats_   = false;
    CaptureStats lastStats_   = {};                ///< Its statistics.
    Quality      lastQuality_ = Quality::Invalid;  ///< Its quality token.
    Accumulator  accumulator_ = {};                ///< Its sums.

    /// The last range switch, for SENSe:CURRent:RANGe:REPort?.
    bool          lastRangeChanged_   = false;
    std::uint32_t lastRangeDiscardUs_ = 0;  ///< The time it discarded.
    std::uint32_t lastRangeConverted_ = 0;  ///< 1 when CNVRF was seen after it.

    /// The null window: its circuit at start, and the last result.
    relay::Mask     zeroRelays_       = relay::kAllReleased;
    relay::Evidence zeroEvidence_     = relay::Evidence::None;  ///< Its null-path check.
    std::uint32_t   zeroSeconds_      = 0;                      ///< Its length, seconds.
    bool            haveZero_         = false;                  ///< A null has closed since boot.
    ZeroResult      lastZero_         = {};                     ///< The last null.
    std::uint32_t   lastZeroRange_    = 0;                      ///< Its range index.
    bool            lastZeroStored_   = false;                  ///< Whether it was stored.
    relay::Mask     lastZeroRelays_   = relay::kAllReleased;    ///< Its coils.
    relay::Evidence lastZeroEvidence_ = relay::Evidence::None;  ///< Its null-path check.

    /// The shunt calibration (`CALibration:SHUNt`): the reference resistor and
    /// the last point measured against it.
    std::uint32_t       shuntReferenceMicroOhm_      = calmath::kDefaultReferenceMicroOhm;
    bool                haveShuntPoint_              = false;  ///< A point was measured.
    calmath::ShuntPoint shuntPoint_                  = {};     ///< The last point.
    std::uint32_t       shuntPointRange_             = 0;      ///< Its range index.
    std::int64_t        shuntPointNanovolts_         = 0;      ///< Its shunt voltage, nV.
    std::int64_t        shuntPointReferenceUv_       = 0;      ///< Its reference voltage, uV.
    std::uint32_t       shuntPointSamples_           = 0;      ///< Its samples.
    std::uint32_t       shuntPointReferenceMicroOhm_ = 0;      ///< Its reference, uohm.
    std::int32_t        shuntPointDieMc_             = kDieMilliCelsiusUnknown;  ///< Its die, m°C.

    /// What `NULLpath ON` saw of the circuit as it stands; None once any other
    /// order moves a contact.
    relay::Evidence evidence_ = relay::Evidence::None;

    /// The last relay order's outcome, for ROUTe:RESult?.
    bool          haveOrder_         = false;
    char          orderMoved_[16]    = {};  ///< The contacts it moved (`moved=`).
    std::uint64_t orderSettleUs_     = 0;   ///< Its settle time (`settle_us=`).
    std::uint32_t orderLive_         = 0;   ///< 1 when it moved under a capture.
    std::uint32_t orderMarked_       = 0;   ///< The marks it recorded.
    char          orderReadings_[72] = {};  ///< Its check's readings (`vs_pv=`).

    /// The null-path hold (relay.h kNullPathHoldS), in esp_timer microseconds,
    /// and how many times it has released the path by itself since boot.
    bool          holdArmed_      = false;
    std::int64_t  holdDeadlineUs_ = 0;  ///< When it releases.
    std::uint32_t holdReleases_   = 0;  ///< Its releases since boot.
    /// The hold's releases under the open or last capture while it sampled
    /// (0.16.0), until the next capture starts: QUEStionable bit 10; and the
    /// count the last closed capture kept, for `FETCh:STATistics?`.
    std::uint32_t holdTeardowns_     = 0;
    std::uint32_t lastHoldTeardowns_ = 0;  ///< The last closed capture's count.

    char relayText_[112] = {};  ///< relayTokens()'s text.
    char holdText_[16]   = {};  ///< holdText()'s text.

    BringUpResult bringUp_ = BringUpResult::NoDevice;  ///< The INA228's, from announce().

    /// The staged pin map (`SYSTem:PINS:STAGe` and its siblings): the live map
    /// until a unit changes it, and again after `SYSTem:PINS:CLEar`.
    pinmap::PinMap pinsPending_ = pinmap::kDefaultMap;
    PinsOutcome    pinsOutcome_ = {};  ///< The last apply's, since the boot.
    /// Whether the relays' wiring has passed a check on the live map: from
    /// begin() when they are the rig's own (rev D), 0 after an apply moved
    /// them, and then what the last `ROUTe:NULLpath ON` saw.
    bool relaysVerified_ = false;

    /// When `SENSe:CHARge:RESet` last cleared the accumulators.
    std::uint64_t chargeWindowStartUs_ = 0;

    // The inbox (receive()), the line, the formatter and the response.
    char        inbox_[kInboxCapacity] = {};  ///< Bytes read, not yet taken.
    std::size_t inboxHead_             = 0;   ///< The next byte to take.
    std::size_t inboxTail_             = 0;   ///< One past the last byte read.

    char          line_[kConsoleLineCapacity]     = {};     ///< The line being assembled.
    std::size_t   lineLength_                     = 0;      ///< Its length.
    bool          lineOverflow_                   = false;  ///< It outgrew its capacity (-363).
    bool          lineInvalid_                    = false;  ///< A byte for -101 came.
    std::uint32_t lineInvalidByte_                = 0;      ///< That byte, for the error text.
    bool          lineLost_                       = false;  ///< Input was lost (-363 queued).
    char          output_[kConsoleOutputCapacity] = {};     ///< A record's line (openRecord()).
    char          response_[kResponseCapacity]    = {};     ///< The units of the message.
    char          scratch_[kResponseCapacity]     = {};     ///< A unit being composed (openUnit()).
    ErrorDetail   errorDetail_                    = {};  ///< An error being composed (openError()).
    std::size_t   responseLength_                 = 0;   ///< Bytes in response_.
    std::size_t   responseUnits_                  = 0;   ///< Units in response_.

    /// True once part of a response is on the wire and its newline is not:
    /// no stream record may be written until endMessage().
    bool wirePartial_ = false;

    /// While counting, write() adds to countedBytes_ instead of writing.
    bool        counting_     = false;
    std::size_t countedBytes_ = 0;  ///< The bytes counted.
};

/// @}

}  // namespace kleidos::meter

#endif  // KLEIDOS_METER_CONSOLE_H
