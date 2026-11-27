// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_scpi.cpp
 * @brief Host tests of the SCPI layer (src/scpi.h): the grammar the
 *        instrument parses, exercised without the instrument.
 *
 * Build and run: `make -C firmware/test/native`.
 */
#include "scpi.h"
#include "unit.h"

#include <cstdio>
#include <cstring>
#include <span>
#include <string>
#include <string_view>
#include <vector>

using namespace kleidos::meter::scpi;

namespace {

#define CHECK_STR( actual, expected ) CHECK( std::string( actual ) == std::string( expected ) )

/// The resolved headers and parameters of one program message.
std::vector<std::pair<std::string, std::string>> units( const char* message ) {
    std::vector<char> line( message, message + std::strlen( message ) + 1 );
    std::vector<std::pair<std::string, std::string>> out;
    Splitter                                         splitter( line.data() );
    PathResolver                                     resolver;
    Unit                                             unit;
    while ( splitter.next( unit ) ) {
        if ( unit.headerLength == 0 ) {
            continue;
        }
        char header[kHeaderCapacity] = {};
        CHECK( resolver.resolve( unit, header, sizeof( header ) ) );
        out.emplace_back( header, unit.parameters );
    }
    return out;
}

bool match( const char* pattern, const char* header, int* suffix = nullptr ) {
    Suffix     s;
    const bool ok = matches( pattern, header, s );
    if ( ok && suffix != nullptr ) {
        *suffix = s.value;
    }
    return ok;
}

struct Params {
    explicit Params( const char* text ) : buffer( text, text + std::strlen( text ) + 1 ) {
        parsed = params.parse( buffer.data() );
    }
    std::vector<char> buffer;
    Parameters        params;
    bool              parsed = false;
};

void testSplitterAndPaths() {
    auto u = units( "SENS:CURR:RANG 1;INDex?" );
    CHECK( u.size() == 2 );
    CHECK_STR( u[0].first, "SENS:CURR:RANG" );
    CHECK_STR( u[0].second, "1" );
    // A header with no colon after `;` is resolved against the previous path.
    CHECK_STR( u[1].first, "SENS:CURR:INDex?" );

    u = units( "ROUT:NULL ON;:SYST:ERR:ALL?" );
    CHECK( u.size() == 2 );
    CHECK_STR( u[1].first, "SYST:ERR:ALL?" );

    // Common commands leave the path alone.
    u = units( "SYST:ERR?;*CLS;COUN?" );
    CHECK( u.size() == 3 );
    CHECK_STR( u[1].first, "*CLS" );
    CHECK_STR( u[2].first, "SYST:COUN?" );

    // A `;` inside a quoted string does not split.
    u = units( "TRAC:MARK \"a;b\";:ABOR" );
    CHECK( u.size() == 2 );
    CHECK_STR( u[0].second, "\"a;b\"" );
    CHECK_STR( u[1].first, "ABOR" );

    // Whitespace around everything, and an empty trailing unit.
    u = units( "  *IDN?  ;  " );
    CHECK( u.size() == 1 );
    CHECK_STR( u[0].first, "*IDN?" );

    u = units( "ROUT:LEAD OPEN, FORC ,LIVE" );
    CHECK( u.size() == 1 );
    CHECK_STR( u[0].second, "OPEN, FORC ,LIVE" );
}

void testMatching() {
    CHECK( match( "SYSTem:ERRor[:NEXT]?", "SYST:ERR?" ) );
    CHECK( match( "SYSTem:ERRor[:NEXT]?", "system:error:next?" ) );
    CHECK( match( "SYSTem:ERRor[:NEXT]?", "SYSTEM:ERR:NEXT?" ) );
    CHECK( !match( "SYSTem:ERRor[:NEXT]?", "SYST:ERR" ) );  // query form only
    CHECK( !match( "SYSTem:ERRor[:NEXT]?", "SYST:ERR:COUN?" ) );
    CHECK( !match( "SYSTem:ERRor[:NEXT]?", "SYSTE:ERR?" ) );  // neither form
    CHECK( match( "MEASure[:SCALar]:CURRent[:DC]?", "MEAS:CURR?" ) );
    CHECK( match( "MEASure[:SCALar]:CURRent[:DC]?", "MEAS:SCAL:CURR:DC?" ) );
    CHECK( match( "MEASure[:SCALar]:CURRent[:DC]?", "meas:curr:dc?" ) );
    CHECK( !match( "MEASure[:SCALar]:CURRent[:DC]?", "MEAS:VOLT?" ) );
    CHECK( match( "*IDN?", "*idn?" ) );
    CHECK( !match( "*IDN?", "*IDN" ) );
    CHECK( match( "SYSTem:COMMunicate:I2C:FREQuency?", "SYST:COMM:I2C:FREQ?" ) );
    CHECK( match( "ROUTe:NULLpath", "ROUT:NULL" ) );
    CHECK( match( "ROUTe:NULLpath", "route:nullpath" ) );
    CHECK( match( "FORMat[:DATA]", "FORM" ) );

    int suffix = 0;
    CHECK( match( "ROUTe:RELay#", "ROUT:REL3", &suffix ) );
    CHECK( suffix == 3 );
    CHECK( match( "ROUTe:RELay#", "ROUTE:RELAY4", &suffix ) );
    CHECK( suffix == 4 );
    CHECK( match( "ROUTe:RELay#", "ROUT:REL", &suffix ) );
    CHECK( suffix == 1 );                           // the default suffix
    CHECK( !match( "ROUTe:LEAD", "ROUT:LEAD2" ) );  // no suffix allowed
    CHECK( !match( "ROUTe:LEAD", "ROUT::LEAD" ) );  // empty node
}

void testParameters() {
    {
        Params p( "OPEN, FORC ,LIVE" );
        CHECK( p.parsed );
        CHECK( p.params.count() == 3 );
        static constexpr const char* kActions[] = { "OPEN", "CLOSe" };
        static constexpr const char* kOptions[] = { "FORCe", "LIVE" };
        std::size_t                  which      = 9;
        CHECK( p.params.choice( 0, kActions, 2, which ) && which == 0 );
        CHECK( p.params.choice( 1, kOptions, 2, which ) && which == 0 );
        CHECK( p.params.choice( 2, kOptions, 2, which ) && which == 1 );
        CHECK( !p.params.choice( 0, kOptions, 2, which ) );
        CHECK( p.params.error() == err::kIllegalParameter );
    }
    {
        Params p( "ON" );
        bool   value = false;
        CHECK( p.params.boolean( 0, value ) && value );
        Params q( "0" );
        CHECK( q.params.boolean( 0, value ) && !value );
        Params r( "MAYBE" );
        CHECK( !r.params.boolean( 0, value ) );
    }
    {
        std::uint32_t value = 0;
        Params        p( "60" );
        CHECK( p.params.unsignedInteger( 0, value ) && value == 60 );
        Params h( "#H04" );
        CHECK( h.params.unsignedInteger( 0, value ) && value == 4 );
        Params b( "#B101" );
        CHECK( b.params.unsignedInteger( 0, value ) && value == 5 );
        Params q( "#Q17" );
        CHECK( q.params.unsignedInteger( 0, value ) && value == 15 );
        Params n( "-1" );
        CHECK( !n.params.unsignedInteger( 0, value ) );
        CHECK( n.params.error() == err::kDataOutOfRange );
        Params f( "1.5" );
        CHECK( !f.params.unsignedInteger( 0, value ) );
        CHECK( f.params.error() == err::kDataType );
        Params big( "4294967296" );
        CHECK( !big.params.unsignedInteger( 0, value ) );
        Params missing( "" );
        CHECK( !missing.params.unsignedInteger( 0, value ) );
        CHECK( missing.params.error() == err::kMissingParameter );
    }
    {
        std::int64_t value = 0;
        Params       p( "0.1000125" );
        CHECK( p.params.scaled( 0, 6, value ) && value == 100013 );  // rounded half away
        Params e( "1.00012E-01" );
        CHECK( e.params.scaled( 0, 6, value ) && value == 100012 );
        Params a( "0.4096" );
        CHECK( a.params.scaled( 0, 6, value ) && value == 409600 );
        Params m( "-2.5e-6" );
        CHECK( m.params.scaled( 0, 6, value ) && value == -3 );  // -2.5 rounds to -3
        Params i( "100" );
        CHECK( i.params.scaled( 0, 3, value ) && value == 100000 );
        Params z( "0.000" );
        CHECK( z.params.scaled( 0, 6, value ) && value == 0 );
        Params bad( "1.2.3" );
        CHECK( !bad.params.scaled( 0, 6, value ) );
        Params word( "MAX" );
        CHECK( !word.params.scaled( 0, 6, value ) );
        CHECK( word.params.error() == err::kDataType );
        Params huge( "1E30" );
        CHECK( !huge.params.scaled( 0, 6, value ) );
        CHECK( huge.params.error() == err::kDataOutOfRange );
    }
    {
        char   out[16] = {};
        Params s( "\"say \"\"hi\"\"\"" );
        CHECK( s.parsed );
        CHECK( s.params.string( 0, out, sizeof( out ) ) );
        CHECK_STR( out, "say \"hi\"" );
        Params t( "'x,y'" );
        CHECK( t.params.count() == 1 );
        CHECK( t.params.string( 0, out, sizeof( out ) ) );
        CHECK_STR( out, "x,y" );
        Params u( "\"unterminated" );
        CHECK( !u.parsed );
        CHECK( u.params.error() == err::kStringData );
        Params bare( "bare" );
        CHECK( !bare.params.string( 0, out, sizeof( out ) ) );
    }
    {
        Params empty( "1,,2" );
        CHECK( !empty.parsed );
        CHECK( empty.params.error() == err::kSyntax );
        Params many( "1,2,3,4,5,6,7,8,9" );
        CHECK( !many.parsed );
    }
}

void testErrors() {
    ErrorQueue queue;
    char       out[160] = {};
    formatError( queue.pop(), out );
    CHECK_STR( out, "0,\"No error\"" );

    queue.push( err::kSettingsConflict, "capture running (ABORt first)" );
    queue.push( err::kNullNotStored, "said \"no\"" );
    CHECK( queue.count() == 2 );
    formatError( queue.pop(), out );
    CHECK_STR( out, "-221,\"Settings conflict;capture running (ABORt first)\"" );
    formatError( queue.pop(), out );
    CHECK_STR( out, "102,\"Null not stored;said 'no'\"" );  // no `"` inside the string

    for ( int i = 0; i < 15; ++i ) {
        queue.push( err::kSyntax, "x" );
    }
    CHECK( queue.count() == kErrorQueueDepth );
    for ( std::size_t i = 0; i + 1 < kErrorQueueDepth; ++i ) {
        CHECK( queue.pop().code == err::kSyntax );
    }
    CHECK( queue.pop().code == err::kQueueOverflow );  // the newest is replaced
    CHECK( queue.count() == 0 );

    CHECK( esrBitFor( err::kSyntax ) == esr::kCommandError );
    CHECK( esrBitFor( err::kSettingsConflict ) == esr::kExecutionError );
    CHECK( esrBitFor( err::kQueueOverflow ) == esr::kDeviceError );
    CHECK( esrBitFor( err::kNullNotStored ) == esr::kDeviceError );
    CHECK( esrBitFor( err::kQuery ) == esr::kQueryError );
    CHECK( esrBitFor( err::kNone ) == 0 );

    // A detail too long for the response still closes its string.
    ErrorEntry entry;
    entry.code = err::kExecution;
    std::memset( entry.detail, 'a', kErrorDetailCapacity - 1 );
    char small[32] = {};
    formatError( entry, small );
    CHECK( std::strlen( small ) == sizeof( small ) - 1 );
    CHECK( small[sizeof( small ) - 2] == '"' );
}

}  // namespace

