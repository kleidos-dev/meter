// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file sampler.cpp
 * @ingroup sampling
 * @brief The sampler task: pacing, reading, the ring and the null.
 */
#include "sampler.h"

#include "bits.h"
#include "pin_table.h"
#include "rtos_ticks.h"
#include "scale.h"
#include "text.h"
#include "watchdog.h"

#include <esp_attr.h>
#include <esp_timer.h>
#include <freertos/task.h>
#include <hal/gpio_ll.h>
#include <soc/interrupts.h>

#include <algorithm>
#include <array>

namespace kleidos::meter {

/// The register map is referred to unqualified throughout this file; it is the
/// one vocabulary the sampler speaks, and every name in it carries its
/// datasheet citation at its definition in ina228_regs.h.
using namespace ina228;

namespace {

/// Sampler task: Core 1, the UI-free core of this instrument, so the
/// latency-critical loop never contends with anything that blocks. The console
/// owns Core 0 (`docs/architecture.md` SS 5.1).
constexpr BaseType_t  kSamplerCore     = 1;
constexpr UBaseType_t kSamplerPriority = 10;  ///< The sampler task's priority.

/// How long the idle sampler waits for a start before it checks in with the
/// watchdog and waits again (0.14.0; watchdog.h).
constexpr std::uint32_t kIdleFeedMs = 1000;

/// How many times a ring capture writes the INA228's pointer before it gives
/// up (Sampler::retainShuntPointer()): a failed transfer is recovered by the
/// bus, so a second write normally takes.
constexpr std::uint32_t kPointerWrites = 3;

/// About how often a running capture checks in with the watchdog (0.14.0):
/// every this many microseconds' worth of samples, counted rather than timed so
/// that the loop reads no clock for it.
constexpr std::uint32_t kCaptureFeedUs = 200000;

/// Below this remainder the timer-paced loop spins instead of delaying: a
/// 280 us cadence is below the FreeRTOS tick, so no blocking primitive can
/// express it. The spin is why the watchdog supervises Core 0's idle task and
/// not Core 1's (watchdog.h).
constexpr std::int64_t kSpinThresholdUs = 2000;

/// Conversions the pacing probe counts before deciding, reading DIAG_ALRT after
/// each edge to release the pin as the capture will. Within +/-1 of this and
/// the pin makes an edge per conversion; otherwise it is absent or ringing and
/// the host timer paces instead.
constexpr std::uint32_t kPaceProbeConversions = 8;
constexpr std::uint32_t kPaceProbeTolerance   = 1;  ///< The edges the count may be off by.

/// Periods `*TST?`'s probe lets pass after its ADC_CONFIG and DIAG_ALRT
/// writes, so that a conversion begun after them has ended before the probe's
/// first DIAG_ALRT read. A conversion that ends inside the ADC_CONFIG write
/// pulls `ALE` low and the write clears CNVRF behind it; a read then has no
/// flag to clear and leaves the pin low, and the probe counts no edge. The
/// first conversion after the write ended 1.35 ms after it on the bench, a
/// STATE period and 0.2 ms (2026-11-21), so one period is not enough and two
/// are (`docs/architecture.md` SS 3.3).
constexpr std::uint32_t kSelfTestSettlePeriods = 2;

/// How long the console waits for the sampler task to run a probe, beyond the
/// probe's own window. The task answers within microseconds of the window's
/// end; every I2C transfer has its own 20 ms timeout, so a task that has not
/// answered by then is stuck, and the probe says `timer`.
constexpr std::uint32_t kProbeAnswerMarginMs = 500;

/// How long `halt()` waits for the sampler task to acknowledge a stop. One SLEEP
/// period is 67.5 ms and every I2C transfer has its own 20 ms timeout, so a
/// loop that has not answered by then is stuck, and the stop fails (107).
constexpr std::uint32_t kStopTimeoutMs = 500;

/// @brief How long an alert-paced loop waits for an edge before it takes the
/// pin for dead: two periods and 10 ms.
/// @param[in] periodUs The sample period, microseconds.
/// @return The wait, milliseconds.
IRAM_ATTR constexpr std::uint32_t alertTimeoutMs( std::uint32_t periodUs ) noexcept {
    return ( ( 2U * periodUs ) / 1000U ) + 10U;
}

/// GPIOs 0 to 31 latch their interrupt status in one register
/// (`GPIO_STATUS_REG`, read through `pcpu_int`), which is the one alertIsr()
/// reads: the ALERT pin must be one of them (GPIO6 on this rig, main.cpp).
constexpr std::uint32_t kStatusBankPins = 32;

// The board's table (pin_table.h, generated from firmware/board/pins.toml)
// gives ALERT the same limit, so the pin map refuses an ALERT pin this
// handler could not read.
static_assert( static_cast<std::uint32_t>(
                   pin_table::kRoleBelow[static_cast<std::size_t>( pin_table::Role::Alert )] )
                   == kStatusBankPins,
               "the table's limit for ALERT is not the status register's bank" );

/// @brief Whether @p pin latches its interrupt in the status register
///        alertIsr() reads.
/// @param[in] pin The GPIO.
/// @return True for GPIO0 to GPIO31; false for the rest and GPIO_NUM_NC.
constexpr bool inStatusBank( gpio_num_t pin ) noexcept {
    return static_cast<std::uint32_t>( pin ) < kStatusBankPins;
}

/// @brief @p pin's bit in the interrupt status register.
/// @param[in] pin The GPIO, one inStatusBank() accepts.
/// @return The bit.
IRAM_ATTR constexpr std::uint32_t statusBit( gpio_num_t pin ) noexcept {
    return 1U << static_cast<std::uint32_t>( pin );
}

/// @brief Starts taking @p pin's edges: clears an edge latched while nothing
///        listened, then sets the pin's interrupt enable, as ESP-IDF's
///        `gpio_intr_enable()` did (`gpio_hal_intr_enable_on_core()`).
/// @param[in] pin   The ALERT pin.
/// @param[in] route Where its interrupt is routed: the core named to the
///                  enable (the ESP32-S3 has one enable for both cores and
///                  ignores it).
IRAM_ATTR void listen( gpio_num_t pin, const interrupt::Route& route ) noexcept {
    gpio_ll_clear_intr_status( &GPIO, statusBit( pin ) );
    gpio_ll_intr_enable_on_core( &GPIO, static_cast<std::uint32_t>( route.core ),
                                 static_cast<std::uint32_t>( pin ) );
}

/// @brief Stops taking @p pin's edges: clears the enable, then the status bit,
///        as ESP-IDF's `gpio_intr_disable()` did (`gpio_hal_intr_disable()`).
/// @param[in] pin The ALERT pin.
IRAM_ATTR void stopListening( gpio_num_t pin ) noexcept {
    gpio_ll_intr_disable( &GPIO, static_cast<std::uint32_t>( pin ) );
    gpio_ll_clear_intr_status( &GPIO, statusBit( pin ) );
}

/// Scale applied inside the standard-deviation square root, for three decimal
/// digits of resolution in a result reported as an integer.
constexpr std::uint64_t kDeviationScale = 1000000;

/// The largest sample variance two 20-bit counts can give, in counts squared,
/// rounded up to a power of two: the bound deviationMilliCounts() rests on.
constexpr std::uint64_t kMaxVarianceCounts = 1ULL << 40U;
static_assert( kMaxVarianceCounts <= UINT64_MAX / kDeviationScale,
               "the scaled variance of a capture must fit 64 bits" );

/// The four profiles. Each ADC_CONFIG word is **derived** from the field
/// encodings and checked below against the value the design states
/// (`docs/architecture.md` SS 3.2); none is a copied literal. In internal
/// RAM, not flash: the loop reads its profile's row every sample (0.16.1,
/// `docs/architecture.md` SS 5.2).
DRAM_ATTR constexpr ProfileSpec kProfileSpecs[] = {
    // TRACE -- shape a burst. MODE Ah (shunt only) and AVG 1, because averaging
    // replaces N conversions with their mean and so dilutes an event shorter
    // than the window: at AVG 16 a 1 ms, +100 mA burst shows up once, as
    // +22 mA, with no way to tell a long small event from a short large one.
    // At AVG 1 it spans three to four consecutive samples. Average afterwards,
    // in software, where the raw stream is still there to look at.
    { "TRACE", kModeContinuousShunt, kCt50us, kCt280us, kCt50us, kAvg1,
      adcConfigWord( kModeContinuousShunt, kCt50us, kCt280us, kCt50us, kAvg1 ), 1,
      periodMicroseconds( kModeContinuousShunt, kCt50us, kCt280us, kCt50us, kAvg1 ), true, false,
      false, 0 },

    // STATE -- measure a state. MODE Fh converts temperature too, for +4.5 % of
    // the period, because die temperature is the quantity that tells you the
    // offset null has gone stale.
    { "STATE", kModeContinuousAll, kCt50us, kCt1052us, kCt50us, kAvg1,
      adcConfigWord( kModeContinuousAll, kCt50us, kCt1052us, kCt50us, kAvg1 ), 1,
      periodMicroseconds( kModeContinuousAll, kCt50us, kCt1052us, kCt50us, kAvg1 ), false, false,
      true, channelBit( Channel::CurrentMicroamps ) },

    // SLEEP -- measure deep sleep. The longest conversion time and AVG 16;
    // 2.3 uA rms per point, 0.19 uA rms after 10 s of host averaging, which is
    // already inside the +/-0.5 uA the offset null leaves. DIAG_ALRT is read
    // every sample here: at 0.8 % bus duty it is free, and this is the profile
    // where a latched MATHOF would otherwise go unseen for a whole window.
    { "SLEEP", kModeContinuousAll, kCt50us, kCt4120us, kCt50us, kAvg16,
      adcConfigWord( kModeContinuousAll, kCt50us, kCt4120us, kCt50us, kAvg16 ), 1,
      periodMicroseconds( kModeContinuousAll, kCt50us, kCt4120us, kCt50us, kAvg16 ), false, true,
      true,
      channelBit( Channel::CurrentMicroamps ) | channelBit( Channel::VoltageMillivolts )
          | channelBit( Channel::DieCelsius ) },

    // BURST -- resolve a transient. The device's shortest conversion, so the
    // shunt notch sits at 10 kHz instead of 1,785 Hz and a 200 us edge is four
    // samples wide rather than one. It buys time resolution and nothing else:
    // a 50 us conversion is noisier per point by about the square root of the
    // ratio of the windows, so the mean of a whole second is no better here
    // than TRACE's. Use it to see an event, not to put a number on a level.
    //
    // Its offset is its own, too: with the bus silent, the CHARGE integral of
    // a 50 us conversion reads 9.5 uA (0.95 uV) below TRACE's with the shunt
    // carrying nothing, and 27 uA below it under a 38 uA DUT (bench,
    // 2026-11-20, `docs/architecture.md` SS 3.4), so a null taken on
    // another profile does not apply to it.
    //
    // It is also the one profile the bus can refuse: 38 us of retained read at
    // 1 MHz fits a 50 us period with a quarter of it to spare, while the same
    // read at the 400 kHz default takes 95 us and cannot keep up at all.
    // `INITiate` checks that against the live bus speed rather than trusting this row.
    { "BURST", kModeContinuousShunt, kCt50us, kCt50us, kCt50us, kAvg1,
      adcConfigWord( kModeContinuousShunt, kCt50us, kCt50us, kCt50us, kAvg1 ), 2,
      2U * periodMicroseconds( kModeContinuousShunt, kCt50us, kCt50us, kCt50us, kAvg1 ), true,
      false, false, 0 },
};

static_assert( sizeof( kProfileSpecs ) / sizeof( kProfileSpecs[0] ) == kProfileCount,
               "one row per profile, in Profile's order" );

static_assert( kProfileSpecs[0].adcConfig == 0xA0C0U, "TRACE must derive to 0xA0C0" );
static_assert( kProfileSpecs[1].adcConfig == 0xF140U, "STATE must derive to 0xF140" );
static_assert( kProfileSpecs[2].adcConfig == 0xF1C2U, "SLEEP must derive to 0xF1C2" );
static_assert( kProfileSpecs[0].periodUs == 280U, "TRACE period is 280 us" );
static_assert( kProfileSpecs[1].periodUs == 1152U, "STATE period is (50+1052+50) us" );
static_assert( kProfileSpecs[2].periodUs == 67520U, "SLEEP period is (50+4120+50) x 16 us" );
static_assert( kProfileSpecs[3].adcConfig == 0xA000U, "BURST must derive to 0xA000" );
static_assert( kProfileSpecs[3].periodUs == 100U, "BURST period is two 50 us conversions" );
static_assert( kProfileSpecs[2].periodUs < kCaptureFeedUs,
               "the slowest profile still feeds the watchdog once per 200 ms of samples" );
static_assert( shuntNotchHz( kProfileSpecs[0].vShCt ) == 1785U, "TRACE notch is 1/(2 x 280 us)" );
static_assert( shuntNotchHz( kProfileSpecs[3].vShCt ) == 10000U, "BURST notch is 1/(2 x 50 us)" );
static_assert( !kProfileSpecs[0].pacedByAlert && !kProfileSpecs[3].pacedByAlert,
               "a ring profile keeps its retained pointer: no DIAG_ALRT read per sample" );
static_assert( kProfileSpecs[1].pacedByAlert && kProfileSpecs[2].pacedByAlert,
               "STATE and SLEEP are paced by the ALERT pin" );

// Facts about every profile the code relies on instead of testing them at run
// time (FW-27): a branch that could only ever go one way is a compile-time
// fact here, and a profile that broke one would not build. Written row by row,
// so a fifth profile is added to each, as the count below asks.
static_assert( kProfileCount == 4U, "a new profile is held to every fact below: extend them" );

static_assert( modeConvertsShunt( kProfileSpecs[0].mode )
                   && modeConvertsShunt( kProfileSpecs[1].mode )
                   && modeConvertsShunt( kProfileSpecs[2].mode )
                   && modeConvertsShunt( kProfileSpecs[3].mode ),
               "every profile converts the shunt: the current aperture and the INA228 report "
               "state the shunt's conversion time without asking (console_commands.cpp)" );

static_assert( ( kProfileSpecs[0].periodUs > 0U ) && ( kProfileSpecs[1].periodUs > 0U )
                   && ( kProfileSpecs[2].periodUs > 0U ) && ( kProfileSpecs[3].periodUs > 0U ),
               "every period is positive: the pace error divides by it (closeCapture())" );

/// One second of samples: a null window's shortest.
constexpr std::uint32_t kMicrosecondsPerSecond = 1000000U;
static_assert( ( kProfileSpecs[0].periodUs <= kMicrosecondsPerSecond )
                   && ( kProfileSpecs[1].periodUs <= kMicrosecondsPerSecond )
                   && ( kProfileSpecs[2].periodUs <= kMicrosecondsPerSecond )
                   && ( kProfileSpecs[3].periodUs <= kMicrosecondsPerSecond ),
               "every profile samples at least once a second, so a null window of one second "
               "or more always has samples to take (startZero())" );

/// The bytes a second of one sample of every channel takes on the line, at
/// the longest line the stream writes (sampler.h kStreamLineBytes): divided by
/// a streaming profile's period, the rate its widest stream needs.
constexpr std::uint32_t kEveryChannelLineByteMicroseconds =
    static_cast<std::uint32_t>( kChannelCount ) * kStreamLineBytes * kMicrosecondsPerSecond;
static_assert( kProfileSpecs[0].capturesToRing && kProfileSpecs[3].capturesToRing
                   && !kProfileSpecs[1].capturesToRing && !kProfileSpecs[2].capturesToRing,
               "STATE and SLEEP are the streaming profiles, TRACE and BURST capture to RAM" );
static_assert( ( kEveryChannelLineByteMicroseconds / kProfileSpecs[1].periodUs
                 <= kStreamCapBytesPerSecond )
                   && ( kEveryChannelLineByteMicroseconds / kProfileSpecs[2].periodUs
                        <= kStreamCapBytesPerSecond ),
               "a streaming profile carries every channel inside the stream's share of the "
               "line: no channel set is refused at INITiate, and none can overrun the line" );

// --- The averaging override's limits (docs/architecture.md SS 3.2) ----------

/// The longest period an averaging override may give: kCaptureFeedUs, so that
/// the loop checks in with the watchdog at least once a sample.
constexpr std::uint32_t kLongestAveragedPeriodUs = kCaptureFeedUs;
static_assert( alertTimeoutMs( kLongestAveragedPeriodUs ) < kStopTimeoutMs,
               "a stop is acknowledged in time however long the sample, the ALERT wire dead" );
static_assert( kLongestAveragedPeriodUs * ( 1U + kPaceProbeConversions )
                   < watchdog::kTimeoutSeconds * kMicrosecondsPerSecond / 2U,
               "INITiate's first conversion and its pacing probe hold the console well under "
               "the watchdog's timeout" );

/// The shortest period an override may give a ring profile: BURST's, the
/// shortest its timer-paced loop has been shown to keep
/// (docs/architecture.md SS 3.4).
constexpr std::uint32_t kShortestRingPeriodUs = kProfileSpecs[3].periodUs;

/// What a streamed sample's reads take on the bus: the DIAG_ALRT that
/// releases the ALERT pin, VSHUNT, VBUS and DIETEMP (readSample()).
constexpr std::uint32_t kStreamedReadBitTimes =
    ( 2U * registerReadBitTimes( kWidth16Bytes ) ) + ( 2U * registerReadBitTimes( kWidth24Bytes ) );
static_assert( busMicroseconds( kStreamedReadBitTimes, 400000 ) == 525U,
               "the bus time a streamed sample costs at 400 kHz" );
static_assert( modeConvertsBus( kProfileSpecs[1].mode )
                   && modeConvertsTemperature( kProfileSpecs[1].mode )
                   && modeConvertsBus( kProfileSpecs[2].mode )
                   && modeConvertsTemperature( kProfileSpecs[2].mode ),
               "a streaming profile reads all four registers a sample" );

/// @brief What one sample's reads of @p spec take on the bus, in microseconds.
/// @param[in] spec  The profile, or its override.
/// @param[in] sclHz The bus clock, hertz.
/// @return The time, rounded up.
constexpr std::uint32_t sampleReadMicroseconds( const ProfileSpec& spec,
                                                std::uint32_t      sclHz ) noexcept {
    return spec.capturesToRing ? retainedReadMicroseconds( kWidth24Bytes, sclHz )
                               : busMicroseconds( kStreamedReadBitTimes, sclHz );
}

/// @brief The shortest period an override of @p spec may give at @p sclHz:
/// over twice the bus time a sample's reads take, the margin measured for the
/// ring profiles (beginCapture()), and never under kShortestRingPeriodUs on a
/// ring.
/// @param[in] spec  The profile, or its override.
/// @param[in] sclHz The bus clock, hertz.
/// @return The period, microseconds.
constexpr std::uint32_t shortestPeriodUs( const ProfileSpec& spec, std::uint32_t sclHz ) noexcept {
    const std::uint32_t overTwiceTheReads = ( 2U * sampleReadMicroseconds( spec, sclHz ) ) + 1U;
    return spec.capturesToRing ? std::max( overTwiceTheReads, kShortestRingPeriodUs )
                               : overTwiceTheReads;
}

// Every profile's own period passes the limits at the clock it is used at, so
// turning an override on with the profile's own count and time is accepted.
static_assert( shortestPeriodUs( kProfileSpecs[0], 400000 ) <= kProfileSpecs[0].periodUs
                   && shortestPeriodUs( kProfileSpecs[1], 400000 ) <= kProfileSpecs[1].periodUs
                   && kProfileSpecs[2].periodUs <= kLongestAveragedPeriodUs,
               "an override of the profile's own count and time is accepted" );
static_assert( kEveryChannelLineByteMicroseconds
                       / shortestPeriodUs( kProfileSpecs[1], Ina228Driver::kMaxBusSpeedHz )
                   <= kStreamCapBytesPerSecond,
               "no override gives a stream more than its share of the line, at any bus clock" );

/// @brief @p own with @p averaging's count and shunt conversion time, and the
/// ADC_CONFIG word and the period they give.
/// @param[in] own       The profile's own specification.
/// @param[in] averaging The override's count and time.
/// @return The override's specification.
ProfileSpec averagedSpec( const ProfileSpec& own, const Averaging& averaging ) noexcept {
    ProfileSpec spec = own;
    spec.avg         = averaging.avg;
    spec.vShCt       = averaging.vShCt;
    spec.adcConfig   = adcConfigWord( own.mode, own.vbusCt, spec.vShCt, own.vtCt, spec.avg );
    spec.periodUs    = periodMicroseconds( own.mode, own.vbusCt, spec.vShCt, own.vtCt, spec.avg );
    return spec;
}

/// @brief @p profile's place in a table of every profile.
/// @param[in] profile The profile.
/// @return Its index; TRACE's, 0, for a value outside the table.
std::size_t profileIndex( Profile profile ) noexcept {
    const auto index = static_cast<std::size_t>( profile );
    return ( index < kProfileCount ) ? index : 0U;
}

/// @brief Waits until @p deadlineUs on the monotonic timer, delaying the bulk
/// and spinning only the sub-tick remainder.
/// @param[in] deadlineUs The deadline, esp_timer microseconds.
/// @return The clock as the wait first read it: the time the caller came to
/// the deadline, which says how late it was at no extra read.
IRAM_ATTR std::uint64_t waitUntil( std::uint64_t deadlineUs ) noexcept {
    const std::int64_t arrivedUs = esp_timer_get_time();
    std::int64_t       nowUs     = arrivedUs;
    while ( true ) {
        const std::int64_t remaining = static_cast<std::int64_t>( deadlineUs ) - nowUs;
        if ( remaining <= 0 ) {
            return static_cast<std::uint64_t>( arrivedUs );
        }
        if ( remaining > kSpinThresholdUs ) {
            const auto milliseconds =
                static_cast<std::uint32_t>( ( remaining - kSpinThresholdUs ) / 1000 );
            if ( milliseconds > 0U ) {
                vTaskDelay( rtos::ticksFromMs( milliseconds ) );
            }
        }
        nowUs = esp_timer_get_time();
    }
}

/// The sample standard deviation of @p count values, two or more, in
/// thousandths of a count, from their deviations' sum and sum of squares (any
/// common reference). `( sum(d^2) - sum(d)^2 / n ) / ( n - 1 )` in 128 bits,
/// so neither the square of the sum nor the sum of squares can overflow.
/// @param[in] count        The number of values.
/// @param[in] sumDeviation Their deviations' sum.
/// @param[in] sumSquares   Their deviations' sum of squares.
/// @return The standard deviation, thousandths of a count.
std::uint64_t deviationMilliCounts( std::uint32_t count, std::int64_t sumDeviation,
                                    wide::U128 sumSquares ) noexcept {
    const std::uint64_t magnitude  = ( sumDeviation < 0 )
                                         ? ( 0ULL - static_cast<std::uint64_t>( sumDeviation ) )
                                         : static_cast<std::uint64_t>( sumDeviation );
    const wide::U128    correction = wide::divide( wide::multiply( magnitude, magnitude ), count );
    // Scaled before the division, so the variance keeps six decimals of a
    // count squared: truncated to whole counts first, a variance of 36.9
    // counts^2 would read 36 and its root 1.2 % low (bench, 2026-11-20). A
    // sample variance of 20-bit counts is under 2^40, so the scaled one fits
    // the low word.
    const wide::U128 scaled =
        wide::multiply( wide::subtractClamped( sumSquares, correction ), kDeviationScale );
    return scale::integerSqrt( wide::divide( scaled, count - 1U ).lo );
}

}  // namespace

const char* const kChannelNames[kChannelCount] = { "i_ua", "v_mv", "p_mw", "die_c" };

const ProfileSpec& profileSpec( Profile profile ) noexcept {
    return kProfileSpecs[profileIndex( profile )];
}

// --- Lifecycle --------------------------------------------------------------

bool Sampler::begin( Ina228Driver& device, CalibrationStore& calibration, gpio_num_t alertPin,
                     bool alertPullUp, std::span<std::int32_t> ring ) noexcept {
    device_      = &device;
    calibration_ = &calibration;
    resetAveraging();
    windowSpec_ = profileSpec( profile_.load() );

    if ( ring.size() < kTraceSamplesFloor || ring.size() > kTraceSamples ) {
        return false;
    }
    ring_         = ring.data();
    ringCapacity_ = static_cast<std::uint32_t>( ring.size() );

    // Everything both tasks may reach is built here, before the sampler task
    // exists (the dual-core rule of the Kleidos firmware repository), over
    // storage the Sampler holds: nothing is allocated.
    streamQueue_   = xQueueCreateStatic( kStreamQueueDepth, sizeof( Sample ),
                                         streamQueueStorage_.data(), &streamQueueBuffer_ );
    conversionSem_ = xSemaphoreCreateBinaryStatic( &conversionSemBuffer_ );
    startSem_      = xSemaphoreCreateBinaryStatic( &startSemBuffer_ );
    doneSem_       = xSemaphoreCreateBinaryStatic( &doneSemBuffer_ );
    probeDoneSem_  = xSemaphoreCreateBinaryStatic( &probeDoneSemBuffer_ );
    if ( streamQueue_ == nullptr || conversionSem_ == nullptr || startSem_ == nullptr
         || doneSem_ == nullptr || probeDoneSem_ == nullptr ) {
        return false;
    }

    // Its interrupt is then routed to a level-1 line of this core with
    // alertIsr() on it, by the CPU's own tables (interrupt.h): no ISR service,
    // no allocator record, nothing allocated (docs/misra.md D1, FW-09). The
    // handler is in IRAM (0.16.1), so that its fetch never waits on a cache
    // the other core's work has filled (`docs/architecture.md` SS 5.2).
    if ( !connectAlert( alertPin, alertPullUp ) ) {
        return false;
    }
    if ( interrupt::routeHere( static_cast<std::uint32_t>( ETS_GPIO_INTR_SOURCE ),
                               &Sampler::alertIsr, this, alertRoute_ )
         != ESP_OK ) {
        return false;
    }

    task_ = xTaskCreateStaticPinnedToCore( &Sampler::taskEntry, "meter-sample", kSamplerStackBytes,
                                           this, kSamplerPriority, taskStack_.data(), &taskBuffer_,
                                           kSamplerCore );
    engineReady_ = task_ != nullptr;
    return engineReady_;
}

bool Sampler::connectAlert( gpio_num_t pin, bool pullUp ) noexcept {
    // The ALERT pin is open-drain ([DS] Table 7-16 `APOL`), so it needs a
    // pull-up. The breakout appears to carry one, but the default map enables
    // the internal pull-up as well: if that third resistor is not on ALE, the
    // pin floats and an edge interrupt fires on noise. The internal one is
    // weak (~45 kOhm) and a signal that changes every 280 us needs nothing
    // stronger. A map turns it off when the wiring carries its own. Its
    // interrupt is the sampler's own business (below), so gpio_config()
    // leaves it off: with an interrupt type, it would enable the
    // pin's interrupt for ESP-IDF's GPIO ISR service, which is never installed.
    if ( !inStatusBank( pin ) ) {
        return false;
    }
    alertPin_            = pin;
    gpio_config_t config = {};
    config.pin_bit_mask  = 1ULL << static_cast<std::uint32_t>( pin );
    config.mode          = GPIO_MODE_INPUT;
    config.pull_up_en    = pullUp ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE;
    config.pull_down_en  = GPIO_PULLDOWN_DISABLE;
    config.intr_type     = GPIO_INTR_DISABLE;
    if ( gpio_config( &config ) != ESP_OK ) {
        return false;
    }
    // The falling edge (APOL = 0: active low) latches the pin's status bit,
    // and the bit raises the GPIO's interrupt while the pin's enable is set
    // (listen()).
    gpio_ll_set_intr_type( &GPIO, static_cast<std::uint32_t>( pin ), GPIO_INTR_NEGEDGE );
    stopListening( pin );
    return true;
}

void Sampler::moveAlert( gpio_num_t pin, bool pullUp ) noexcept {
    if ( task_ == nullptr ) {
        return;  // begin() did not start the engine: there is nothing to move
    }
    // The pin ALERT leaves latches nothing more, whatever takes it next.
    stopListening( alertPin_ );
    gpio_ll_set_intr_type( &GPIO, static_cast<std::uint32_t>( alertPin_ ), GPIO_INTR_DISABLE );
    engineReady_ = connectAlert( pin, pullUp );
}

const char* Sampler::sinkName() const noexcept {
    // In Sink's order: sink_ only ever holds one of its enumerators.
    static constexpr std::array<const char*, kSinkCount> kNames = { "idle", "ring", "stream",
                                                                    "null", "stats" };
    return kNames[static_cast<std::size_t>( sink_.load() )];
}

std::uint64_t Sampler::dieAgeUs() const noexcept {
    const std::uint64_t stamp = lastDieStampUs_.load();
    if ( stamp == 0U ) {
        return 0;
    }
    const auto now = static_cast<std::uint64_t>( esp_timer_get_time() );
    return ( now > stamp ) ? ( now - stamp ) : 0U;
}

IRAM_ATTR void Sampler::noteDieMilliCelsius( std::int32_t milliCelsius ) noexcept {
    lastDieMilliCelsius_.store( milliCelsius );
    // A sentinel is not a reading, so it carries no stamp -- `dieAgeUs()` then
    // reports 0 beside a `die_mc=unknown` rather than an age for nothing.
    lastDieStampUs_.store( ( milliCelsius == kDieMilliCelsiusUnknown )
                               ? 0U
                               : static_cast<std::uint64_t>( esp_timer_get_time() ) );
}

std::int64_t Sampler::readChargeRaw( bool& valid ) noexcept {
    std::uint64_t raw40 = 0;
    valid               = device_->read40( kRegCharge, raw40 ) == ESP_OK;
    return valid ? scale::decode40( raw40 ) : 0;
}

IRAM_ATTR void Sampler::alertIsr( void* context ) noexcept {
    auto*         self    = static_cast<Sampler*>( context );
    std::uint32_t pending = 0;
    gpio_ll_get_intr_status( &GPIO, static_cast<std::uint32_t>( self->alertRoute_.core ),
                             &pending );
    // Every pending bit is cleared, the ALERT pin's or not: the line is
    // level-triggered, and a bit left set raises it again as this returns.
    gpio_ll_clear_intr_status( &GPIO, pending );
    if ( ( pending & statusBit( self->alertPin_ ) ) == 0U ) {
        return;
    }
    (void)self->alertEdges_.fetch_add( 1 );
    BaseType_t higherPriorityWoken = pdFALSE;
    (void)xSemaphoreGiveFromISR( self->conversionSem_, &higherPriorityWoken );
    if ( higherPriorityWoken == pdTRUE ) {
        portYIELD_FROM_ISR();
    }
}

void Sampler::taskEntry( void* context ) noexcept {
    static_cast<Sampler*>( context )->runCapture();
}

// --- Starting and stopping --------------------------------------------------

void Sampler::snapshotCalibration() noexcept {
    captureRange_         = device_->range();
    captureShuntLsbPv_    = shuntLsbPicovolts( captureRange_ );
    captureOffsetPv_      = calibration_->offsetPicovolts( captureRange_ );
    captureShuntMicroOhm_ = calibration_->shuntMicroOhm( captureRange_ );
}

IRAM_ATTR std::int64_t Sampler::currentNanoamps( std::int32_t shuntCounts ) const noexcept {
    const std::int64_t shuntPv = scale::shuntPicovolts( shuntCounts, captureShuntLsbPv_ );
    return scale::currentNanoamps( shuntPv, captureOffsetPv_, captureShuntMicroOhm_ );
}

std::int32_t Sampler::readConsistencyDelta() noexcept {
    // CURRENT_LSB equals V_LSB / R exactly in both ranges, so CURRENT counts
    // track VSHUNT counts one for one up to the device's internal rounding.
    // Anything beyond a couple of counts means SHUNT_CAL is not what we think
    // it is -- a free check, run once per capture and reported in the header.
    std::uint32_t shuntRaw   = 0;
    std::uint32_t currentRaw = 0;
    esp_err_t     status     = device_->read24( kRegVShunt, shuntRaw );
    if ( status == ESP_OK ) {
        status = device_->read24( kRegCurrent, currentRaw );
    }
    if ( status != ESP_OK ) {
        return 0;
    }
    return scale::decode20( currentRaw ) - scale::decode20( shuntRaw );
}

std::int32_t Sampler::readDieMilliCelsius( const ProfileSpec& spec ) noexcept {
    if ( modeConvertsTemperature( spec.mode ) ) {
        std::uint16_t raw = 0;
        if ( device_->read16( kRegDieTemp, raw ) != ESP_OK ) {
            return kDieMilliCelsiusUnknown;
        }
        return scale::dieMilliCelsius( scale::decode16Signed( raw ) );
    }

    // TRACE cannot afford VTCT inside a 280 us period, so it takes one
    // triggered conversion at the boundary and restores its own profile word.
    std::int16_t raw = 0;
    if ( device_->triggeredDieTemperature( spec.adcConfig, raw ) != ESP_OK ) {
        return kDieMilliCelsiusUnknown;
    }
    return scale::dieMilliCelsius( raw );
}

Pace Sampler::requestPace( const ProfileSpec& spec ) noexcept {
    if ( !spec.pacedByAlert ) {
        alertEdges_.store( 0 );
        return Pace::Timer;
    }
    // The probe runs where the capture will, in the sampler task on Core 1:
    // run by the console on Core 0, a read delayed past the next conversion by
    // whatever else Core 0 runs lets that conversion end on a pin still low,
    // and the probe counts fewer edges than the capture would see.
    if ( awaitingDone_ ) {
        // A stop that timed out (halt()): the loop may still be running and
        // would not see the request until it ends, then run the probe on its
        // own, over whatever the console set up by then. Asked only of a task
        // waiting for a start, as beginCapture() starts one.
        if ( xSemaphoreTake( doneSem_, 0 ) != pdTRUE ) {
            return Pace::Timer;
        }
        awaitingDone_ = false;
    }
    probePeriodUs_.store( spec.periodUs );
    (void)xSemaphoreTake( probeDoneSem_, 0 );
    probeRequested_.store( true );
    (void)xSemaphoreGive( startSem_ );
    const std::uint32_t windowMs = spec.periodUs * kPaceProbeConversions / 1000U;
    if ( xSemaphoreTake( probeDoneSem_, rtos::ticksFromMs( windowMs + kProbeAnswerMarginMs ) )
         != pdTRUE ) {
        // Asked only of a task waiting for a start (above), so the task took
        // this one and is still in the probe; the next request clears its
        // late answer before it asks.
        return Pace::Timer;
    }
    return probeResult_.load();
}

IRAM_ATTR Pace Sampler::measurePace( std::uint32_t periodUs ) noexcept {
    alertEdges_.store( 0 );

    // Listen first, then clear CNVRF and release the pin ([DS] SS 7.3.4), the
    // loop's own order: a conversion that ends while the read releases the pin
    // still makes an edge someone sees. Released first and armed after, such a
    // conversion left the pin low with no edge, and the probe counted none.
    std::uint16_t diag = 0;
    (void)xSemaphoreTake( conversionSem_, 0 );
    listen( alertPin_, alertRoute_ );
    (void)device_->readDiagAlrt( diag );

    // The pin stays low after an edge until DIAG_ALRT is read, so the probe
    // reads it after each edge, as the loop will: a pin that works gives one
    // edge per conversion only to a reader that releases it.
    const std::uint64_t end = static_cast<std::uint64_t>( esp_timer_get_time() )
                              + static_cast<std::uint64_t>( periodUs ) * kPaceProbeConversions;
    while ( true ) {
        const std::int64_t remaining = static_cast<std::int64_t>( end ) - esp_timer_get_time();
        if ( remaining <= 0 ) {
            break;
        }
        // Whole ticks while more than one is left, polling for the sub-tick
        // rest, so the window ends where it was set.
        const auto wait = static_cast<TickType_t>( remaining / 1000 );
        if ( xSemaphoreTake( conversionSem_, ( wait > 0U ) ? rtos::ticksFromMs( wait ) : 0U )
             == pdTRUE ) {
            (void)device_->readDiagAlrt( diag );
        }
    }
    stopListening( alertPin_ );

    const std::uint32_t edges = alertEdges_.load();
    const std::uint32_t low   = kPaceProbeConversions - kPaceProbeTolerance;
    const std::uint32_t high  = kPaceProbeConversions + kPaceProbeTolerance;
    return ( edges >= low && edges <= high ) ? Pace::Alert : Pace::Timer;
}

bool Sampler::setThreshold( bool enabled, std::int64_t levelNa, std::int64_t hysteresisNa,
                            const char*& error ) noexcept {
    if ( sink_.load() != Sink::Idle ) {
        error = "capture running (ABORt first)";
        return false;
    }
    if ( hysteresisNa < 0 ) {
        error = "the hysteresis is 0 or more";
        return false;
    }
    thresholdEnabled_ = enabled;
    thresholdNa_      = levelNa;
    hysteresisNa_     = hysteresisNa;
    return true;
}

bool Sampler::setProfile( Profile profile, const char*& error ) noexcept {
    if ( sink_.load() != Sink::Idle ) {
        error = "capture running (ABORt first)";
        return false;
    }
    profile_.store( profile );
    return true;
}

ProfileSpec Sampler::conversion( Profile profile ) const noexcept {
    const ProfileSpec& own       = profileSpec( profile );
    const Averaging&   averaging = averaging_[profileIndex( profile )];
    return averaging.on ? averagedSpec( own, averaging ) : own;
}

Averaging Sampler::averaging() const noexcept {
    return averaging_[profileIndex( profile_.load() )];
}

bool Sampler::setAveraging( const Averaging& wanted, const char*& error ) noexcept {
    if ( sink_.load() != Sink::Idle ) {
        error = "capture running (ABORt first)";
        return false;
    }
    const Profile      profile = profile_.load();
    const ProfileSpec& own     = profileSpec( profile );
    if ( static_cast<std::uint32_t>( own.conversionsPerSample ) > 1U ) {
        error = "BURSt has no averaging override: its 50 us conversion is its definition";
        return false;
    }
    if ( wanted.on ) {
        if ( !paceAllows( averagedSpec( own, wanted ), error ) ) {
            return false;
        }
    }
    averaging_[profileIndex( profile )] = wanted;
    return true;
}

void Sampler::resetAveraging() noexcept {
    for ( std::size_t i = 0; i < kProfileCount; ++i ) {
        averaging_[i] = Averaging{ false, kProfileSpecs[i].avg, kProfileSpecs[i].vShCt };
    }
}

bool Sampler::paceAllows( const ProfileSpec& spec, const char*& error ) noexcept {
    const std::uint32_t sclHz    = device_->busSpeedHz();
    const std::uint32_t shortest = shortestPeriodUs( spec, sclHz );
    const bool          tooShort = spec.periodUs < shortest;
    if ( !tooShort && spec.periodUs <= kLongestAveragedPeriodUs ) {
        return true;
    }
    text::Writer w( errorText_ );
    w.text( "averaging " );
    w.unsignedDecimal( kAverageCount[spec.avg] );
    w.text( " x " );
    w.unsignedDecimal( kConversionTimeUs[spec.vShCt] );
    w.text( " us gives " );
    w.unsignedDecimal( spec.periodUs );
    w.text( tooShort ? " us a sample, under the " : " us a sample, over the " );
    w.unsignedDecimal( tooShort ? shortest : kLongestAveragedPeriodUs );
    if ( tooShort ) {
        w.text( " us its reads need at " );
        w.unsignedDecimal( sclHz );
        w.text( " Hz" );
    } else {
        w.text( " us its pacing allows" );
    }
    error = w.view().data();
    return false;
}

bool Sampler::beginCapture( Profile profile, Sink sink, std::uint32_t channelMask,
                            std::uint32_t sampleLimit, bool triggered, CaptureInfo& info,
                            const char*& error ) noexcept {
    if ( !engineReady_ ) {
        // begin() failed -- a queue, a semaphore, the ALERT gpio_config, the
        // interrupt's route or the task itself. Refused by name, because the alternative
        // is a capture header for a capture that can never produce a sample.
        error = "sampler engine failed to start (reboot)";
        return false;
    }
    if ( sink_.load() != Sink::Idle ) {
        error = "capture running (ABORt first)";
        return false;
    }
    if ( awaitingDone_ ) {
        // A stop that timed out (halt()): the loop may still be running, and a
        // start now would let it run on into this capture with the last one's
        // profile. Its late acknowledgement is what clears the way.
        if ( xSemaphoreTake( doneSem_, 0 ) != pdTRUE ) {
            error =
                "sampler engine has not acknowledged the last stop (try again; reboot if it "
                "persists)";
            return false;
        }
        awaitingDone_ = false;
    }
    if ( !device_->ready() ) {
        error = "no device (scan found none)";
        return false;
    }

    // What the capture converts with, latched for its loop and for its dump
    // once nothing below refuses it (windowSpec_).
    const ProfileSpec spec     = conversion( profile );
    const bool        averaged = averaging_[profileIndex( profile )].on;

    // A stream needs a channel. It never needs more of the line than its
    // share: every channel on the fastest streaming profile fits inside it, a
    // fact of the profile table checked where the table is (above), so no
    // channel set is refused for its byte rate.
    if ( ( sink == Sink::Stream ) && ( channelMask == 0U ) ) {
        error = "no channels selected";
        return false;
    }

    // A ring profile's per-sample cost is one retained read plus the loop's own
    // work, and the bus speed decides whether that fits the period. The check
    // lives here and not in the table because
    // `SYSTem:COMMunicate:I2C:FREQuency` changes the speed at runtime.
    //
    // The read must come in under **half** the period, which is measured and
    // not a round number. At 400 kHz a 95 us read -- which does fit inside
    // 100 us -- slips BURST to 150 us, 50.4 % off: three conversions per
    // sample instead of two, because the loop's non-bus work is itself worth
    // tens of microseconds. So a read that merely fits the period is not
    // enough, and half is the one threshold that classifies every combination
    // measured on this rig correctly: BURST and TRACE, at 400 kHz and at
    // 1 MHz. Even at 1 MHz BURST does not quite keep 100 us: its loop costs
    // about 101.5 us a sample at this build's optimization, so `pace_err_ppm`
    // reads +14,000 to +20,000 (bench, 2026-11-20). That is the loop, not the
    // INA228's oscillator, which a timer-paced capture never sees: TRACE on
    // the same loop reads +28 ppm. A slip like that is caught after the fact,
    // by `pace_err_ppm` in `FETCh:STATistics?`, and `pace_unmet` past 3 %.
    //
    // An averaging override is held to the same margin whatever the profile
    // (paceAllows()): its period was accepted at the clock the bus ran at
    // when it was set, which may have changed since.
    if ( averaged ) {
        if ( !paceAllows( spec, error ) ) {
            return false;
        }
    }
    if ( spec.capturesToRing ) {
        const std::uint32_t readUs =
            retainedReadMicroseconds( ina228::kWidth24Bytes, device_->busSpeedHz() );
        if ( 2U * readUs >= spec.periodUs ) {
            text::Writer w( errorText_ );
            w.text( spec.name );
            w.text( " needs a faster bus (a " );
            w.unsignedDecimal( spec.periodUs );
            w.text( " us period needs a read under " );
            w.unsignedDecimal( spec.periodUs / 2U );
            w.text( " us; it is " );
            w.unsignedDecimal( readUs );
            w.text( " us at " );
            w.unsignedDecimal( device_->busSpeedHz() );
            w.text( " Hz)" );
            error = w.view().data();
            return false;
        }
    }

    // The clip detector is armed on every window, capture or null: SOVL, SUVL
    // and DIAG_ALRT all reset with the device, and three writes cost nothing
    // against a conversion period. The DIAG_ALRT read that beginCapture() makes
    // next clears anything latched before the window opened.
    // Each write is made only while the ones before it were.
    esp_err_t status = device_->applyAdcConfig( spec.adcConfig );
    if ( status == ESP_OK ) {
        status = device_->enableConversionReadyAlert();
    }
    if ( status == ESP_OK ) {
        status = device_->armShuntClipLimits();
    }
    if ( status != ESP_OK ) {
        error = "i2c write failed";
        return false;
    }

    // Let one full conversion complete before anything is read, so the
    // consistency check and the boundary temperature describe live data.
    (void)waitUntil( static_cast<std::uint64_t>( esp_timer_get_time() ) + spec.periodUs );

    info                      = CaptureInfo{};
    info.profile              = profile;
    info.averaging            = Averaging{ averaged, spec.avg, spec.vShCt };
    info.periodUs             = spec.periodUs;
    info.channels             = channelMask;
    info.shuntCurrentDelta    = readConsistencyDelta();
    info.dieMilliCelsiusStart = readDieMilliCelsius( spec );

    // The boundary temperature reaches the one value `q=` is judged against,
    // unconditionally -- with the sentinel when the read failed -- so a
    // reading never outlives the capture that took it.
    noteDieMilliCelsius( info.dieMilliCelsiusStart );

    info.chargeRawStart      = readChargeRaw( info.chargeValidStart );
    captureChargeStartUs_    = static_cast<std::uint64_t>( esp_timer_get_time() );
    captureChargeRawStart_   = info.chargeRawStart;
    captureChargeValidStart_ = info.chargeValidStart;

    pace_.store( requestPace( spec ) );
    info.pace         = pace_.load();
    info.paceEdges    = alertEdges_.load();
    info.paceExpected = spec.pacedByAlert ? kPaceProbeConversions : 0U;

    if ( sink == Sink::Ring ) {
        info.ringSamples = ringCapacity_;
    }
    info.triggerSource  = triggered ? TriggerSource::Level : TriggerSource::Immediate;
    info.triggerLevelNa = triggerLevelNa_;
    info.triggerRising  = triggerRising_;
    info.pretrigger     = triggered ? pretrigger_ : 0U;
    info.sampleLimit    = sampleLimit;

    snapshotCalibration();
    windowSpec_ = spec;

    ringCount_.store( 0 );
    ringHead_             = 0;
    anchorTotal_          = 0;
    forcedAnchors_        = 0;
    triggerArmed_         = triggered;
    triggerPrimed_        = false;
    triggerIndex_         = 0;
    triggerTimeUs_        = 0;
    stopAt_               = 0;
    thresholdMarksQueued_ = 0;
    triggerFired_.store( false );
    markCount_ = 0;
    samples_.store( 0 );
    dropped_.store( 0 );
    readFailures_.store( 0 );
    staleEdges_.store( 0 );
    rebases_.store( 0 );
    paceFellBack_.store( false );
    forceAnchor_  = false;
    batchSums_    = {};
    batchFill_    = 0;
    batchTaken_   = 0;
    batchSamples_ = 1;

    // Clear any stale flag and CNVRF, so what the statistics report belongs to this
    // window ([DS] Table 7-16, SS 7.3.4).
    std::uint16_t diag = 0;
    (void)device_->readDiagAlrt( diag );
    flags_.store( 0 );

    // Drain anything left from a previous run before the task is told to go.
    Sample discarded = {};
    while ( xQueueReceive( streamQueue_, &discarded, 0 ) == pdTRUE ) {}
    (void)xSemaphoreTake( conversionSem_, 0 );

    sink_.store( sink );
    profile_.store( profile );
    channels_        = channelMask;
    sampleLimit_     = sampleLimit;
    captureStartUs_  = static_cast<std::uint64_t>( esp_timer_get_time() );
    nextDeadlineUs_  = captureStartUs_;
    info.startTimeUs = captureStartUs_;
    totals_          = LoopTotals{};
    totals_.endUs    = captureStartUs_;

    awaitingDone_ = true;
    running_.store( true );
    (void)xSemaphoreGive( startSem_ );
    return true;
}

bool Sampler::start( Profile profile, std::uint32_t channelMask, CaptureInfo& info,
                     const char*& error ) noexcept {
    const ProfileSpec&  spec = profileSpec( profile );
    const Sink          sink = spec.capturesToRing ? Sink::Ring : Sink::Stream;
    const std::uint32_t mask = spec.capturesToRing ? 0U : channelMask;
    // A ring ends at its own size, or at its post-trigger samples.
    const std::uint32_t limit = ( sink == Sink::Ring ) ? 0U : captureLimit_;
    return beginCapture( profile, sink, mask, limit, false, info, error );
}

bool Sampler::startStatistics( Profile profile, CaptureInfo& info, const char*& error ) noexcept {
    return beginCapture( profile, Sink::Stats, 0, captureLimit_, false, info, error );
}

bool Sampler::setThresholdMarks( bool enabled, const char*& error ) noexcept {
    if ( sink_.load() != Sink::Idle ) {
        error = "capture running (ABORt first)";
        return false;
    }
    thresholdMarks_ = enabled;
    return true;
}

bool Sampler::startTriggered( Profile profile, std::int64_t levelNa, bool rising,
                              std::uint32_t pretrigger, CaptureInfo& info,
                              const char*& error ) noexcept {
    if ( !profileSpec( profile ).capturesToRing ) {
        error = "a level trigger needs a ring profile (SENSe:PROFile TRACe or BURSt)";
        return false;
    }
    if ( pretrigger >= ringCapacity_ ) {
        error = "the pre-trigger must be under the ring (TRACe:POINts:MAXimum?)";
        return false;
    }
    triggerLevelNa_ = levelNa;
    triggerRising_  = rising;
    pretrigger_     = pretrigger;
    return beginCapture( profile, Sink::Ring, 0, 0, true, info, error );
}

bool Sampler::probeAlert() noexcept {
    const ProfileSpec& spec   = profileSpec( Profile::State );
    esp_err_t          status = device_->applyAdcConfig( spec.adcConfig );
    if ( status == ESP_OK ) {
        status = device_->enableConversionReadyAlert();
    }
    if ( status != ESP_OK ) {
        return false;
    }
    // As beginCapture() lets a conversion complete before its probe, so the
    // probe's first read finds CNVRF set and releases the pin whatever the
    // writes left it in.
    (void)waitUntil( static_cast<std::uint64_t>( esp_timer_get_time() )
                     + static_cast<std::uint64_t>( spec.periodUs ) * kSelfTestSettlePeriods );
    return requestPace( spec ) == Pace::Alert;
}

std::uint32_t Sampler::stackFreeBytes() const noexcept {
    return ( task_ != nullptr ) ? static_cast<std::uint32_t>( uxTaskGetStackHighWaterMark( task_ ) )
                                : 0U;
}

bool Sampler::endsItself() const noexcept {
    const Sink sink = sink_.load();
    return sink == Sink::Ring || sink == Sink::Null || sampleLimit_ != 0U;
}

bool Sampler::queueRecord( const Sample& record ) noexcept {
    // Only a streaming capture queues records, and one starts only on an
    // engine whose begin() built the queue.
    return xQueueSend( streamQueue_, &record, 0 ) == pdTRUE;
}

std::uint32_t Sampler::ringFirstIndex() const noexcept {
    // The ring holds the last ringCount() samples taken.
    const std::uint32_t taken = samples_.load();
    const std::uint32_t held  = ringCount_.load();
    return ( taken > held ) ? ( taken - held ) : 0U;
}

void Sampler::finishCapture() noexcept {
    stopListening( alertPin_ );
    sink_.store( Sink::Idle );
}

bool Sampler::halt() noexcept {
    running_.store( false );
    if ( !awaitingDone_ ) {
        return true;
    }
    if ( xSemaphoreTake( doneSem_, rtos::ticksFromMs( kStopTimeoutMs ) ) == pdTRUE ) {
        awaitingDone_ = false;
        return true;
    }
    finishCapture();
    return false;
}

bool Sampler::stop( CaptureStats& stats ) noexcept {
    if ( sink_.load() == Sink::Idle ) {
        return false;
    }
    if ( !halt() ) {
        return false;
    }
    closeCapture( stats );
    return true;
}

void Sampler::closeCapture( CaptureStats& stats ) noexcept {
    const ProfileSpec& spec = windowSpec_;

    // One DIAG_ALRT read per window, to catch MATHOF: while it is set,
    // "current and power data may be invalid" ([DS] Table 7-16).
    std::uint16_t diag = 0;
    if ( device_->readDiagAlrt( diag ) == ESP_OK ) {
        (void)flags_.fetch_or( diag );
    }

    // CHARGE after DIAG_ALRT, never before: CHARGEOF "clears when CHARGE is
    // read" ([DS] Table 7-16), so the overflow bit has to reach flags_ first.
    // handleCharge() keeps the same ordering for the same reason. The capture
    // has ended by now, so this read cannot move the register pointer out from
    // under a TRACE loop.
    bool               chargeValid = false;
    const std::int64_t chargeEnd   = readChargeRaw( chargeValid );
    const auto         chargeEndUs = static_cast<std::uint64_t>( esp_timer_get_time() );

    const std::int32_t dieEnd = readDieMilliCelsius( spec );
    if ( dieEnd != kDieMilliCelsiusUnknown ) {
        noteDieMilliCelsius( dieEnd );
    }

    const std::uint32_t samples = samples_.load();
    stats                       = CaptureStats{};
    stats.samples               = samples;
    stats.dropped               = dropped_.load();
    stats.durationUs            = totals_.endUs - captureStartUs_;

    // The achieved period against the declared one. Checked rather than
    // trusted, because a slipped cadence is invisible in every other number
    // the capture reports: the samples are real conversions and `dropped`
    // only ever counts a stream-queue overflow.
    //
    // The span runs from the first sample to the last, over the intervals
    // between them: from the capture's start instead, it would carry the
    // start's phase against the first conversion, up to a whole period, as an
    // error of up to 1 / samples. In microseconds against the nominal span,
    // so a 100 us period is not held to whole nanoseconds; a day of samples
    // is 8.6e10 us, times 10^6 inside int64.
    const std::uint32_t nominalUs = spec.periodUs;
    if ( samples > 1U ) {
        const auto achievedUs = static_cast<std::int64_t>( totals_.endUs - totals_.firstUs );
        const std::int64_t nominalSpanUs =
            static_cast<std::int64_t>( samples - 1U ) * static_cast<std::int64_t>( nominalUs );
        stats.paceErrorPpm = static_cast<std::int32_t>( ( ( achievedUs - nominalSpanUs ) * 1000000 )
                                                        / nominalSpanUs );
        stats.paceUnmet =
            stats.paceErrorPpm > kPaceErrorLimitPpm || stats.paceErrorPpm < -kPaceErrorLimitPpm;
    }
    // The mean is reported in picoamps, i.e. thousandths of the nanoamp the
    // samples are summed in. Splitting the division keeps the full precision
    // without ever forming `sum x 1000`, which a long SLEEP run could push
    // against the int64 ceiling.
    if ( samples > 0U ) {
        const auto count   = static_cast<std::int64_t>( samples );
        stats.meanPicoamps = ( totals_.sumNanoamps / count ) * scale::kThousand
                             + ( ( totals_.sumNanoamps % count ) * scale::kThousand ) / count;
    }
    stats.minNanoamps        = ( samples > 0U ) ? totals_.minNanoamps : 0;
    stats.maxNanoamps        = ( samples > 0U ) ? totals_.maxNanoamps : 0;
    stats.dieMilliCelsiusEnd = dieEnd;
    stats.flags              = flags_.load();
    stats.readFailures       = readFailures_.load();
    stats.paceFellBack       = paceFellBack_.load();
    stats.staleEdges         = staleEdges_.load();
    stats.rebases            = rebases_.load();
    stats.chargeRawStart     = captureChargeRawStart_;
    stats.chargeRawEnd       = chargeEnd;
    stats.chargeValid        = captureChargeValidStart_ && chargeValid;
    stats.chargeSpanUs       = chargeEndUs - captureChargeStartUs_;

    if ( samples > 1U ) {
        const std::uint64_t milliCounts =
            deviationMilliCounts( samples, totals_.nullSumDeviation, totals_.sumSquares );
        // pA = counts x LSB_pv x 1e6 / shunt_uohm; here from thousandths of a
        // count, so x 1000: at most 2^21 x 1000 x 312,500 x 1000 = 6.6e17.
        stats.deviationPicoamps =
            ( captureShuntMicroOhm_ > 0U )
                ? ( static_cast<std::int64_t>( milliCounts ) * captureShuntLsbPv_ * scale::kThousand
                    / static_cast<std::int64_t>( captureShuntMicroOhm_ ) )
                : 0;
        stats.deviationValid = true;
    }
    stats.maxIndex         = totals_.maxIndex;
    stats.maxTimeUs        = totals_.maxTimeUs;
    stats.busSamples       = totals_.busSamples;
    stats.sumDutUv         = totals_.sumDutUv;
    stats.sumPowerUw       = totals_.sumPowerUw;
    stats.minDutUv         = totals_.minDutUv;
    stats.maxDutUv         = totals_.maxDutUv;
    stats.minPowerUw       = totals_.minPowerUw;
    stats.maxPowerUw       = totals_.maxPowerUw;
    stats.dieSamples       = totals_.dieSamples;
    stats.sumDieMc         = totals_.sumDieMc;
    stats.thresholdEnabled = thresholdEnabled_;
    stats.thresholdNa      = thresholdNa_;
    stats.aboveSamples     = totals_.aboveSamples;
    stats.rises            = totals_.rises;
    stats.batchCount       = batchFill_;
    stats.batchSamples     = batchSamples_;
    stats.batchSumsNa      = batchSums_;
    stats.triggered        = triggerFired_.load();
    stats.triggerIndex     = triggerIndex_;
    stats.triggerTimeUs    = triggerTimeUs_;
    stats.thresholdMarks   = thresholdMarksQueued_;

    finishCapture();
}

std::uint32_t Sampler::zeroSampleCount( std::uint32_t seconds ) const noexcept {
    const std::uint64_t wanted = static_cast<std::uint64_t>( seconds ) * 1000000ULL;
    return static_cast<std::uint32_t>( wanted / periodUs() );
}

bool Sampler::startZero( std::uint32_t seconds, const char*& error ) noexcept {
    if ( seconds == 0U || seconds > kZeroMaxSeconds ) {
        error = "use CALibration:ZERO <seconds> (1..600)";
        return false;
    }

    // At least one sample a second on every profile (a fact of the profile
    // table, above) and on every override (kLongestAveragedPeriodUs), so a
    // window of one second or more always has some.
    const std::uint32_t samples = zeroSampleCount( seconds );
    CaptureInfo         info    = {};
    if ( !beginCapture( profile_.load(), Sink::Null, 0, samples, false, info, error ) ) {
        return false;
    }
    nullSeconds_ = seconds;
    return true;
}

bool Sampler::finishZero( ZeroResult& result ) noexcept {
    // A window that ended early still leaves a computable null -- from fewer
    // samples, which the reported `n=` says. One whose loop never acknowledged
    // the stop leaves nothing: halt() has closed it, and the console says so.
    const ProfileSpec& spec = windowSpec_;

    result = ZeroResult{};
    if ( !halt() ) {
        return false;
    }

    const std::uint32_t taken = samples_.load();
    result.samples            = taken;
    result.seconds            = nullSeconds_;
    result.dieMilliCelsius    = readDieMilliCelsius( spec );
    noteDieMilliCelsius( result.dieMilliCelsius );

    if ( taken > 0U ) {
        // The mean is computed from the reference plus the accumulated
        // deviation, so no intermediate can overflow on a long run.
        const auto         samples = static_cast<std::int64_t>( taken );
        const std::int64_t total = ( static_cast<std::int64_t>( totals_.nullReference ) * samples )
                                   + totals_.nullSumDeviation;
        result.offsetPicovolts   = total * captureShuntLsbPv_ / samples;
    }
    if ( taken > 1U ) {
        // The same standard deviation a capture reports (deviationMilliCounts()),
        // in thousandths of a count, to picovolts.
        result.deviationPicovolts = static_cast<std::int64_t>( deviationMilliCounts(
                                        taken, totals_.nullSumDeviation, totals_.sumSquares ) )
                                    * captureShuntLsbPv_ / 1000;
    }

    finishCapture();
    return taken > 0U;
}

// --- The sampling loop ------------------------------------------------------
//
// Everything a conversion executes on Core 1 is in IRAM (IRAM_ATTR here, on
// waitUntil(), currentNanoamps(), noteDieMilliCelsius() and alertIsr() above
// and on the driver's register reads; the ESP-IDF side by sdkconfig.defaults
// and src/linker.lf), so the loop never fetches an instruction through the
// flash cache it shares with Core 0 (0.16.1, `docs/architecture.md`
// SS 5.2). scripts/check_iram_hot_path.py holds it on the linked image.

IRAM_ATTR bool Sampler::retainShuntPointer() noexcept {
    for ( std::uint32_t attempt = 0; attempt < kPointerWrites; ++attempt ) {
        if ( device_->setPointer( kRegVShunt ) == ESP_OK ) {
            return true;
        }
    }
    return false;
}

IRAM_ATTR bool Sampler::alertPacing() const noexcept {
    return pace_.load() == Pace::Alert && !paceFellBack_.load();
}

IRAM_ATTR void Sampler::waitForConversion( const ProfileSpec& spec ) noexcept {
    if ( alertPacing() ) {
        // Zero bus cost: the ISR hands over a semaphore and the task blocks, so
        // IDLE1 runs normally. The timeout only exists so a dead pin cannot
        // hang a capture for ever.
        const std::uint32_t timeoutMs = alertTimeoutMs( spec.periodUs );
        if ( xSemaphoreTake( conversionSem_, rtos::ticksFromMs( timeoutMs ) ) == pdTRUE ) {
            return;
        }

        // A timeout is not an edge. The pin was pulsing when measurePace()
        // measured it and has stopped since -- a loose jumper, or no pull-up on
        // ALE -- and continuing here would sample at the timeout rate instead
        // of the period: 12 ms where STATE's period is 1.152 ms. So the
        // cadence falls back to the host timer from here on, the deadline is
        // re-based to now, and `FETCh:STATistics?` says `pace_fallback=1`. A
        // DIAG_ALRT read that failed leaves the pin low and ends here too.
        paceFellBack_.store( true );
        nextDeadlineUs_ = static_cast<std::uint64_t>( esp_timer_get_time() );
    }
    nextDeadlineUs_ += spec.periodUs;
    const std::uint64_t arrivedUs = waitUntil( nextDeadlineUs_ );
    // A loop that came more than a period late (a stall: a slow read, a
    // preemption) would otherwise read back to back until the deadline caught
    // up, taking the same conversion more than once. The deadline is re-based
    // to the arrival instead, so the next sample is a period after this one,
    // and the re-base is counted (`rebased=`, 0.14.0): it is where a ring's
    // implicit timestamps stop being index x period until the next anchor.
    if ( arrivedUs > nextDeadlineUs_ + spec.periodUs ) {
        nextDeadlineUs_ = arrivedUs;
        (void)rebases_.fetch_add( 1 );
    }
}

IRAM_ATTR Sampler::ReadOutcome Sampler::readSample( const ProfileSpec& spec, bool alert,
                                                    std::uint64_t wokeUs,
                                                    Sample&       sample ) noexcept {
    sample          = Sample{};
    sample.busValid = false;
    sample.dieValid = false;

    // On an alert-paced capture this read is what releases the pin for the
    // next conversion, so it comes first; and it says whether the edge was a
    // conversion at all. Latched (ALATCH = 1), it also carries any clip flag
    // a conversion of the window raised into `flags`.
    if ( spec.diagEverySample || alert ) {
        std::uint16_t diag = 0;
        if ( device_->readDiagAlrt( diag ) == ESP_OK ) {
            sample.flags = diag;
            (void)flags_.fetch_or( diag );
            if ( alert && ( diag & kDiagCnvrf ) == 0U ) {
                return ReadOutcome::Stale;
            }
        }
    }

    if ( spec.capturesToRing ) {
        // The retained pointer is what makes TRACE affordable: 95 us of bus
        // time instead of 143 us ([DS] SS 7.5.1.1).
        std::array<std::uint8_t, 3> raw = {};
        if ( device_->readRetained( raw.data(), raw.size() ) != ESP_OK ) {
            return ReadOutcome::Failed;
        }
        const std::uint32_t packed = ( static_cast<std::uint32_t>( raw[0] ) << 16 )
                                     | ( static_cast<std::uint32_t>( raw[1] ) << 8 )
                                     | static_cast<std::uint32_t>( raw[2] );
        sample.shuntCounts         = scale::decode20( packed );
    } else {
        std::uint32_t shuntRaw = 0;
        if ( device_->read24( kRegVShunt, shuntRaw ) != ESP_OK ) {
            return ReadOutcome::Failed;
        }
        sample.shuntCounts = scale::decode20( shuntRaw );

        if ( modeConvertsBus( spec.mode ) ) {
            std::uint32_t busRaw = 0;
            if ( device_->read24( kRegVBus, busRaw ) == ESP_OK ) {
                sample.busCounts = scale::decode20( busRaw );
                sample.busValid  = true;
            }
        }
        if ( modeConvertsTemperature( spec.mode ) ) {
            std::uint16_t dieRaw = 0;
            if ( device_->read16( kRegDieTemp, dieRaw ) == ESP_OK ) {
                sample.dieCounts = scale::decode16Signed( dieRaw );
                sample.dieValid  = true;
            }
        }
    }

    // The edge is the conversion's end, so an alert-paced sample is stamped
    // when it woke the loop, before the bus latency of the reads.
    sample.timestampUs = alert ? wokeUs : static_cast<std::uint64_t>( esp_timer_get_time() );
    return ReadOutcome::Taken;
}

IRAM_ATTR void Sampler::addToBatch( std::int64_t currentNa ) noexcept {
    batchSums_[batchFill_] += currentNa;
    if ( ++batchTaken_ < batchSamples_ ) {
        return;
    }
    batchTaken_ = 0;
    if ( ++batchFill_ < kBatchSlots ) {
        return;
    }
    // Every slot holds a complete batch: merge them pairwise into the first
    // half and double the batch. A slot's sum stays inside int64: a batch is
    // at most a 64th of a capture of 2^32 samples, 2^26 samples of at most
    // 1.64e9 nA, 1.1e17.
    for ( std::size_t i = 0; i < kBatchSlotsAfterMerge; ++i ) {
        batchSums_[i] = batchSums_[2U * i] + batchSums_[( 2U * i ) + 1U];
    }
    for ( std::size_t i = kBatchSlotsAfterMerge; i < kBatchSlots; ++i ) {
        batchSums_[i] = 0;
    }
    batchFill_ = kBatchSlotsAfterMerge;
    batchSamples_ *= 2U;
}

IRAM_ATTR void Sampler::storeInRing( const Sample& sample, std::int64_t currentNa,
                                     std::uint32_t index ) noexcept {
    // Anchors in a circular array, like the ring: the last kAnchorSlots hold
    // every regular anchor of a full ring's span and up to kMaxForcedAnchors
    // forced ones, so a forced anchor never pushes a regular one out.
    const bool regular = index % kAnchorEvery == 0U;
    if ( regular || ( forceAnchor_ && forcedAnchors_ < kMaxForcedAnchors ) ) {
        anchors_[anchorTotal_ % kAnchorSlots] = Anchor{ index, sample.timestampUs };
        ++anchorTotal_;
        forcedAnchors_ += regular ? 0U : 1U;
    }
    forceAnchor_     = false;
    ring_[ringHead_] = sample.shuntCounts;
    ringHead_        = ( ringHead_ + 1U < ringCapacity_ ) ? ( ringHead_ + 1U ) : 0U;
    const auto count = ringCount_.load();
    if ( count < ringCapacity_ ) {
        ringCount_.store( count + 1U );
    }

    if ( !triggerArmed_ || triggerFired_.load() ) {
        return;
    }
    // A level is crossed only from its other side: a current already beyond
    // it at INITiate primes nothing until it has come back.
    const bool beyond =
        triggerRising_ ? ( currentNa >= triggerLevelNa_ ) : ( currentNa <= triggerLevelNa_ );
    if ( !beyond ) {
        triggerPrimed_ = true;
        return;
    }
    if ( !triggerPrimed_ ) {
        return;
    }
    triggerIndex_  = index;
    triggerTimeUs_ = sample.timestampUs;
    stopAt_        = index + ( ringCapacity_ - pretrigger_ );
    triggerFired_.store( true );
}

IRAM_ATTR void Sampler::queueThresholdMark( RecordKind kind, const Sample& sample,
                                            std::uint32_t index ) noexcept {
    if ( thresholdMarksQueued_ >= kMaxThresholdMarks ) {
        return;
    }
    Sample record      = {};
    record.kind        = kind;
    record.index       = index;
    record.timestampUs = sample.timestampUs;
    if ( xQueueSend( streamQueue_, &record, 0 ) == pdTRUE ) {
        ++thresholdMarksQueued_;
    }
}

IRAM_ATTR void Sampler::accumulate( const Sample& sample, std::int64_t currentNa,
                                    std::uint32_t index, LoopTotals& totals ) noexcept {
    if ( index == 0U ) {
        totals.firstUs       = sample.timestampUs;
        totals.minNanoamps   = currentNa;
        totals.maxNanoamps   = currentNa;
        totals.maxTimeUs     = sample.timestampUs;
        totals.nullReference = sample.shuntCounts;
    } else {
        totals.minNanoamps = ( currentNa < totals.minNanoamps ) ? currentNa : totals.minNanoamps;
        if ( currentNa > totals.maxNanoamps ) {
            totals.maxNanoamps = currentNa;
            totals.maxIndex    = index;
            totals.maxTimeUs   = sample.timestampUs;
        }
    }
    totals.sumNanoamps += currentNa;
    totals.endUs = sample.timestampUs;

    // Deviations from the first sample, in counts: a 20-bit field differs
    // from another by under 2^20, so each square is under 2^40, and the
    // 128-bit sum holds any capture of them.
    const std::int64_t deviation =
        static_cast<std::int64_t>( sample.shuntCounts ) - totals.nullReference;
    totals.nullSumDeviation += deviation;
    totals.sumSquares =
        wide::add( totals.sumSquares, static_cast<std::uint64_t>( deviation * deviation ) );

    addToBatch( currentNa );

    totals.crossing = RecordKind::Conversion;
    if ( thresholdEnabled_ ) {
        if ( !totals.above && currentNa >= thresholdNa_ ) {
            totals.above    = true;
            totals.crossing = RecordKind::ThresholdRise;
            ++totals.rises;
        } else if ( totals.above && currentNa < thresholdNa_ - hysteresisNa_ ) {
            totals.above    = false;
            totals.crossing = RecordKind::ThresholdFall;
        } else {
            // No crossing: the level stays on its side of the hysteresis band.
        }
        if ( totals.above ) {
            ++totals.aboveSamples;
        }
    }

    if ( sample.busValid ) {
        const std::int64_t shuntPv =
            scale::shuntPicovolts( sample.shuntCounts, captureShuntLsbPv_ );
        const std::int64_t busPv   = scale::busPicovolts( sample.busCounts, kBusLsbPv );
        const std::int64_t dutPv   = scale::dutPicovolts( busPv, shuntPv - captureOffsetPv_ );
        const std::int64_t dutUv   = dutPv / scale::kPicoPerMicro;
        const std::int64_t powerUw = scale::powerMicrowatts( dutPv, currentNa );
        if ( totals.busSamples == 0U ) {
            totals.minDutUv   = dutUv;
            totals.maxDutUv   = dutUv;
            totals.minPowerUw = powerUw;
            totals.maxPowerUw = powerUw;
        } else {
            totals.minDutUv   = ( dutUv < totals.minDutUv ) ? dutUv : totals.minDutUv;
            totals.maxDutUv   = ( dutUv > totals.maxDutUv ) ? dutUv : totals.maxDutUv;
            totals.minPowerUw = ( powerUw < totals.minPowerUw ) ? powerUw : totals.minPowerUw;
            totals.maxPowerUw = ( powerUw > totals.maxPowerUw ) ? powerUw : totals.maxPowerUw;
        }
        ++totals.busSamples;
        totals.sumDutUv += dutUv;
        totals.sumPowerUw += powerUw;
    }
    if ( sample.dieValid ) {
        ++totals.dieSamples;
        totals.sumDieMc += scale::dieMilliCelsius( sample.dieCounts );
    }
}

IRAM_ATTR void Sampler::runCapture() noexcept {
    // Subscribed from its own task (0.14.0): a sampler that stops coming back
    // to its loop is reset by the watchdog (watchdog.h).
    (void)watchdog::subscribe( watchdog::Party::Sampler );
    while ( true ) {
        while ( xSemaphoreTake( startSem_, rtos::ticksFromMs( kIdleFeedMs ) ) != pdTRUE ) {
            watchdog::checkIn( watchdog::Party::Sampler );
        }
        watchdog::checkIn( watchdog::Party::Sampler );

        // A pacing probe is a request of its own: run it, answer, and wait
        // for the next start.
        if ( probeRequested_.exchange( false ) ) {
            probeResult_.store( measurePace( probePeriodUs_.load() ) );
            (void)xSemaphoreGive( probeDoneSem_ );
            continue;
        }

        // Read once per window: the console changes neither while the window
        // is open, and the loop's totals start from the window's start. The
        // specification is the one beginCapture() latched, in internal RAM.
        const ProfileSpec& spec   = windowSpec_;
        const Sink         sink   = sink_.load();
        LoopTotals         totals = {};
        totals.endUs              = captureStartUs_;
        if ( pace_.load() == Pace::Alert ) {
            (void)xSemaphoreTake( conversionSem_, 0 );
            listen( alertPin_, alertRoute_ );
            // A conversion that ended since the start's last DIAG_ALRT read
            // pulled the pin low while nothing listened, and a low pin makes no
            // edge: release it, so the next conversion is the first edge.
            std::uint16_t diag = 0;
            (void)device_->readDiagAlrt( diag );
        }
        // One pointer write for the whole capture. With none written, a read
        // would return whatever register the pointer names: the capture ends
        // with no sample, the failure counted: its first, as the start set the
        // count to 0 and nothing has been read.
        if ( spec.capturesToRing ) {
            const bool pointed = retainShuntPointer();
            if ( !pointed ) {
                readFailures_.store( 1U );
                running_.store( false );
            }
        }
        nextDeadlineUs_ = static_cast<std::uint64_t>( esp_timer_get_time() );
        // SLEEP's 67.5 ms period feeds every second sample, BURST's 100 us
        // every 2,000th: a decrement and a test a sample.
        const std::uint32_t feedEvery = kCaptureFeedUs / spec.periodUs;
        std::uint32_t       untilFeed = feedEvery;

        while ( running_.load() ) {
            if ( --untilFeed == 0U ) {
                untilFeed = feedEvery;
                watchdog::checkIn( watchdog::Party::Sampler );
            }
            waitForConversion( spec );
            if ( !running_.load() ) {
                break;
            }
            // The clock is read here only when the edge is the timestamp: on
            // the timer path BURST's loop has no microsecond to spare.
            const bool          alert = alertPacing();
            const std::uint64_t wokeUs =
                alert ? static_cast<std::uint64_t>( esp_timer_get_time() ) : 0U;

            Sample            sample  = {};
            const ReadOutcome outcome = readSample( spec, alert, wokeUs, sample );
            if ( outcome == ReadOutcome::Stale ) {
                // An edge with no conversion behind it: counted, and no
                // sample, so the stream stays one sample per conversion.
                (void)staleEdges_.fetch_add( 1 );
                continue;
            }
            if ( outcome == ReadOutcome::Failed ) {
                // Counted and marked, never swallowed. The loop does not
                // advance, so for TRACE -- whose timestamps are implicit --
                // every later sample's reported time is now a period early;
                // the forced anchor at the next sample is what lets the host
                // see that discontinuity instead of interpolating across it.
                (void)readFailures_.fetch_add( 1 );
                forceAnchor_ = true;
                continue;
            }

            const std::int64_t  currentNa = currentNanoamps( sample.shuntCounts );
            const std::uint32_t index     = samples_.load();
            accumulate( sample, currentNa, index, totals );

            lastShuntCounts_.store( sample.shuntCounts );
            if ( sample.busValid ) {
                lastBusCounts_.store( sample.busCounts );
            }
            if ( sample.dieValid ) {
                noteDieMilliCelsius( scale::dieMilliCelsius( sample.dieCounts ) );
            }

            if ( sink == Sink::Ring ) {
                storeInRing( sample, currentNa, index );
            } else if ( sink == Sink::Stream ) {
                sample.index = index;
                if ( xQueueSend( streamQueue_, &sample, 0 ) != pdTRUE ) {
                    (void)dropped_.fetch_add( 1 );
                }
                // A crossing's mark goes behind the sample it was seen at,
                // so no mark is written before a sample of a lower index.
                if ( totals.crossing != RecordKind::Conversion && thresholdMarks_ ) {
                    queueThresholdMark( totals.crossing, sample, index );
                }
            } else {
                // Null and Stats keep the totals alone (accumulate(), above).
            }

            (void)samples_.fetch_add( 1 );
            const std::uint32_t taken = index + 1U;

            // An untriggered trace fills once and the capture ends. A
            // triggered one runs circular until its level, then ends once the
            // post-trigger samples are in: the indices and the anchors are the
            // capture's own, never the ring's, so a wrap leaves the implicit
            // timestamps `t0 + index x period` unambiguous.
            if ( sink == Sink::Ring ) {
                const bool ended = triggerArmed_ ? ( triggerFired_.load() && taken >= stopAt_ )
                                                 : ( ringCount_.load() >= ringCapacity_ );
                if ( ended ) {
                    running_.store( false );
                }
            }
            if ( sampleLimit_ != 0U && taken >= sampleLimit_ ) {
                running_.store( false );
            }
        }

        // The totals cross by value, before the acknowledgement that orders
        // them for the console (halt()).
        totals_ = totals;
        stopListening( alertPin_ );
        (void)xSemaphoreGive( doneSem_ );
    }
}

// --- Accessors used by the console -----------------------------------------

bool Sampler::nextStreamSample( Sample& out, std::uint32_t waitMs ) noexcept {
    if ( streamQueue_ == nullptr ) {
        return false;
    }
    return xQueueReceive( streamQueue_, &out, rtos::ticksFromMs( waitMs ) ) == pdTRUE;
}

bool Sampler::addMark( const char* label, Mark& stored ) noexcept {
    return recordMark( label, stored, kMaxMarks );
}

bool Sampler::addHoldMark( const char* label, Mark& stored ) noexcept {
    return recordMark( label, stored, kMarkSlots );
}

bool Sampler::recordMark( const char* label, Mark& stored, std::size_t limit ) noexcept {
    if ( markCount_ >= limit ) {
        return false;
    }
    Mark& mark       = marks_[markCount_];
    mark.index       = samples_.load();
    mark.timestampUs = static_cast<std::uint64_t>( esp_timer_get_time() );
    std::size_t i    = 0;
    if ( label != nullptr ) {
        while ( i < kMarkLabelCapacity && label[i] != '\0' ) {
            mark.label[i] = label[i];
            ++i;
        }
    }
    mark.label[i] = '\0';
    ++markCount_;
    stored = mark;
    return true;
}

std::int32_t Sampler::ringAt( std::uint32_t offset ) const noexcept {
    const std::uint32_t held = ringCount_.load();
    if ( ring_ == nullptr || offset >= held ) {
        return 0;
    }
    // The oldest held sample sits `held` places behind the head, modulo the
    // ring: at 0 until the ring has wrapped.
    const std::uint32_t oldest =
        ( ringHead_ >= held ) ? ( ringHead_ - held ) : ( ringHead_ + ringCapacity_ - held );
    const std::uint32_t slot = oldest + offset;
    return ring_[( slot < ringCapacity_ ) ? slot : ( slot - ringCapacity_ )];
}

}  // namespace kleidos::meter
