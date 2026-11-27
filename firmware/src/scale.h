// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file scale.h
 * @ingroup sampling
 * @brief The conversion arithmetic: integer, exact, no floating point.
 *
 * The accumulation path carries no floating point. Floats appear only as the
 * `v=<float>` token a host parser reads, and even there the digits are produced
 * by integer division rather than by a float formatter, so a printed value is
 * exactly the integer the device computed
 * (`docs/architecture.md` SS 3.6).
 *
 * Units are **picovolts**, **nanoamps**, **picowatts** and **nano-degrees**,
 * chosen so that every datasheet LSB is an exact integer (see ina228_regs.h).
 *
 * Nothing in this header touches ESP-IDF or the device, which is deliberate:
 * these are the functions worth a host unit test.
 */
#ifndef KLEIDOS_METER_SCALE_H
#define KLEIDOS_METER_SCALE_H

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::scale {

/// @addtogroup sampling
/// @{

/// One microvolt in picovolts.
constexpr std::int64_t kPicoPerMicro = 1000000;

/// One nanoamp in picoamps; also one microvolt in picovolts' thousandths.
constexpr std::int64_t kThousand = 1000;

/// One watt's worth of (microvolt x nanoamp) per microwatt: uV x nA is
/// 1e-6 x 1e-9 = 1e-15 W, and a microwatt is 1e-6 W.
constexpr std::int64_t kMicrovoltNanoampPerMicrowatt = 1000000000;

/// The sign bit of a 20-bit two's-complement value.
constexpr std::uint32_t kSign20 = 0x80000U;

/// The 20 bits of a 20-bit value.
constexpr std::uint32_t kMask20 = 0xFFFFFU;

/// The sign bit of a 40-bit two's-complement value.
constexpr std::uint64_t kSign40 = 0x80'0000'0000ULL;

/// The 40 bits of a 40-bit value.
constexpr std::uint64_t kMask40 = 0xFF'FFFF'FFFFULL;

/// @brief Decodes a 24-bit register carrying a 20-bit signed value in bits
/// 23-4.
///
/// VSHUNT, VBUS and CURRENT all have this shape; POWER does not -- it uses all
/// 24 bits and has no reserved nibble ([DS] SS 7.6.1.9), which is the asymmetry
/// that bites. Range of the result: [-524288, 524287].
///
/// The sign is extended in unsigned arithmetic (MISRA C++:2023 7.0.4: no
/// shift or bitwise operator on a signed operand): flipping the sign bit maps
/// the 20-bit two's-complement range onto [0, 2^20) in order, so subtracting
/// the sign bit's weight gives the signed value, and the bits above 23 are
/// masked off, as the shift out of a 32-bit word did before.
///
/// @param[in] raw24 The register's 24 bits, right-aligned.
/// @return The sign-extended 20-bit value, in counts.
constexpr std::int32_t decode20( std::uint32_t raw24 ) noexcept {
    const std::uint32_t value20 = ( raw24 >> 4U ) & kMask20;
    return static_cast<std::int32_t>( value20 ^ kSign20 ) - static_cast<std::int32_t>( kSign20 );
}

/// @brief Decodes a 40-bit two's-complement CHARGE value ([DS] SS 7.6.1.11).
///
/// Range: +/-2^39. The sign is extended as decode20() extends it, in unsigned
/// arithmetic, and the bits above 39 are masked off.
///
/// @param[in] raw40 The register's 40 bits, right-aligned.
/// @return The sign-extended value, in counts.
constexpr std::int64_t decode40( std::uint64_t raw40 ) noexcept {
    const std::uint64_t value40 = raw40 & kMask40;
    return static_cast<std::int64_t>( value40 ^ kSign40 ) - static_cast<std::int64_t>( kSign40 );
}

/// @brief Decodes the 16-bit two's-complement DIETEMP register
/// ([DS] SS 7.6.1.7).
///
/// @param[in] raw16 The register's 16 bits.
/// @return The signed value, in counts.
constexpr std::int16_t decode16Signed( std::uint16_t raw16 ) noexcept {
    return static_cast<std::int16_t>( raw16 );
}

/// @brief Shunt voltage in picovolts from raw VSHUNT counts.
///
/// @param[in] counts     Raw VSHUNT counts, as decode20() returns them.
/// @param[in] shuntLsbPv The VSHUNT LSB of the live range, in picovolts.
/// @return The shunt voltage, in picovolts.
constexpr std::int64_t shuntPicovolts( std::int32_t counts, std::int64_t shuntLsbPv ) noexcept {
    return static_cast<std::int64_t>( counts ) * shuntLsbPv;
}

/// @brief Bus voltage in picovolts from raw VBUS counts.
///
/// "Two's complement value, however always positive" ([DS] SS 7.6.1.6).
///
/// @param[in] counts   Raw VBUS counts, as decode20() returns them.
/// @param[in] busLsbPv The VBUS LSB, in picovolts.
/// @return The bus voltage, in picovolts.
constexpr std::int64_t busPicovolts( std::int32_t counts, std::int64_t busLsbPv ) noexcept {
    return static_cast<std::int64_t>( counts ) * busLsbPv;
}

/// @brief Die temperature in milli-degrees Celsius from the raw DIETEMP
/// register.
///
/// 7.8125 m C/LSB is exactly 125/16 m C, so this is exact integer arithmetic
/// ([DS] SS 7.6.1.7).
///
/// @param[in] raw The DIETEMP value, as decode16Signed() returns it.
/// @return The die temperature, in milli-degrees Celsius.
constexpr std::int32_t dieMilliCelsius( std::int16_t raw ) noexcept {
    return static_cast<std::int32_t>( static_cast<std::int32_t>( raw ) * 125 / 16 );
}

/// @brief Current in nanoamps from the offset-corrected shunt voltage.
///
/// `I = V / R`. With V in picovolts (1e-12 V) and R in micro-ohms (1e-6 Ohm),
/// `I = pv / uohm` microamps, so nanoamps is one multiplication by 1000 and one
/// division. The arithmetic is self-checking: at the nominal 100000 uohm, one
/// count of ADCRANGE = 0 gives `312500 x 1000 / 100000 = 3125 nA`, which is
/// exactly `CURRENT_LSB`.
///
/// The measured shunt resistance is corrected here rather than in `SHUNT_CAL`,
/// because VSHUNT is the primary quantity and `SHUNT_CAL` does not touch it at
/// all (`docs/architecture.md` SS 3.1).
///
/// @param[in] shuntPv       Raw shunt voltage in picovolts.
/// @param[in] offsetPv      The stored zero-current null for the live range.
/// @param[in] shuntMicroOhm The measured shunt resistance; never 0.
/// @return The current, in nanoamps; 0 if @p shuntMicroOhm is 0.
constexpr std::int64_t currentNanoamps( std::int64_t shuntPv, std::int64_t offsetPv,
                                        std::uint32_t shuntMicroOhm ) noexcept {
    if ( shuntMicroOhm == 0U ) {
        return 0;
    }
    return ( shuntPv - offsetPv ) * kThousand / static_cast<std::int64_t>( shuntMicroOhm );
}

/// @brief The voltage the DUT actually sees, in picovolts.
///
/// The burden voltage is free: `I x R_shunt` is, by construction, the
/// offset-corrected shunt voltage itself, so the DUT's voltage is the bus
/// voltage minus it -- **provided VBUS is wired upstream of the shunt**, which
/// it must be anyway, because the VBUS pin's 1 MOhm input impedance
/// ([DS] SS 6.5 `ZVBUS`) would otherwise put 3.8 uA at 3.8 V through the shunt
/// and spoil a 10 uA reading by 38 %. The capture header records
/// `vbus_side=` so a reader knows which wiring produced the number.
///
/// @param[in] busPv            The bus voltage, in picovolts.
/// @param[in] correctedShuntPv The offset-corrected shunt voltage, in
///                             picovolts.
/// @return The DUT's voltage, in picovolts.
constexpr std::int64_t dutPicovolts( std::int64_t busPv, std::int64_t correctedShuntPv ) noexcept {
    return busPv - correctedShuntPv;
}

/// @brief Power delivered to the DUT, in microwatts.
///
/// The bus voltage is reduced to microvolts **first**: `v_pv x i_na` would
/// reach 3.8e20 at 3.8 V and 100 mA and overflow a signed 64-bit integer
/// (max 9.2e18). Reducing first keeps the product at 3.8e14.
///
/// @param[in] dutPv     The DUT's voltage, in picovolts.
/// @param[in] currentNa The current, in nanoamps.
/// @return The power, in microwatts.
constexpr std::int64_t powerMicrowatts( std::int64_t dutPv, std::int64_t currentNa ) noexcept {
    const std::int64_t dutMicrovolts = dutPv / kPicoPerMicro;
    return dutMicrovolts * currentNa / kMicrovoltNanoampPerMicrowatt;
}

/// @brief Charge in nanocoulombs from raw CHARGE counts, at the **nominal**
/// shunt.
///
/// This is the one quantity the firmware does not shunt-correct. A full-scale
/// 40-bit count is 5.5e11; in nanocoulombs that is 1.7e15, and applying
/// `x 100000 / shunt_uohm` on top of it overflows int64. Doing the division
/// first throws away precision, and a 128-bit intermediate is a lot of
/// machinery for a register the firmware keeps only as a probe. So the host applies
/// `k = 100000 / shunt_uohm`, with `shunt_uohm` carried in the header
/// (`docs/architecture.md` SS 3.6, SS 11.1).
///
/// **The multiplication comes first.** Dividing the LSB first is exact in
/// range 0 (3125000/1000 = 3125 nC) and not in range 1, where 781250/1000
/// truncates to 781 instead of 781.25 -- a systematic -0.032 % gain error on
/// every range-1 charge figure. Multiplying first costs no headroom: the worst
/// case is `2^39 x 3125000 = 1.72e18`, inside int64's 9.22e18.
///
/// @param[in] counts      Raw CHARGE counts, as decode40() returns them.
/// @param[in] chargeLsbPc The CHARGE LSB of the live range, in picocoulombs.
/// @return The charge at the nominal shunt, in nanocoulombs.
constexpr std::int64_t chargeNanocoulombs( std::int64_t counts,
                                           std::int64_t chargeLsbPc ) noexcept {
    return counts * chargeLsbPc / kThousand;
}

/// @brief Integer square root of @p value, truncated.
///
/// Used for the standard deviation a zero-offset null reports, so that even
/// that number costs no float.
///
/// @param[in] value The radicand.
/// @return The square root of @p value, truncated to an integer.
constexpr std::uint64_t integerSqrt( std::uint64_t value ) noexcept {
    if ( value < 2U ) {
        return value;
    }
    // Newton's method from a power-of-two seed; converges in a handful of
    // iterations and is exact for the truncated root.
    // The first step is (value + value / value) / 2, written so that it cannot
    // wrap: `( value + 1 ) / 2` overflows to 0 at UINT64_MAX and the next
    // division would be by zero (found by the host tests' sanitizer).
    std::uint64_t guess = value;
    std::uint64_t next  = guess / 2U + ( guess & 1U );
    while ( next < guess ) {
        guess = next;
        next  = ( guess + value / guess ) / 2U;
    }
    return guess;
}

/// @}

}  // namespace kleidos::meter::scale

#endif  // KLEIDOS_METER_SCALE_H
