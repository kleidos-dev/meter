// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_identity.cpp
 * @brief sha256.h against the FIPS 180-4 example vectors, and identity.h's
 *        serial: the vector the host's registry code shares, and the eFuse
 *        read that fails.
 */
#include "bench.h"
#include "identity.h"
#include "scope.h"
#include "sha256.h"
#include "unit.h"

#include <array>
#include <cstdio>
#include <cstring>
#include <string>

using namespace kleidos::meter;
using bench::Bench;
using bench::field;
using bench::Scope;

namespace {

std::string hexDigest( const std::string& message, std::size_t chunk = 0 ) {
    sha256::Hasher hasher;
    const auto*    bytes = reinterpret_cast<const std::uint8_t*>( message.data() );
    if ( chunk == 0 ) {
        hasher.update( bytes, message.size() );
    } else {
        for ( std::size_t at = 0; at < message.size(); at += chunk ) {
            const std::size_t n = message.size() - at < chunk ? message.size() - at : chunk;
            hasher.update( bytes + at, n );
        }
    }
    std::uint8_t digest[sha256::kDigestBytes] = {};
    hasher.finish( digest );
    std::string text;
    char        pair[3] = {};
    for ( const std::uint8_t byte : digest ) {
        std::snprintf( pair, sizeof( pair ), "%02x", byte );
        text += pair;
    }
    return text;
}

}  // namespace

TEST( "identity", "SHA-256: the FIPS 180-4 examples and the padding edges" ) {
    CHECK_EQ( hexDigest( "" ), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" );
    CHECK_EQ( hexDigest( "abc" ),
              "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" );
    // 56 bytes: the length no longer fits the block, so the padding spills.
    CHECK_EQ( hexDigest( "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq" ),
              "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1" );
    // 55 bytes: the last length that fits; 64: a whole block, then padding.
    CHECK_EQ( hexDigest( std::string( 55, 'a' ) ),
              "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318" );
    CHECK_EQ( hexDigest( std::string( 64, 'a' ) ),
              "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb" );
    // The million a's, fed in pieces that straddle the blocks.
    CHECK_EQ( hexDigest( std::string( 1'000'000, 'a' ), 997 ),
              "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0" );
}

TEST( "identity", "the serial: a digest of the MAC, the vector the host shares" ) {
    char serial[identity::kSerialLength + 1] = {};
    // host/tests/test_registry.py checks the same three.
    identity::formatSerial( std::to_array<std::uint8_t>( { 0x02, 0x00, 0x5E, 0x10, 0x20, 0x30 } ),
                            serial );
    CHECK_EQ( std::string( serial ), "KM-EE529C1B03" );
    identity::formatSerial( std::to_array<std::uint8_t>( { 0, 0, 0, 0, 0, 0 } ), serial );
    CHECK_EQ( std::string( serial ), "KM-2E53FD936E" );
    identity::formatSerial( std::to_array<std::uint8_t>( { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF } ),
                            serial );
    CHECK_EQ( std::string( serial ), "KM-72B3D45A8D" );
}

TEST( "identity", "readSerial: the eFuse MAC, or 0 when it cannot be read" ) {
    Scope scope;
    char  serial[identity::kSerialLength + 1] = {};
    CHECK( identity::readSerial( serial ) );
    CHECK_EQ( std::string( serial ), "KM-EE529C1B03" );
    scope->failMac = true;
    CHECK( !identity::readSerial( serial ) );
    CHECK_EQ( std::string( serial ), "0" );
}

TEST( "identity", "*IDN? and SYSTem:INFormation? carry the serial and the image" ) {
    Bench b;
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0" );
    const std::string info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "serial" ), "KM-EE529C1B03" );
    CHECK_EQ( field( info, "image" ), "meter" );
    CHECK_EQ( field( info, "bypass_part" ), "relay2click" );
}

TEST( "identity", "an eFuse that cannot be read: serial 0, as IEEE 488.2 has it" ) {
    Bench b( []( sim::World& w ) { w.failMac = true; } );
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,0,0.18.0" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "serial" ), "0" );
}
