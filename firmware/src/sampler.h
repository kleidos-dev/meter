// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file sampler.h
 * @ingroup sampling
 * @brief The four profiles, the pacing, the RAM ring and the zero-offset null.
 *
 * The requirements are five orders of magnitude apart in current **and in
 * time**, so they are four configurations of one instrument, chosen per run:
 * no single `AVG` setting both resolves 10 uA and shows a BLE peak
 * (`docs/architecture.md` SS 3.2).
 *
 * The sampler task owns Core 1 and never prints. It hands finished samples to
 * the console task through a queue when streaming, so a slow line can never
 * stall the cadence -- it can only raise `dropped=`
 * (`docs/architecture.md` SS 5.1).
 */
#ifndef KLEIDOS_METER_SAMPLER_H
#define KLEIDOS_METER_SAMPLER_H

#include "calibration.h"
#include "ina228.h"
#include "ina228_regs.h"
#include "interrupt.h"
#include "wide.h"

#include <driver/gpio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace kleidos::meter {

/// @addtogroup sampling
/// @{

/// The four sampling profiles.
enum class Profile : std::uint8_t {
    Trace,  ///< Shape a burst: 280 us, AVG 1, shunt only, captured to RAM.
    State,  ///< Measure a state: 1152 us, bus + shunt + temperature, streamed.
    Sleep,  ///< Measure deep sleep: 67.52 ms, AVG 16, streamed.
    Burst,  ///< Resolve a transient: 50 us, the device's floor. Needs a 1 MHz bus
            ///< (`SYSTem:COMMunicate:I2C:FREQuency 1000000`).
};

/// What paced a capture's samples. `STATE` and `SLEEP` are paced by the ALERT
/// pin when the probe at `INITiate` counts an edge per conversion; `TRACE` and
/// `BURST` always by the timer (`docs/architecture.md` SS 3.3).
enum class Pace : std::uint8_t {
    Alert,  ///< One sample per conversion-ready edge of the ALERT pin, each
            ///< followed by the DIAG_ALRT read that releases the pin.
    Timer,  ///< The host timer at the profile's period: a ring profile, or a
            ///< pin the probe found absent or ringing.
};

/// What starts a ring capture's record (`TRIGger:SOURce`).
enum class TriggerSource : std::uint8_t {
    Immediate,  ///< `INITiate` itself: the ring fills once from the start.
    Level,      ///< A level: the ring runs circular until the current crosses
                ///< it, then records the post-trigger samples and stops.
};

/// What a queued record is: a conversion, or a mark written in conversion
/// order with the samples (0.11.0).
enum class RecordKind : std::uint8_t {
    Conversion,     ///< A sample.
    Mark,           ///< A `TRACe:MARK` or a `LIVE` relay move, slot markSlot.
    ThresholdRise,  ///< The current rose through the threshold.
    ThresholdFall,  ///< The current fell below the threshold less its hysteresis.
};

/// A streamable channel. The values are bit positions in a channel mask.
enum class Channel : std::uint8_t {
    CurrentMicroamps  = 0,  ///< `i_ua`, signed.
    VoltageMillivolts = 1,  ///< `v_mv` at the sense point.
    PowerMilliwatts   = 2,  ///< `p_mw` delivered to the DUT.
    DieCelsius        = 3,  ///< `die_c`.
};

/// Number of streamable channels, one per Channel enumerator.
constexpr std::size_t kChannelCount = 4;

/// The channel's spelling on the wire, indexed by Channel.
extern const char* const kChannelNames[kChannelCount];

/// @brief The channel's bit in a channel mask.
///
/// @param[in] channel The channel.
/// @return A mask with only @p channel's bit set.
constexpr std::uint32_t channelBit( Channel channel ) noexcept {
    return 1U << static_cast<std::uint32_t>( channel );
}

/// Everything that distinguishes one profile from another.
struct ProfileSpec {
    std::string_view name{};       ///< The profile's name, as `SENSe:PROFile` takes it.
    std::uint8_t     mode{};       ///< ADC_CONFIG MODE encoding.
    std::uint8_t     vbusCt{};     ///< ADC_CONFIG VBUSCT conversion-time encoding.
    std::uint8_t     vShCt{};      ///< ADC_CONFIG VSHCT conversion-time encoding.
    std::uint8_t     vtCt{};       ///< ADC_CONFIG VTCT conversion-time encoding.
    std::uint8_t     avg{};        ///< ADC_CONFIG AVG encoding.
    std::uint16_t    adcConfig{};  ///< The ADC_CONFIG word derived from the five fields above.

    /// Conversions one sample costs. One everywhere but BURST: the loop cannot
    /// complete a 50 us conversion's read inside 50 us, and the conversion-ready
    /// semaphore is binary, so it necessarily takes every second edge. Stating
    /// it here is what keeps the period honest -- the samples are real
    /// conversions, evenly spaced, and TRACE-style implicit timestamps
    /// (`t0 + index x period`) would otherwise reconstruct the whole capture
    /// twice too fast. Measured, not assumed: `paceErrorPpm` checks it when the capture closes.
    std::uint8_t  conversionsPerSample{};
    std::uint32_t periodUs{};         ///< Microseconds per sample: conversionsPerSample periods.
    bool          capturesToRing{};   ///< TRACE and BURST capture to RAM; STATE and SLEEP stream.
    bool          diagEverySample{};  ///< Read DIAG_ALRT per sample, or once per window.

    /// Paced by the ALERT pin when the probe finds it working. The pin goes low
    /// at the end of a conversion and stays low until DIAG_ALRT is read
    /// (measured on the bench, 2026-11-20), so each edge costs a DIAG_ALRT read.
    /// `STATE` and `SLEEP` can afford it; `TRACE` and `BURST` cannot, because
    /// the read would move the retained register pointer their period is built
    /// on, and they stay on the timer without probing.
    bool pacedByAlert{};
    std::uint32_t
        defaultChannels{};  ///< The set a capture streams when `SENSe:FUNCtion:ON` named none.
};

/// How many profiles there are, one per Profile enumerator.
constexpr std::size_t kProfileCount = 4;

/// @brief The specification of @p profile.
///
/// @param[in] profile The profile; a value outside the table yields TRACE's.
/// @return The profile's entry in the profile table.
const ProfileSpec& profileSpec( Profile profile ) noexcept;

/// A profile's averaging override (`SENSe:AVERage`, 0.18.0): the INA228's
/// averaging and shunt conversion time in place of the profile's own, a
/// measurement setting. Off, its count and time are kept and the profile
/// converts with its own ADC_CONFIG.
struct Averaging {
    bool         on{};     ///< Whether the two below replace the profile's own.
    std::uint8_t avg{};    ///< ADC_CONFIG AVG encoding.
    std::uint8_t vShCt{};  ///< ADC_CONFIG VSHCT conversion-time encoding.
};

// --- Capacities -------------------------------------------------------------

/// Samples the TRACE ring holds: 1,048,576 x 4 bytes = 4 MiB = 293 s at
/// 3,571 Sa/s, a static array in the module's 8 MB of PSRAM (`app_main()`,
/// `EXT_RAM_BSS_ATTR`), leaving half of it free. It is reserved at link time
/// and handed to Sampler::begin(): nothing allocates it at run time. The
/// timestamps are implicit -- the period is fixed -- so a sample is just the
/// sign-extended 20-bit VSHUNT count in an int32.
///
/// It was 32,768 (128 KiB, 9.18 s of internal RAM) while PSRAM was off. A
/// window that short cannot show an autocorrelation time, which is what the
/// uncertainty model needs before it will print a margin, so the ring and the
/// PSRAM switch move together.
constexpr std::uint32_t kTraceSamples = 1048576;

/// The smallest ring Sampler::begin() accepts: a fraction of a second is not
/// worth capturing into. The host tests hand it rings this small, so that
/// one fills quickly; the device's is kTraceSamples.
constexpr std::uint32_t kTraceSamplesFloor = 4096;

/// A drift anchor every this many samples: 1,024 anchors over a full ring of
/// kTraceSamples, 16 KiB, 0.1 % overhead. They let the host reconstruct real
/// timestamps by interpolation and **see** a stall or oscillator skew instead
/// of trusting the nominal period.
constexpr std::uint32_t kAnchorEvery = 1024;

/// Drift anchors a full ring carries, one per kAnchorEvery samples.
constexpr std::size_t kMaxAnchors = kTraceSamples / kAnchorEvery;

/// Forced anchors (one at the sample after a failed read) a capture adds at
/// most, beside the regular ones (0.15.0). The array holds both, so a forced
/// anchor never pushes out a regular one a full ring's span holds: before,
/// one failed read in a full ring cost it its first anchor, the t0 one, and
/// in 0.10.0 and earlier its last. `read_err=` stays the authoritative count.
constexpr std::size_t kMaxForcedAnchors = 16;

/// The anchor array: the regular anchors of a full ring and the forced ones.
constexpr std::size_t kAnchorSlots = kMaxAnchors + kMaxForcedAnchors;

