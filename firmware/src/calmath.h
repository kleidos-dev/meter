// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file calmath.h
 * @ingroup calibration_store
 * @brief The shunt calibration's arithmetic: the shunt's resistance from a
 *        known reference resistor in series with it, and the limits a result
 *        must meet before it may replace the nominal value.
 *
 * The method (`calibration/README.md`, "The shunt, two points"): the DUT is
 * replaced by a reference resistor of known value (the VPG Y0785100R000T9L,
 * 100 Ohm +/-0.01 %, `hardware/bom.md`), so one current crosses both. The
 * INA228 reads that current's drop across the shunt on its shunt channel and,
 * with the `VBUS` lead moved to the reference's top lead, the drop across the
 * reference on its bus channel. The same current makes both, so
 *
 *     R_shunt = V_shunt x R_ref / V_ref
 *
 * and neither the cell's voltage nor its drift enters. The two points are the
 * two ADC ranges at that one current: each range has its own gain error, and
 * two figures that agree are one resistance seen twice rather than one range's
 * error seen once. The instrument gives each point with its verdict
 * (`CALibration:SHUNt:RESult?`); whether the two ranges agree, and the mean
 * that is stored, are judged on the host (`kleidos-meter calibrate`, the
 * configuration's `range_split_max_ppm`).
 *
 * Integer only, like every value on the wire: the result is carried in
 * nano-ohms (0.01 ppm of the 0.100 Ohm part) and rounded to the micro-ohms
 * `SENSe:CURRent:SHUNt` stores only at the end. Nothing here touches ESP-IDF,
 * so every branch is a host unit test.
 */
#ifndef KLEIDOS_METER_CALMATH_H
#define KLEIDOS_METER_CALMATH_H

#include <cstdint>

namespace kleidos::meter::calmath {

/// @addtogroup calibration_store
/// @{

/// @brief The reference resistor `CALibration:SHUNt:REFerence` starts at: the
///        VPG foil part, 100.000000 Ohm, in micro-ohms.
constexpr std::uint32_t kDefaultReferenceMicroOhm = 100'000'000;

/// @brief The least reference value accepted, in micro-ohms: 1 Ohm.
///
/// The reference values accepted are 1 Ohm to 4 kOhm. Below 1 Ohm the current
/// a 4.2 V cell drives through it is past the INA228's range on a 0.100 Ohm
/// shunt; above 4 kOhm it is too small for the shunt channel to resolve to the
/// agreement limit.
constexpr std::uint32_t kMinReferenceMicroOhm = 1'000'000;
/// @brief The greatest reference value accepted, in micro-ohms: 4 kOhm (see
///        kMinReferenceMicroOhm).
constexpr std::uint32_t kMaxReferenceMicroOhm = 4'000'000'000;

/// @brief The nominal shunt, in nano-ohms: `R100`, 0.100 Ohm.
constexpr std::int64_t kNominalShuntNanoOhm = 100'000'000;

/// @brief The window a measured shunt must fall in, in parts per million of
///        nominal: +/-2 %.
///
/// The part is a 1 % resistor by its marking and its tag
/// (`calibration/records/`); twice that is the most a sound reading can sit
/// from nominal, and anything further is the wiring -- the `VBUS` lead not on
/// the reference, a contact in series with it, a reference of another value.
constexpr std::int64_t kShuntWindowPpm = 20'000;

/// @brief The least drop across the reference that is used, in microvolts:
///        1 V.
///
/// Below it the bus channel's 195.3125 uV LSB is more than 0.02 % of a reading.
constexpr std::int64_t kMinReferenceMicrovolts = 1'000'000;

/// @brief The least drop across the shunt that is used, in nanovolts: 1 mV,
///        10 mA through 0.100 Ohm.
///
/// Below it the offset null's own uncertainty (a few
/// tenths of a microvolt on this rig) is more than 0.05 % of the reading.
constexpr std::int64_t kMinShuntNanovolts = 1'000'000;

/// @brief What a measured point says.
enum class Verdict : std::uint8_t {
    Ok,            ///< Within every limit: the figure may be used.
    NoSamples,     ///< The acquisition converted no bus voltage.
    Reversed,      ///< A negative drop: the reference or the shunt is wired backwards.
    ReferenceLow,  ///< Under kMinReferenceMicrovolts across the reference.
    SignalLow,     ///< Under kMinShuntNanovolts across the shunt.
    OutOfWindow,   ///< Outside nominal +/-kShuntWindowPpm.
};

/// @brief The verdict's spelling on the wire.
/// @param[in] verdict The verdict to spell.
/// @return Its lower-case name (`ok`, `no_samples`, `reversed`,
///         `reference_low`, `signal_low`, `out_of_window`), a static string;
///         `no_samples` for a value outside the enumeration.
constexpr const char* verdictName( Verdict verdict ) noexcept {
    switch ( verdict ) {
        case Verdict::Ok:
            return "ok";
        case Verdict::NoSamples:
            return "no_samples";
        case Verdict::Reversed:
            return "reversed";
        case Verdict::ReferenceLow:
            return "reference_low";
        case Verdict::SignalLow:
            return "signal_low";
        case Verdict::OutOfWindow:
            return "out_of_window";
    }
    return "no_samples";
}

/// @brief One point: the shunt's resistance at one range, and how it was reached.
struct ShuntPoint {
    std::int64_t nanoOhm      = 0;                   ///< The shunt's resistance.
    std::int64_t currentNa    = 0;                   ///< The current the reference says flowed.
    std::int64_t deviationPpm = 0;                   ///< From nominal, signed.
    Verdict      verdict      = Verdict::NoSamples;  ///< Whether the figure may be used.
};

/// @brief Rounds a non-negative @p numerator / @p denominator half up.
/// @param[in] numerator   The dividend; must not be negative.
/// @param[in] denominator The divisor; must be positive.
/// @return The quotient rounded half up; 0 when the denominator is not
///         positive or the numerator is negative.
constexpr std::int64_t divideRounded( std::int64_t numerator, std::int64_t denominator ) noexcept {
    if ( denominator <= 0 || numerator < 0 ) {
        return 0;
    }
    return ( numerator + denominator / 2 ) / denominator;
}

/// @brief The shunt's resistance from one acquisition.
///
/// Overflow: the product is at most 163.84 mV (range 0 full scale, 1.6384e8
/// nV) times 4 kOhm (4e9 uOhm), 6.6e17, inside int64's 9.2e18.
///
/// @param[in] shuntNanovolts      The mean offset-corrected drop across the
///                                shunt.
/// @param[in] referenceMicrovolts The mean drop across the reference (the bus
///                                channel, its lead moved to the reference).
/// @param[in] samples             Bus-converted samples the means are over.
/// @param[in] referenceMicroOhm   The reference's value.
/// @return The point: its resistance, current and deviation from nominal when
///         the inputs pass every check, and the Verdict that says which check
///         failed otherwise.
constexpr ShuntPoint shuntFromReference( std::int64_t shuntNanovolts,
                                         std::int64_t referenceMicrovolts, std::uint32_t samples,
                                         std::uint32_t referenceMicroOhm ) noexcept {
    ShuntPoint point;
    if ( samples == 0U || referenceMicroOhm == 0U ) {
        point.verdict = Verdict::NoSamples;
        return point;
    }
    if ( shuntNanovolts < 0 || referenceMicrovolts < 0 ) {
        point.verdict = Verdict::Reversed;
        return point;
    }
    if ( referenceMicrovolts < kMinReferenceMicrovolts ) {
        point.verdict = Verdict::ReferenceLow;
        return point;
    }
    if ( shuntNanovolts < kMinShuntNanovolts ) {
        point.verdict = Verdict::SignalLow;
        return point;
    }
    // (1e-9 V)(1e-6 Ohm) / (1e-6 V) = 1e-9 Ohm: the quotient is in nano-ohms.
    point.nanoOhm = divideRounded( shuntNanovolts * static_cast<std::int64_t>( referenceMicroOhm ),
                                   referenceMicrovolts );
    // uV / uOhm = A; x 1e9 is nanoamps. 85 V (the bus channel's limit) x 1e9
    // is 8.5e16, inside int64.
    point.currentNa = divideRounded( referenceMicrovolts * 1'000'000'000,
                                     static_cast<std::int64_t>( referenceMicroOhm ) );
    point.deviationPpm =
        ( point.nanoOhm - kNominalShuntNanoOhm ) * 1'000'000 / kNominalShuntNanoOhm;
    const std::int64_t spread =
        ( point.deviationPpm < 0 ) ? -point.deviationPpm : point.deviationPpm;
    point.verdict = ( spread > kShuntWindowPpm ) ? Verdict::OutOfWindow : Verdict::Ok;
    return point;
}

// --- The arithmetic, checked where it is defined ----------------------------

/// @brief Fixtures for the compile-time checks below.
namespace detail {

/// 41.234 mA through the 0.100 Ohm nominal and the 100 Ohm reference.
constexpr ShuntPoint kNominal =
    shuntFromReference( 4'123'400, 4'123'400, 1000, kDefaultReferenceMicroOhm );

}  // namespace detail

static_assert( detail::kNominal.nanoOhm == kNominalShuntNanoOhm
                   && detail::kNominal.verdict == Verdict::Ok,
               "A shunt dropping a thousandth of the reference's voltage is 0.100 Ohm" );
static_assert( detail::kNominal.currentNa == 41'234'000, "4.1234 V over 100 Ohm is 41.234 mA" );
static_assert( static_cast<std::uint64_t>( kMaxReferenceMicroOhm ) * 163'840'000ULL
                   < 9'223'372'036'854'775'807ULL,
               "The worst product, range 0 full scale times the largest reference, fits int64" );

/// @}

}  // namespace kleidos::meter::calmath

#endif  // KLEIDOS_METER_CALMATH_H