TEST( "scpi", "program messages split into units and resolve their paths" ) {
    testSplitterAndPaths();
}

TEST( "scpi", "headers match patterns in long, short, optional and suffixed forms" ) {
    testMatching();
}

TEST( "scpi", "parameters decode exactly or are refused with their error" ) {
    testParameters();
}

TEST( "scpi", "responses are formatted exactly, with no float" ) {}

TEST( "scpi", "the error queue keeps its order, overflows to -350 and maps ESR bits" ) {
    testErrors();
}

// --- MC/DC: each condition of each decision shown to flip it on its own -----

namespace {

bool boolOf( const char* text, bool& value ) {
    Params p( text );
    return p.parsed && p.params.boolean( 0, value );
}

std::int16_t choiceError( const char* text ) {
    static constexpr const char* kChoices[] = { "ONE", "TWO" };
    Params                       p( text );
    std::size_t                  which = 0;
    (void)p.params.choice( 0, kChoices, 2, which );
    return p.params.error();
}

std::int16_t unsignedError( const char* text ) {
    Params        p( text );
    std::uint32_t value = 0;
    (void)p.params.unsignedInteger( 0, value );
    return p.params.error();
}

std::int16_t stringError( const char* text, std::size_t capacity = 16 ) {
    Params p( text );
    char   out[16] = {};
    (void)p.params.string( 0, out, capacity );
    return p.params.error();
}

}  // namespace