/// Marks align a trace to an event on the DUT's own console.
constexpr std::size_t kMaxMarks = 16;

/// Slots kept beyond kMaxMarks for the hold's own marks (0.16.0): one per
/// relay a hold release moves, so a capture whose `TRACe:MARK` table is full
/// still records the instrument's own move.
constexpr std::size_t kHoldMarks = 2;

/// The mark array: kMaxMarks and the hold's.
constexpr std::size_t kMarkSlots = kMaxMarks + kHoldMarks;

/// Threshold marks one streamed capture writes at most
/// (`SENSe:CURRent:THReshold:MARK ON`): a cap, so a level set inside the
/// noise cannot flood the line.
constexpr std::uint32_t kMaxThresholdMarks = 1024;
constexpr std::size_t   kMarkLabelCapacity = 16;  ///< Characters a mark's label keeps.

/// Batch means a capture keeps, at most: the sampler sums each batch of
/// samples into a slot, and when every slot is full it merges them pairwise
/// into half of them and doubles the batch. So a closed capture of 32 samples
/// or more reports between 32 and 63 complete batches of equal size, whatever
/// its length, which is what an effective sample size is estimated from
/// (batch means, `docs/architecture.md` SS 3.8). 512 bytes.
constexpr std::size_t kBatchSlots = 64;

/// The batches a merge leaves: half the slots.
constexpr std::size_t kBatchSlotsAfterMerge = kBatchSlots / 2U;

// --- The line budget --------------------------------------------------------

/// Console baud rate; must match `monitor_speed` in platformio.ini.
// 6 Mbps, the CH343P's rated maximum, and measured rather than assumed. It was
// 921600 (too slow: STATE on two channels needs 1,736 lines/s and 921600
// admits 1,536, so an honest cap refused the capture the uncertainty work
// depends on), then 2 Mbps, which a timed ring dump showed running at 196 kB/s --
// 98 % of its own 200 kB/s ceiling, i.e. saturated, so the line and not the
// formatting was what bounded a dump.
//
// This is UART0 behind the carrier's CH343P (`docs/carrier-board.md` SS 7), so
// the baud is a real divider; the S3's fractional divider lands 6 Mbps 0.08 %
// off exact from the 80 MHz APB, inside any UART's tolerance. Verified on a
// 7.85 MB dump: 582.6 kB/s, 97 % of the line, all 209,936 sample lines parsing
// in full, no byte outside ASCII, and timestamp gaps exactly the declared
// period. `monitor_speed` in platformio.ini must match.
constexpr std::uint32_t kConsoleBaud = 6000000;

/// Bits on the wire per byte at 8N1.
constexpr std::uint32_t kBitsPerByte = 10;

/// Bytes per second the line carries: 600,000 B/s at 6,000,000 baud.
constexpr std::uint32_t kLineBytesPerSecond = kConsoleBaud / kBitsPerByte;

/// Digits the stream's timestamp may reach. `esp_timer_get_time()` counts
/// microseconds since boot, so fifteen digits is 10^15 us -- 31.7 years of
/// uptime. Beyond it the bound below would stop being one.
constexpr std::size_t kStreamTimestampDigits = 15;

/// The longest `METER t=<us> ch=<name> v=<value>` line, with its newline,
/// DERIVED from the format instead of estimated. Each term is the widest this
/// instrument can produce: the timestamp above, `die_c` of the four channel
/// names, and a full-scale negative current, the widest value any range admits
/// (`ADCRANGE 0` tops out at 1.6384 A; the bus and die channels are narrower).
constexpr std::uint32_t kStreamLineBytes = static_cast<std::uint32_t>(
    sizeof( "METER t=" ) - 1U + kStreamTimestampDigits + sizeof( " ch=" ) - 1U + sizeof( "die_c" )
    - 1U + sizeof( " v=" ) - 1U + sizeof( "-1638400.000" ) - 1U + sizeof( "\n" ) - 1U );

static_assert( kStreamLineBytes == 48,
               "The derivation changed. That is allowed, but the stream cap is checked "
               "against this number: a smaller bound admits a profile the line cannot "
               "carry, and the failure is a stream that starts and then overruns" );

/// The fraction of the line a stream may claim. Back-pressure is refused up
/// front, never absorbed: a profile table with a stream that could not be
/// delivered does not build (sampler.cpp), so no capture that cannot be
/// delivered is ever started.
constexpr std::uint32_t kLineBudgetPercent = 80;

/// Bytes per second a stream may claim: kLineBudgetPercent of the line.
constexpr std::uint32_t kStreamCapBytesPerSecond = kLineBytesPerSecond * kLineBudgetPercent / 100U;

/// Samples the stream queue buffers between the sampler and the console.
constexpr std::size_t kStreamQueueDepth = 256;

/// The sampler task's stack, in bytes (ESP-IDF counts a stack in bytes). A
/// static array inside the Sampler, like the queue's storage below.
constexpr std::uint32_t kSamplerStackBytes = 4096;

// --- The data -------------------------------------------------------------

/// One conversion, as the sampler read it. Raw counts, so the conversion to
/// amps happens once, at the print boundary, with the capture's own snapshot
/// of the calibration.
struct Sample {
    /// In us since boot: when the conversion-ready edge woke the loop on an
    /// alert-paced capture, when the read finished on a timer-paced one.
    std::uint64_t timestampUs;
    std::int32_t  shuntCounts;  ///< Raw VSHUNT counts, sign-extended.
    std::int32_t  busCounts;    ///< Raw VBUS counts; meaningful when busValid.
    std::int16_t  dieCounts;    ///< Raw DIETEMP value; meaningful when dieValid.
    std::uint16_t flags;        ///< DIAG_ALRT, on profiles that read it per sample.
    bool          busValid;     ///< The profile converts the bus and the read succeeded.
    bool          dieValid;     ///< The profile converts temperature and the read succeeded.
    RecordKind    kind;         ///< A conversion, or a mark queued in its order.
    std::uint8_t  markSlot;     ///< The mark's slot, for RecordKind::Mark.
    std::uint32_t index;        ///< The sample's index in the capture, or the mark's.
};

/// A drift anchor: the real timestamp of one ring sample.
struct Anchor {
    std::uint32_t index;        ///< The sample's index in the ring.
    std::uint64_t timestampUs;  ///< When it was read, in us since boot.
};

/// A mark recorded with Sampler::addMark().
struct Mark {
    std::uint32_t index;                          ///< The sample index it was recorded at.
    std::uint64_t timestampUs;                    ///< When it was recorded, in us since boot.
    char          label[kMarkLabelCapacity + 1];  ///< Its label, NUL-terminated.
};

/// What `FETCh:HEADer?` reports of the capture `INITiate` opened.
struct CaptureInfo {
    Profile       profile              = Profile::Trace;  ///< The capture's profile.
    Averaging     averaging            = {};  ///< The count and time it converts with (0.18.0).
    std::uint32_t periodUs             = 0;   ///< Microseconds per sample they give.
    Pace          pace                 = Pace::Timer;  ///< What paces it, as measured.
    std::uint32_t paceEdges            = 0;            ///< ALERT edges the pacing probe counted.
    std::uint32_t paceExpected         = 0;  ///< Conversions its window spans; 0: no probe.
    std::uint32_t ringSamples          = 0;  ///< 0 when streaming.
    std::uint32_t channels             = 0;  ///< The streamed channel mask; 0 unless streaming.
    std::uint64_t startTimeUs          = 0;  ///< When the capture started, in us since boot.
    std::int32_t  dieMilliCelsiusStart = kDieMilliCelsiusUnknown;  ///< At the opening boundary.
    std::int32_t  shuntCurrentDelta    = 0;  ///< `vs_cur_delta`, the free consistency check.

    /// Raw CHARGE at the opening boundary, and whether the read succeeded.
    ///
    /// Read, not reset: a window the operator opened with `SENSe:CHARge:RESet` is
    /// not stomped on, and `charge_raw_end - charge_raw_start` over the
    /// capture's own `dur_us` is the hardware-integrated mean current that
    /// `docs/ina228-reference.md` SS 8.4 makes the primary sleep
    /// number (`docs/architecture.md` SS 3.5).
    std::int64_t chargeRawStart   = 0;
    bool         chargeValidStart = false;  ///< The opening CHARGE read succeeded.

    // --- 0.11.0 -------------------------------------------------------------

    TriggerSource triggerSource  = TriggerSource::Immediate;  ///< `TRIGger:SOURce`.
    std::int64_t  triggerLevelNa = 0;                         ///< `TRIGger:LEVel`, nanoamps.
    bool          triggerRising  = true;                      ///< `TRIGger:SLOPe POSitive`.
    std::uint32_t pretrigger     = 0;                         ///< `TRIGger:PRETrigger`, samples.
    std::uint32_t sampleLimit    = 0;  ///< Samples it ends itself after; 0: none.
};

