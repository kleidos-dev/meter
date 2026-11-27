// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_chars.cpp
 * @brief Host tests of the character table (src/chars.h): every class, the
 *        case folding, the digits and the bytes, compared with what the C
 *        library's <cctype> says of every byte.
 *
 * Build and run: `make -C firmware/test/native`.
 */
#include "chars.h"
#include "unit.h"

#include <cctype>
#include <cstddef>
#include <cstdint>

using namespace kleidos::meter::chars;

namespace {

/// The character of code @p code, for the comparisons with <cctype>.
char characterOf( int code ) {
    return std::char_traits<char>::to_char_type( code );
}

}  // namespace

// The table is built at compile time.
static_assert( kTable.size() == kAsciiCount );
static_assert( isDigit( '7' ) && !isDigit( 'a' ) );
static_assert( toUpper( 'q' ) == 'Q' );

TEST( "chars", "every ASCII byte is classed as <cctype> classes it" ) {
    for ( int code = 0; code < 128; ++code ) {
        const char c = characterOf( code );
        CHECK( isDigit( c ) == ( std::isdigit( code ) != 0 ) );
        CHECK( isUpper( c ) == ( std::isupper( code ) != 0 ) );
        CHECK( isLetter( c ) == ( std::isalpha( code ) != 0 ) );
        CHECK( isPrintable( c ) == ( std::isprint( code ) != 0 ) );
        CHECK( isGraphic( c ) == ( std::isgraph( code ) != 0 ) );
        CHECK( toUpper( c ) == characterOf( std::toupper( code ) ) );
        const bool space =
            ( code == ' ' ) || ( code == '\t' ) || ( code == '\r' ) || ( code == '\n' );
        CHECK( isSpace( c ) == space );
    }
}

TEST( "chars", "a byte above 0x7F belongs to no class and keeps its case" ) {
    for ( int code = 128; code < 256; ++code ) {
        const char c = characterOf( code );
        CHECK( !isDigit( c ) && !isUpper( c ) && !isLetter( c ) && !isSpace( c ) );
        CHECK( !isPrintable( c ) && !isGraphic( c ) );
        CHECK( toUpper( c ) == c );
    }
}

TEST( "chars", "MC/DC: a letter of either case, the space and DEL" ) {
    CHECK( isLetter( 'A' ) );   // upper alone
    CHECK( isLetter( 'z' ) );   // lower alone
    CHECK( !isLetter( '5' ) );  // neither
    CHECK( isGraphic( '~' ) );
    CHECK( !isGraphic( ' ' ) );       // printable, but the space
    CHECK( !isGraphic( '\x7F' ) );    // not printable
    CHECK( !isPrintable( '\x1F' ) );  // a control character
}

TEST( "chars", "digits are kDigits at their value, bytes go both ways" ) {
    for ( std::size_t value = 0; value < 10; ++value ) {
        CHECK( traitsOf( kDigits[value] ).value == value );
    }
    CHECK( traitsOf( 'F' ).value == 15 && traitsOf( 'f' ).value == 15 );
    CHECK( traitsOf( 'Z' ).value == 35 && traitsOf( 'z' ).value == 35 );
    for ( int byte = 0; byte < 256; ++byte ) {
        const auto value = static_cast<std::uint8_t>( byte );
        CHECK( byteOf( fromByte( value ) ) == value );
    }
    CHECK( byteOf( 'A' ) == 0x41U );
    CHECK( fromByte( 0x7EU ) == '~' );
}

TEST( "chars", "an array's text ends at its first NUL, or at its end" ) {
    const char empty[4] = {};
    const char word[8]  = "abc";
    const char full[3]  = { 'x', 'y', 'z' };
    const char inner[6] = { 'a', '\0', 'b', 'c', '\0', '\0' };
    CHECK( untilNul( empty ).empty() );
    CHECK( untilNul( word ) == "abc" );
    CHECK( untilNul( full ) == "xyz" );  // no NUL: the whole array, and no further
    CHECK( untilNul( inner ) == "a" );
    static_assert( untilNul( "kleidos" ).size() == 7U, "a constant expression" );
}