TEST( "scpi", "MC/DC: whitespace, case and the null guards" ) {
    // Every whitespace character trims and separates.
    auto u = units( "\tSYST:ERR?\r;\n*IDN?" );
    CHECK( u.size() == 2 );
    CHECK_STR( u[0].first, "SYST:ERR?" );
    CHECK_STR( u[1].first, "*IDN?" );
    // Above 'z' is not lower case: no fold.
    CHECK( !mnemonicMatches( "AB", "a{", 2 ) );
    CHECK( mnemonicMatches( "ABc", "ab", 2 ) );
    // An all-lower-case long form has no short form.
    CHECK( !mnemonicMatches( "lower", "lo", 2 ) );
    CHECK( mnemonicMatches( "lower", "LOWER", 5 ) );
    CHECK( !mnemonicMatches( nullptr, "A", 1 ) );
    CHECK( !mnemonicMatches( "A", nullptr, 1 ) );
    CHECK( !equalsIgnoreCase( nullptr, "a" ) );
    CHECK( !equalsIgnoreCase( "a", nullptr ) );
    CHECK( equalsIgnoreCase( "aBc", "AbC" ) );
    Splitter none( nullptr );
    Unit     unit;
    CHECK( !none.next( unit ) );
}

TEST( "scpi", "MC/DC: malformed patterns and headers never match" ) {
    Suffix s;
    CHECK( !matches( "", "", s ) );                      // empty both: no segment
    CHECK( !matches( "A?", "", s ) );                    // query mismatch, empty header
    CHECK( !matches( "A[", "A", s ) );                   // optional with nothing after
    CHECK( !matches( "A[:B", "A", s ) );                 // optional never closed
    CHECK( !matches( "A[:B:C]", "A", s ) );              // optional closed by ':' not ']'
    CHECK( matches( "A[B]", "A", s ) );                  // optional node with no colon
    CHECK( !matches( "A::B", "A:B", s ) );               // an empty segment
    CHECK( !matches( "#", "A", s ) );                    // a suffix with no name
    CHECK( !matches( "A:B:C:D:E:F:G:H:I", "A", s ) );    // too many segments
    CHECK( !matches( "A", "A:B:C:D:E:F:G:H:I", s ) );    // too many nodes
    CHECK( !matches( "A:B", "A::B", s ) );               // an empty node
    CHECK( !matches( "ROUTe:RELay#", "ROUT:7", s ) );    // digits only
    CHECK( !matches( "ROUTe:RELay", "ROUT:REL3", s ) );  // no suffix allowed
    CHECK( matches( "ROUTe:RELay#", "ROUT:RELAY12", s ) && s.value == 12 );
    CHECK( !matches( "ROUTe:RELay#", "ROUT:XEL3", s ) );
}