/// How far a capture's achieved period may sit from its profile's nominal one
/// before the cadence counts as not kept. The device's internal oscillator is
/// specified to +/-1 % ([DS] SS 7.5), so the limit is three times that: wide
/// enough never to flag a healthy capture, far below the 100 % a profile whose
/// period the loop cannot keep would show.
constexpr std::int32_t kPaceErrorLimitPpm = 30000;

/// What `FETCh:STATistics?` reports once `ABORt` (or a full ring) ends a
/// capture.
struct CaptureStats {  // NOLINT(clang-analyzer-optin.performance.Padding): in report order
    std::uint32_t samples            = 0;  ///< Samples the capture recorded.
    std::uint32_t dropped            = 0;  ///< Samples that did not reach the host.
    std::uint64_t durationUs         = 0;  ///< From the start to the last sample, in us.
    std::int64_t  meanPicoamps       = 0;  ///< Mean current, in picoamps.
    std::int64_t  minNanoamps        = 0;  ///< Lowest sample current, in nanoamps.
    std::int64_t  maxNanoamps        = 0;  ///< Highest sample current, in nanoamps.
    std::int32_t  dieMilliCelsiusEnd = kDieMilliCelsiusUnknown;  ///< At the closing boundary.
    std::uint16_t flags              = 0;  ///< Every DIAG_ALRT bit the window read, OR-ed.

    /// Conversions the sampler asked for and could not read off the bus.
    ///
    /// Never silently swallowed: the loop does not advance on a failed read, so
    /// for TRACE -- whose timestamps are implicit, `t0 + index x period` -- one
    /// lost read shifts every later sample's reported time by a period. The
    /// count is reported here and a drift anchor is forced at the gap, so the
    /// host sees the discontinuity instead of interpolating across it.
    std::uint32_t readFailures = 0;

    /// True if the ALERT line stopped pulsing mid-capture and the cadence fell
    /// back to the host timer (see Sampler::effectivePace()).
    bool paceFellBack = false;

    /// How far the period the capture actually achieved sits from the one its
    /// profile declares, in parts per million, signed; positive means slower
    /// than nominal. The achieved period is the span from the first sample to
    /// the last over the intervals between them (0.15.0): before, it was
    /// `durationUs / samples`, whose span starts at the capture's start, up to
    /// a period before the first sample, and so read the start's phase
    /// against the first conversion as an error of up to 1 / samples (SLEEP's
    /// 296 samples in 20 s read +14 ppm where their own intervals gave +102).
    ///
    /// It exists because nothing else here can see a missed pace. `dropped`
    /// counts stream-queue overflow only, so for a capture to RAM it is
    /// structurally zero however badly the cadence slips, and the samples
    /// themselves look perfect -- they are real conversions, just further
    /// apart than the header says. The device's own oscillator is specified to
    /// 1 %, so anything past kPaceErrorLimitPpm is the loop falling behind.
    std::int32_t paceErrorPpm = 0;

    /// True when |paceErrorPpm| exceeds kPaceErrorLimitPpm: the profile's
    /// period was not kept and implicit timestamps cannot be trusted.
    bool paceUnmet = false;

    std::int64_t chargeRawStart = 0;      ///< Raw CHARGE at the opening boundary.
    std::int64_t chargeRawEnd   = 0;      ///< Raw CHARGE at the closing boundary.
    bool         chargeValid    = false;  ///< Both boundary reads succeeded.

    /// Microseconds between the two CHARGE boundary reads: the span the pair
    /// integrates over, which brackets `durationUs` by the pacing probe at
    /// the start and the stop at the end (0.10.0).
    std::uint64_t chargeSpanUs = 0;

    /// Edges of an alert-paced capture whose DIAG_ALRT read found no
    /// conversion ready (CNVRF clear): a glitch on the wire, not a conversion.
    /// No sample is taken for one, so the samples stay one per conversion and
    /// `samples / (samples + staleEdges)` is the fresh-sample ratio. Always 0
    /// on a timer-paced capture.
    std::uint32_t staleEdges = 0;

    // --- 0.10.0: the statistics the uncertainty model needs ------------------

    /// The sample standard deviation of the current, in picoamps, and whether
    /// there is one (two samples or more). Computed in counts, from the
    /// deviations of every sample from the first, in 128 bits, so a capture of
    /// any length is exact (wide.h).
    std::int64_t deviationPicoamps = 0;
    bool         deviationValid    = false;  ///< Two samples or more were taken.

    std::uint32_t maxIndex  = 0;  ///< The sample index of the highest current.
    std::uint64_t maxTimeUs = 0;  ///< That sample's timestamp, in us since boot.

    /// Samples with a bus conversion, and the DUT's voltage and power over
    /// them: sums for the means, and the extremes (battery sag at a burst).
    /// Counted in the sampling loop, so a sample the stream dropped is still
    /// in them.
    std::uint32_t busSamples = 0;
    std::int64_t  sumDutUv   = 0;  ///< Sum of the DUT's voltage, microvolts.
    std::int64_t  sumPowerUw = 0;  ///< Sum of the delivered power, microwatts.
    std::int64_t  minDutUv   = 0;  ///< Lowest DUT voltage, microvolts.
    std::int64_t  maxDutUv   = 0;  ///< Highest DUT voltage, microvolts.
    std::int64_t  minPowerUw = 0;  ///< Lowest delivered power, microwatts.
    std::int64_t  maxPowerUw = 0;  ///< Highest delivered power, microwatts.

    std::uint32_t dieSamples = 0;  ///< Samples with a die conversion.
    std::int64_t  sumDieMc   = 0;  ///< Their sum, milli-degrees Celsius.

    /// The current threshold the capture ran with (Sampler::setThreshold()),
    /// the samples taken while the current was above it, and how many times
    /// it rose through it. With hysteresis: the state rises at the level and
    /// falls only below the level less the hysteresis.
    bool          thresholdEnabled = false;
    std::int64_t  thresholdNa      = 0;  ///< The level, nanoamps.
    std::uint32_t aboveSamples     = 0;  ///< Samples taken in the above state.
    std::uint32_t rises            = 0;  ///< Rising crossings.

    /// The complete batches (kBatchSlots); a partial last batch is left out.
    std::uint32_t batchCount = 0;
    /// Samples in each of them.
    std::uint32_t batchSamples = 0;
    /// Each complete batch's sum of nanoamps, the first batchCount of them.
    std::array<std::int64_t, kBatchSlots> batchSumsNa = {};

    // --- 0.11.0 -------------------------------------------------------------

    bool          triggered      = false;  ///< A level trigger fired.
    std::uint32_t triggerIndex   = 0;      ///< The sample index it fired at.
    std::uint64_t triggerTimeUs  = 0;      ///< That sample's timestamp.
    std::uint32_t thresholdMarks = 0;      ///< Threshold marks queued for the line.

    /// Times a timer-paced loop came more than a period late and its deadline
    /// was re-based to the arrival rather than read back to back (0.14.0).
    /// Always 0 on an alert-paced capture that never fell back.
    std::uint32_t rebases = 0;
};

/// What `CALibration:ZERO:RESult?` reports.
struct ZeroResult {
    std::int64_t  offsetPicovolts    = 0;  ///< Mean shunt voltage, in picovolts.
    std::int64_t  deviationPicovolts = 0;  ///< Its sample standard deviation, in picovolts.
    std::uint32_t samples            = 0;  ///< Samples the null was computed from.
    std::uint32_t seconds            = 0;  ///< The window startZero() asked for.
    std::int32_t  dieMilliCelsius    = kDieMilliCelsiusUnknown;  ///< When the null closed.
};

/// Longest a `CALibration:ZERO` window may be asked for, in seconds.
constexpr std::uint32_t kZeroMaxSeconds = 600;

/// The sampling engine.
class Sampler {  // NOLINT(clang-analyzer-optin.performance.Padding): grouped by role
   public:
    Sampler()                            = default;
    ~Sampler()                           = default;
    Sampler( const Sampler& )            = delete;
    Sampler& operator=( const Sampler& ) = delete;
    Sampler( Sampler&& )                 = delete;
    Sampler& operator=( Sampler&& )      = delete;

