// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file wide.h
 * @ingroup sampling
 * @brief 128-bit unsigned arithmetic for the statistics a long capture
 *        accumulates.
 *
 * The device is a 32-bit core and its compiler has no `__int128`, yet a
 * capture of hours at TRACE's rate sums more squared deviations than 64 bits
 * hold: a deviation of 2^20 counts squares to 2^40, and 2^24 such samples --
 * 78 minutes of TRACE -- fill a `std::uint64_t`. So the sum of squares, and
 * the few products and quotients the statistics need once a capture closes,
 * are carried in two 64-bit words. The per-sample cost is one addition with a
 * carry; the division runs once per capture.
 */
#ifndef KLEIDOS_METER_WIDE_H
#define KLEIDOS_METER_WIDE_H

#include <cstdint>

namespace kleidos::meter::wide {

/// @addtogroup sampling
/// @{

/// An unsigned 128-bit integer, as two 64-bit words.
struct U128 {
    std::uint64_t hi = 0;  ///< The upper 64 bits.
    std::uint64_t lo = 0;  ///< The lower 64 bits.
};

/// @brief Adds a 64-bit value to a 128-bit one.
///
/// @param[in] a The 128-bit addend.
/// @param[in] b The 64-bit addend.
/// @return `a + b`, modulo 2^128.
constexpr U128 add( U128 a, std::uint64_t b ) noexcept {
    U128 sum{ a.hi, a.lo + b };
    if ( sum.lo < a.lo ) {
        ++sum.hi;
    }
    return sum;
}

/// @brief The full 128-bit product of two 64-bit values.
///
/// @param[in] a A factor.
/// @param[in] b The other factor.
/// @return `a x b`, exactly.
constexpr U128 multiply( std::uint64_t a, std::uint64_t b ) noexcept {
    constexpr std::uint64_t kLowMask = 0xFFFFFFFFULL;
    const std::uint64_t     aLow     = a & kLowMask;
    const std::uint64_t     aHigh    = a >> 32U;
    const std::uint64_t     bLow     = b & kLowMask;
    const std::uint64_t     bHigh    = b >> 32U;

    const std::uint64_t lowLow   = aLow * bLow;
    const std::uint64_t lowHigh  = aLow * bHigh;
    const std::uint64_t highLow  = aHigh * bLow;
    const std::uint64_t highHigh = aHigh * bHigh;

    // The middle column: each partial product's low half, and the carry of
    // the low column, each under 2^32, so their sum fits in 64 bits.
    const std::uint64_t middle =
        ( lowLow >> 32U ) + ( lowHigh & kLowMask ) + ( highLow & kLowMask );
    U128 product;
    product.lo = ( middle << 32U ) | ( lowLow & kLowMask );
    product.hi = highHigh + ( lowHigh >> 32U ) + ( highLow >> 32U ) + ( middle >> 32U );
    return product;
}

/// @brief A 128-bit value times a 64-bit one, modulo 2^128.
///
/// @param[in] a The 128-bit factor.
/// @param[in] b The 64-bit factor.
/// @return `a x b`; exact while it fits 128 bits.
constexpr U128 multiply( U128 a, std::uint64_t b ) noexcept {
    U128 product = multiply( a.lo, b );
    product.hi += a.hi * b;
    return product;
}

/// @brief Whether @p a is less than @p b.
///
/// @param[in] a The left operand.
/// @param[in] b The right operand.
/// @return `a < b`.
constexpr bool less( U128 a, U128 b ) noexcept {
    return a.hi < b.hi || ( a.hi == b.hi && a.lo < b.lo );
}

/// @brief Subtracts @p b from @p a, clamped at zero.
///
/// @param[in] a The minuend.
/// @param[in] b The subtrahend.
/// @return `a - b`, or 0 when @p b is larger.
constexpr U128 subtractClamped( U128 a, U128 b ) noexcept {
    if ( less( a, b ) ) {
        return U128{};
    }
    U128 difference{ a.hi - b.hi, a.lo - b.lo };
    if ( a.lo < b.lo ) {
        --difference.hi;
    }
    return difference;
}

/// @brief Divides a 128-bit value by a 64-bit one, truncating.
///
/// Shift-and-subtract, one bit of quotient per step: 64 steps for the low
/// word, after the upper word is divided in one.
///
/// @param[in] numerator The dividend.
/// @param[in] divisor   The divisor; 0 yields 0.
/// @return `numerator / divisor`, truncated.
constexpr U128 divide( U128 numerator, std::uint64_t divisor ) noexcept {
    if ( divisor == 0U ) {
        return U128{};
    }
    U128          quotient{ numerator.hi / divisor, 0 };
    std::uint64_t remainder = numerator.hi % divisor;
    for ( std::uint32_t step = 0; step < 64U; ++step ) {
        // The bits of the low word from the top down. The remainder is below
        // the divisor, so twice it plus one bit is below twice the divisor:
        // one carry out of 64 bits at most, and one subtraction brings it
        // back under the divisor.
        const std::uint32_t bit   = 63U - step;
        const bool          carry = ( remainder >> 63U ) != 0U;
        remainder                 = ( remainder << 1U ) | ( ( numerator.lo >> bit ) & 1U );
        if ( carry || remainder >= divisor ) {
            remainder -= divisor;
            quotient.lo |= 1ULL << bit;
        }
    }
    return quotient;
}

/// @brief `a x b / d` for a signed @p a, truncated toward zero, without an
/// intermediate overflow.
///
/// Saturates at the int64 limits when the quotient does not fit.
///
/// @param[in] a The signed factor.
/// @param[in] b The unsigned factor.
/// @param[in] d The divisor; 0 yields 0.
/// @return `a x b / d`.
constexpr std::int64_t mulDiv( std::int64_t a, std::uint64_t b, std::uint64_t d ) noexcept {
    const bool          negative = a < 0;
    const std::uint64_t magnitude =
        negative ? ( 0ULL - static_cast<std::uint64_t>( a ) ) : static_cast<std::uint64_t>( a );
    const U128     quotient = divide( multiply( magnitude, b ), d );
    constexpr auto kLimit   = static_cast<std::uint64_t>( INT64_MAX );
    if ( quotient.hi != 0U || quotient.lo > kLimit ) {
        return negative ? INT64_MIN : INT64_MAX;
    }
    const auto value = static_cast<std::int64_t>( quotient.lo );
    return negative ? -value : value;
}

/// @}

}  // namespace kleidos::meter::wide

#endif  // KLEIDOS_METER_WIDE_H