TEST( "scpi", "MC/DC: the path resolver on an empty and an absolute header" ) {
    PathResolver resolver;
    char         out[kHeaderCapacity] = {};
    Unit         empty;
    char         blank[] = "";
    empty.header         = blank;
    empty.parameters     = blank;
    CHECK( resolver.resolve( empty, out, sizeof( out ) ) );
    CHECK( !resolver.resolve( empty, out, 0 ) );
    char absolute[] = ":SYST:ERR?";
    Unit rooted;
    rooted.header       = absolute;
    rooted.headerLength = std::strlen( absolute );
    CHECK( resolver.resolve( rooted, out, sizeof( out ) ) );
    CHECK_STR( out, "SYST:ERR?" );
    CHECK( !resolver.resolve( rooted, out, 4 ) );
}

TEST( "scpi", "MC/DC: ESR bits for every error class and outside them" ) {
    CHECK( esrBitFor( -50 ) == esr::kDeviceError );  // above every class
    CHECK( esrBitFor( -150 ) == esr::kCommandError );
    CHECK( esrBitFor( -250 ) == esr::kExecutionError );
    CHECK( esrBitFor( -350 ) == esr::kDeviceError );
    CHECK( esrBitFor( -410 ) == esr::kQueryError );
    CHECK( esrBitFor( -600 ) == esr::kDeviceError );  // below every class
    CHECK( esrBitFor( 5 ) == esr::kDeviceError );
}

