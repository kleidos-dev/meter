// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_calmath.cpp
 * @brief calmath.h: the shunt from a reference resistor, and the limits a
 *        calibration result must meet. One case per condition of each decision.
 */
#include "calmath.h"
#include "unit.h"

#include <string>

using namespace kleidos::meter::calmath;

namespace {

/// 41.234 mA through a 100 Ohm reference and a shunt of @p nanoOhm.
ShuntPoint pointFor( std::int64_t nanoOhm, std::uint32_t samples = 1000 ) {
    const std::int64_t currentNa   = 41'234'000;
    const std::int64_t referenceUv = currentNa * 100'000'000 / 1'000'000'000;  // I x 100 Ohm
    const std::int64_t shuntNv     = currentNa * nanoOhm / 1'000'000'000;
    return shuntFromReference( shuntNv, referenceUv, samples, kDefaultReferenceMicroOhm );
}

}  // namespace

TEST( "calmath", "the shunt is the ratio of the two drops times the reference" ) {
    const ShuntPoint nominal = pointFor( 100'000'000 );
    CHECK( nominal.verdict == Verdict::Ok );
    CHECK( nominal.nanoOhm == 100'000'000 );
    CHECK( nominal.currentNa == 41'234'000 );
    CHECK( nominal.deviationPpm == 0 );

    const ShuntPoint high = pointFor( 100'123'400 );
    CHECK( high.verdict == Verdict::Ok );
    CHECK( high.nanoOhm > 100'123'000 && high.nanoOhm < 100'124'000 );
    CHECK( high.deviationPpm == 1233 || high.deviationPpm == 1234 );

    const ShuntPoint low = pointFor( 99'000'000 );
    CHECK( low.verdict == Verdict::Ok );
    CHECK( low.deviationPpm < 0 );
}

TEST( "calmath", "each refusal condition is reached on its own" ) {
    // samples == 0 (and the reference value alone)
    CHECK( shuntFromReference( 4'000'000, 4'000'000, 0, kDefaultReferenceMicroOhm ).verdict
           == Verdict::NoSamples );
    CHECK( shuntFromReference( 4'000'000, 4'000'000, 10, 0 ).verdict == Verdict::NoSamples );
    // a negative shunt drop, then a negative reference drop
    CHECK( shuntFromReference( -4'000'000, 4'000'000, 10, kDefaultReferenceMicroOhm ).verdict
           == Verdict::Reversed );
    CHECK( shuntFromReference( 4'000'000, -4'000'000, 10, kDefaultReferenceMicroOhm ).verdict
           == Verdict::Reversed );
    // just under, and exactly at, the reference floor
    CHECK(
        shuntFromReference( 4'000'000, kMinReferenceMicrovolts - 1, 10, kDefaultReferenceMicroOhm )
            .verdict
        == Verdict::ReferenceLow );
    CHECK( shuntFromReference( 1'000'000, kMinReferenceMicrovolts, 10, kDefaultReferenceMicroOhm )
               .verdict
           == Verdict::Ok );
    // just under the shunt floor
    CHECK( shuntFromReference( kMinShuntNanovolts - 1, 4'000'000, 10, kDefaultReferenceMicroOhm )
               .verdict
           == Verdict::SignalLow );
    // either side of the window, and its edge
    CHECK( pointFor( 102'000'000 ).verdict == Verdict::Ok );
    CHECK( pointFor( 102'100'000 ).verdict == Verdict::OutOfWindow );
    CHECK( pointFor( 97'900'000 ).verdict == Verdict::OutOfWindow );
}

TEST( "calmath", "rounded division refuses what it cannot divide" ) {
    CHECK( divideRounded( 5, 2 ) == 3 );
    CHECK( divideRounded( 4, 2 ) == 2 );
    CHECK( divideRounded( 5, 0 ) == 0 );
    CHECK( divideRounded( 5, -1 ) == 0 );
    CHECK( divideRounded( -5, 2 ) == 0 );
}

TEST( "calmath", "every verdict has its wire spelling" ) {
    CHECK_EQ( std::string( verdictName( Verdict::Ok ) ), "ok" );
    CHECK_EQ( std::string( verdictName( Verdict::NoSamples ) ), "no_samples" );
    CHECK_EQ( std::string( verdictName( Verdict::Reversed ) ), "reversed" );
    CHECK_EQ( std::string( verdictName( Verdict::ReferenceLow ) ), "reference_low" );
    CHECK_EQ( std::string( verdictName( Verdict::SignalLow ) ), "signal_low" );
    CHECK_EQ( std::string( verdictName( Verdict::OutOfWindow ) ), "out_of_window" );
    CHECK_EQ( std::string( verdictName( static_cast<Verdict>( 99 ) ) ), "no_samples" );
}