    /// @brief Creates the queue, the semaphores and the sampler task, and
    /// configures the ALERT pin.
    ///
    /// Every object both tasks may reach is built here, **before the consumer
    /// task exists** -- never by a bare `if ( handle == nullptr )` create, which
    /// both cores can pass and build twice into one control block (the
    /// dual-core rule of the Kleidos firmware repository, which this firmware
    /// keeps).
    ///
    /// The queue, the semaphores and the task are created over storage the
    /// Sampler holds (`xQueueCreateStatic`, `xSemaphoreCreateBinaryStatic`,
    /// `xTaskCreateStaticPinnedToCore`), the ring is the caller's static
    /// array, and the ALERT pin's interrupt is routed to a level-1 line of the
    /// calling core by the CPU's own tables (interrupt::routeHere()), with its
    /// enable left off until a capture or a probe listens: nothing here
    /// allocates. Called on Core 1, the sampler's (main.cpp), so that the
    /// interrupt runs where the task it wakes does.
    ///
    /// @param[in,out] device      The INA228 the sampler reads; kept by pointer.
    /// @param[in,out] calibration The calibration store; kept by pointer.
    /// @param[in]     alertPin    The GPIO wired to the device's ALERT pin, one
    ///                            of GPIO0 to GPIO31 (the status register
    ///                            alertIsr() reads).
    /// @param[in]     alertPullUp Whether the pin has the chip's internal
    ///                            pull-up (the pin map's `pull_alert`).
    /// @param[in,out] ring        The TRACE ring, kTraceSamplesFloor to
    ///                            kTraceSamples samples, kept for the life of
    ///                            the Sampler.
    /// @retval true  Every object was built and the sampler task started
    ///               (engineReady()).
    /// @retval false Something failed (no free CPU line among them), the
    ///               ALERT pin is outside GPIO0 to GPIO31, or the ring is
    ///               outside its bounds; every capture is then refused.
    bool begin( Ina228Driver& device, CalibrationStore& calibration, gpio_num_t alertPin,
                bool alertPullUp, std::span<std::int32_t> ring ) noexcept;

    /// @brief Moves ALERT to @p pin: the old pin's interrupt taken off and the
    /// new one set up as begin() sets it, on the same CPU line.
    ///
    /// Run on the core begin() ran on, with no window running and no probe. The
    /// pin ALERT leaves is its new owner's, or the caller's to park. A pin the
    /// pin layer refuses, or one outside GPIO0 to GPIO31, leaves engineReady()
    /// false until a later move is taken; an engine begin() did not start stays
    /// as it is.
    ///
    /// @param[in] pin     The GPIO wired to the device's ALERT pin.
    /// @param[in] pullUp  Whether it has the chip's internal pull-up.
    void moveAlert( gpio_num_t pin, bool pullUp ) noexcept;

    /// @brief Starts a capture.
    ///
    /// On false, @p error names the refusal in the wording the console puts in
    /// the error it queues.
    ///
    /// @param[in]  profile     The profile to capture with.
    /// @param[in]  channelMask The channels to stream (channelBit()); ignored
    ///                         by a profile that captures to RAM.
    /// @param[out] info        What `FETCh:HEADer?` reports of the capture.
    /// @param[out] error       The refusal, on false.
    /// @retval true  The capture started.
    /// @retval false It was refused; @p error says why.
    bool start( Profile profile, std::uint32_t channelMask, CaptureInfo& info,
                const char*& error ) noexcept;

    /// @brief Starts a statistics-only capture: no stream and no ring, only
    /// what `FETCh:STATistics?` reports, on any profile for any duration.
    ///
    /// The same loop and the same reads as start(), so the figures are those
    /// a streamed or ring capture of the profile would give; nothing is
    /// queued for the line, so no stream cap applies and the ring is not
    /// touched (TRACE's rate for hours, which neither the 293 s ring nor the
    /// line allows).
    ///
    /// @param[in]  profile The profile to capture with.
    /// @param[out] info    What `FETCh:HEADer?` reports of the capture.
    /// @param[out] error   The refusal, on false.
    /// @retval true  The capture started.
    /// @retval false It was refused; @p error says why.
    bool startStatistics( Profile profile, CaptureInfo& info, const char*& error ) noexcept;

    /// @brief Sets the current threshold the next captures count against.
    ///
    /// Refused while a capture is open.
    ///
    /// @param[in]  enabled      Whether the threshold is counted at all.
    /// @param[in]  levelNa      The level, nanoamps; the state rises at it.
    /// @param[in]  hysteresisNa How far below the level the state falls,
    ///                          nanoamps, 0 or more.
    /// @param[out] error        The refusal, on false.
    /// @retval true  The threshold is set.
    /// @retval false A capture is open, or @p hysteresisNa is negative.
    bool setThreshold( bool enabled, std::int64_t levelNa, std::int64_t hysteresisNa,
                       const char*& error ) noexcept;

    /// @brief Whether a streamed capture writes a mark record each time the
    /// current crosses the threshold (`SENSe:CURRent:THReshold:MARK`).
    ///
    /// The marks travel the stream queue with the samples, so none is written
    /// before a sample of a lower index; at most kMaxThresholdMarks a capture.
    /// Refused while a capture is open.
    ///
    /// @param[in]  enabled Whether to write them.
    /// @param[out] error   The refusal, on false.
    /// @retval true  Set.
    /// @retval false A capture is open.
    bool setThresholdMarks( bool enabled, const char*& error ) noexcept;
    /// @brief The self-test's look at the ALERT wire: STATE's conversion
    /// word and its pacing probe, edges counted over kPaceProbeConversions
    /// (8) conversions with a DIAG_ALRT read after each, as a capture does,
    /// and in the sampler task on Core 1, where a capture reads them (0.16.2).
    /// It lets two periods pass after the writes before it asks (0.16.4), so
    /// a conversion begun after them sets CNVRF before the probe's first read.
    ///
    /// Leaves the device converting with STATE's word; every capture applies
    /// its own. For the console's self-test, which refuses itself under an
    /// open window and with no device or engine.
    ///
    /// @return True when the wire paces: the probe a capture runs at
    ///         `INITiate` would choose `pace=alert`.
    bool probeAlert() noexcept;

    /// @brief The fewest bytes of the sampler task's stack ever left free.
    /// @return The bytes, or 0 when the task never started.
    std::uint32_t stackFreeBytes() const noexcept;

    /// @brief Whether threshold marks are written.
    /// @return The setting.
    bool thresholdMarks() const noexcept { return thresholdMarks_; }

    /// @brief Starts a ring capture whose record a level starts: the ring
    /// runs circular from now until the current crosses @p levelNa in the
    /// direction @p rising names, having first been on the other side of it;
    /// it then keeps @p pretrigger samples from before the crossing, records
    /// the rest of the ring after it, and the capture ends itself.
    ///
    /// @param[in]  profile    A ring profile (TRACE, BURST).
    /// @param[in]  levelNa    The level, nanoamps.
    /// @param[in]  rising     True: upwards through the level; false: downwards.
    /// @param[in]  pretrigger Samples kept before the crossing, under the ring.
    /// @param[out] info       What `FETCh:HEADer?` reports of the capture.
    /// @param[out] error      The refusal, on false.
    /// @retval true  The capture started.
    /// @retval false It was refused; @p error says why.
    bool startTriggered( Profile profile, std::int64_t levelNa, bool rising,
                         std::uint32_t pretrigger, CaptureInfo& info, const char*& error ) noexcept;
    /// @brief Whether the open capture's level trigger has fired.
    /// @return True once it has.
    bool triggerFired() const noexcept { return triggerFired_.load(); }
    /// @brief The sample index the open or last capture's trigger fired at.
    /// @return The index; meaningful when triggerFired().
    std::uint32_t triggerIndex() const noexcept { return triggerIndex_; }
    /// @brief That sample's timestamp.
    /// @return The time, in us since boot; meaningful when triggerFired().
    std::uint64_t triggerTimeUs() const noexcept { return triggerTimeUs_; }

    /// @brief The samples the next start() or startStatistics() ends itself
    /// after (`SENSe:CAPTure:LIMit` with `SAMPle:COUNt`); 0 for none.
    ///
    /// A ring capture ends at its ring whatever this says, and a triggered
    /// one when its post-trigger samples are in.
    ///
    /// @param[in] samples The limit; 0 for none.
    void setCaptureLimit( std::uint32_t samples ) noexcept { captureLimit_ = samples; }

    /// @brief Whether the open capture ends by itself: a ring, a null, or a
    /// limited one. `*OPC` waits for that end.
    /// @return True when it will end with no `ABORt`.
    bool endsItself() const noexcept;

    /// @brief Queues a mark record behind the samples already queued, so the
    /// console writes it in conversion order.
    ///
    /// @param[in] record The record (RecordKind::Mark, its slot, index and time).
    /// @retval true  Queued.
    /// @retval false The queue is full, or begin() never built it.
    bool queueRecord( const Sample& record ) noexcept;

    /// @brief Whether a threshold is set.
    /// @return True when captures count against one.
    bool thresholdEnabled() const noexcept { return thresholdEnabled_; }
    /// @brief The threshold's level.
    /// @return The level, in nanoamps.
    std::int64_t thresholdNa() const noexcept { return thresholdNa_; }
    /// @brief The threshold's hysteresis.
    /// @return The hysteresis, in nanoamps.
    std::int64_t hysteresisNa() const noexcept { return hysteresisNa_; }

    /// @brief Ends a capture and fills @p stats.
    ///
    /// False if none was open, or if the sampler task did not acknowledge the
    /// stop (halt()): then the window is closed with nothing computed.
    ///
    /// @param[out] stats What `FETCh:STATistics?` reports of the capture.
    /// @retval true  The capture ended and @p stats is filled.
    /// @retval false None was open, or the stop was not acknowledged.
    bool stop( CaptureStats& stats ) noexcept;

