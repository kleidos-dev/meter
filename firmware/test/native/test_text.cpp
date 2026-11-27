// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_text.cpp
 * @brief The bounded writer of text.h: every append, against the formats the
 *        firmware wrote with `std::snprintf` before it, and every way a
 *        buffer is cut short.
 */
#include "text.h"
#include "unit.h"

#include <array>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <span>
#include <string>

using kleidos::meter::text::kMaxDecimals;
using kleidos::meter::text::LetterCase;
using kleidos::meter::text::Writer;

namespace {

/// What a writer over @p capacity bytes holds after @p write.
template <typename Write>
std::string written( std::size_t capacity, Write write ) {
    std::array<char, 64> buffer = {};
    buffer.fill( 'x' );
    Writer w( std::span<char>( buffer.data(), capacity ) );
    write( w );
    return std::string( buffer.data(), w.size() );
}

/// The text `std::snprintf()` gave for a scaled value before the writer:
/// scpi::formatScaled()'s format, `%s%llu.%0*llu` with the fraction's trailing
/// zeros removed down to one digit, or `%s%llu` with no decimals.
std::string reference( std::int64_t value, std::uint32_t decimals ) {
    std::uint64_t unit = 1;
    for ( std::uint32_t i = 0; i < decimals; ++i ) {
        unit *= 10U;
    }
    const bool          negative  = value < 0;
    const std::uint64_t magnitude = negative ? ( ~static_cast<std::uint64_t>( value ) + 1U )
                                             : static_cast<std::uint64_t>( value );
    std::uint64_t       fraction  = magnitude % unit;
    int                 kept      = static_cast<int>( decimals );
    while ( kept > 1 && fraction % 10U == 0U ) {
        fraction /= 10U;
        --kept;
    }
    char out[64] = {};
    if ( decimals == 0U ) {
        std::snprintf( out, sizeof( out ), "%s%llu", negative ? "-" : "",
                       static_cast<unsigned long long>( magnitude / unit ) );
    } else {
        std::snprintf( out, sizeof( out ), "%s%llu.%0*llu", negative ? "-" : "",
                       static_cast<unsigned long long>( magnitude / unit ), kept,
                       static_cast<unsigned long long>( fraction ) );
    }
    return out;
}

}  // namespace

TEST( "text", "a writer starts an empty string, and never touches an empty buffer" ) {
    std::array<char, 4> buffer = { 'a', 'b', 'c', 'd' };
    {
        Writer w( std::span<char>( buffer.data(), 0 ) );
        CHECK( w.size() == 0 );
        CHECK( !w.truncated() );
        w.text( "" );
        CHECK( !w.truncated() );
        w.text( "z" );
        CHECK( w.truncated() );
        w.signedDecimal( -1 );
        CHECK( w.size() == 0 );
    }
    CHECK( buffer[0] == 'a' );
    {
        Writer w( buffer );
        CHECK( buffer[0] == '\0' );
        CHECK( buffer[1] == 'b' );
        CHECK( w.size() == 0 );
    }
}

TEST( "text", "text fits, fits exactly, or is cut to what fits, NUL-terminated" ) {
    CHECK_EQ( written( 8, []( Writer& w ) { w.text( "abc" ); } ), "abc" );
    CHECK_EQ( written( 4, []( Writer& w ) { w.text( "abc" ); } ), "abc" );
    CHECK_EQ( written( 3, []( Writer& w ) { w.text( "abc" ); } ), "ab" );
    CHECK_EQ( written( 1, []( Writer& w ) { w.text( "abc" ); } ), "" );
    std::array<char, 4> buffer = {};
    buffer.fill( 'x' );
    Writer w( buffer );
    w.text( "ab" );
    CHECK( !w.truncated() );
    w.text( "" );
    CHECK( !w.truncated() );
    w.text( "cd" );
    CHECK( w.truncated() );
    CHECK( w.size() == 3 );
    CHECK_EQ( std::string( buffer.data() ), "abc" );
    // Full: everything later is dropped and the string stays terminated.
    w.character( 'e' );
    w.unsignedDecimal( 7 );
    CHECK( w.size() == 3 );
    CHECK( buffer[3] == '\0' );
}

TEST( "text", "the view is what was written, in the caller's buffer" ) {
    std::array<char, 8> buffer = {};
    Writer              w( buffer );
    CHECK( w.view().empty() );
    w.text( "ab" );
    w.unsignedDecimal( 12 );
    CHECK_EQ( std::string( w.view() ), "ab12" );
    CHECK( w.view().data() == buffer.data() );
    w.text( "cdefgh" );
    CHECK_EQ( std::string( w.view() ), "ab12cde" );
    Writer none( std::span<char>( buffer.data(), 0 ) );
    CHECK( none.view().empty() );
}

TEST( "text", "decimal integers, the extremes of both types included" ) {
    CHECK_EQ( written( 32, []( Writer& w ) { w.unsignedDecimal( 0 ); } ), "0" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.unsignedDecimal( UINT64_MAX ); } ),
              "18446744073709551615" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.signedDecimal( 0 ); } ), "0" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.signedDecimal( -113 ); } ), "-113" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.signedDecimal( INT64_MIN ); } ),
              "-9223372036854775808" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.signedDecimal( INT64_MAX ); } ),
              "9223372036854775807" );
    // Cut inside the number, as snprintf() cut it: the leading digits.
    CHECK_EQ( written( 4, []( Writer& w ) { w.unsignedDecimal( 123456 ); } ), "123" );
    for ( std::uint64_t value = 1; value < UINT64_MAX / 3U; value = value * 3U + 1U ) {
        CHECK_EQ( written( 32, [value]( Writer& w ) { w.unsignedDecimal( value ); } ),
                  std::to_string( value ) );
    }
}

