// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file text.cpp
 * @ingroup text_output
 * @brief The bounded writer: every append on `std::to_chars` and one copy.
 *
 * Each number is converted into a local array as wide as the widest value
 * of its type, which `std::to_chars` therefore always fills without running
 * out of room (the static_asserts below), and then appended as text, so the
 * one place that copies into the caller's buffer and decides what fits is
 * `Writer::text()`.
 */
#include "text.h"

#include "chars.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <string_view>

namespace kleidos::meter::text {

namespace {

/// @brief Room for any 64-bit integer in decimal or hexadecimal: 20 decimal
///        digits and a sign, or 16 hexadecimal digits.
constexpr std::size_t kNumberCapacity = 24U;

static_assert( kNumberCapacity
                   > static_cast<std::size_t>( std::numeric_limits<std::uint64_t>::digits10 ) + 1U,
               "every decimal digit of a std::uint64_t fits" );
static_assert( kNumberCapacity
                   > static_cast<std::size_t>( std::numeric_limits<std::int64_t>::digits10 ) + 2U,
               "every decimal digit of a std::int64_t and its sign fit" );
static_assert( kNumberCapacity
                   >= static_cast<std::size_t>( std::numeric_limits<std::uint64_t>::digits ) / 4U,
               "every hexadecimal digit of a std::uint64_t fits" );

/// @brief The digits of one number, as `std::to_chars` wrote them.
using Digits = std::array<char, kNumberCapacity>;

/// @brief The powers of ten up to 10^kMaxDecimals, the units `scaled()`
///        divides by.
constexpr std::array<std::uint64_t, kMaxDecimals + 1U> kPow10 = {
    1ULL,
    10ULL,
    100ULL,
    1'000ULL,
    10'000ULL,
    100'000ULL,
    1'000'000ULL,
    10'000'000ULL,
    100'000'000ULL,
    1'000'000'000ULL,
    10'000'000'000ULL,
    100'000'000'000ULL,
    1'000'000'000'000ULL,
    10'000'000'000'000ULL,
    100'000'000'000'000ULL,
    1'000'000'000'000'000ULL,
    10'000'000'000'000'000ULL,
    100'000'000'000'000'000ULL,
    1'000'000'000'000'000'000ULL,
};

/// @brief The zeros a padded number takes its padding from: as many as the
///        widest number has digits, so no padding a caller asks for is cut.
constexpr std::string_view kZeros = "000000000000000000000000";
static_assert( kZeros.size() == kNumberCapacity, "a zero for every digit a number can have" );

/// @brief The decimals `thousandths()` writes.
constexpr std::size_t kThousandthsDecimals = 3U;

/// @brief The base of the decimal digits.
constexpr std::uint64_t kDecimalBase = 10U;

/// @brief The base of the hexadecimal digits.
constexpr std::int32_t kHexBase = 16;

/// @brief The text `std::to_chars` wrote into @p digits, ending at @p end.
/// @param[in] digits The array it wrote into, from its first element.
/// @param[in] end    The `ptr` of its result.
/// @return The digits, as a view of @p digits.
std::string_view written( const Digits& digits, const char* end ) noexcept {
    return { digits.data(), static_cast<std::size_t>( end - digits.data() ) };
}

/// @brief The zeros a number of @p number's digits needs ahead of it to
///        be @p width digits wide.
/// @param[in] number The number's digits.
/// @param[in] width  The fewest digits wanted, at most kNumberCapacity.
/// @return The zeros, none when @p number is that wide already.
std::string_view zerosBefore( std::string_view number, std::size_t width ) noexcept {
    return kZeros.substr( 0U, ( width > number.size() ) ? ( width - number.size() ) : 0U );
}

/// @brief The sign written ahead of @p value.
/// @param[in] value The value.
/// @return `-` for a negative value, else nothing.
constexpr std::string_view signOf( std::int64_t value ) noexcept {
    return ( value < 0 ) ? "-" : "";
}

/// @brief The magnitude of @p value, `INT64_MIN`'s included, as unsigned.
/// @param[in] value The value.
/// @return |@p value|.
constexpr std::uint64_t magnitudeOf( std::int64_t value ) noexcept {
    return ( value < 0 ) ? ( ~static_cast<std::uint64_t>( value ) + 1U )
                         : static_cast<std::uint64_t>( value );
}

}  // namespace

Writer::Writer( std::span<char> out ) noexcept : out_( out ) {
    if ( !out_.empty() ) {
        out_[0] = '\0';
    }
}

std::size_t Writer::room() const noexcept {
    return out_.empty() ? 0U : ( out_.size() - 1U - used_ );
}

void Writer::text( std::string_view piece ) noexcept {
    const std::size_t taken = std::min( piece.size(), room() );
    if ( taken < piece.size() ) {
        truncated_ = true;
    }
    if ( taken > 0U ) {
        const std::span<char> rest = out_.subspan( used_ );
        (void)std::copy_n( piece.begin(), taken, rest.begin() );
        used_ += taken;
        out_[used_] = '\0';
    }
}

void Writer::character( char c ) noexcept {
    text( std::string_view( &c, 1U ) );
}

void Writer::unsignedDecimal( std::uint64_t value ) noexcept {
    Digits                     digits = {};
    const std::to_chars_result result =
        std::to_chars( digits.data(), digits.data() + digits.size(), value );
    text( written( digits, result.ptr ) );
}

void Writer::signedDecimal( std::int64_t value ) noexcept {
    Digits                     digits = {};
    const std::to_chars_result result =
        std::to_chars( digits.data(), digits.data() + digits.size(), value );
    text( written( digits, result.ptr ) );
}

void Writer::zeroPadded( std::uint64_t value, std::size_t width ) noexcept {
    Digits                     digits = {};
    const std::to_chars_result result =
        std::to_chars( digits.data(), digits.data() + digits.size(), value );
    const std::string_view number = written( digits, result.ptr );
    text( zerosBefore( number, width ) );
    text( number );
}

void Writer::hex( std::uint64_t value, std::size_t width, LetterCase letterCase ) noexcept {
    Digits                     digits = {};
    const std::to_chars_result result =
        std::to_chars( digits.data(), digits.data() + digits.size(), value, kHexBase );
    const std::string_view number = written( digits, result.ptr );
    if ( letterCase == LetterCase::Upper ) {
        for ( char& digit : std::span<char>( digits.data(), number.size() ) ) {
            digit = chars::toUpper( digit );
        }
    }
    text( zerosBefore( number, width ) );
    text( number );
}

void Writer::scaled( std::int64_t value, std::uint32_t decimals ) noexcept {
    const std::uint32_t places    = std::min( decimals, kMaxDecimals );
    const std::uint64_t magnitude = magnitudeOf( value );
    const std::uint64_t unit      = kPow10[places];
    std::uint64_t       fraction  = magnitude % unit;

    // Trailing zeros of the fraction go, down to one digit; with no decimals
    // the fraction is 0 and nothing is kept.
    std::uint32_t kept = places;
    while ( ( kept > 1U ) && ( ( fraction % kDecimalBase ) == 0U ) ) {
        fraction /= kDecimalBase;
        --kept;
    }
    text( signOf( value ) );
    unsignedDecimal( magnitude / unit );
    if ( places != 0U ) {
        character( '.' );
        zeroPadded( fraction, kept );
    }
}

void Writer::thousandths( std::int64_t value ) noexcept {
    const std::uint64_t magnitude = magnitudeOf( value );
    const std::uint64_t unit      = kPow10[kThousandthsDecimals];
    text( signOf( value ) );
    unsignedDecimal( magnitude / unit );
    character( '.' );
    zeroPadded( magnitude % unit, kThousandthsDecimals );
}

void Writer::key( std::string_view name ) noexcept {
    if ( used_ != 0U ) {
        character( ' ' );
    }
    text( name );
    character( '=' );
}

void Writer::field( std::string_view name, std::uint64_t value ) noexcept {
    key( name );
    unsignedDecimal( value );
}

void Writer::field( std::string_view name, std::string_view value ) noexcept {
    key( name );
    text( value );
}

std::size_t Writer::size() const noexcept {
    return used_;
}

std::string_view Writer::view() const noexcept {
    return { out_.data(), used_ };
}

bool Writer::truncated() const noexcept {
    return truncated_;
}

}  // namespace kleidos::meter::text