    /// @brief Fills @p stats of the open capture and closes its window: the
    /// second half of stop(), for a caller that has halted the loop itself.
    ///
    /// @pre A capture is open and halt() returned true for it.
    ///
    /// @param[out] stats What `FETCh:STATistics?` reports of the capture.
    void closeCapture( CaptureStats& stats ) noexcept;

    /// @brief Ends the sampling loop and waits up to `kStopTimeoutMs` (500 ms,
    /// sampler.cpp) for the sampler task to acknowledge it.
    ///
    /// True once it has, at once when it already had.
    ///
    /// The statistics the loop accumulated cross over by value with that
    /// acknowledgement, so nothing the task writes is read before it. On a
    /// timeout the open window is closed with nothing computed, and every
    /// later start() is refused, naming the engine, until the task's late
    /// acknowledgement arrives: a loop still running must not run on into the
    /// next capture with the previous one's profile.
    ///
    /// @retval true  The task acknowledged the stop, now or before.
    /// @retval false It did not within the timeout.
    bool halt() noexcept;

    /// @brief Starts a zero-offset null over @p seconds with the DUT lead open.
    ///
    /// Split from finishZero() on purpose: `CALibration:ZERO 600` is ten
    /// minutes, and a blocking wait inside the sampler would hold the console
    /// task for all of it -- with no progress and no way to abort, because
    /// `ABORt` could not be read. The console services the line while this
    /// runs, `SYSTem:STATus?` reports `samples=`, and `ABORt` ends the window
    /// early.
    ///
    /// @param[in]  seconds The window, 1 to kZeroMaxSeconds.
    /// @param[out] error   The refusal, on false.
    /// @retval true  The window opened.
    /// @retval false It was refused; @p error says why.
    bool startZero( std::uint32_t seconds, const char*& error ) noexcept;

    /// @brief Computes the null from whatever the run collected and closes the
    /// capture.
    ///
    /// Reports the mean shunt voltage and its sample standard deviation, so
    /// that a bad null is visible.
    ///
    /// Safe to call after `ABORt` has already closed the window: the
    /// accumulators the null is computed from are untouched by stop().
    ///
    /// @param[out] result What `CALibration:ZERO:RESult?` reports.
    /// @retval true  At least one sample was taken and @p result holds the
    ///               null.
    /// @retval false None was, or the stop was not acknowledged (halt()).
    bool finishZero( ZeroResult& result ) noexcept;

    /// @brief Samples a `CALibration:ZERO <seconds>` window will take, for its
    /// running report.
    ///
    /// @param[in] seconds The window, in seconds.
    /// @return The samples the live profile's period fits in it.
    std::uint32_t zeroSampleCount( std::uint32_t seconds ) const noexcept;

    /// @brief Selects the profile for the next capture.
    ///
    /// Refused while one runs.
    ///
    /// @param[in]  profile The profile.
    /// @param[out] error   The refusal, on false.
    /// @retval true  The profile is selected.
    /// @retval false A capture is open; @p error says so.
    bool setProfile( Profile profile, const char*& error ) noexcept;

    /// @brief What @p profile converts with: its own specification, or the
    /// averaging override's when that is on (0.18.0): the AVG count and the
    /// shunt conversion time replaced, and the ADC_CONFIG word and the period
    /// that follow from them.
    ///
    /// @param[in] profile The profile; a value outside the table yields TRACE's.
    /// @return The specification the next capture of it runs with.
    ProfileSpec conversion( Profile profile ) const noexcept;

    /// @brief The live profile's averaging override.
    /// @return The override, its count and time kept while it is off.
    Averaging averaging() const noexcept;

    /// @brief Sets the live profile's averaging override (`SENSe:AVERage`).
    ///
    /// Held to what paces the profile (docs/architecture.md SS 3.2): BURST
    /// has no override, and one that is on must give a period no shorter than
    /// twice the bus time a sample's reads take at the running clock (nor,
    /// on a ring profile, than the 100 us BURST keeps), and no longer than
    /// the 200 ms a stop, the pacing probe and the watchdog allow. A refusal
    /// changes nothing. Not persisted: begin() and resetAveraging() set every
    /// profile's to off.
    ///
    /// @param[in]  wanted The override.
    /// @param[out] error  The refusal, on false.
    /// @retval true  Set, for the profile's next capture.
    /// @retval false Refused: a capture is open, the profile is BURST, or the
    ///               period is outside what the profile's pacing allows.
    bool setAveraging( const Averaging& wanted, const char*& error ) noexcept;

    /// @brief Turns every profile's averaging override off, its count and
    /// time back to the profile's own (`*RST`).
    void resetAveraging() noexcept;

    /// @brief True once begin() has built every object and started the sampler
    /// task.
    ///
    /// A capture is refused by name while this is false, because an engine
    /// that never came up cannot produce a sample and must not print a header
    /// promising one.
    ///
    /// @return Whether the engine came up.
    bool engineReady() const noexcept { return engineReady_; }

    /// @brief The profile selected for the next or the live capture.
    /// @return The live profile.
    Profile profile() const noexcept { return profile_.load(); }
    /// @brief Whether the sampling loop is running.
    /// @return True while the loop samples.
    bool running() const noexcept { return running_.load(); }

    /// @brief The pace actually in force.
    ///
    /// Timer once the ALERT line has stopped pulsing and the cadence has fallen
    /// back (see paceFellBack()).
    ///
    /// @return The pace in force.
    Pace effectivePace() const noexcept {
        return paceFellBack_.load() ? Pace::Timer : pace_.load();
    }

    /// @brief Whether the ALERT line stopped pulsing mid-capture and the
    /// cadence fell back to the host timer.
    /// @return True once it has fallen back.
    bool paceFellBack() const noexcept { return paceFellBack_.load(); }

    /// @brief Conversions this capture could not read off the bus.
    /// @return The count of failed reads.
    std::uint32_t readFailures() const noexcept { return readFailures_.load(); }

    /// @brief What the open capture does with its samples: `ring`, `stream`,
    /// `null`, or `idle` when none is open.
    ///
    /// A ring capture that fills its ring ends itself -- `running()` goes
    /// false -- but the capture stays **open** until `ABORt` collects its
    /// statistics, so `running=0 sink=ring` means "finished, awaiting ABORt"
    /// and `sink=idle` means idle. A host that polls `SYSTem:STATus?` and then
    /// starts again needs that distinction, because `INITiate` is refused
    /// while a capture is open.
    ///
    /// @return The sink's name, a static string.
    const char* sinkName() const noexcept;

    /// @brief True while any capture is open, running or finished and
    /// awaiting `ABORt`.
    ///
    /// The relay checks read the device only when this is false.
    ///
    /// @return Whether a capture is open.
    bool captureOpen() const noexcept { return sink_.load() != Sink::Idle; }

    /// @brief True while the open capture streams its samples to the console.
    /// @return Whether the open capture is a stream.
    bool streaming() const noexcept { return sink_.load() == Sink::Stream; }

    /// @brief True while a `CALibration:ZERO` window is the open capture,
    /// running or finished.
    ///
    /// `ABORt` closes a window of either kind, so this is how it tells which
    /// result it stores.
    ///
    /// @return Whether the open capture is a null.
    bool zeroOpen() const noexcept { return sink_.load() == Sink::Null; }
    /// @brief The live profile's sample period.
    /// @return The period, in microseconds.
    std::uint32_t periodUs() const noexcept { return conversion( profile_.load() ).periodUs; }
    /// @brief Samples the open or last capture has recorded.
    /// @return The sample count.
    std::uint32_t sampleCount() const noexcept { return samples_.load(); }
    /// @brief Samples of the open or last capture that did not reach the host.
    /// @return The dropped count.
    std::uint32_t dropped() const noexcept { return dropped_.load(); }
    /// @brief The DIAG_ALRT bits the open or last capture has read, OR-ed.
    /// @return The accumulated flags.
    std::uint16_t flags() const noexcept { return flags_.load(); }
    /// @brief The channel mask the open or last capture streams.
    /// @return The mask; 0 unless streaming.
    std::uint32_t channelMask() const noexcept { return channels_; }
    /// @brief When the open or last capture started.
    /// @return The start, in us since boot.
    std::uint64_t captureStartUs() const noexcept { return captureStartUs_; }

    /// @brief Counts samples the console could not place on the line.
    ///
    /// `dropped=` is the count of samples that did not reach the host, whatever
    /// swallowed them: a full queue here, or a short write there. A short write
    /// is the default failure of a sink, so the console uses what
    /// uart0::write() returns and reports what it could not place rather
    /// than assuming it all went out.
    ///
    /// @param[in] count Samples to add to `dropped=`.
    void noteDropped( std::uint32_t count ) noexcept { (void)dropped_.fetch_add( count ); }

    /// @brief Takes the next streamed sample, waiting at most @p waitMs.
    ///
    /// @param[out] out    The sample taken.
    /// @param[in]  waitMs The longest wait, in milliseconds.
    /// @retval true  A sample was taken into @p out.
    /// @retval false None arrived in time, or begin() never built the queue.
    bool nextStreamSample( Sample& out, std::uint32_t waitMs ) noexcept;

