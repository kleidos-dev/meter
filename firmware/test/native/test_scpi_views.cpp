// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_scpi_views.cpp
 * @brief Host tests of the SCPI layer's string_view surface (src/scpi.h)
 *        and of the bounds its parsers keep since they read numbers with
 *        `std::from_chars` (FW-22).
 *
 * test_scpi.cpp holds the grammar as it was before the parser carried names
 * as views; these cases hold what the views added: a pattern or a header
 * that is not NUL-terminated, the keyword tables as views, and every bound a
 * number had when it was read digit by digit.
 *
 * Build and run: `make -C firmware/test/native`.
 */
#include "scpi.h"
#include "unit.h"

#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

using namespace kleidos::meter::scpi;

namespace {

/// One parameter list, parsed in a buffer of its own.
struct Params {
    explicit Params( const char* text ) : buffer( text, text + std::strlen( text ) + 1 ) {
        parsed = params.parse( buffer.data() );
    }
    std::vector<char> buffer;
    Parameters        params;
    bool              parsed = false;
};

/// The error an unsigned integer leaves, or kNone and @p value.
std::int16_t unsignedOf( const char* text, std::uint32_t& value ) {
    Params p( text );
    (void)p.params.unsignedInteger( 0, value );
    return p.params.error();
}

/// The error a decimal number leaves, or kNone and @p value.
std::int16_t scaledOf( const char* text, unsigned decimals, std::int64_t& value ) {
    Params p( text );
    (void)p.params.scaled( 0, decimals, value );
    return p.params.error();
}

/// The suffix @p header carries against `ROUTe:RELay#`, or -1 when it does
/// not match.
std::int32_t suffixOf( std::string_view header ) {
    Suffix suffix;
    return matches( std::string_view( "ROUTe:RELay#" ), header, suffix ) ? suffix.value : -1;
}

}  // namespace

TEST( "views", "a pattern and a header need no terminator" ) {
    // Views into a longer text: the matcher reads their length, not a NUL.
    const std::string_view text    = "SYSTem:ERRor?trailing";
    const std::string_view pattern = text.substr( 0, 13 );
    Suffix                 suffix;
    CHECK( matches( pattern, std::string_view( "SYST:ERR?xyz" ).substr( 0, 9 ), suffix ) );
    CHECK( !matches( pattern, std::string_view( "SYST:ERR?xyz" ).substr( 0, 8 ), suffix ) );
    CHECK( matches( std::string_view( "*RST" ), std::string_view( "*rst" ), suffix ) );
    CHECK( !matches( std::string_view( "*RST" ), std::string_view( "*RS" ), suffix ) );
    CHECK( !matches( std::string_view(), std::string_view( "A" ), suffix ) );
    CHECK( mnemonicMatches( std::string_view( "CURRent" ), std::string_view( "curr" ) ) );
    CHECK( mnemonicMatches( std::string_view( "CURRent" ), std::string_view( "CURRENT" ) ) );
    CHECK( !mnemonicMatches( std::string_view( "CURRent" ), std::string_view( "CURRE" ) ) );
    CHECK( errorText( err::kQueueOverflow ) == "Queue overflow" );
}

TEST( "views", "a keyword table of views" ) {
    static constexpr std::string_view kWords[] = { "OPEN", "CLOSe" };
    std::size_t                       which    = 9;
    Params                            p( "CLOS, OPEN, 1, SHUT" );
    CHECK( p.params.choice( 0, kWords, which ) && which == 1 );
    CHECK( p.params.choice( 1, kWords, which ) && which == 0 );
    CHECK( !p.params.choice( 2, kWords, which ) && p.params.error() == err::kDataType );
    CHECK( !p.params.choice( 3, kWords, which ) && p.params.error() == err::kIllegalParameter );
    CHECK( !p.params.choice( 4, kWords, which ) && p.params.error() == err::kMissingParameter );
}

TEST( "views", "a numeric suffix keeps its six-digit bound" ) {
    CHECK( suffixOf( "ROUT:REL3" ) == 3 );
    CHECK( suffixOf( "ROUT:REL999999" ) == 999999 );
    CHECK( suffixOf( "ROUT:REL1000000" ) == -1 );
    CHECK( suffixOf( "ROUT:REL0000000000000042" ) == 42 );      // leading zeros are free
    CHECK( suffixOf( "ROUT:REL99999999999999999999" ) == -1 );  // past 32 bits
}

TEST( "views", "an unsigned integer: each refusal in the order it was found" ) {
    std::uint32_t value = 0;
    CHECK( unsignedOf( "#hff", value ) == err::kNone && value == 255 );
    CHECK( unsignedOf( "4294967295", value ) == err::kNone && value == 4294967295U );
    CHECK( unsignedOf( "#", value ) == err::kNumericData );
    CHECK( unsignedOf( "#Q8", value ) == err::kNumericData );
    CHECK( unsignedOf( "#B12", value ) == err::kNumericData );
    CHECK( unsignedOf( "+-1", value ) == err::kNumericData );
    CHECK( unsignedOf( "-1", value ) == err::kDataOutOfRange );
    CHECK( unsignedOf( "1e3", value ) == err::kDataType );
    CHECK( unsignedOf( ".5", value ) == err::kDataType );
    CHECK( unsignedOf( "12A", value ) == err::kNumericData );
    // Past 32 bits wins over what follows the digits, as it did digit by digit.
    CHECK( unsignedOf( "99999999999x", value ) == err::kDataOutOfRange );
    CHECK( unsignedOf( "1x99999999999", value ) == err::kNumericData );
}

TEST( "views", "a decimal number: significant digits, the exponent, the trailing text" ) {
    std::int64_t value = 0;
    CHECK( scaledOf( "123456789012345678", 0, value ) == err::kNone
           && value == 123456789012345678 );
    CHECK( scaledOf( "1.00000000000000000", 0, value ) == err::kNone && value == 1 );
    CHECK( scaledOf( "1.000000000000000000", 0, value ) == err::kNumericData );
    CHECK( scaledOf( "0000000000000000000001.5", 1, value ) == err::kNone && value == 15 );
    CHECK( scaledOf( "0.000000000000000000000123456789012345678", 0, value ) == err::kNone
           && value == 0 );
    CHECK( scaledOf( "0.0000000000000000000001234567890123456789", 0, value )
           == err::kNumericData );
    CHECK( scaledOf( "1234567890123456789x", 0, value ) == err::kNumericData );
    CHECK( scaledOf( "5.", 2, value ) == err::kNone && value == 500 );
    CHECK( scaledOf( ".25", 2, value ) == err::kNone && value == 25 );
    CHECK( scaledOf( ".", 0, value ) == err::kDataType );
    CHECK( scaledOf( "-", 0, value ) == err::kDataType );
    CHECK( scaledOf( "1.2.3", 0, value ) == err::kDataType );
    CHECK( scaledOf( "1e999", 0, value ) == err::kDataOutOfRange );
    CHECK( scaledOf( "1e-999", 0, value ) == err::kNone && value == 0 );
    CHECK( scaledOf( "1e0000000000000000000000000002", 0, value ) == err::kNone && value == 100 );
    CHECK( scaledOf( "1e99999999999999999999", 0, value ) == err::kNumericData );
    CHECK( scaledOf( "1e+", 0, value ) == err::kNumericData );
    CHECK( scaledOf( "1e5.5", 0, value ) == err::kDataType );
    CHECK( scaledOf( "2.5e-1", 2, value ) == err::kNone && value == 25 );
}
