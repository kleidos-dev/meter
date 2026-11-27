// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_scale.cpp
 * @brief scale.h: the integer conversions every reported value goes through.
 */
#include "scale.h"
#include "unit.h"

#include <cstdint>

using namespace kleidos::meter::scale;

TEST( "scale", "20-bit, 40-bit and 16-bit registers sign-extend" ) {
    CHECK( decode20( 0x000010 ) == 1 );
    CHECK( decode20( 0x7FFFF0 ) == 524287 );
    CHECK( decode20( 0x800000 ) == -524288 );
    CHECK( decode20( 0xFFFFF0 ) == -1 );
    CHECK( decode20( 0x00000F ) == 0 );  // the reserved nibble is ignored
    CHECK( decode40( 0x7FFFFFFFFFULL ) == 549755813887LL );
    CHECK( decode40( 0x8000000000ULL ) == -549755813888LL );
    CHECK( decode40( 0xFFFFFFFFFFULL ) == -1 );
    CHECK( decode20( 0xFF000010 ) == 1 );             // the bits above 23 are ignored
    CHECK( decode40( 0xFF00000000000001ULL ) == 1 );  // and those above 39
    CHECK( decode16Signed( 0xFFFF ) == -1 );
    CHECK( decode16Signed( 0x0C80 ) == 3200 );
}

TEST( "scale", "voltages, temperature, current, power and charge in integer units" ) {
    CHECK( shuntPicovolts( 16000, 312500 ) == 5'000'000'000LL );
    CHECK( busPicovolts( 19968, 195312500 ) == 3'900'000'000'000LL );
    CHECK( dieMilliCelsius( 3200 ) == 25000 );
    CHECK( dieMilliCelsius( -16 ) == -125 );
    // 1 count of range 0 at the nominal shunt is exactly CURRENT_LSB.
    CHECK( currentNanoamps( 312500, 0, 100000 ) == 3125 );
    CHECK( currentNanoamps( 5'000'480'000, 480'000, 100000 ) == 50'000'000 );
    CHECK( currentNanoamps( 1000, 0, 0 ) == 0 );  // a zero shunt never divides
    CHECK( dutPicovolts( 3'900'000'000'000, 5'000'000'000 ) == 3'895'000'000'000 );
    // 3.8 V at 100 mA is 380 mW, with no int64 overflow.
    CHECK( powerMicrowatts( 3'800'000'000'000, 100'000'000 ) == 380'000 );
    CHECK( chargeNanocoulombs( 1000, 3125000 ) == 3'125'000 );
    CHECK( chargeNanocoulombs( 1, 781250 ) == 781 );
}

TEST( "scale", "integer square root is exact for the truncated root" ) {
    CHECK( integerSqrt( 0 ) == 0 );
    CHECK( integerSqrt( 1 ) == 1 );
    CHECK( integerSqrt( 2 ) == 1 );
    CHECK( integerSqrt( 15 ) == 3 );
    CHECK( integerSqrt( 16 ) == 4 );
    CHECK( integerSqrt( 1'000'000'000'000ULL ) == 1'000'000 );
    CHECK( integerSqrt( UINT64_MAX ) == 4294967295ULL );
}