    /// @brief Records a mark at the current sample index.
    ///
    /// @param[in]  label  The mark's label, truncated to kMarkLabelCapacity
    ///                    characters; may be null.
    /// @param[out] stored A copy of the mark as recorded.
    /// @retval true  The mark was recorded.
    /// @retval false kMaxMarks are already recorded.
    bool addMark( const char* label, Mark& stored ) noexcept;

    /// @brief Records the hold's own mark at the current sample index
    /// (0.16.0), as addMark() does, into the kHoldMarks slots kept for it
    /// once kMaxMarks are taken.
    ///
    /// @param[in]  label  The mark's label, truncated to kMarkLabelCapacity
    ///                    characters; may be null.
    /// @param[out] stored A copy of the mark as recorded.
    /// @retval true  The mark was recorded.
    /// @retval false kMarkSlots are already recorded.
    bool addHoldMark( const char* label, Mark& stored ) noexcept;

    // --- The captured trace, for TRACe:DATA? -------------------------------

    /// @brief Samples the ring holds.
    /// @return The ring's fill, in samples.
    std::uint32_t ringCount() const noexcept { return ringCount_.load(); }
    /// @brief The capture's sample index of the oldest sample the ring holds:
    /// 0 unless a level trigger let it run circular.
    /// @return The index.
    std::uint32_t ringFirstIndex() const noexcept;
    /// @brief Samples the ring holds: the array begin() was handed.
    /// @return The ring's capacity, in samples.
    std::uint32_t ringCapacity() const noexcept { return ringCapacity_; }
    /// @brief One ring sample, in time order.
    /// @param[in] offset Its place from the oldest sample the ring holds.
    /// @return Its raw VSHUNT counts, or 0 if @p offset is past ringCount().
    std::int32_t ringAt( std::uint32_t offset ) const noexcept;
    /// @brief Drift anchors the ring holds, the last kAnchorSlots recorded.
    /// @return The anchor count.
    std::size_t anchorCount() const noexcept {
        return ( anchorTotal_ < kAnchorSlots ) ? anchorTotal_ : kAnchorSlots;
    }
    /// @brief One drift anchor, oldest first.
    /// @param[in] index The anchor's place; must be below anchorCount().
    /// @return The anchor.
    const Anchor& anchorAt( std::size_t index ) const noexcept {
        const std::size_t first =
            ( anchorTotal_ < kAnchorSlots ) ? 0U : ( anchorTotal_ % kAnchorSlots );
        return anchors_[( first + index ) % kAnchorSlots];
    }
    /// @brief Marks the capture recorded.
    /// @return The mark count.
    std::size_t markCount() const noexcept { return markCount_; }
    /// @brief One mark.
    /// @param[in] index The mark's index; must be below markCount().
    /// @return The mark.
    const Mark& markAt( std::size_t index ) const noexcept { return marks_[index]; }

    // --- The calibration the live or last capture used ----------------------

    /// @brief The zero-current null the capture snapshotted.
    /// @return The offset, in picovolts.
    std::int64_t captureOffsetPicovolts() const noexcept { return captureOffsetPv_; }
    /// @brief The shunt resistance the capture snapshotted.
    /// @return The resistance, in micro-ohms.
    std::uint32_t captureShuntMicroOhm() const noexcept { return captureShuntMicroOhm_; }
    /// @brief The ADC range the capture snapshotted.
    /// @return The range.
    ina228::ShuntRange captureRange() const noexcept { return captureRange_; }

    /// @brief The period the live or last capture ran with, latched at
    /// `INITiate`.
    ///
    /// A dump needs this and not the live profile's: a `SENSe:PROFile` or a
    /// `SENSe:AVERage` between the capture and `TRACe:DATA?` changes the live
    /// one, and the ring's timestamps are implicit, so a dump that took its
    /// period from it would silently re-time the whole trace.
    ///
    /// @return The period, in microseconds.
    std::uint32_t capturedPeriodUs() const noexcept { return windowSpec_.periodUs; }

    /// @brief Converts a sample's shunt counts to nanoamps with that snapshot.
    ///
    /// @param[in] shuntCounts Raw VSHUNT counts.
    /// @return The current, in nanoamps.
    std::int64_t currentNanoamps( std::int32_t shuntCounts ) const noexcept;

    // --- The last conversion the sampler saw --------------------------------
    // `SYSTem:STATus?` reports these while a capture runs rather than reading the
    // device itself: TRACE's affordability rests on the register pointer being
    // retained, and a console-side read would move it out from under the
    // sampling loop.

    /// @brief The last sample's raw VSHUNT counts.
    /// @return The counts.
    std::int32_t lastShuntCounts() const noexcept { return lastShuntCounts_.load(); }
    /// @brief The last valid bus reading's raw VBUS counts.
    /// @return The counts.
    std::int32_t lastBusCounts() const noexcept { return lastBusCounts_.load(); }

    /// @brief The last die temperature the sampler saw, at a sample or at a
    /// capture boundary, and the one the quality token is judged against.
    ///
    /// Every writer stores here. TRACE converts temperature only at its
    /// boundaries, which is the whole reason the triggered conversion exists,
    /// so a boundary reading has to reach this value or `q=` would fall through
    /// to `cal` on no evidence however far the bench had drifted. Refreshed at
    /// every `INITiate`, with the sentinel when the boundary read failed, so a
    /// reading never outlives the capture that took it.
    ///
    /// @return The temperature, in milli-degrees Celsius, or
    ///         kDieMilliCelsiusUnknown.
    std::int32_t lastDieMilliCelsius() const noexcept { return lastDieMilliCelsius_.load(); }

    /// @brief How long ago that temperature was read, in microseconds, or 0
    /// when there is none.
    ///
    /// Reported rather than expired: the instrument has no basis in [DS] or in
    /// this repository for a number of seconds after which a die temperature
    /// stops being evidence, so it states the age and leaves the judgment to
    /// whoever reads it.
    ///
    /// @return The age, in microseconds; 0 when there is no reading.
    std::uint64_t dieAgeUs() const noexcept;

   private:
    /// What the running capture does with each sample.
    enum class Sink : std::uint8_t {
        Idle,    ///< No window open.
        Ring,    ///< TRACE, BURST: append to RAM, print nothing.
        Stream,  ///< STATE, SLEEP: hand to the console task.
        Null,    ///< CALibration:ZERO: accumulate mean and deviation, print nothing.
        Stats,   ///< Any profile: the statistics only, print nothing, keep nothing.
    };

    /// How many sinks there are, the last one's index and one more.
    static constexpr std::size_t kSinkCount = static_cast<std::size_t>( Sink::Stats ) + 1U;

    /// What the sampling loop accumulated over one window. The task keeps its
    /// own copy while it runs and hands it over by value, in totals_, just
    /// before it gives doneSem_; the console reads totals_ only after taking
    /// it (halt()).
    struct LoopTotals {
        std::uint64_t firstUs     = 0;  ///< The first sample's timestamp.
        std::uint64_t endUs       = 0;  ///< The last sample's timestamp.
        std::int64_t  sumNanoamps = 0;  ///< The sum of the current, nanoamps.
        std::int64_t  minNanoamps = 0;  ///< The least current.
        std::int64_t  maxNanoamps = 0;  ///< The greatest current.
        /// The null accumulates deviations from its first sample, so that
        /// neither the sum nor the sum of squares can overflow on a long run.
        std::int32_t nullReference    = 0;
        std::int64_t nullSumDeviation = 0;  ///< The null's sum of deviations, counts.
        /// The squared deviations, for every sink, in 128 bits (wide.h): the
        /// standard deviation of a capture's current and of a null.
        wide::U128    sumSquares = {};
        std::uint32_t maxIndex   = 0;      ///< The greatest current's sample index.
        std::uint64_t maxTimeUs  = 0;      ///< Its timestamp.
        std::uint32_t busSamples = 0;      ///< Samples with a bus conversion.
        std::int64_t  sumDutUv   = 0;      ///< Their DUT voltage, summed, microvolts.
        std::int64_t  sumPowerUw = 0;      ///< Their power, summed, microwatts.
        std::int64_t  minDutUv   = 0;      ///< The least DUT voltage.
        std::int64_t  maxDutUv   = 0;      ///< The greatest DUT voltage.
        std::int64_t  minPowerUw = 0;      ///< The least power.
        std::int64_t  maxPowerUw = 0;      ///< The greatest power.
        std::uint32_t dieSamples = 0;      ///< Samples with a die conversion.
        std::int64_t  sumDieMc   = 0;      ///< Their die temperature, summed, m°C.
        bool          above      = false;  ///< The last sample was above the threshold.
        /// The threshold's crossing at the last sample, if any: a mark to
        /// queue behind it.
        RecordKind    crossing     = RecordKind::Conversion;
        std::uint32_t aboveSamples = 0;  ///< Samples above the threshold.
        std::uint32_t rises        = 0;  ///< Rises through it.
    };