TEST( "scpi", "MC/DC: an error detail longer than its room is cut, quotes swapped" ) {
    ErrorQueue  queue;
    std::string detail( kErrorDetailCapacity + 20, 'x' );
    detail[0] = '"';
    queue.push( err::kExecution, detail.c_str() );
    const ErrorEntry entry = queue.pop();
    CHECK( std::strlen( entry.detail ) == kErrorDetailCapacity - 1 );
    CHECK( entry.detail[0] == '\'' );
    queue.push( err::kExecution, {} );
    CHECK( queue.pop().detail[0] == '\0' );
    // A view is read to its length, not to a NUL: what follows it is not taken.
    queue.push( err::kExecution, std::string_view( "abcdef", 3 ) );
    CHECK_EQ( std::string( queue.pop().detail ), "abc" );
}

TEST( "scpi", "MC/DC: character data refuses each non-mnemonic lead" ) {
    CHECK( choiceError( "\"ONE\"" ) == err::kDataType );
    CHECK( choiceError( "1" ) == err::kDataType );
    CHECK( choiceError( "#H1" ) == err::kDataType );
    CHECK( choiceError( "+ONE" ) == err::kDataType );
    CHECK( choiceError( "-ONE" ) == err::kDataType );
    CHECK( choiceError( ".ONE" ) == err::kDataType );
    CHECK( choiceError( "THREE" ) == err::kIllegalParameter );
    CHECK( choiceError( "two" ) == err::kNone );
    bool value = false;
    CHECK( boolOf( "1", value ) && value );
    CHECK( boolOf( "0", value ) && !value );
    CHECK( boolOf( "on", value ) && value );
    CHECK( boolOf( "Off", value ) && !value );
    CHECK( !boolOf( "2", value ) );
}

TEST( "scpi", "MC/DC: numbers in every base and every refusal" ) {
    CHECK( unsignedError( "#HFF" ) == err::kNone );
    CHECK( unsignedError( "#HG" ) == err::kNumericData );   // past 'F'
    CHECK( unsignedError( "#H1." ) == err::kNumericData );  // '.' outside base 10
    CHECK( unsignedError( "#HE" ) == err::kNone );          // 'E' a digit in base 16
    CHECK( unsignedError( "1E3" ) == err::kDataType );      // an exponent in base 10
    CHECK( unsignedError( "1.5" ) == err::kDataType );
    CHECK( unsignedError( "1x" ) == err::kNumericData );
    std::int64_t value = 0;
    {
        Params p( "+1.5" );
        CHECK( p.params.scaled( 0, 3, value ) && value == 1500 );
    }
    {
        Params p( "1E+2" );
        CHECK( p.params.scaled( 0, 0, value ) && value == 100 );
    }
    {
        Params p( "1E25" );  // the scale alone is past int64
        CHECK( !p.params.scaled( 0, 6, value ) && p.params.error() == err::kDataOutOfRange );
    }
    {
        Params p( "99999999999" );  // the mantissa past int64 at this scale
        CHECK( !p.params.scaled( 0, 12, value ) && p.params.error() == err::kDataOutOfRange );
    }
}

