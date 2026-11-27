// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_bits.cpp
 * @brief Host tests of the flag tests and field reads on `std::uint32_t`
 *        (src/bits.h): a narrow register value and a mask meet there.
 *
 * Build and run: `make -C firmware/test/native`.
 */
#include "bits.h"
#include "unit.h"

#include <cstdint>

using namespace kleidos::meter::bits;

// Both helpers are compile-time functions.
static_assert( hasBit( 0x8001U, 0x8000U ) && !hasBit( 0x8001U, 0x0002U ) );
static_assert( field( 0x2281U, 0xFFF0U, 4U ) == 0x228U );

TEST( "bits", "a set bit of the mask is found and a clear one is not (MC/DC)" ) {
    const std::uint16_t word = 0xC001U;  // DIAG_ALRT's conversion-ready word
    CHECK( hasBit( word, 0x0001U ) );
    CHECK( hasBit( word, 0x8000U ) );
    CHECK( !hasBit( word, 0x0002U ) );
    CHECK( hasBit( word, 0x0003U ) );  // any bit of the mask is enough
}

TEST( "bits", "an empty mask finds nothing, and a narrow value is read whole" ) {
    const std::uint8_t flags = 0xFFU;
    CHECK( !hasBit( flags, 0U ) );
    CHECK( hasBit( flags, 0x80U ) );
    CHECK( !hasBit( flags, 0x100U ) );  // above the narrow type's bits
}

TEST( "bits", "a field is masked, then moved down" ) {
    const std::uint16_t deviceId = 0x2281U;  // DIEID 0x228, REV_ID 1
    CHECK( field( deviceId, 0xFFF0U, 4U ) == 0x228U );
    CHECK( field( deviceId, 0x000FU, 0U ) == 0x1U );
    const std::uint8_t byte = 0xA5U;
    CHECK( field( byte, 0xF0U, 4U ) == 0xAU );
    CHECK( field( 0xFFFFFFFFU, 0x80000000U, 31U ) == 1U );
}