    /// @brief The sampler task's entry point: runs runCapture().
    /// @param[in] context The Sampler, as begin() passed it.
    static void taskEntry( void* context ) noexcept;
    /// @brief The ALERT pin's interrupt, on the CPU line begin() routed: clears
    /// every GPIO status bit it reads (the line is level-triggered), and for
    /// the ALERT pin's counts the edge and wakes the loop.
    /// @param[in] context The Sampler, as begin() routed it.
    static void alertIsr( void* context ) noexcept;

    /// @brief Whether what paces @p spec allows its period, for an averaging
    /// override that is on (setAveraging()).
    /// @param[in]  spec  The override's specification.
    /// @param[out] error The refusal, on false.
    /// @retval true  The period is inside the profile's limits.
    /// @retval false It is not; @p error names the averaging and the limit.
    bool paceAllows( const ProfileSpec& spec, const char*& error ) noexcept;

    /// @brief Opens a window: the refusals, the profile's word, the pacing
    /// probe, the opening CHARGE read and the header facts, then the start
    /// handed to the sampler task.
    /// @param[in]  profile     The profile to sample on.
    /// @param[in]  sink        What each sample is for.
    /// @param[in]  channelMask The channels a stream carries.
    /// @param[in]  sampleLimit Samples the window ends at; 0 = until ABORt.
    /// @param[in]  triggered   A ring capture that waits for the level.
    /// @param[out] info        The header facts.
    /// @param[out] error       The reason, when refused.
    /// @retval true  The window is open.
    /// @retval false It was refused (@p error says why).
    bool beginCapture( Profile profile, Sink sink, std::uint32_t channelMask,
                       std::uint32_t sampleLimit, bool triggered, CaptureInfo& info,
                       const char*& error ) noexcept;
    /// @brief Reads the CHARGE accumulator.
    /// @param[out] valid Whether the read succeeded.
    /// @return The signed 40-bit count; 0 when the read failed.
    std::int64_t readChargeRaw( bool& valid ) noexcept;
    /// @brief Keeps the last die temperature and when it was read.
    /// @param[in] milliCelsius The temperature, or kDieMilliCelsiusUnknown.
    void noteDieMilliCelsius( std::int32_t milliCelsius ) noexcept;
    /// @brief Ends the window on the console's side: the ALERT interrupt off,
    /// the sink idle.
    void finishCapture() noexcept;
    /// @brief The sampler task's loop: waits for a start, runs the pacing
    /// probe or the window it asks for, and acknowledges. Never returns.
    void runCapture() noexcept;
    /// @brief Adds one sample to the window's totals.
    /// @param[in]     sample    The sample.
    /// @param[in]     currentNa Its current, nanoamps.
    /// @param[in]     index     Its index in the window.
    /// @param[in,out] totals    The totals.
    void accumulate( const Sample& sample, std::int64_t currentNa, std::uint32_t index,
                     LoopTotals& totals ) noexcept;
    /// @brief Adds one sample's current to the batch means, merging the
    /// batches pairwise when every slot is full.
    /// @param[in] currentNa The current, nanoamps.
    void addToBatch( std::int64_t currentNa ) noexcept;
    /// @brief Appends one sample to the ring, with an anchor when it is due or
    /// forced, and runs the level trigger.
    /// @param[in] sample    The sample.
    /// @param[in] currentNa Its current, nanoamps.
    /// @param[in] index     Its index in the window.
    void storeInRing( const Sample& sample, std::int64_t currentNa, std::uint32_t index ) noexcept;
    /// @brief Queues a threshold crossing's mark record behind @p sample, at
    /// most kMaxThresholdMarks a capture.
    /// @param[in] kind   ThresholdRise or ThresholdFall.
    /// @param[in] sample The sample at the crossing.
    /// @param[in] index  Its index in the window.
    void queueThresholdMark( RecordKind kind, const Sample& sample, std::uint32_t index ) noexcept;
    /// @brief Points the INA228 at VSHUNT for a ring capture's retained reads,
    ///        writing the pointer again after a failed write, kPointerWrites
    ///        times at most.
    ///
    /// A ring profile reads every sample from the retained pointer with no
    /// pointer of its own, so a write that failed would leave the pointer on
    /// DIAG_ALRT, where the start's last read put it, and every read would
    /// return that register's bytes as shunt counts.
    /// @return true once a write succeeded; false when none did.
    bool retainShuntPointer() noexcept;

    /// What one turn of the loop's read produced.
    enum class ReadOutcome : std::uint8_t {
        Taken,   ///< A sample was read.
        Failed,  ///< A register read failed.
        Stale    ///< An ALERT edge with no conversion ready.
    };
    /// @brief Takes @p pin for ALERT: an input with or without its pull-up,
    /// its falling edge latched and its interrupt off.
    /// @param[in] pin     The GPIO.
    /// @param[in] pullUp  Whether it has the chip's internal pull-up.
    /// @return false for a pin outside GPIO0 to GPIO31 or one `gpio_config()`
    ///         refuses.
    bool connectAlert( gpio_num_t pin, bool pullUp ) noexcept;
    /// @brief Reads one sample's registers as @p spec converts them.
    /// @param[in]  spec   The profile.
    /// @param[in]  alert  The loop was woken by an ALERT edge.
    /// @param[in]  wokeUs When it woke, the sample's timestamp on an edge.
    /// @param[out] sample The sample.
    /// @return What the read produced.
    ReadOutcome readSample( const ProfileSpec& spec, bool alert, std::uint64_t wokeUs,
                            Sample& sample ) noexcept;
    /// @brief Whether the open window is paced by ALERT and has not fallen
    /// back to the timer.
    /// @retval true  The loop waits for an edge.
    /// @retval false It waits for the timer's deadline.
    bool alertPacing() const noexcept;
    /// @brief Waits for the next conversion: an ALERT edge, or the next
    /// deadline of the timer; falls back to the timer when the edges stop.
    /// @param[in] spec The profile.
    void waitForConversion( const ProfileSpec& spec ) noexcept;
    /// @brief The pacing probe: counts ALERT edges over a short window to see
    /// whether the pin is wired and pulsing.
    /// @param[in] periodUs The period an edge is due every.
    /// @return Pace::Alert when the edges came at that rate, else Pace::Timer.
    Pace measurePace( std::uint32_t periodUs ) noexcept;
    /// @brief Runs the pacing probe in the sampler task, where the capture
    /// will run, and waits for its answer.
    /// @param[in] spec The profile, or its override.
    /// @return The probe's result; Pace::Timer when it did not answer, or for
    ///         a profile the timer paces, which is not probed.
    Pace requestPace( const ProfileSpec& spec ) noexcept;
    /// @brief Latches the range, its LSB, offset and shunt for the window.
    void snapshotCalibration() noexcept;
    /// @brief CURRENT counts less VSHUNT counts, the header's check of
    /// SHUNT_CAL.
    /// @return The difference in counts; 0 when a read failed.
    std::int32_t readConsistencyDelta() noexcept;
    /// @brief One die temperature: the profile's own conversion, or a
    /// triggered one on a profile that does not convert it.
    /// @param[in] spec The profile.
    /// @return The temperature in m°C, or kDieMilliCelsiusUnknown.
    std::int32_t readDieMilliCelsius( const ProfileSpec& spec ) noexcept;

    Ina228Driver*     device_      = nullptr;      ///< The INA228, from begin().
    CalibrationStore* calibration_ = nullptr;      ///< The calibration store, from begin().
    gpio_num_t        alertPin_    = GPIO_NUM_NC;  ///< The GPIO wired to ALERT.
    interrupt::Route  alertRoute_  = {};           ///< Its interrupt's core and CPU line.
    bool              engineReady_ = false;  ///< begin() built everything; ALERT's pin was taken.

    QueueHandle_t     streamQueue_   = nullptr;  ///< Samples and marks for the console.
    SemaphoreHandle_t conversionSem_ = nullptr;  ///< Given by alertIsr() on each edge.
    SemaphoreHandle_t startSem_      = nullptr;  ///< The console's start, or probe request.
    SemaphoreHandle_t doneSem_       = nullptr;  ///< The task's acknowledgement of a stop.
    SemaphoreHandle_t probeDoneSem_  = nullptr;  ///< The task's answer to a probe.
    TaskHandle_t      task_          = nullptr;  ///< The sampler task.

    /// The storage the queue, the semaphores and the task are created over:
    /// static with the Sampler, never allocated.
    StaticQueue_t streamQueueBuffer_ = {};
    /// The queue's items.
    std::array<std::uint8_t, kStreamQueueDepth * sizeof( Sample )> streamQueueStorage_ = {};
    StaticSemaphore_t conversionSemBuffer_ = {};  ///< conversionSem_'s.
    StaticSemaphore_t startSemBuffer_      = {};  ///< startSem_'s.
    StaticSemaphore_t doneSemBuffer_       = {};  ///< doneSem_'s.
    StaticSemaphore_t probeDoneSemBuffer_  = {};  ///< probeDoneSem_'s.
    StaticTask_t      taskBuffer_          = {};  ///< The task's control block.
    /// The task's stack.
    std::array<StackType_t, kSamplerStackBytes / sizeof( StackType_t )> taskStack_ = {};

