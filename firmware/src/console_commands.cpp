// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file console_commands.cpp
 * @ingroup console_dispatch
 * @brief The SENSe, SAMPle, FORMat, TRIGger, CONFigure, MEASure, READ, FETCh,
 *        INITiate, ABORt, TRACe, CALibration and DIAGnostic handlers.
 *
 * The dispatcher, the output and the relay orders are console.cpp; every
 * command and response is `docs/scpi.md`. Values a report carries keep the
 * integer units the device computes in (`_na`, `_pv`, `_uohm`, `_mc`), so
 * their digits are exactly the device's; a SCPI numeric response is the same
 * integer written in its base unit by text::Writer::scaled(), with no float.
 *
 * Every response, error and dump record is composed with the bounded writer
 * of text.h through the console's typed members (FW-26): no format string,
 * no variadic call and nothing allocated, held byte for byte to the golden
 * transcript (`firmware/test/native/golden/console.transcript`).
 */
#include "bits.h"
#include "chars.h"
#include "console.h"
#include "crc32.h"
#include "heap_monitor.h"
#include "rtos_ticks.h"
#include "scale.h"
#include "text.h"

#include <esp_err.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <algorithm>
#include <array>
#include <span>
#include <string_view>

namespace kleidos::meter {

using namespace ina228;

namespace {

constexpr std::string_view kAbsent     = "-";  ///< Printed for a value that does not exist yet.
constexpr const char*      kNoReason   = "-";  ///< A refusal's text until the sampler names it.
constexpr std::string_view kDieUnknown = "unknown";  ///< Printed for a die temperature not read.

/// The `TRACe:DATA?` block header's buffers: `#`, the count of the length's
/// digits and the length, for the widest std::size_t (20 digits), and the NUL.
constexpr std::size_t kBlockHeaderCapacity = 24;

/// Milliseconds added to the conversion period a range switch discards, so
/// that a period shorter than the 1 ms tick still waits out a whole one.
constexpr std::uint32_t kDiscardGraceMs = 2;

/// Full scale of each range in microamps at the nominal shunt: the current
/// LSB times 2^19, the positive end of the 20-bit field.
constexpr std::int64_t kFullScaleUaRange0 = kCurrentLsbPaRange0 * 524288 / 1000000;
constexpr std::int64_t kFullScaleUaRange1 = kCurrentLsbPaRange1 * 524288 / 1000000;  ///< Range 1's.
static_assert( kFullScaleUaRange0 == 1638400, "range 0 spans 1.6384 A at 0.100 ohm" );
static_assert( kFullScaleUaRange1 == 409600, "range 1 spans 409.6 mA at 0.100 ohm" );

/// Range 0's full scale in nanoamps: the bound of a current threshold.
constexpr std::int64_t kFullScaleNaRange0 = kFullScaleUaRange0 * 1000;

/// Nanocoulombs per microsecond to picoamps: 10^-9 C / 10^-6 s is a
/// milliamp, 10^9 picoamps.
constexpr std::uint64_t kPicoPerNanoPerSecond = 1000000000ULL;

/// SAMPle:COUNt's window.
constexpr std::uint32_t kMaxSampleCount = 1000000;

/// How many Functions there are: Temperature is the last.
constexpr std::size_t kFunctionCount = static_cast<std::size_t>( Function::Temperature ) + 1U;

/// The channels `CONFigure` streams for each Function, in Function's order.
/// CHARGE has no `CONFigure` of its own (`FETCh:CHARge?` reads the
/// accumulator); its entry is the current a capture for it would stream.
constexpr std::array<std::uint32_t, kFunctionCount> kStreamedChannels = {
    channelBit( Channel::CurrentMicroamps ),                                             // Current
    channelBit( Channel::VoltageMillivolts ),                                            // Voltage
    channelBit( Channel::PowerMilliwatts ),                                              // Power
    channelBit( Channel::CurrentMicroamps ) | channelBit( Channel::VoltageMillivolts ),  // Energy
    channelBit( Channel::CurrentMicroamps ),                                             // Charge
    channelBit( Channel::DieCelsius ),  // Temperature
};

/// @brief The averaging override's state as the reports write it
/// (`avg_state`).
/// @param[in] on Whether the override is on; off is the profile's own
///               `ADC_CONFIG`.
/// @return `on` or `off`.
constexpr std::string_view averageState( bool on ) noexcept {
    return on ? std::string_view( "on" ) : std::string_view( "off" );
}

/// @brief The ADC_CONFIG encoding of @p value in a table of what each encoding
/// stands for (ina228_regs.h kAverageCount, kConversionTimeUs).
/// @param[in]  values The table, indexed by the encoding.
/// @param[in]  value  The count, or the microseconds.
/// @param[out] code   The encoding.
/// @retval true  @p value is one of the table's.
/// @retval false It is not; @p code is left alone.
constexpr bool encodingOf( const std::array<std::uint16_t, 8>& values, std::int64_t value,
                           std::uint8_t& code ) noexcept {
    for ( std::size_t i = 0; i < values.size(); ++i ) {
        if ( value == static_cast<std::int64_t>( values[i] ) ) {
            code = static_cast<std::uint8_t>( i );
            return true;
        }
    }
    return false;
}

/// @brief Reads a current in amps, or `OFF` when @p allowOff, from the unit's
/// first parameter.
/// @param[in,out] params   The unit's parameters.
/// @param[in]     allowOff Whether `OFF` is a choice.
/// @param[out]    off      Set for `OFF`.
/// @param[out]    nanoamps The current, nanoamps, when it is not `OFF`.
/// @retval true  A value or `OFF` was read.
/// @retval false It was refused (params holds the error).
bool currentParameter( scpi::Parameters& params, bool allowOff, bool& off,
                       std::int64_t& nanoamps ) noexcept {
    off             = false;
    const char* raw = params.raw( 0 );
    if ( allowOff && raw != nullptr && ( raw[0] == 'O' || raw[0] == 'o' ) ) {
        static constexpr std::array<std::string_view, 1> kOff  = { "OFF" };
        std::size_t                                      which = 0;
        if ( !params.choice( 0, kOff, which ) ) {
            return false;
        }
        off = true;
        return true;
    }
    if ( !params.scaled( 0, 9, nanoamps ) ) {
        return false;
    }
    return true;
}

/// @brief The text @p held holds, or kAbsent when it is empty, so a report's
///        `key=non-space` contract holds for an empty tag or label.
/// @tparam    N    The array's size.
/// @param[in] held The array, NUL-terminated or full.
/// @return Its characters up to the first NUL, or kAbsent for none.
template <std::size_t N>
std::string_view heldOrAbsent( const char ( &held )[N] ) noexcept {
    const std::string_view text = chars::untilNul( held );
    return text.empty() ? kAbsent : text;
}

/// @brief Appends a die temperature in m°C, or kDieUnknown.
/// @param[in,out] w            The writer.
/// @param[in]     milliCelsius The temperature, or kDieMilliCelsiusUnknown.
void writeDie( text::Writer& w, std::int32_t milliCelsius ) noexcept {
    if ( milliCelsius == kDieMilliCelsiusUnknown ) {
        w.text( kDieUnknown );
        return;
    }
    w.signedDecimal( milliCelsius );
}

/// @brief Appends a report's `key=value` pair whose value is signed: key(),
///        then signedDecimal().
/// @param[in,out] w     The writer.
/// @param[in]     name  The key.
/// @param[in]     value The value.
void signedField( text::Writer& w, std::string_view name, std::int64_t value ) noexcept {
    w.key( name );
    w.signedDecimal( value );
}

/// @brief Appends a report's `key=0x...` pair: key(), then @p value in
///        upper-case hexadecimal padded to @p width digits, as a register's
///        value is written.
/// @param[in,out] w     The writer.
/// @param[in]     name  The key.
/// @param[in]     value The value.
/// @param[in]     width The fewest digits written.
void hexField( text::Writer& w, std::string_view name, std::uint64_t value,
               std::size_t width ) noexcept {
    w.key( name );
    w.text( "0x" );
    w.hex( value, width, text::LetterCase::Upper );
}

/// @brief Appends a report's `key=value` pair whose value is a signed figure
///        that may not exist: the figure, or kAbsent.
/// @param[in,out] w     The writer.
/// @param[in]     name  The key.
/// @param[in]     valid Whether the figure exists.
/// @param[in]     value The figure.
void signedOrAbsent( text::Writer& w, std::string_view name, bool valid,
                     std::int64_t value ) noexcept {
    w.key( name );
    if ( valid ) {
        w.signedDecimal( value );
    } else {
        w.text( kAbsent );
    }
}

/// @brief The `vbus_side=` token of @p side.
/// @param[in] side Where the `VBUS` sense is wired.
/// @return `source` or `unknown`.
std::string_view vbusSideName( VbusSide side ) noexcept {
    return ( side == VbusSide::Source ) ? std::string_view( "source" )
                                        : std::string_view( "unknown" );
}

/// A channel name of the FUNCtion list, by Channel.
constexpr std::array<std::string_view, kChannelCount> kFunctionNames = { "CURRent", "VOLTage",
                                                                         "POWer", "TEMPerature" };
/// A channel's short name, by Channel: what `SENSe:FUNCtion:ON?` answers.
constexpr std::array<std::string_view, kChannelCount> kFunctionShort = { "CURR", "VOLT", "POW",
                                                                         "TEMP" };

/// Whether @p text is safe inside a `key=value` token: no space, no
/// separator, nothing a host's parser would split on. A byte above 0x7E never
/// gets this far: the line refuses it with -101 (Console::acceptByte()).
/// @param[in] text The text.
/// @retval true  It is non-empty and holds none of those bytes.
/// @retval false It is empty or holds one.
bool isTokenSafe( std::string_view text ) noexcept {
    if ( text.empty() ) {
        return false;
    }
    for ( const char c : text ) {
        if ( !chars::isGraphic( c ) || ( c == ',' ) || ( c == ';' ) || ( c == '"' ) || ( c == '\'' )
             || ( c == '=' ) ) {
            return false;
        }
    }
    return true;
}

/// @brief Character data or a quoted string, copied into @p out.
/// @param[in,out] params The unit's parameters.
/// @param[in]     index  The parameter's position.
/// @param[out]    out    The text, terminated.
/// @retval true  @p out holds the text.
/// @retval false It is missing, malformed or does not fit.
bool textParameter( scpi::Parameters& params, std::size_t index, std::span<char> out ) noexcept {
    const char* raw = params.raw( index );
    if ( raw == nullptr ) {
        return params.string( index, out.data(), out.size() );  // records -109
    }
    if ( raw[0] == '"' || raw[0] == '\'' ) {
        return params.string( index, out.data(), out.size() );
    }
    const std::string_view text( raw );
    if ( text.size() + 1U > out.size() ) {
        return false;
    }
    text::Writer w( out );
    w.text( text );
    return true;
}

}  // namespace

// --- SENSe: profile and range -----------------------------------------------

void Console::cmdProfileSet( Call& call ) noexcept {
    static constexpr std::array<std::string_view, 4> kNames    = { "TRACe", "STATe", "SLEep",
                                                                   "BURSt" };
    static constexpr std::array<Profile, 4>          kProfiles = { Profile::Trace, Profile::State,
                                                                   Profile::Sleep, Profile::Burst };
    std::size_t                                      which     = 0;
    if ( !call.params.choice( 0, kNames, which ) ) {
        failParameter( call.params, "use SENSe:PROFile TRACe|STATe|SLEep|BURSt" );
        return;
    }
    // Refused only while a window is open, which the flags R and C refuse
    // before this handler runs (Console::execute()).
    const char* error = nullptr;
    (void)sampler_->setProfile( kProfiles[which], error );
    // A channel choice belongs to the profile it was made for: TRACE streams
    // none, and STATE's budget is not SLEEP's.
    chosenMask_ = 0;
}

void Console::cmdProfileQuery( Call& ) noexcept {
    respondText( profileSpec( sampler_->profile() ).name );
}

void Console::cmdProfileReport( Call& ) noexcept {
    const ProfileSpec spec = sampler_->conversion( sampler_->profile() );
    text::Writer      w    = openUnit();
    w.field( "profile", spec.name );
    hexField( w, "adc_config", spec.adcConfig, 4U );
    w.field( "period_us", spec.periodUs );
    w.field( "vshct_us", kConversionTimeUs[spec.vShCt] );
    w.field( "avg", kAverageCount[spec.avg] );
    // 0.18.0: the averaging override (off: the profile's own word, above),
    // and the bus's conversion time, 0 for a profile whose MODE skips it.
    w.field( "avg_state", averageState( sampler_->averaging().on ) );
    w.field( "vbusct_us", modeConvertsBus( spec.mode ) ? kConversionTimeUs[spec.vbusCt] : 0U );
    respondText( w.view() );
}

bool Console::selectRange( ShuntRange range ) noexcept {
    const bool changed = device_->range() != range;

    // setRange() clears the accumulators on a real change, because CHARGE
    // counts are in units of CURRENT_LSB and the two ranges do not share one.
    const esp_err_t err = device_->setRange( range );
    if ( err != ESP_OK ) {
        failI2c( err, "" );
        return false;
    }
    lastRangeChanged_   = changed;
    lastRangeDiscardUs_ = 0;
    lastRangeConverted_ = 0;
    if ( changed ) {
        chargeWindowStartUs_ = static_cast<std::uint64_t>( esp_timer_get_time() );
        // One discarded conversion period, then CNVRF re-checked rather than
        // assumed: writing ADC_CONFIG restarts a conversion and nothing says
        // CONFIG does (`docs/ina228-reference.md` SS 8.2 item 3). Reading
        // DIAG_ALRT clears CNVRF, so the first read arms the check. The period
        // is the device's own: no profile's word is written until a capture
        // starts, and before one the device converts at its reset word's.
        std::uint16_t diag = 0;
        (void)device_->readDiagAlrt( diag );
        lastRangeDiscardUs_ = adcConfigPeriodMicroseconds( device_->adcConfig() );
        vTaskDelay( rtos::ticksFromMs( lastRangeDiscardUs_ / 1000U + kDiscardGraceMs ) );
        if ( device_->readDiagAlrt( diag ) == ESP_OK ) {
            lastRangeConverted_ = ( bits::hasBit( diag, kDiagCnvrf ) ) ? 1U : 0U;
        }
    }
    return true;
}

bool Console::applyRangeParameter( Call& call, std::size_t index ) noexcept {
    if ( call.params.count() <= index ) {
        return true;
    }
    constexpr const char* kUsage = "use a range in amps (0.4096 or 1.6384), MIN, MAX or DEF";

    const char* raw = call.params.raw( index );  // given: the count says so
    if ( chars::isLetter( raw[0] ) ) {
        static constexpr std::array<std::string_view, 3> kKeywords = { "MINimum", "MAXimum",
                                                                       "DEFault" };
        std::size_t                                      keyword   = 0;
        if ( !call.params.choice( index, kKeywords, keyword ) ) {
            failParameter( call.params, kUsage );
            return false;
        }
        return selectRange( ( keyword == 0U ) ? ShuntRange::Narrow41mV : ShuntRange::Wide163mV );
    }
    std::int64_t microamps = 0;
    if ( !call.params.scaled( index, 6, microamps ) ) {
        failParameter( call.params, kUsage );
        return false;
    }
    if ( microamps < 0 || microamps > kFullScaleUaRange0 ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return false;
    }
    // The smallest range that holds the value (SCPI-1999 vol. 2 SS 18.16).
    return selectRange( ( microamps <= kFullScaleUaRange1 ) ? ShuntRange::Narrow41mV
                                                            : ShuntRange::Wide163mV );
}

void Console::cmdRangeSet( Call& call ) noexcept {
    if ( call.params.count() == 0U ) {
        failText( scpi::err::kMissingParameter, "use SENSe:CURRent:RANGe <amps>|MIN|MAX|DEF" );
        return;
    }
    (void)applyRangeParameter( call, 0 );
}

void Console::cmdRangeQuery( Call& ) noexcept {
    respondScaled(
        ( device_->range() == ShuntRange::Wide163mV ) ? kFullScaleUaRange0 : kFullScaleUaRange1,
        6U );
}

void Console::cmdRangeIndexSet( Call& call ) noexcept {
    std::uint32_t index = 0;
    if ( !call.params.unsignedInteger( 0, index ) || index > 1U ) {
        if ( call.params.error() == scpi::err::kNone ) {
            failText( scpi::err::kDataOutOfRange, "use SENSe:CURRent:RANGe:INDex 0|1" );
        } else {
            failParameter( call.params, "use SENSe:CURRent:RANGe:INDex 0|1" );
        }
        return;
    }
    (void)selectRange( ( index == 0U ) ? ShuntRange::Wide163mV : ShuntRange::Narrow41mV );
}

void Console::cmdRangeIndexQuery( Call& ) noexcept {
    respondNumber( static_cast<std::uint32_t>( device_->range() ) );
}

void Console::cmdRangeReport( Call& ) noexcept {
    const ShuntRange range = device_->range();
    text::Writer     w     = openUnit();
    w.field( "range", static_cast<std::uint32_t>( range ) );
    signedField( w, "current_lsb_pa", currentLsbPicoamps( range ) );
    signedField( w, "ceiling_ma", rangeCeilingMilliamps( range ) );
    w.field( "rstacc", lastRangeChanged_ ? 1U : 0U );
    w.field( "discard_us", lastRangeDiscardUs_ );
    w.field( "cnvrf", lastRangeConverted_ );
    w.field( "q", qualityName( liveQuality() ) );
    respondText( w.view() );
}

// --- SENSe: shunt, apertures, functions, charge -----------------------------

void Console::cmdShuntSet( Call& call ) noexcept {
    std::int64_t microOhm = 0;
    if ( !call.params.scaled( 0, 6, microOhm ) ) {
        failParameter( call.params, "use SENSe:CURRent:SHUNt <ohms>" );
        return;
    }
    if ( microOhm < static_cast<std::int64_t>( kMinShuntMicroOhm )
         || microOhm > static_cast<std::int64_t>( kMaxShuntMicroOhm ) ) {
        failText( scpi::err::kDataOutOfRange, "use SENSe:CURRent:SHUNt <ohms> (0.05..0.2)" );
        return;
    }
    calibration_->setShuntMicroOhm( static_cast<std::uint32_t>( microOhm ) );
    saveCalibration();
}

void Console::cmdShuntQuery( Call& ) noexcept {
    respondScaled( calibration_->value().shuntMicroOhm, 6U );
}

bool Console::shuntRangeSuffix( const Call& call, ina228::ShuntRange& range ) noexcept {
    // No default: a value written to the wrong range would be read for a
    // whole session before anyone noticed, so the range is always named.
    if ( !call.suffix.present || call.suffix.value > 1 ) {
        failText( scpi::err::kSuffixOutOfRange,
                  "name the range: SENSe:CURRent:SHUNt:RANGe0|RANGe1" );
        return false;
    }
    range = static_cast<ina228::ShuntRange>( call.suffix.value );
    return true;
}

void Console::cmdShuntRangeSet( Call& call ) noexcept {
    ina228::ShuntRange range = ina228::ShuntRange::Wide163mV;
    if ( !shuntRangeSuffix( call, range ) ) {
        return;
    }
    std::int64_t microOhm = 0;
    if ( !call.params.scaled( 0, 6, microOhm ) ) {
        failParameter( call.params, "use SENSe:CURRent:SHUNt:RANGe<n> <ohms>" );
        return;
    }
    if ( microOhm < static_cast<std::int64_t>( kMinShuntMicroOhm )
         || microOhm > static_cast<std::int64_t>( kMaxShuntMicroOhm ) ) {
        failText( scpi::err::kDataOutOfRange,
                  "use SENSe:CURRent:SHUNt:RANGe<n> <ohms> (0.05..0.2)" );
        return;
    }
    calibration_->setShuntRangeMicroOhm( range, static_cast<std::uint32_t>( microOhm ) );
    saveCalibration();
}

// cppcheck-suppress constParameterReference ; every handler has the table's Handler type
void Console::cmdShuntRangeQuery( Call& call ) noexcept {
    ina228::ShuntRange range = ina228::ShuntRange::Wide163mV;
    if ( !shuntRangeSuffix( call, range ) ) {
        return;
    }
    respondScaled( calibration_->shuntMicroOhm( range ), 6U );
}

void Console::cmdCurrentAperture( Call& ) noexcept {
    // Every profile converts the shunt (a fact sampler.cpp's profile table is
    // built to), so its conversion time is the aperture, never 0.
    const ProfileSpec spec = sampler_->conversion( sampler_->profile() );
    respondScaled( kConversionTimeUs[spec.vShCt], 6U );
}

void Console::cmdVoltageAperture( Call& ) noexcept {
    const ProfileSpec& spec = profileSpec( sampler_->profile() );
    respondScaled( modeConvertsBus( spec.mode ) ? kConversionTimeUs[spec.vbusCt] : 0U, 6U );
}

void Console::cmdTemperatureAperture( Call& ) noexcept {
    const ProfileSpec& spec = profileSpec( sampler_->profile() );
    respondScaled( modeConvertsTemperature( spec.mode ) ? kConversionTimeUs[spec.vtCt] : 0U, 6U );
}

void Console::cmdAverageCount( Call& ) noexcept {
    respondNumber( kAverageCount[sampler_->conversion( sampler_->profile() ).avg] );
}

void Console::setAveraging( const Averaging& wanted ) noexcept {
    const char* error = kNoReason;
    if ( !sampler_->setAveraging( wanted, error ) ) {
        failSampler( error );
    }
}

void Console::cmdAverageSet( Call& call ) noexcept {
    Averaging wanted = sampler_->averaging();
    if ( !call.params.boolean( 0, wanted.on ) ) {
        failParameter( call.params, "use SENSe:AVERage[:STATe] ON|OFF" );
        return;
    }
    setAveraging( wanted );
}

void Console::cmdAverageQuery( Call& ) noexcept {
    respondNumber( sampler_->averaging().on ? 1U : 0U );
}

void Console::cmdAverageCountSet( Call& call ) noexcept {
    constexpr const char* kUsage = "use SENSe:AVERage:COUNt 1|4|16|64|128|256|512|1024";
    Averaging             wanted = sampler_->averaging();
    std::uint32_t         count  = 0;
    if ( !call.params.unsignedInteger( 0, count ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( !encodingOf( kAverageCount, count, wanted.avg ) ) {
        failText( scpi::err::kIllegalParameter, kUsage );
        return;
    }
    setAveraging( wanted );
}

void Console::cmdCurrentApertureSet( Call& call ) noexcept {
    constexpr const char* kUsage       = "use SENSe:CURRent:APERture <seconds> (0.00005..0.00412)";
    Averaging             wanted       = sampler_->averaging();
    std::int64_t          microseconds = 0;
    if ( !call.params.scaled( 0, 6U, microseconds ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( !encodingOf( kConversionTimeUs, microseconds, wanted.vShCt ) ) {
        failText( scpi::err::kIllegalParameter, kUsage );
        return;
    }
    setAveraging( wanted );
}

void Console::cmdFunctionOn( Call& call ) noexcept {
    constexpr const char* kUsage = "use SENSe:FUNCtion:ON \"CURR\"[,\"VOLT\"][,\"POW\"][,\"TEMP\"]";
    if ( call.params.count() == 0U ) {
        failText( scpi::err::kMissingParameter, kUsage );
        return;
    }
    std::uint32_t mask = 0;
    for ( std::size_t i = 0; i < call.params.count(); ++i ) {
        std::array<char, 16> name = {};
        if ( !textParameter( call.params, i, name ) ) {
            failParameter( call.params, kUsage );
            return;
        }
        bool found = false;
        for ( std::size_t c = 0; c < kChannelCount; ++c ) {
            if ( scpi::mnemonicMatches( kFunctionNames[c], chars::untilNul( name ) ) ) {
                mask |= 1U << c;
                found = true;
            }
        }
        if ( !found ) {
            text::Writer w = openError();
            w.text( "unknown function " );
            w.text( chars::untilNul( name ) );
            w.text( " (" );
            w.text( kUsage );
            w.character( ')' );
            failText( scpi::err::kIllegalParameter, w.view() );
            return;
        }
    }
    chosenMask_ = mask;
}

void Console::cmdFunctionOnQuery( Call& ) noexcept {
    const std::uint32_t mask = streamMask();
    text::Writer        w    = openUnit();
    for ( std::size_t c = 0; c < kChannelCount; ++c ) {
        if ( !bits::hasBit( mask, ( 1U << c ) ) ) {
            continue;
        }
        if ( w.size() != 0U ) {
            w.character( ',' );
        }
        w.character( '"' );
        w.text( kFunctionShort[c] );
        w.character( '"' );
    }
    if ( w.size() == 0U ) {
        w.text( "\"\"" );  // a ring profile streams nothing
    }
    respondText( w.view() );
}

void Console::cmdFunctionDefault( Call& ) noexcept {
    chosenMask_ = 0;
}

void Console::cmdCharge( Call& ) noexcept {
    // DIAG_ALRT first, then CHARGE: CHARGEOF "clears when CHARGE is read"
    // ([DS] Table 7-16), so reading the register first would lose it.
    std::uint16_t diag  = 0;
    esp_err_t     err   = device_->readDiagAlrt( diag );
    std::uint64_t raw40 = 0;
    if ( err == ESP_OK ) {
        err = device_->read40( kRegCharge, raw40 );
    }
    if ( err != ESP_OK ) {
        failI2c( err, "" );
        return;
    }
    const std::int64_t counts = scale::decode40( raw40 );
    const auto         now    = static_cast<std::uint64_t>( esp_timer_get_time() );

    // The nanocoulomb figure is at the NOMINAL shunt; `shunt_uohm` is carried
    // so the host applies `100000 / shunt_uohm` (`docs/architecture.md`
    // SS 3.6). `osc_tol_ppm` bounds a mean derived from it: the accumulator
    // counts on the device's own oscillator, `dur_us` on esp_timer.
    text::Writer w = openUnit();
    signedField( w, "raw", counts );
    signedField( w, "nc_nom",
                 scale::chargeNanocoulombs( counts, chargeLsbPicocoulombs( device_->range() ) ) );
    w.field( "dur_us", now - chargeWindowStartUs_ );
    w.field( "of", bits::hasBit( diag, kDiagChargeOf ) ? 1U : 0U );
    w.field( "shunt_uohm", calibration_->shuntMicroOhm( device_->range() ) );
    w.field( "osc_tol_ppm", kOscillatorTolerancePpm );
    respondText( w.view() );
}

void Console::cmdChargeReset( Call& ) noexcept {
    const esp_err_t err = device_->resetAccumulators();
    if ( err != ESP_OK ) {
        failI2c( err, "" );
        return;
    }
    chargeWindowStartUs_ = static_cast<std::uint64_t>( esp_timer_get_time() );
}

// --- SAMPle, FORMat, TRIGger ------------------------------------------------

void Console::cmdSampleTimer( Call& ) noexcept {
    respondScaled( sampler_->periodUs(), 6U );
}

void Console::cmdSampleCountSet( Call& call ) noexcept {
    std::uint32_t count = 0;
    if ( !call.params.unsignedInteger( 0, count ) || count == 0U || count > kMaxSampleCount ) {
        if ( call.params.error() == scpi::err::kNone ) {
            text::Writer w = openError();
            w.text( "use SAMPle:COUNt <1.." );
            w.unsignedDecimal( kMaxSampleCount );
            w.character( '>' );
            failText( scpi::err::kDataOutOfRange, w.view() );
        } else {
            failParameter( call.params, "use SAMPle:COUNt <n>" );
        }
        return;
    }
    sampleCount_ = count;
}

void Console::cmdSampleCountQuery( Call& ) noexcept {
    respondNumber( sampleCount_ );
}

void Console::cmdFormatSet( Call& call ) noexcept {
    static constexpr std::array<std::string_view, 2> kFormats = { "TEXT", "CSV" };
    std::size_t                                      which    = 0;
    if ( !call.params.choice( 0, kFormats, which ) ) {
        failParameter( call.params, "use FORMat:DATA TEXT|CSV" );
        return;
    }
    format_ = ( which == 0U ) ? StreamFormat::Text : StreamFormat::Csv;
}

void Console::cmdFormatQuery( Call& ) noexcept {
    respondText( ( format_ == StreamFormat::Csv ) ? std::string_view( "CSV" )
                                                  : std::string_view( "TEXT" ) );
}

void Console::cmdTriggerSourceSet( Call& call ) noexcept {
    static constexpr std::array<std::string_view, 2> kSources = { "IMMediate", "LEVel" };
    std::size_t                                      which    = 0;
    if ( !call.params.choice( 0, kSources, which ) ) {
        failParameter( call.params, "use TRIGger:SOURce IMMediate|LEVel" );
        return;
    }
    triggerSource_ = ( which == 1U ) ? TriggerSource::Level : TriggerSource::Immediate;
}

void Console::cmdTriggerSourceQuery( Call& ) noexcept {
    respondText( ( triggerSource_ == TriggerSource::Level ) ? std::string_view( "LEV" )
                                                            : std::string_view( "IMM" ) );
}

void Console::cmdTriggerLevelSet( Call& call ) noexcept {
    constexpr const char* kUsage   = "use TRIGger:LEVel <amps> (-1.6384..1.6384)";
    std::int64_t          nanoamps = 0;
    if ( !call.params.scaled( 0, 9, nanoamps ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( nanoamps < -kFullScaleNaRange0 || nanoamps > kFullScaleNaRange0 ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    triggerLevelNa_ = nanoamps;
}

void Console::cmdTriggerLevelQuery( Call& ) noexcept {
    respondScaled( triggerLevelNa_, 9U );
}

void Console::cmdTriggerSlopeSet( Call& call ) noexcept {
    static constexpr std::array<std::string_view, 2> kSlopes = { "POSitive", "NEGative" };
    std::size_t                                      which   = 0;
    if ( !call.params.choice( 0, kSlopes, which ) ) {
        failParameter( call.params, "use TRIGger:SLOPe POSitive|NEGative" );
        return;
    }
    triggerRising_ = which == 0U;
}

void Console::cmdTriggerSlopeQuery( Call& ) noexcept {
    respondText( triggerRising_ ? std::string_view( "POS" ) : std::string_view( "NEG" ) );
}

void Console::cmdPretriggerSet( Call& call ) noexcept {
    std::uint32_t samples = 0;
    if ( !call.params.unsignedInteger( 0, samples ) ) {
        failParameter( call.params, "use TRIGger:PRETrigger <samples>" );
        return;
    }
    if ( samples >= sampler_->ringCapacity() ) {
        text::Writer w = openError();
        w.text( "use TRIGger:PRETrigger <0.." );
        w.unsignedDecimal( sampler_->ringCapacity() - 1U );
        w.character( '>' );
        failText( scpi::err::kDataOutOfRange, w.view() );
        return;
    }
    pretrigger_ = samples;
}

void Console::cmdPretriggerQuery( Call& ) noexcept {
    respondNumber( pretrigger_ );
}

void Console::cmdCaptureLimitSet( Call& call ) noexcept {
    bool limited = false;
    if ( !call.params.boolean( 0, limited ) ) {
        failParameter( call.params, "use SENSe:CAPTure:LIMit ON|OFF" );
        return;
    }
    captureLimited_ = limited;
}

void Console::cmdCaptureLimitQuery( Call& ) noexcept {
    respondNumber( captureLimited_ ? 1U : 0U );
}

void Console::cmdThresholdMarkSet( Call& call ) noexcept {
    bool marks = false;
    if ( !call.params.boolean( 0, marks ) ) {
        failParameter( call.params, "use SENSe:CURRent:THReshold:MARK ON|OFF" );
        return;
    }
    // The flag R has refused it under an open capture, setThresholdMarks()'s
    // one refusal.
    const char* error = nullptr;
    (void)sampler_->setThresholdMarks( marks, error );
}

void Console::cmdThresholdMarkQuery( Call& ) noexcept {
    respondNumber( sampler_->thresholdMarks() ? 1U : 0U );
}

// --- CONFigure, MEASure, READ, FETCh ----------------------------------------

void Console::selectBusProfile( Function function ) noexcept {
    // The voltage, the power and the energy need a profile that converts the
    // bus. Configuring one of them on a profile that does not selects STATE,
    // as SCPI's CONFigure sets the instrument up for the function (0.12.0):
    // MEASure:VOLTage? after a boot on TRACE answered -230 before.
    const bool needsBus = function == Function::Voltage || function == Function::Power
                          || function == Function::Energy;
    const bool converts = modeConvertsBus( profileSpec( sampler_->profile() ).mode );
    if ( !needsBus || converts ) {
        return;
    }
    // The flag R has refused it under an open capture, setProfile()'s one
    // refusal.
    const char* error = nullptr;
    (void)sampler_->setProfile( Profile::State, error );
    chosenMask_ = 0;
}

void Console::configure( Function function, Call& call ) noexcept {
    // Only the current is ever given a range: the table gives the other
    // functions' headers no parameter.
    if ( function == Function::Current ) {
        if ( !applyRangeParameter( call, 0 ) ) {
            return;
        }
    }
    function_ = function;
    selectBusProfile( function );
    if ( profileSpec( sampler_->profile() ).capturesToRing ) {
        return;  // a ring profile streams no channel to choose
    }
    // A table, not a switch: every Function has its set, and no label is left
    // for a function CONFigure is never given.
    chosenMask_ = kStreamedChannels[static_cast<std::size_t>( function )];
}

void Console::cmdConfCurrent( Call& call ) noexcept {
    configure( Function::Current, call );
}
void Console::cmdConfVoltage( Call& call ) noexcept {
    configure( Function::Voltage, call );
}
void Console::cmdConfPower( Call& call ) noexcept {
    configure( Function::Power, call );
}
void Console::cmdConfEnergy( Call& call ) noexcept {
    configure( Function::Energy, call );
}
void Console::cmdConfTemperature( Call& call ) noexcept {
    configure( Function::Temperature, call );
}

void Console::cmdConfQuery( Call& ) noexcept {
    static constexpr std::array<std::string_view, 6> kNames = { "CURR", "VOLT", "POW",
                                                                "ENER", "CHAR", "TEMP" };
    text::Writer                                     w      = openUnit();
    w.character( '"' );
    w.text( kNames[static_cast<std::size_t>( function_ )] );
    w.character( '"' );
    respondText( w.view() );
}

bool Console::acquire() noexcept {
    if ( relays_->shuntBypassed() ) {
        text::Writer w = openError();
        w.text( "shunt bypassed (path=" );
        w.text( relay::pathName( relays_->path() ) );
        w.text( ": ROUTe:NULLpath OFF first)" );
        failText( scpi::err::kShuntBypassed, w.view() );
        return false;
    }
    const Profile       profile  = sampler_->profile();
    const std::uint32_t periodUs = sampler_->periodUs();

    // A ring profile takes at most what the ring holds; the ring is the
    // sampler's from boot, so its size is known before the first capture.
    std::uint32_t wanted = sampleCount_;
    if ( profileSpec( profile ).capturesToRing && wanted > sampler_->ringCapacity() ) {
        wanted = sampler_->ringCapacity();
    }
    // The console reads nothing but ABORt while an acquisition runs, so its
    // window is bounded: 10^6 SLEEP samples would be 18.8 hours.
    const std::uint64_t windowUs = static_cast<std::uint64_t>( wanted ) * periodUs;
    if ( windowUs > kMaxAcquisitionUs ) {
        text::Writer w = openError();
        w.text( "acquisition of " );
        w.unsignedDecimal( wanted );
        w.text( " x " );
        w.unsignedDecimal( periodUs );
        w.text( " us exceeds " );
        w.unsignedDecimal( kMaxAcquisitionUs / 1000000U );
        w.text( " s (lower SAMPle:COUNt, or INITiate a capture)" );
        failText( scpi::err::kDataOutOfRange, w.view() );
        return false;
    }

    accumulator_      = Accumulator{};
    CaptureInfo info  = {};
    const char* error = kNoReason;
    // The acquisition counts its own samples; INITiate's limit is not its.
    sampler_->setCaptureLimit( 0 );
    if ( !sampler_->start( profile, channelBit( Channel::CurrentMicroamps ), info, error ) ) {
        failSampler( error );
        return false;
    }
    captureInfo_ = info;
    noteCaptureStart();
    haveCapture_            = true;
    captureStatisticsOnly_  = false;
    captureRelays_          = relays_->energized();
    captureEvidence_        = evidence_;
    captureBypassed_        = false;
    captureBypassedAtStart_ = false;

    // Twice the nominal window and a second more: a pace that fell back to
    // the timer is slower, never twice as slow.
    const std::int64_t budgetUs =
        static_cast<std::int64_t>( wanted ) * static_cast<std::int64_t>( periodUs ) * 2 + 1000000;
    const std::int64_t deadline = esp_timer_get_time() + budgetUs;
    Sample             sample   = {};
    bool               aborted  = false;
    while ( sampler_->sampleCount() < wanted && esp_timer_get_time() < deadline ) {
        feedWatchdog();  // an acquisition holds the console up to 600 s
        if ( sampler_->nextStreamSample( sample, 2 ) ) {
            consume( sample );
        }
        if ( abortRequested() ) {
            aborted = true;
            break;
        }
    }
    CaptureStats stats   = {};
    const bool   stopped = sampler_->stop( stats );
    while ( sampler_->nextStreamSample( sample, 0 ) ) {
        consume( sample );
    }
    if ( !stopped ) {
        failText( scpi::err::kSamplerFailed, "the acquisition did not stop" );
        return false;
    }
    lastStats_              = stats;
    lastHoldTeardowns_      = holdTeardowns_;
    paceUnmet_              = stats.paceUnmet;
    haveStats_              = true;
    const bool mathOverflow = bits::hasBit( stats.flags, kDiagMathOf );
    lastQuality_ =
        calibration_->quality( sampler_->captureRange(), stats.dieMilliCelsiusEnd, mathOverflow );
    accumulator_.valid = true;
    if ( aborted ) {
        text::Writer w = openError();
        w.text( "the acquisition was aborted after " );
        w.unsignedDecimal( stats.samples );
        w.text( " of " );
        w.unsignedDecimal( wanted );
        w.text( " samples (ABORt)" );
        failText( scpi::err::kDataStale, w.view() );
        return false;
    }
    if ( stats.samples < wanted ) {
        text::Writer w = openError();
        w.text( "the acquisition took " );
        w.unsignedDecimal( stats.samples );
        w.text( " of " );
        w.unsignedDecimal( wanted );
        w.text( " samples" );
        failText( scpi::err::kDataStale, w.view() );
        return false;
    }
    return true;
}

bool Console::reportFunction( Function function ) noexcept {
    if ( sampler_->running() ) {
        failText( scpi::err::kDataStale, "capture running (ABORt first)" );
        return false;
    }
    if ( !haveStats_ ) {
        failText( scpi::err::kDataStale, "no acquisition yet (INITiate then ABORt, or MEASure)" );
        return false;
    }
    // The figure in its smallest unit, and the decimals that unit carries
    // below the SCPI base unit the response is written in.
    std::int64_t  value    = 0;
    std::uint32_t decimals = 9U;
    if ( function == Function::Current ) {
        // Picoamps, written in amps.
        value    = lastStats_.meanPicoamps;
        decimals = 12U;
    } else if ( function == Function::Charge ) {
        if ( !lastStats_.chargeValid ) {
            failText( scpi::err::kDataStale, "the acquisition read no CHARGE boundary" );
            return false;
        }
        const std::int64_t counts = lastStats_.chargeRawEnd - lastStats_.chargeRawStart;
        const std::int64_t nominal =
            scale::chargeNanocoulombs( counts, chargeLsbPicocoulombs( sampler_->captureRange() ) );
        const std::int64_t     shunt = calibration_->shuntMicroOhm( sampler_->captureRange() );
        constexpr std::int64_t kMax =
            INT64_MAX / static_cast<std::int64_t>( kNominalShuntMicroOhm );
        value = ( nominal > -kMax && nominal < kMax && shunt > 0 )
                    ? ( nominal * static_cast<std::int64_t>( kNominalShuntMicroOhm ) / shunt )
                    : nominal;
    } else if ( function == Function::Temperature ) {
        std::int64_t milliCelsius = 0;
        if ( lastStats_.dieSamples > 0U ) {
            milliCelsius = lastStats_.sumDieMc / static_cast<std::int64_t>( lastStats_.dieSamples );
        } else if ( lastStats_.dieMilliCelsiusEnd != kDieMilliCelsiusUnknown ) {
            milliCelsius = lastStats_.dieMilliCelsiusEnd;
        } else {
            failText( scpi::err::kDataStale, "no die temperature converted yet" );
            return false;
        }
        value    = milliCelsius;
        decimals = 3U;
    } else {
        // Voltage, Power and Energy: what the bus voltage carries.
        if ( lastStats_.busSamples == 0U ) {
            failText( scpi::err::kDataStale,
                      "the acquisition converted no bus voltage (SENSe:PROFile STATe or SLEep)" );
            return false;
        }
        const std::uint64_t n = lastStats_.busSamples;
        if ( function == Function::Voltage ) {
            // Microvolts to nanovolts, written in volts.
            value = wide::mulDiv( lastStats_.sumDutUv, 1000U, n );
        } else if ( function == Function::Power ) {
            value = wide::mulDiv( lastStats_.sumPowerUw, 1000U, n );
        } else {
            // The mean power times the capture's own duration (0.10.0;
            // before, times the samples that reached the console, so a
            // dropped one shortened it): microwatt-microseconds are
            // picojoules, written in joules from nanojoules.
            value = wide::mulDiv( lastStats_.sumPowerUw, lastStats_.durationUs, n * 1000U );
        }
    }
    respondScaled( value, decimals );
    return true;
}

void Console::measure( Function function, Call& call, bool configureFirst ) noexcept {
    if ( configureFirst ) {
        if ( function == Function::Current ) {
            if ( !applyRangeParameter( call, 0 ) ) {
                return;
            }
        }
        function_ = function;
        selectBusProfile( function );
    }
    if ( !acquire() ) {
        return;
    }
    (void)reportFunction( function );
}

void Console::cmdMeasCurrent( Call& call ) noexcept {
    measure( Function::Current, call, true );
}
void Console::cmdMeasVoltage( Call& call ) noexcept {
    measure( Function::Voltage, call, true );
}
void Console::cmdMeasPower( Call& call ) noexcept {
    measure( Function::Power, call, true );
}
void Console::cmdMeasEnergy( Call& call ) noexcept {
    measure( Function::Energy, call, true );
}
void Console::cmdMeasTemperature( Call& call ) noexcept {
    measure( Function::Temperature, call, true );
}
void Console::cmdRead( Call& call ) noexcept {
    measure( function_, call, false );
}
void Console::cmdReadCurrent( Call& call ) noexcept {
    measure( Function::Current, call, false );
}
void Console::cmdReadVoltage( Call& call ) noexcept {
    measure( Function::Voltage, call, false );
}
void Console::cmdReadPower( Call& call ) noexcept {
    measure( Function::Power, call, false );
}
void Console::cmdReadEnergy( Call& call ) noexcept {
    measure( Function::Energy, call, false );
}
void Console::cmdReadTemperature( Call& call ) noexcept {
    measure( Function::Temperature, call, false );
}
void Console::cmdFetch( Call& ) noexcept {
    (void)reportFunction( function_ );
}
void Console::cmdFetchCurrent( Call& ) noexcept {
    (void)reportFunction( Function::Current );
}
void Console::cmdFetchVoltage( Call& ) noexcept {
    (void)reportFunction( Function::Voltage );
}
void Console::cmdFetchPower( Call& ) noexcept {
    (void)reportFunction( Function::Power );
}
void Console::cmdFetchEnergy( Call& ) noexcept {
    (void)reportFunction( Function::Energy );
}
void Console::cmdFetchCharge( Call& ) noexcept {
    (void)reportFunction( Function::Charge );
}
void Console::cmdFetchTemperature( Call& ) noexcept {
    (void)reportFunction( Function::Temperature );
}

void Console::cmdFetchStatistics( Call& ) noexcept {
    if ( sampler_->running() ) {
        failText( scpi::err::kDataStale, "capture running (ABORt first)" );
        return;
    }
    if ( !haveStats_ ) {
        failText( scpi::err::kDataStale, "no capture closed since boot" );
        return;
    }
    const CaptureStats& stats = lastStats_;

    // The CHARGE pair is the hardware integral over this very window, so it is
    // reported beside the window's own `dur_us`. The mean carries three
    // decimals because averaging resolves below the nanoamp; min and max are
    // single samples, written as the integers they are.
    text::Writer w = openUnit();
    w.field( "samples", stats.samples );
    w.field( "dropped", stats.dropped );
    w.field( "read_err", stats.readFailures );
    w.field( "pace_fallback", stats.paceFellBack ? 1U : 0U );
    signedField( w, "pace_err_ppm", stats.paceErrorPpm );
    w.field( "pace_unmet", stats.paceUnmet ? 1U : 0U );
    w.field( "dur_us", stats.durationUs );
    w.key( "mean_na" );
    w.thousandths( stats.meanPicoamps );
    signedField( w, "min_na", stats.minNanoamps );
    signedField( w, "max_na", stats.maxNanoamps );
    w.key( "die_mc_end" );
    writeDie( w, stats.dieMilliCelsiusEnd );
    hexField( w, "flags", stats.flags, 4U );
    signedOrAbsent( w, "charge_raw_start", stats.chargeValid, stats.chargeRawStart );
    signedOrAbsent( w, "charge_raw_end", stats.chargeValid, stats.chargeRawEnd );
    w.field( "osc_tol_ppm", kOscillatorTolerancePpm );
    w.field( "q", qualityName( lastQuality_ ) );
    w.field( "stale_edges", stats.staleEdges );
    appendStatistics( w, stats );
    respondText( w.view() );
}

void Console::appendStatistics( text::Writer& w, const CaptureStats& stats ) noexcept {
    // 0.10.0: the sample standard deviation, where the maximum fell, the
    // DUT's voltage and power extremes, the CHARGE pair's mean current, the
    // threshold's counts and the batches. A figure that does not exist yet is
    // `-` (`docs/scpi.md`, "Responses").
    w.key( "sd_na" );
    if ( stats.deviationValid ) {
        w.thousandths( stats.deviationPicoamps );
    } else {
        w.text( kAbsent );
    }
    if ( stats.samples > 0U ) {
        w.field( "max_n", stats.maxIndex );
        w.field( "max_t_us", stats.maxTimeUs );
    } else {
        w.field( "max_n", kAbsent );
        w.field( "max_t_us", kAbsent );
    }

    const bool bus = stats.busSamples > 0U;
    signedOrAbsent( w, "vmin_uv", bus, stats.minDutUv );
    signedOrAbsent( w, "vmax_uv", bus, stats.maxDutUv );
    signedOrAbsent( w, "pmin_uw", bus, stats.minPowerUw );
    signedOrAbsent( w, "pmax_uw", bus, stats.maxPowerUw );

    // The CHARGE pair over the span between its own two reads, which is
    // longer than `dur_us` by the pacing probe and the stop, and
    // shunt-corrected: nanocoulombs x 10^9 / microseconds is picoamps,
    // written in nanoamps.
    w.key( "mean_charge_na" );
    if ( stats.chargeValid ) {
        const std::int64_t nominal =
            scale::chargeNanocoulombs( stats.chargeRawEnd - stats.chargeRawStart,
                                       chargeLsbPicocoulombs( sampler_->captureRange() ) );
        const std::int64_t corrected =
            wide::mulDiv( nominal, kNominalShuntMicroOhm, sampler_->captureShuntMicroOhm() );
        w.thousandths( wide::mulDiv( corrected, kPicoPerNanoPerSecond, stats.chargeSpanUs ) );
    } else {
        w.text( kAbsent );
    }
    w.field( "charge_us", stats.chargeSpanUs );

    if ( stats.thresholdEnabled ) {
        const std::uint64_t aboveUs =
            static_cast<std::uint64_t>( stats.aboveSamples ) * sampler_->capturedPeriodUs();
        signedField( w, "thr_na", stats.thresholdNa );
        w.field( "above_n", stats.aboveSamples );
        w.field( "above_us", aboveUs );
        w.field( "rises", stats.rises );
    } else {
        w.field( "thr_na", kAbsent );
        w.field( "above_n", kAbsent );
        w.field( "above_us", kAbsent );
        w.field( "rises", kAbsent );
    }
    w.field( "batches", stats.batchCount );
    w.field( "batch_samples", stats.batchSamples );

    // 0.11.0: where a level trigger fired, and the threshold marks written.
    if ( stats.triggered ) {
        w.field( "trig_n", stats.triggerIndex );
        w.field( "trig_t_us", stats.triggerTimeUs );
    } else {
        w.field( "trig_n", kAbsent );
        w.field( "trig_t_us", kAbsent );
    }
    w.field( "thr_marks", stats.thresholdMarks );
    // 0.14.0: deadlines a late timer-paced loop re-based.
    w.field( "rebased", stats.rebases );
    // 0.16.0: the hold's releases while the capture sampled, each marked.
    w.field( "hold_teardowns", lastHoldTeardowns_ );
}

void Console::cmdFetchBatches( Call& ) noexcept {
    if ( sampler_->running() ) {
        failText( scpi::err::kDataStale, "capture running (ABORt first)" );
        return;
    }
    if ( !haveStats_ ) {
        failText( scpi::err::kDataStale, "no capture closed since boot" );
        return;
    }
    const CaptureStats& stats = lastStats_;
    text::Writer        w     = openUnit();
    w.field( "batches", stats.batchCount );
    w.field( "batch_samples", stats.batchSamples );
    w.key( "means_na" );
    if ( stats.batchCount == 0U ) {
        w.text( kAbsent );
    }
    // Each batch's mean in nanoamps with three decimals, as `mean_na`; the
    // division is split so that the sum is never multiplied by 1000.
    const auto size = static_cast<std::int64_t>( stats.batchSamples );
    for ( std::uint32_t i = 0; i < stats.batchCount; ++i ) {
        const std::int64_t sum = stats.batchSumsNa[i];
        const std::int64_t picoamps =
            ( sum / size ) * scale::kThousand + ( ( sum % size ) * scale::kThousand ) / size;
        if ( i != 0U ) {
            w.character( ',' );
        }
        w.thousandths( picoamps );
    }
    respondText( w.view() );
}

void Console::cmdCaptureSet( Call& call ) noexcept {
    static constexpr std::array<std::string_view, 2> kModes = { "DATA", "STATistics" };
    std::size_t                                      which  = 0;
    if ( !call.params.choice( 0, kModes, which ) ) {
        failParameter( call.params, "use SENSe:CAPTure DATA|STATistics" );
        return;
    }
    statisticsOnly_ = which == 1U;
}

void Console::cmdCaptureQuery( Call& ) noexcept {
    respondText( statisticsOnly_ ? std::string_view( "STAT" ) : std::string_view( "DATA" ) );
}

void Console::cmdThresholdSet( Call& call ) noexcept {
    constexpr const char* kUsage   = "use SENSe:CURRent:THReshold <amps> (-1.6384..1.6384) or OFF";
    bool                  off      = false;
    std::int64_t          nanoamps = 0;
    if ( !currentParameter( call.params, true, off, nanoamps ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( nanoamps < -kFullScaleNaRange0 || nanoamps > kFullScaleNaRange0 ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    // The flag R has refused it under an open capture, the one refusal
    // setThreshold() has for a level.
    const char* error = nullptr;
    (void)sampler_->setThreshold( !off, off ? sampler_->thresholdNa() : nanoamps,
                                  sampler_->hysteresisNa(), error );
}

void Console::cmdThresholdQuery( Call& ) noexcept {
    if ( !sampler_->thresholdEnabled() ) {
        respondText( "OFF" );
        return;
    }
    respondScaled( sampler_->thresholdNa(), 9U );
}

void Console::cmdHysteresisSet( Call& call ) noexcept {
    constexpr const char* kUsage   = "use SENSe:CURRent:THReshold:HYSTeresis <amps> (0..1.6384)";
    bool                  off      = false;
    std::int64_t          nanoamps = 0;
    if ( !currentParameter( call.params, false, off, nanoamps ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( nanoamps < 0 || nanoamps > kFullScaleNaRange0 ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    // The flag R and the bound above leave setThreshold() nothing to refuse.
    const char* error = nullptr;
    (void)sampler_->setThreshold( sampler_->thresholdEnabled(), sampler_->thresholdNa(), nanoamps,
                                  error );
}

void Console::cmdHysteresisQuery( Call& ) noexcept {
    respondScaled( sampler_->hysteresisNa(), 9U );
}

void Console::cmdFetchHeader( Call& ) noexcept {
    if ( !haveCapture_ ) {
        failText( scpi::err::kDataStale, "no capture started since boot" );
        return;
    }
    const CaptureInfo& info  = captureInfo_;
    const ProfileSpec& spec  = profileSpec( info.profile );
    const ShuntRange   range = sampler_->captureRange();
    const Calibration& cal   = calibration_->value();
    const Quality quality = captureBypassedAtStart_
                                ? Quality::Invalid
                                : calibration_->quality( range, info.dieMilliCelsiusStart, false );

    // One unit, the capture's facts in the order the device produced them:
    // identity; the registers written; the clip detector (`alatch=1` and the
    // two limits make SHNTOL/SHNTUL in the closing statistics' flags mean a clip); the
    // conversion (0 for an input the MODE does not convert, so the period is
    // the sum of the numbers beside it); the pace and the oscillator bound on
    // a timer-paced timestamp; every LSB; the calibration; the circuit.
    text::Writer w = openUnit();
    w.field( "fw", firmwareVersion() );
    w.field( "chip", "ina228" );
    hexField( w, "addr", device_->address(), 2U );
    hexField( w, "mfg", device_->manufacturerId(), 4U );
    hexField( w, "die", device_->dieId(), 3U );
    hexField( w, "rev", device_->revisionId(), 1U );
    hexField( w, "config", device_->config(), 4U );
    hexField( w, "adc_config", device_->adcConfig(), 4U );
    w.field( "shunt_cal", device_->shuntCal() );
    w.field( "range", static_cast<std::uint32_t>( range ) );
    hexField( w, "diag_cfg", device_->diagConfig(), 4U );
    w.field( "alatch", bits::field( device_->diagConfig(), kDiagAlatch, kDiagAlatchShift ) );
    hexField( w, "sovl", device_->shuntOverLimit(), 4U );
    hexField( w, "suvl", device_->shuntUnderLimit(), 4U );
    hexField( w, "mode", spec.mode, 1U );
    w.field( "vbusct_us", modeConvertsBus( spec.mode ) ? kConversionTimeUs[spec.vbusCt] : 0U );
    // Every profile converts the shunt, with its own time or the override's.
    w.field( "vshct_us", kConversionTimeUs[info.averaging.vShCt] );
    w.field( "vtct_us", modeConvertsTemperature( spec.mode ) ? kConversionTimeUs[spec.vtCt] : 0U );
    w.field( "avg", kAverageCount[info.averaging.avg] );
    w.field( "period_us", info.periodUs );
    w.field( "pace", ( info.pace == Pace::Alert ) ? std::string_view( "alert" )
                                                  : std::string_view( "timer" ) );
    w.field( "pace_edges", info.paceEdges );
    w.field( "pace_expect", info.paceExpected );
    w.field( "osc_tol_ppm", kOscillatorTolerancePpm );
    signedField( w, "shunt_lsb_pv", shuntLsbPicovolts( range ) );
    signedField( w, "bus_lsb_pv", kBusLsbPv );
    signedField( w, "temp_lsb_nc", kTempLsbNanoC );
    signedField( w, "current_lsb_pa", currentLsbPicoamps( range ) );
    signedField( w, "power_lsb_pw", powerLsbPicowatts( range ) );
    signedField( w, "charge_lsb_pc", chargeLsbPicocoulombs( range ) );
    signedOrAbsent( w, "charge_raw_start", info.chargeValidStart, info.chargeRawStart );
    w.field( "shunt_uohm", sampler_->captureShuntMicroOhm() );
    w.field( "shunt_measured", calibration_->shuntMeasured() ? 1U : 0U );
    signedField( w, "offset_pv", sampler_->captureOffsetPicovolts() );
    w.key( "off_die_mc" );
    writeDie( w, calibration_->offsetDieMilliCelsius( range ) );
    w.field( "vbus_side", vbusSideName( cal.vbusSide ) );
    w.field( "cal_tag", heldOrAbsent( cal.tag ) );
    w.field( "q", qualityName( quality ) );
    w.key( "die_mc_start" );
    writeDie( w, info.dieMilliCelsiusStart );
    signedField( w, "vs_cur_delta", info.shuntCurrentDelta );
    w.field( "scl_hz", device_->busSpeedHz() );
    std::string_view sink = "stream";
    if ( captureStatisticsOnly_ ) {
        sink = "stats";
    } else if ( spec.capturesToRing ) {
        sink = "ring";
    } else {
        // Streamed: the default above.
    }
    w.field( "sink", sink );
    w.field( "ring", info.ringSamples );
    w.field( "anchor_every", kAnchorEvery );
    w.field( "t0_us", info.startTimeUs );
    w.field( "format", ( format_ == StreamFormat::Csv ) ? std::string_view( "csv" )
                                                        : std::string_view( "text" ) );
    w.field( "relays", relays_->ready() ? 1U : 0U );
    w.character( ' ' );
    w.text( relayTokens( captureRelays_, captureEvidence_ ) );

    // 0.11.0: what starts the record and what ends the capture.
    if ( info.triggerSource == TriggerSource::Level ) {
        w.field( "trig_source", "level" );
        signedField( w, "trig_level_na", info.triggerLevelNa );
        w.field( "trig_slope",
                 info.triggerRising ? std::string_view( "pos" ) : std::string_view( "neg" ) );
    } else {
        w.field( "trig_source", "imm" );
        w.field( "trig_level_na", kAbsent );
        w.field( "trig_slope", kAbsent );
    }
    w.field( "trig_pre", info.pretrigger );
    w.field( "limit", info.sampleLimit );
    // 0.18.0: the averaging override the capture ran with, and where the pin
    // map it ran on came from.
    w.field( "avg_state", averageState( info.averaging.on ) );
    w.field( "pins_src", pin_boot::sourceWord( pins_->source ) );
    respondText( w.view() );
}

// --- INITiate, ABORt --------------------------------------------------------

void Console::startCapture( bool force ) noexcept {
    // A shunt with K2 across it reads a fraction of the current, and one with
    // the DUT fed around it reads nothing of a running board, so a capture of
    // either is refused unless the order is INITiate:FORCe -- and then every
    // report on it says `q=invalid`.
    if ( relays_->shuntBypassed() && !force ) {
        if ( relays_->path() == relay::Path::Both ) {
            failText( scpi::err::kShuntBypassed,
                      "shunt bypassed (path=both: ROUTe:BYPass OFF, or INITiate:FORCe)" );
        } else {
            failText( scpi::err::kShuntBypassed,
                      "shunt bypassed (path=bypass: ROUTe:NULLpath OFF, or INITiate:FORCe)" );
        }
        return;
    }
    const Profile      profile = sampler_->profile();
    const ProfileSpec& spec    = profileSpec( profile );
    if ( spec.capturesToRing && chosenMask_ != 0U && !statisticsOnly_ ) {
        // TRACE streams nothing and replays one channel from the ring, so a
        // channel choice would have no effect.
        failText( scpi::err::kSettingsConflict,
                  "a ring profile takes no channels (SENSe:FUNCtion:ON:DEFault; TRACe:DATA? after "
                  "ABORt)" );
        return;
    }

    accumulator_        = Accumulator{};
    CaptureInfo info    = {};
    const char* error   = kNoReason;
    bool        started = false;
    if ( triggerSource_ == TriggerSource::Level ) {
        // A level starts a ring's record; a stream or a statistics-only
        // capture has no ring to keep the pre-trigger samples in.
        if ( statisticsOnly_ ) {
            failText( scpi::err::kSettingsConflict,
                      "a level trigger needs a ring capture (SENSe:CAPTure DATA, or TRIGger:SOURce "
                      "IMMediate)" );
            return;
        }
        started = sampler_->startTriggered( profile, triggerLevelNa_, triggerRising_, pretrigger_,
                                            info, error );
    } else {
        sampler_->setCaptureLimit( captureLimited_ ? sampleCount_ : 0U );
        started = statisticsOnly_ ? sampler_->startStatistics( profile, info, error )
                                  : sampler_->start( profile, streamMask(), info, error );
    }
    if ( !started ) {
        failSampler( error );
        return;
    }
    captureInfo_ = info;
    noteCaptureStart();
    haveCapture_            = true;
    captureStatisticsOnly_  = statisticsOnly_;
    captureRelays_          = relays_->energized();
    captureEvidence_        = evidence_;
    captureBypassed_        = relays_->shuntBypassed();
    captureBypassedAtStart_ = captureBypassed_;
}

void Console::cmdInitiate( Call& ) noexcept {
    startCapture( false );
}

void Console::cmdInitiateForce( Call& ) noexcept {
    startCapture( true );
}

void Console::cmdAbort( Call& ) noexcept {
    // ABORt is the one way a window of any kind is closed. A null window's
    // result is CALibration:ZERO:RESult?, a capture's FETCh:STATistics?.
    if ( !sampler_->captureOpen() ) {
        failText( scpi::err::kNotRunning, "no capture or null window is open" );
        return;
    }
    // The sampler task acknowledges the stop before anything it accumulated is
    // read. When it does not, the window is closed with no result and the
    // engine refuses a new one until it does (Sampler::halt()).
    const bool wasZero = sampler_->zeroOpen();
    if ( !sampler_->halt() ) {
        captureBypassed_ = false;
        failText(
            scpi::err::kSamplerFailed,
            "the sampler did not acknowledge the stop (the window is closed with no result)" );
        updateHold();
        return;
    }
    if ( wasZero ) {
        finishZeroRun();
        // A held null path gets its whole hold again from the moment the
        // window closes (relay.h kNullPathHoldS).
        updateHold();
        return;
    }
    CaptureStats stats = {};
    sampler_->closeCapture( stats );
    // Anything still queued belongs to this window: written now, before any
    // response of this message, so no sample is lost to the stop itself.
    pumpStream();

    const bool mathOverflow = bits::hasBit( stats.flags, kDiagMathOf );
    lastQuality_ = captureBypassed_
                       ? Quality::Invalid
                       : calibration_->quality( sampler_->captureRange(), stats.dieMilliCelsiusEnd,
                                                mathOverflow );
    lastStats_   = stats;
    lastHoldTeardowns_ = holdTeardowns_;
    paceUnmet_         = stats.paceUnmet;
    haveStats_         = true;
    accumulator_.valid = true;
    captureBypassed_   = false;
    updateHold();
}

// --- TRACe ------------------------------------------------------------------

void Console::cmdMark( Call& call ) noexcept {
    std::array<char, kMarkLabelCapacity + 8U> label = {};
    if ( !textParameter( call.params, 0, label ) ) {
        failParameter( call.params, "use TRACe:MARK \"<label>\"" );
        return;
    }
    const std::string_view text( label.data() );
    if ( !isTokenSafe( text ) || ( text.size() >= kMarkLabelCapacity ) ) {
        text::Writer w = openError();
        w.text( "a label is 1.." );
        w.unsignedDecimal( kMarkLabelCapacity - 1U );
        w.text( " printable characters with no space, comma, quote, ';' or '='" );
        failText( scpi::err::kIllegalParameter, w.view() );
        return;
    }
    if ( !sampler_->running() ) {
        failText( scpi::err::kNotRunning, "no capture is open" );
        return;
    }
    Mark stored = {};
    if ( !sampler_->addMark( label.data(), stored ) ) {
        text::Writer w = openError();
        w.text( "marks full (" );
        w.unsignedDecimal( kMaxMarks );
        w.character( ')' );
        failText( scpi::err::kMarksFull, w.view() );
        return;
    }
    emitMark( stored );
}

void Console::writeDump( bool raw, std::uint32_t from, std::uint32_t count ) noexcept {
    constexpr std::size_t kRawCountsPerLine = 16;

    // The period the implicit timestamps rest on is the one the capture ran
    // with, latched at its start.
    const std::uint32_t period = sampler_->capturedPeriodUs();
    const std::uint64_t t0     = sampler_->captureStartUs();

    // Every index on the wire is the capture's own sample index: the ring's
    // oldest sample is index `first`, 0 unless a level trigger let the ring
    // run circular, and an offset `from` into what it holds is index
    // `first + from`. So `t0_us + index x period_us` holds across a wrap.
    const std::uint32_t first     = sampler_->ringFirstIndex();
    const bool          triggered = sampler_->triggerFired();

    // The CRC covers every byte of the block before METER_DUMP_END.
    crcRunning_ = true;
    crc_        = crc32::kInitial;
    crcBytes_   = 0;

    {
        text::Writer w = openRecord();
        w.text( "METER_DUMP" );
        w.field( "count", count );
        w.field( "t0_us", t0 );
        w.field( "period_us", period );
        if ( raw ) {
            w.field( "fmt", "raw20" );
        } else {
            w.field( "ring", sampler_->ringCapacity() );
        }
        // The opening row gains `first=` when the ring wrapped and `trig_n=`
        // when a level trigger fired, so an untriggered dump is the row it
        // always was.
        if ( triggered ) {
            w.field( "first", first );
            w.field( "trig_n", sampler_->triggerIndex() );
        } else if ( first != 0U ) {
            w.field( "first", first );
        } else {
            // An untriggered capture from sample 0: no extra key.
        }
        endRecord( w );
    }

    // An anchor's record: the sample index it pins and its timestamp.
    const auto anchorRecord = [this]( const Anchor& anchor ) noexcept {
        text::Writer w = openRecord();
        w.text( "A" );
        w.field( "n", anchor.index );
        w.field( "t", anchor.timestampUs );
        endRecord( w );
    };

    const std::uint32_t start = first + from;
    const std::uint32_t last  = start + count;

    // Every anchor the ring holds below the first sample dumped, once up
    // front: a partial dump otherwise carries no drift reference at all. The
    // anchors and the marks are in index order, so one cursor each walks them
    // beside the rows: a row costs nothing for the ones it does not hold.
    const std::size_t anchors = sampler_->anchorCount();
    const std::size_t marks   = sampler_->markCount();
    std::size_t       a       = 0;
    while ( a < anchors && sampler_->anchorAt( a ).index < start ) {
        const Anchor& anchor = sampler_->anchorAt( a );
        if ( anchor.index >= first ) {
            anchorRecord( anchor );
        }
        ++a;
    }
    std::size_t m = 0;
    while ( m < marks && sampler_->markAt( m ).index < start ) {
        ++m;
    }

    std::uint32_t index = start;
    while ( index < last ) {
        // A dump holds the console for seconds: input lost meanwhile is
        // reported as it happens, not after.
        noteOverrun();

        // A row covers a span: anchors, marks and the trigger are emitted for
        // every index inside the row about to be printed, not only the one it
        // starts at.
        const std::uint32_t rowEnd =
            raw ? ( ( last - index < kRawCountsPerLine )
                        ? last
                        : ( index + static_cast<std::uint32_t>( kRawCountsPerLine ) ) )
                : ( index + 1U );

        while ( a < anchors && sampler_->anchorAt( a ).index < rowEnd ) {
            anchorRecord( sampler_->anchorAt( a ) );
            ++a;
        }
        while ( m < marks && sampler_->markAt( m ).index < rowEnd ) {
            const Mark&  mark = sampler_->markAt( m );
            text::Writer w    = openRecord();
            w.text( "K" );
            w.field( "n", mark.index );
            w.field( "label", heldOrAbsent( mark.label ) );
            endRecord( w );
            ++m;
        }
        if ( triggered && sampler_->triggerIndex() >= index && sampler_->triggerIndex() < rowEnd ) {
            text::Writer w = openRecord();
            w.text( "T" );
            w.field( "n", sampler_->triggerIndex() );
            w.field( "t", sampler_->triggerTimeUs() );
            endRecord( w );
        }

        text::Writer w = openRecord();
        if ( !raw ) {
            w.text( "METER t=" );
            w.unsignedDecimal( t0 + static_cast<std::uint64_t>( index ) * period );
            w.field( "ch", kChannelNames[static_cast<std::size_t>( Channel::CurrentMicroamps )] );
            w.key( "v" );
            w.thousandths( sampler_->currentNanoamps( sampler_->ringAt( index - first ) ) );
            endRecord( w );
            ++index;
            continue;
        }

        // rowEnd is at most kRawCountsPerLine past index, so the row's
        // columns are the indices up to it.
        w.text( "R" );
        w.field( "n", index );
        w.key( "c" );
        const std::uint32_t rowStart = index;
        while ( index < rowEnd ) {
            if ( index != rowStart ) {
                w.character( ',' );
            }
            w.signedDecimal( sampler_->ringAt( index - first ) );
            ++index;
        }
        endRecord( w );
    }

    crcRunning_    = false;
    text::Writer w = openRecord();
    w.text( "METER_DUMP_END" );
    w.key( "crc32" );
    w.hex( crc_ ^ crc32::kInitial, 8U, text::LetterCase::Lower );
    w.field( "bytes", crcBytes_ );
    endRecord( w );
}

void Console::cmdTraceData( Call& call ) noexcept {
    constexpr const char* kUsage   = "use TRACe:DATA? [SCALed|RAW][,<from>[,<count>]]";
    const std::uint32_t   captured = sampler_->ringCount();
    if ( captured == 0U ) {
        failText( scpi::err::kDataStale, "no trace captured" );
        return;
    }

    bool        raw   = false;
    std::size_t index = 0;
    const char* first = call.params.raw( 0 );
    if ( ( first != nullptr ) && chars::isLetter( first[0] ) ) {
        static constexpr std::array<std::string_view, 2> kForms = { "SCALed", "RAW" };
        std::size_t                                      which  = 0;
        if ( !call.params.choice( 0, kForms, which ) ) {
            failParameter( call.params, kUsage );
            return;
        }
        raw   = which == 1U;
        index = 1;
    }
    // The table allows the form, a place and a count; with no form written
    // the two numbers are all there is.
    if ( call.params.count() > index + 2U ) {
        failText( scpi::err::kParameterNotAllowed, kUsage );
        return;
    }
    std::uint32_t from = 0;
    if ( call.params.count() > index ) {
        if ( !call.params.unsignedInteger( index, from ) ) {
            failParameter( call.params, kUsage );
            return;
        }
    }
    if ( from >= captured ) {
        text::Writer w = openError();
        w.text( kUsage );
        w.text( " (" );
        w.unsignedDecimal( captured );
        w.text( " samples captured)" );
        failText( scpi::err::kDataOutOfRange, w.view() );
        return;
    }
    std::uint32_t count = captured - from;
    if ( call.params.count() > index + 1U ) {
        std::uint32_t requested = 0;
        if ( !call.params.unsignedInteger( index + 1U, requested ) ) {
            failParameter( call.params, kUsage );
            return;
        }
        if ( requested == 0U ) {
            failText( scpi::err::kDataOutOfRange, kUsage );
            return;
        }
        count = ( requested < count ) ? requested : count;
    }

    // A definite-length arbitrary block (IEEE 488.2 SS 8.7.9): the rows are
    // formatted twice, once to count the bytes for the header and once onto
    // the wire. The ring cannot change in between: TRACe:DATA? is refused
    // while a capture runs.
    counting_     = true;
    countedBytes_ = 0;
    writeDump( raw, from, count );
    counting_ = false;

    // `#`, the count of the length's digits, then the length: sized for the
    // widest std::size_t, not only the byte counts a dump reaches.
    std::array<char, kBlockHeaderCapacity> digits = {};
    text::Writer                           length( digits );
    length.unsignedDecimal( countedBytes_ );
    std::array<char, kBlockHeaderCapacity> header = {};
    text::Writer                           w( header );
    w.character( '#' );
    w.unsignedDecimal( length.size() );
    w.text( length.view() );
    if ( responseUnits_ > 0U ) {
        appendResponse( ";" );
    }
    ++responseUnits_;
    flushResponse();
    write( w.view() );
    writeDump( raw, from, count );
    wirePartial_ = true;
}

void Console::cmdTracePoints( Call& ) noexcept {
    respondNumber( sampler_->ringCount() );
}

void Console::cmdTracePointsMax( Call& ) noexcept {
    respondNumber( sampler_->ringCapacity() );
}

// --- CALibration ------------------------------------------------------------

void Console::saveCalibration() noexcept {
    // The value is live in RAM either way; the failure says it will not
    // survive a reboot.
    if ( !calibration_->save() ) {
        failText( scpi::err::kCalibrationNotSaved, "calibration write failed (live until reboot)" );
    }
}

void Console::cmdZeroStart( Call& call ) noexcept {
    std::uint32_t seconds = 0;
    if ( !call.params.unsignedInteger( 0, seconds ) ) {
        failParameter( call.params, "use CALibration:ZERO <seconds> (1..600)" );
        return;
    }
    // A null assumes the shunt carries nothing but its own offset. With K2
    // across a closed K1 it still carries part of the DUT's current, so that
    // null is refused outright: nothing makes it a null.
    if ( relays_->path() == relay::Path::Both ) {
        failText( scpi::err::kShuntBypassed,
                  "shunt bypassed (path=both: ROUTe:BYPass OFF or ROUTe:NULLpath ON first)" );
        return;
    }
    // A null window refuses every contact move, so one opened under a pending
    // ROUTe:LEAD:CYCLe would refuse its reclose and leave the DUT without its
    // battery for the hold's 120 s instead of the cycle's time (0.15.0).
    if ( leadCycleArmed_ ) {
        failText( scpi::err::kSettingsConflict,
                  "a lead cycle is pending (wait for its reclose, or ROUTe:LEAD CLOSe)" );
        return;
    }
    const char* error = kNoReason;
    if ( !sampler_->startZero( seconds, error ) ) {
        failSampler( error );
        return;
    }
    zeroRelays_   = relays_->energized();
    zeroEvidence_ = evidence_;
    zeroSeconds_  = seconds;
}

void Console::finishZeroRun() noexcept {
    ZeroResult result = {};
    if ( !sampler_->finishZero( result ) ) {
        failText( scpi::err::kZeroNoSamples, "the null window took no samples" );
        return;
    }
    const ShuntRange range = device_->range();
    lastZero_              = result;
    lastZeroRange_         = static_cast<std::uint32_t>( range );
    lastZeroRelays_        = zeroRelays_;
    lastZeroEvidence_      = zeroEvidence_;
    lastZeroStored_        = false;
    haveZero_              = true;

    // K1 was commanded open, so the shunt should have carried its offset and
    // nothing else. A null beyond the residual limit is a lead that did not
    // open, and storing it would subtract the DUT's own current from every
    // later reading under `q=cal`. The numbers are kept; the stored null stands.
    if ( relay::isEnergized( zeroRelays_, relay::kLead )
         && relay::magnitude( result.offsetPicovolts ) > relay::kNullResidualLimitPv ) {
        failText( scpi::err::kNullNotStored,
                  "null not stored (a relay null beyond 50 uV: k1 may not have opened; the stored "
                  "null stands)" );
        return;
    }
    calibration_->setOffset( range, result.offsetPicovolts, result.dieMilliCelsius );
    lastZeroStored_ = true;
    saveCalibration();
}

void Console::cmdZeroResult( Call& ) noexcept {
    if ( sampler_->zeroOpen() ) {
        text::Writer w = openUnit();
        w.field( "running", 1U );
        w.field( "profile", profileSpec( sampler_->profile() ).name );
        w.field( "over_s", zeroSeconds_ );
        w.field( "n_expect", sampler_->zeroSampleCount( zeroSeconds_ ) );
        w.field( "samples", sampler_->sampleCount() );
        w.character( ' ' );
        w.text( relayTokens( zeroRelays_, zeroEvidence_ ) );
        respondText( w.view() );
        return;
    }
    if ( !haveZero_ ) {
        failText( scpi::err::kDataStale, "no null taken since boot" );
        return;
    }
    // The relay tokens are the circuit the window was opened in: `path=bypass`
    // a null with the lead opened by K1, `path=shunt` one resting on the lead
    // having been opened by hand, and `check=` what NULLpath ON saw.
    text::Writer w = openUnit();
    w.field( "running", 0U );
    w.field( "range", lastZeroRange_ );
    signedField( w, "offset_pv", lastZero_.offsetPicovolts );
    signedField( w, "sd_pv", lastZero_.deviationPicovolts );
    w.field( "over_s", lastZero_.seconds );
    w.key( "die_mc" );
    writeDie( w, lastZero_.dieMilliCelsius );
    w.field( "n", lastZero_.samples );
    w.character( ' ' );
    w.text( relayTokens( lastZeroRelays_, lastZeroEvidence_ ) );
    w.field( "stored", lastZeroStored_ ? 1U : 0U );
    respondText( w.view() );
}

void Console::cmdTagSet( Call& call ) noexcept {
    std::array<char, kCalTagCapacity> tag = {};
    if ( !textParameter( call.params, 0, tag ) ) {
        failParameter( call.params, "use CALibration:TAG \"<text>\" (23 characters at most)" );
        return;
    }
    if ( !isTokenSafe( std::string_view( tag.data() ) ) ) {
        failText( scpi::err::kIllegalParameter,
                  "a tag is printable characters with no space, comma, quote, ';' or '='" );
        return;
    }
    // Opaque: the instrument copies it, echoes it, and never acts on it.
    calibration_->setTag( tag.data() );
    saveCalibration();
}

void Console::cmdTagQuery( Call& ) noexcept {
    text::Writer w = openUnit();
    w.character( '"' );
    w.text( chars::untilNul( calibration_->value().tag ) );
    w.character( '"' );
    respondText( w.view() );
}

void Console::cmdDateSet( Call& call ) noexcept {
    constexpr const char* kUsage = "use CALibration:DATE <year>,<month>,<day>";
    std::uint32_t         year   = 0;
    std::uint32_t         month  = 0;
    std::uint32_t         day    = 0;
    if ( call.params.count() < 3U ) {  // the table refuses a fourth
        failText( scpi::err::kMissingParameter, kUsage );
        return;
    }
    // Each part is decoded only while the ones before it were.
    bool decoded = call.params.unsignedInteger( 0, year );
    if ( decoded ) {
        decoded = call.params.unsignedInteger( 1, month );
    }
    if ( decoded ) {
        decoded = call.params.unsignedInteger( 2, day );
    }
    if ( !decoded ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( !isCalendarDate( year, month, day ) ) {
        text::Writer w = openError();
        w.text( "not a calendar date in " );
        w.unsignedDecimal( kMinCalYear );
        w.text( ".." );
        w.unsignedDecimal( kMaxCalYear );
        failText( scpi::err::kDataOutOfRange, w.view() );
        return;
    }
    // Opaque to the instrument, like the tag: the host's date, echoed.
    calibration_->setDate( year * 10000U + month * 100U + day );
    saveCalibration();
}

void Console::cmdDateQuery( Call& ) noexcept {
    const std::uint32_t date = calibration_->value().date;
    text::Writer        w    = openUnit();
    w.unsignedDecimal( date / 10000U );
    w.character( ',' );
    w.unsignedDecimal( ( date / 100U ) % 100U );
    w.character( ',' );
    w.unsignedDecimal( date % 100U );
    respondText( w.view() );
}

void Console::cmdVbusSideSet( Call& call ) noexcept {
    static constexpr std::array<std::string_view, 2> kSides = { "SOURce", "UNKNown" };
    std::size_t                                      which  = 0;
    if ( !call.params.choice( 0, kSides, which ) ) {
        failParameter( call.params, "use CALibration:VBUSside SOURce|UNKNown" );
        return;
    }
    calibration_->setVbusSide( ( which == 0U ) ? VbusSide::Source : VbusSide::Unknown );
    saveCalibration();
}

void Console::cmdVbusSideQuery( Call& ) noexcept {
    respondText( ( calibration_->value().vbusSide == VbusSide::Source )
                     ? std::string_view( "SOURCE" )
                     : std::string_view( "UNKNOWN" ) );
}

void Console::cmdCalData( Call& ) noexcept {
    const Calibration& cal = calibration_->value();
    text::Writer       w   = openUnit();
    w.field( "shunt_uohm", cal.shuntMicroOhm );
    w.field( "shunt_measured", cal.shuntMeasured ? 1U : 0U );
    signedField( w, "off_pv_r0", cal.offsetPicovolts[0] );
    signedField( w, "off_pv_r1", cal.offsetPicovolts[1] );
    w.field( "nulled_r0", cal.nulled[0] ? 1U : 0U );
    w.field( "nulled_r1", cal.nulled[1] ? 1U : 0U );
    w.key( "off_die_mc_r0" );
    writeDie( w, cal.offsetDieMilliCelsius[0] );
    w.key( "off_die_mc_r1" );
    writeDie( w, cal.offsetDieMilliCelsius[1] );
    w.field( "vbus_side", vbusSideName( cal.vbusSide ) );
    w.field( "cal_tag", heldOrAbsent( cal.tag ) );
    // Appended, never interleaved: a report key keeps its place.
    w.field( "shunt_uohm_r0", calibration_->shuntMicroOhm( ina228::ShuntRange::Wide163mV ) );
    w.field( "shunt_uohm_r1", calibration_->shuntMicroOhm( static_cast<ina228::ShuntRange>( 1 ) ) );
    w.key( "cal_date" );
    if ( cal.date == 0U ) {
        w.text( kAbsent );
    } else {
        w.zeroPadded( cal.date / 10000U, 4U );
        w.character( '-' );
        w.zeroPadded( ( cal.date / 100U ) % 100U, 2U );
        w.character( '-' );
        w.zeroPadded( cal.date % 100U, 2U );
    }
    respondText( w.view() );
}

// --- DIAGnostic -------------------------------------------------------------

void Console::cmdShuntReferenceSet( Call& call ) noexcept {
    std::int64_t microOhm = 0;
    if ( !call.params.scaled( 0, 6, microOhm ) ) {
        failParameter( call.params, "use CALibration:SHUNt:REFerence <ohms>" );
        return;
    }
    if ( microOhm < static_cast<std::int64_t>( calmath::kMinReferenceMicroOhm )
         || microOhm > static_cast<std::int64_t>( calmath::kMaxReferenceMicroOhm ) ) {
        failText( scpi::err::kDataOutOfRange, "use CALibration:SHUNt:REFerence <ohms> (1..4000)" );
        return;
    }
    shuntReferenceMicroOhm_ = static_cast<std::uint32_t>( microOhm );
}

void Console::cmdShuntReferenceQuery( Call& ) noexcept {
    respondScaled( shuntReferenceMicroOhm_, 6U );
}

void Console::cmdShuntMeasure( Call& ) noexcept {
    // The shunt's drop is only the current's once its offset is taken out, so
    // a range with no null of its own is refused rather than measured with a
    // zero offset that would read as part of the resistance.
    const ShuntRange range = device_->range();
    if ( !calibration_->nulled( range ) ) {
        text::Writer w = openError();
        w.text( "range " );
        w.unsignedDecimal( static_cast<std::uint32_t>( range ) );
        w.text( " has no null (CALibration:ZERO first)" );
        failText( scpi::err::kSettingsConflict, w.view() );
        return;
    }
    if ( !acquire() ) {
        return;
    }
    const std::uint32_t n = accumulator_.busSamples;
    if ( n == 0U ) {
        failText( scpi::err::kDataStale,
                  "the acquisition converted no bus voltage (SENSe:PROFile STATe or SLEep)" );
        return;
    }
    const std::int64_t shuntNanovolts =
        accumulator_.sumCorrectedPv / static_cast<std::int64_t>( n ) / scale::kThousand;
    const std::int64_t referenceUv = accumulator_.sumBusUv / static_cast<std::int64_t>( n );

    shuntPoint_ =
        calmath::shuntFromReference( shuntNanovolts, referenceUv, n, shuntReferenceMicroOhm_ );
    shuntPointRange_             = static_cast<std::uint32_t>( range );
    shuntPointNanovolts_         = shuntNanovolts;
    shuntPointReferenceUv_       = referenceUv;
    shuntPointSamples_           = n;
    shuntPointReferenceMicroOhm_ = shuntReferenceMicroOhm_;
    shuntPointDieMc_             = lastStats_.dieMilliCelsiusEnd;
    haveShuntPoint_              = true;
}

void Console::cmdShuntResult( Call& ) noexcept {
    if ( !haveShuntPoint_ ) {
        failText( scpi::err::kDataStale, "no shunt point measured since boot (CALibration:SHUNt)" );
        return;
    }
    // `r_uohm` is the figure SENSe:CURRent:SHUNt would store, rounded from
    // `r_nohm`; nothing is stored here: the host sets the shunt once both
    // ranges agree (calibration/README.md).
    text::Writer w = openUnit();
    w.field( "range", shuntPointRange_ );
    signedField( w, "r_nohm", shuntPoint_.nanoOhm );
    signedField( w, "r_uohm", calmath::divideRounded( shuntPoint_.nanoOhm, 1000 ) );
    signedField( w, "dev_ppm", shuntPoint_.deviationPpm );
    signedField( w, "i_na", shuntPoint_.currentNa );
    signedField( w, "vsh_nv", shuntPointNanovolts_ );
    signedField( w, "vref_uv", shuntPointReferenceUv_ );
    w.field( "ref_uohm", shuntPointReferenceMicroOhm_ );
    w.field( "n", shuntPointSamples_ );
    w.key( "die_mc" );
    writeDie( w, shuntPointDieMc_ );
    w.field( "check", calmath::verdictName( shuntPoint_.verdict ) );
    respondText( w.view() );
}

void Console::cmdDiagAlert( Call& ) noexcept {
    std::uint16_t   diag = 0;
    const esp_err_t err  = device_->readDiagAlrt( diag );
    if ( err != ESP_OK ) {
        failI2c( err, "" );
        return;
    }
    text::Writer w = openUnit();
    hexField( w, "raw", diag, 4U );
    w.field( "memstat", bits::hasBit( diag, kDiagMemStat ) ? 1U : 0U );
    w.field( "mathof", bits::hasBit( diag, kDiagMathOf ) ? 1U : 0U );
    w.field( "energyof", bits::hasBit( diag, kDiagEnergyOf ) ? 1U : 0U );
    w.field( "chargeof", bits::hasBit( diag, kDiagChargeOf ) ? 1U : 0U );
    w.field( "cnvrf", bits::hasBit( diag, kDiagCnvrf ) ? 1U : 0U );
    respondText( w.view() );
}

void Console::cmdDiagHeap( Call& ) noexcept {
    // The counts since the steady state began (heap_monitor.h), then the heap
    // as ESP-IDF reports it. Reads only: allowed while a capture runs.
    const heap::Counts counts = heap::counts();
    text::Writer       w      = openUnit();
    w.field( "armed", counts.armed ? 1U : 0U );
    w.field( "allocs_after_init", counts.allocsAfterInit );
    w.field( "frees_after_init", counts.freesAfterInit );
    w.field( "maint_allocs", counts.maintenanceAllocs );
    w.field( "maint_frees", counts.maintenanceFrees );
    w.field( "free_internal", heap_caps_get_free_size( MALLOC_CAP_INTERNAL ) );
    w.field( "min_free_internal", heap_caps_get_minimum_free_size( MALLOC_CAP_INTERNAL ) );
    w.field( "largest_internal", heap_caps_get_largest_free_block( MALLOC_CAP_INTERNAL ) );
    w.field( "free_spiram", heap_caps_get_free_size( MALLOC_CAP_SPIRAM ) );
    // FW-07: the counts by phase, from the first allocation of ESP-IDF's
    // startup, appended so that every key keeps its place.
    w.field( "pre_main_allocs", counts.preMainAllocs );
    w.field( "pre_main_frees", counts.preMainFrees );
    w.field( "allocs_since_main", counts.allocsSinceMain );
    w.field( "frees_since_main", counts.freesSinceMain );
    respondText( w.view() );
}

void Console::cmdDiagHeapCensus( Call& ) noexcept {
    const heap::CensusSummary summary = heap::censusSummary();
    text::Writer              w       = openUnit();
    w.field( "capacity", summary.capacity );
    w.field( "frames", heap::kCensusFrames );
    w.field( "events", summary.events );
    w.field( "first", summary.first );
    respondText( w.view() );
}

void Console::cmdDiagHeapCensusEvent( Call& call ) noexcept {
    std::uint32_t index = 0;
    if ( !call.params.unsignedInteger( 0, index ) ) {
        failParameter( call.params, "use DIAGnostic:HEAP:CENSus:EVENt? <n>" );
        return;
    }
    heap::CensusEvent event;
    if ( !heap::censusEvent( index, event ) ) {
        // Not yet recorded, overwritten by newer ones, or being written.
        text::Writer w = openError();
        w.text( "census event " );
        w.unsignedDecimal( index );
        w.text( " not held" );
        failText( scpi::err::kDataOutOfRange, w.view() );
        return;
    }
    text::Writer w = openUnit();
    w.field( "n", event.index );
    w.field( "op", event.allocation ? std::string_view( "alloc" ) : std::string_view( "free" ) );
    w.field( "phase", event.beforeMain ? std::string_view( "pre" ) : std::string_view( "main" ) );
    w.field( "size", event.size );
    w.key( "caps" );
    w.text( "0x" );
    w.hex( event.caps, 8U, text::LetterCase::Upper );
    w.key( "block" );
    w.text( "0x" );
    w.hex( event.block, 0U, text::LetterCase::Lower );
    w.key( "callers" );
    std::size_t shown = 0;
    for ( const std::uintptr_t frame : event.callers ) {
        if ( frame == 0U ) {
            break;
        }
        if ( shown != 0U ) {
            w.character( ',' );
        }
        w.text( "0x" );
        w.hex( frame, 0U, text::LetterCase::Lower );
        ++shown;
    }
    if ( shown == 0U ) {
        w.text( kAbsent );
    }
    respondText( w.view() );
}

void Console::cmdDiagScan( Call& ) noexcept {
    std::array<std::uint8_t, kAddressCount> found = {};
    const std::size_t                       count = device_->scan( found.data(), found.size() );
    text::Writer                            w     = openUnit();
    hexField( w, "expected", kAddressDefault, 2U );
    w.key( "found" );
    if ( count == 0U ) {
        w.text( kAbsent );
    }
    for ( std::size_t i = 0; i < count; ++i ) {
        if ( i != 0U ) {
            w.character( ',' );
        }
        w.text( "0x" );
        w.hex( found[i], 2U, text::LetterCase::Upper );
    }
    w.field( "n", count );
    respondText( w.view() );
}

void Console::cmdDiagRegister( Call& call ) noexcept {
    constexpr const char* kUsage  = "use DIAGnostic:REGister? <addr> (#H00..#HFF)";
    std::uint32_t         address = 0;
    if ( !call.params.unsignedInteger( 0, address ) ) {
        failParameter( call.params, kUsage );
        return;
    }
    if ( address > 0xFFU ) {
        failText( scpi::err::kDataOutOfRange, kUsage );
        return;
    }
    const auto        reg   = static_cast<std::uint8_t>( address );
    const std::size_t width = registerWidthBytes( reg );
    if ( width == 0U ) {
        // A reserved address is refused rather than read with a guessed width.
        text::Writer w = openError();
        w.text( "reserved register 0x" );
        w.hex( reg, 2U, text::LetterCase::Upper );
        failText( scpi::err::kIllegalParameter, w.view() );
        return;
    }
    std::uint64_t   value = 0;
    const esp_err_t err   = device_->readRegister( reg, width, value );
    if ( err != ESP_OK ) {
        failI2c( err, "" );
        return;
    }
    text::Writer w = openUnit();
    hexField( w, "addr", reg, 2U );
    w.field( "bytes", width );
    hexField( w, "raw", value, width * 2U );
    respondText( w.view() );
}

}  // namespace kleidos::meter