TEST( "text", "zero-padded decimals: narrower, as wide and wider than the width" ) {
    CHECK_EQ( written( 32, []( Writer& w ) { w.zeroPadded( 5, 3 ); } ), "005" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.zeroPadded( 123, 3 ); } ), "123" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.zeroPadded( 12345, 3 ); } ), "12345" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.zeroPadded( 0, 0 ); } ), "0" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.zeroPadded( 0, 24 ); } ), std::string( 24, '0' ) );
    CHECK_EQ( written( 3, []( Writer& w ) { w.zeroPadded( 7, 4 ); } ), "00" );
}

TEST( "text", "hexadecimal in either case, padded" ) {
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( 0x5449, 4, LetterCase::Upper ); } ), "5449" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( 0xC003, 4, LetterCase::Upper ); } ), "C003" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( 0x36b3aab0, 8, LetterCase::Lower ); } ),
              "36b3aab0" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( 0x40, 2, LetterCase::Upper ); } ), "40" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( 0x6420, 6, LetterCase::Upper ); } ), "006420" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( 0, 0, LetterCase::Lower ); } ), "0" );
    CHECK_EQ( written( 32, []( Writer& w ) { w.hex( UINT64_MAX, 1, LetterCase::Upper ); } ),
              "FFFFFFFFFFFFFFFF" );
    // As printf's %0*X and %0*x write them.
    for ( std::uint64_t value = 1; value < UINT64_MAX / 7U; value = value * 7U + 3U ) {
        char upper[32] = {};
        char lower[32] = {};
        std::snprintf( upper, sizeof( upper ), "%08llX", static_cast<unsigned long long>( value ) );
        std::snprintf( lower, sizeof( lower ), "%08llx", static_cast<unsigned long long>( value ) );
        CHECK_EQ( written( 32, [value]( Writer& w ) { w.hex( value, 8, LetterCase::Upper ); } ),
                  upper );
        CHECK_EQ( written( 32, [value]( Writer& w ) { w.hex( value, 8, LetterCase::Lower ); } ),
                  lower );
    }
}

TEST( "text", "scaled values: the fraction's trailing zeros go, down to one digit" ) {
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 88637490694, 12 ); } ), "0.088637490694" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 1638400, 6 ); } ), "1.6384" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( -1500, 3 ); } ), "-1.5" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 1234, 3 ); } ), "1.234" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 25000, 3 ); } ), "25.0" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 10, 1 ); } ), "1.0" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 0, 9 ); } ), "0.0" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 42, 0 ); } ), "42" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( INT64_MIN, 0 ); } ),
              "-9223372036854775808" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( INT64_MIN, 18 ); } ),
              "-9.223372036854775808" );
    // More decimals than a 64-bit unit holds are the most it holds.
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 1, 30 ); } ), "0.000000000000000001" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.scaled( 1, kMaxDecimals ); } ),
              "0.000000000000000001" );
    // Every value the old format wrote, for a spread of values and decimals.
    for ( std::uint32_t decimals = 0; decimals <= kMaxDecimals; ++decimals ) {
        for ( std::int64_t value = -1; value > INT64_MIN / 5; value = value * 5 - 3 ) {
            CHECK_EQ( written( 40, [=]( Writer& w ) { w.scaled( value, decimals ); } ),
                      reference( value, decimals ) );
            CHECK_EQ( written( 40, [=]( Writer& w ) { w.scaled( -value, decimals ); } ),
                      reference( -value, decimals ) );
        }
    }
}

TEST( "text", "thousandths: always three decimals, as a stream record writes them" ) {
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( 5000449 ); } ), "5000.449" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( 25000 ); } ), "25.000" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( -500 ); } ), "-0.500" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( 0 ); } ), "0.000" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( INT64_MIN ); } ),
              "-9223372036854775.808" );
    // FW-26: the cases scale::formatThousandths() was held to, before the
    // writer took its last caller.
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( 1 ); } ), "0.001" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( -1 ); } ), "-0.001" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( 50'000'000 ); } ), "50000.000" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( -1'638'400'000 ); } ), "-1638400.000" );
    CHECK_EQ( written( 40, []( Writer& w ) { w.thousandths( INT64_MAX ); } ),
              "9223372036854775.807" );
}

TEST( "text", "a report's keys: a space before every key but the first" ) {
    CHECK_EQ( written( 64,
                       []( Writer& w ) {
                           w.field( "pixels", 4U );
                           w.field( "held", "low" );
                           w.key( "flags" );
                           w.text( "0x" );
                           w.hex( 0xC003, 4, LetterCase::Upper );
                       } ),
              "pixels=4 held=low flags=0xC003" );
    CHECK_EQ( written( 64,
                       []( Writer& w ) {
                           w.text( "blanked=48,38" );
                           w.field( "res_us", 1000U );
                       } ),
              "blanked=48,38 res_us=1000" );
    CHECK_EQ( written( 6, []( Writer& w ) { w.field( "pixels", 4U ); } ), "pixel" );
}