    std::atomic<bool>          running_{ false };  ///< A window samples.
    std::atomic<std::uint32_t> samples_{ 0 };      ///< Samples taken this window.
    std::atomic<std::uint32_t> dropped_{ 0 };      ///< Samples the stream queue refused.
    std::atomic<std::uint16_t> flags_{ 0 };        ///< DIAG_ALRT flags latched this window.
    std::atomic<std::uint32_t> alertEdges_{ 0 };   ///< ALERT edges counted.

    /// Conversions the sampling loop could not read off the bus. The loop does
    /// not advance on one, so this is the only evidence a capture's implicit
    /// timestamps have slipped.
    std::atomic<std::uint32_t> readFailures_{ 0 };

    /// Alert-paced edges whose DIAG_ALRT read showed no conversion ready
    /// (CaptureStats::staleEdges).
    std::atomic<std::uint32_t> staleEdges_{ 0 };

    /// Timer-paced deadlines re-based to a late arrival (CaptureStats::rebases).
    std::atomic<std::uint32_t> rebases_{ 0 };

    /// Set when the ALERT wait times out: the pin was pulsing when the probe
    /// measured it and has stopped since. The cadence then falls back to the
    /// host timer rather than running at the timeout rate, which for TRACE is
    /// 10 ms against a 280 us period.
    std::atomic<bool> paceFellBack_{ false };

    std::atomic<std::int32_t> lastShuntCounts_{ 0 };  ///< The last VSHUNT counts.
    std::atomic<std::int32_t> lastBusCounts_{ 0 };    ///< The last VBUS counts.
    std::atomic<std::int32_t> lastDieMilliCelsius_{ kDieMilliCelsiusUnknown };  ///< The last die.

    /// When the value above was read, on the monotonic timer. 0 when there is
    /// no reading.
    std::atomic<std::uint64_t> lastDieStampUs_{ 0 };

    // Read by both tasks while a capture runs: atomics, so that neither reads
    // a value the other is writing (the console reports them, the loop reads
    // them per sample, and halt() closes a window under a loop that has not
    // acknowledged yet).
    std::atomic<Profile> profile_{ Profile::Trace };  ///< The live profile.
    std::atomic<Pace>    pace_{ Pace::Timer };        ///< The open window's pacing.
    std::atomic<Sink>    sink_{ Sink::Idle };         ///< The open window's sink.

    /// The pacing probe's handshake (requestPace()): the console sets the
    /// request and the period and gives startSem_; the task runs the probe,
    /// stores the result and gives probeDoneSem_.
    std::atomic<bool>          probeRequested_{ false };
    std::atomic<Pace>          probeResult_{ Pace::Timer };  ///< The probe's answer.
    std::atomic<std::uint32_t> probePeriodUs_{ 0 };          ///< The period it probes.

    /// Every profile's averaging override, by Profile: off until a person
    /// turns one on, at every boot (begin()) and after `*RST`. Console task
    /// only: the loop reads the window's own latched specification.
    std::array<Averaging, kProfileCount> averaging_ = {};

    /// The console's side of the start/done handshake: true from a start until
    /// the task's acknowledgement has been taken (halt()). Console task only.
    bool awaitingDone_ = false;

    /// The last window's totals, handed over by the task (LoopTotals).
    LoopTotals totals_ = {};

    /// The batch means of the open or last window: written by the sampler
    /// task while the window runs, read by the console only after the task's
    /// acknowledgement (halt()), as the ring is.
    std::array<std::int64_t, kBatchSlots> batchSums_    = {};
    std::uint32_t                         batchFill_    = 0;  ///< Complete batches.
    std::uint32_t                         batchTaken_   = 0;  ///< Samples in the open one.
    std::uint32_t                         batchSamples_ = 1;  ///< Samples per batch.

    /// The threshold the next captures count against (setThreshold()).
    bool         thresholdEnabled_ = false;
    std::int64_t thresholdNa_      = 0;  ///< Its level, nanoamps.
    std::int64_t hysteresisNa_     = 0;  ///< Its hysteresis, nanoamps.

    std::uint32_t channels_       = 0;  ///< The open stream's channel mask.
    std::uint64_t captureStartUs_ = 0;  ///< When the open window started.
    std::uint64_t nextDeadlineUs_ = 0;  ///< The timer's next deadline.
    std::uint32_t sampleLimit_    = 0;  ///< 0 = until ABORt.

    /// Refusals that carry numbers are formatted here, so that the console can
    /// queue one error without owning the arithmetic behind it.
    char errorText_[160] = {};

    std::int32_t*              ring_         = nullptr;  ///< The TRACE ring, from begin().
    std::uint32_t              ringCapacity_ = 0;        ///< Its samples.
    std::atomic<std::uint32_t> ringCount_{ 0 };          ///< TRACe:POINts? reads it mid-capture.

    /// The anchors, a circular array: anchorTotal_ counts every one written,
    /// forcedAnchors_ the forced ones among them this capture.
    Anchor      anchors_[kAnchorSlots] = {};
    std::size_t anchorTotal_           = 0;  ///< Anchors written.
    std::size_t forcedAnchors_         = 0;  ///< The forced ones this capture.

    /// Where the next ring sample goes: the ring runs circular under a level
    /// trigger, and ringAt() unrolls it from the oldest sample.
    std::uint32_t ringHead_ = 0;

    std::int64_t      triggerLevelNa_ = 0;      ///< The next capture's level, nanoamps.
    bool              triggerRising_  = true;   ///< Its slope.
    std::uint32_t     pretrigger_     = 0;      ///< Its pre-trigger samples.
    bool              triggerArmed_   = false;  ///< The open ring capture waits for the level.
    bool              triggerPrimed_  = false;  ///< The current was on the far side first.
    std::atomic<bool> triggerFired_{ false };   ///< The level was crossed.
    std::uint32_t     triggerIndex_  = 0;       ///< The crossing's sample index.
    std::uint64_t     triggerTimeUs_ = 0;       ///< Its timestamp.
    std::uint32_t     stopAt_        = 0;       ///< Samples a fired capture ends at.

    bool          thresholdMarks_       = false;  ///< Streams mark the crossings.
    std::uint32_t thresholdMarksQueued_ = 0;      ///< Crossing marks queued this capture.
    std::uint32_t captureLimit_         = 0;      ///< `SENSe:CAPTure:LIMit`'s count; 0 = off.

    /// Set by the sampling loop when a read failed, so the next sample
    /// recorded carries a drift anchor: the implicit timestamps have slipped by
    /// a period at that point and the host must see the discontinuity rather
    /// than interpolate across it. A capture forces kMaxForcedAnchors at
    /// most, so `read_err=` in `FETCh:STATistics?` is the authoritative
    /// count.
    bool forceAnchor_ = false;

    /// @brief Records a mark at the current sample index, unless @p limit
    /// marks are already recorded: addMark() and addHoldMark() differ only
    /// in it.
    ///
    /// @param[in]  label  The mark's label, truncated to kMarkLabelCapacity
    ///                    characters; may be null.
    /// @param[out] stored A copy of the mark as recorded.
    /// @param[in]  limit  The marks the capture may hold once this one is in.
    /// @retval true  The mark was recorded.
    /// @retval false @p limit marks are already recorded.
    bool recordMark( const char* label, Mark& stored, std::size_t limit ) noexcept;

    Mark        marks_[kMarkSlots] = {};  ///< The open or last capture's marks.
    std::size_t markCount_         = 0;   ///< How many.

    /// The window startZero() asked for, in seconds, so that finishZero() can
    /// report it whether the run completed or `ABORt` ended it early.
    std::uint32_t nullSeconds_ = 0;

    /// The opening boundary's CHARGE count, kept so that the closing
    /// statistics can report the pair without the console having to carry the
    /// header's number back.
    std::int64_t  captureChargeRawStart_   = 0;
    bool          captureChargeValidStart_ = false;  ///< That read succeeded.
    std::uint64_t captureChargeStartUs_    = 0;      ///< When that read finished.

    std::int64_t       captureOffsetPv_      = 0;  ///< The window's offset, picovolts.
    std::uint32_t      captureShuntMicroOhm_ = kNominalShuntMicroOhm;          ///< Its shunt.
    ina228::ShuntRange captureRange_         = ina228::ShuntRange::Wide163mV;  ///< Its range.
    ProfileSpec        windowSpec_        = {};  ///< What it converts with, latched at its start.
    std::int64_t       captureShuntLsbPv_ = ina228::kShuntLsbPvRange0;  ///< Its VSHUNT LSB.
};

/// @}

}  // namespace kleidos::meter

#endif  // KLEIDOS_METER_SAMPLER_H