TEST( "scpi", "MC/DC: strings: each refusal on its own, and doubled quotes" ) {
    CHECK( stringError( "a" ) == err::kStringData );            // too short
    CHECK( stringError( "ab" ) == err::kStringData );           // not quoted
    CHECK( stringError( "\"ab\"c" ) == err::kStringData );      // ends past its quote
    CHECK( stringError( "\"ab\"", 0 ) == err::kStringData );    // no room at all
    CHECK( stringError( "\"a\"\"b\"" ) == err::kNone );         // a doubled quote
    CHECK( stringError( "\"a\"b\"c\"" ) == err::kStringData );  // a lone quote inside
    CHECK( stringError( "\"ab\"\"\"" ) == err::kNone );         // doubled at the end
}

// --- Branch coverage (the decision-coverage gate, docs/testing.md) -----------

TEST( "scpi", "branches: every base, sign and refusal of an unsigned integer" ) {
    std::uint32_t value = 0;
    {
        Params p( "#Q17,#B101,+5,#X1,#H,#B2,4294967296" );
        CHECK( p.params.unsignedInteger( 0, value ) && value == 15 );
        CHECK( p.params.unsignedInteger( 1, value ) && value == 5 );
        CHECK( p.params.unsignedInteger( 2, value ) && value == 5 );
        CHECK( !p.params.unsignedInteger( 3, value ) && p.params.error() == err::kNumericData );
        CHECK( !p.params.unsignedInteger( 4, value ) && p.params.error() == err::kNumericData );
        CHECK( !p.params.unsignedInteger( 5, value ) && p.params.error() == err::kNumericData );
        CHECK( !p.params.unsignedInteger( 6, value ) && p.params.error() == err::kDataOutOfRange );
        CHECK( !p.params.unsignedInteger( 7, value )
               && p.params.error() == err::kMissingParameter );
    }
    bool        flag       = false;
    std::size_t which      = 0;
    const char* kChoices[] = { "ON" };
    Params      none( "" );
    CHECK( none.parsed && none.params.count() == 0 );
    CHECK( !none.params.boolean( 0, flag ) && none.params.error() == err::kMissingParameter );
    CHECK( !none.params.choice( 0, kChoices, 1, which ) );
    std::int64_t scaled = 0;
    CHECK( !none.params.scaled( 0, 3, scaled ) && none.params.error() == err::kMissingParameter );
    Parameters bare;
    CHECK( bare.parse( nullptr ) && bare.count() == 0 );
}

TEST( "scpi", "branches: decimals past the digits kept, the exponent's bounds, rounding" ) {
    std::int64_t value = 0;
    {
        Params p( "1234567890123456789" );  // a nineteenth significant digit
        CHECK( !p.params.scaled( 0, 0, value ) && p.params.error() == err::kNumericData );
    }
    {
        Params p( "1E" );
        CHECK( !p.params.scaled( 0, 0, value ) && p.params.error() == err::kNumericData );
    }
    {
        Params p( "1E1000" );
        CHECK( !p.params.scaled( 0, 0, value ) && p.params.error() == err::kNumericData );
    }
    {
        Params p( "1E-40" );  // past every power of ten: zero, not an error
        CHECK( p.params.scaled( 0, 3, value ) && value == 0 );
    }
    {
        Params p( "0.0004,0.0005,  -2.5E-1" );
        CHECK( p.params.scaled( 0, 3, value ) && value == 0 );  // rounds down
        CHECK( p.params.scaled( 1, 3, value ) && value == 1 );  // half rounds up
        CHECK( p.params.scaled( 2, 2, value ) && value == -25 );
    }
}

TEST( "scpi", "branches: a suffix past five digits, spaces after a header, formats cut short" ) {
    CHECK( !match( "ROUTe:RELay#", "ROUT:REL1234567" ) );
    const auto u = units( "SENS:PROF   STAT" );
    CHECK( u.size() == 1 && u[0].second == "STAT" );

    char       out[32] = {};
    ErrorEntry entry;
    entry.code = -113;
    CHECK( formatError( entry, std::span<char>( out, 0 ) ) == 0 );
    CHECK( formatError( entry, std::span<char>( out, 1 ) ) == 0 && out[0] == '\0' );
    CHECK_STR( errorText( 999 ), "Device-specific error" );
    CHECK_STR( errorText( -999 ), "Error" );
}

TEST( "scpi", "branches: leading spaces, the int64 edge, the path buffer's edges" ) {
    // Parameters a caller hands over with spaces before them: the line's
    // splitter trims them, parse() does not count on it.
    {
        Params p( "   7,  8" );
        REQUIRE( p.parsed && p.params.count() == 2 );
        CHECK_STR( p.params.raw( 0 ), "7" );
        CHECK_STR( p.params.raw( 1 ), "8" );
    }
    {
        Params p( "  \t " );
        CHECK( p.parsed && p.params.count() == 0 );
    }
    // A decimal parameter up to INT64_MAX in magnitude, and not one past it:
    // eighteen significant digits times ten lands either side of 2^63.
    std::int64_t value = 0;
    {
        Params p( "922337203685477580E1,-922337203685477580E1" );
        CHECK( p.params.scaled( 0, 0, value ) && value == 9223372036854775800LL );
        CHECK( p.params.scaled( 1, 0, value ) && value == -9223372036854775800LL );
    }
    {
        Params p( "922337203685477581E1,-922337203685477581E1,9.3E18" );
        CHECK( !p.params.scaled( 0, 0, value ) && p.params.error() == err::kDataOutOfRange );
        CHECK( !p.params.scaled( 1, 0, value ) && p.params.error() == err::kDataOutOfRange );
        CHECK( !p.params.scaled( 2, 0, value ) && p.params.error() == err::kDataOutOfRange );
    }
    {
        // The same edge scaled to a unit's decimals: 9,223,372,036.85477580
        // in nanounits fits, and the next value of that resolution does not.
        Params p( "9223372036.85477580,9223372036.85477581" );
        CHECK( p.params.scaled( 0, 9, value ) && value == 9223372036854775800LL );
        CHECK( !p.params.scaled( 1, 9, value ) && p.params.error() == err::kDataOutOfRange );
    }
    // An unsigned integer up to 2^32 - 1, and past 2^32 and 2^64 refused as
    // out of range, never wrapped.
    std::uint32_t count = 0;
    {
        Params p( "4294967295,4294967296,18446744073709551616" );
        CHECK( p.params.unsignedInteger( 0, count ) && count == 4294967295U );
        CHECK( !p.params.unsignedInteger( 1, count ) && p.params.error() == err::kDataOutOfRange );
        CHECK( !p.params.unsignedInteger( 2, count ) && p.params.error() == err::kDataOutOfRange );
    }
    // A common header is copied whole into the path buffer or refused: it
    // fits with its terminator, and one byte less is a refusal, not a cut.
    PathResolver resolver;
    char         common[] = "*IDN?";
    Unit         unit;
    unit.header       = common;
    unit.headerLength = std::strlen( common );
    unit.common       = true;
    char out[8]       = {};
    CHECK( resolver.resolve( unit, out, sizeof( common ) ) );
    CHECK_STR( out, "*IDN?" );
    CHECK( !resolver.resolve( unit, out, sizeof( common ) - 1U ) );
    CHECK( !resolver.resolve( unit, out, 1 ) );
    CHECK( out[0] == '\0' );
    // A path as long as the resolver's buffer allows is kept whole and the
    // next relative header resolves against it.
    PathResolver nested;
    char         first[]  = "SENS:CURR:RANG:IND?";
    char         second[] = "UPP?";
    Unit         a;
    a.header       = first;
    a.headerLength = std::strlen( first );
    Unit b;
    b.header                       = second;
    b.headerLength                 = std::strlen( second );
    char resolved[kHeaderCapacity] = {};
    CHECK( nested.resolve( a, resolved, sizeof( resolved ) ) );
    CHECK( nested.resolve( b, resolved, sizeof( resolved ) ) );
    CHECK_STR( resolved, "SENS:CURR:RANG:UPP?" );
}
