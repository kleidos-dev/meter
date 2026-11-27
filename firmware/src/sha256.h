// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file sha256.h
 * @ingroup instrument_identity
 * @brief SHA-256 (FIPS 180-4), small and self-contained, for the instrument's
 *        serial (`identity.h`).
 *
 * The serial is a digest of the factory MAC, computed once at boot over 29
 * bytes. That needs no hardware accelerator and no PSA key store, so the hash
 * is written out here, as plain integer code that the host tests run
 * unchanged and pin with the FIPS 180-4 example vectors
 * (`firmware/test/native/test_identity.cpp`). It is not used for anything that
 * must resist an attacker: the serial is a public name, not a secret.
 */
#ifndef KLEIDOS_METER_SHA256_H
#define KLEIDOS_METER_SHA256_H

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::sha256 {

/// @addtogroup instrument_identity
/// @{

/// The digest's length in bytes.
constexpr std::size_t kDigestBytes = 32;

/// @brief The hash's internals, used by Hasher.
namespace detail {

/// The 64 round constants K0..K63 (FIPS 180-4 SS 4.2.2).
constexpr std::uint32_t kRound[64] = {
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU, 0x59f111f1U, 0x923f82a4U,
    0xab1c5ed5U, 0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU,
    0x9bdc06a7U, 0xc19bf174U, 0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU, 0x2de92c6fU,
    0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU, 0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
    0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U, 0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU,
    0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U, 0xa2bfe8a1U, 0xa81a664bU,
    0xc24b8b70U, 0xc76c51a3U, 0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U, 0x19a4c116U,
    0x1e376c08U, 0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
    0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U, 0x90befffaU, 0xa4506cebU, 0xbef9a3f7U,
    0xc67178f2U,
};

/// @brief Rotates @p x right by @p n bits (FIPS 180-4 ROTR).
/// @param[in] x The word.
/// @param[in] n The rotation, 1 to 31.
/// @return @p x rotated right by @p n.
constexpr std::uint32_t rotr( std::uint32_t x, std::uint32_t n ) noexcept {
    return ( x >> n ) | ( x << ( 32U - n ) );
}

}  // namespace detail

/// @brief An incremental SHA-256: update() any number of times, then finish()
///        once.
class Hasher {
   public:
    /// @brief Starts an empty message at the FIPS 180-4 initial hash value.
    constexpr Hasher() noexcept = default;

    /// @brief Adds @p length bytes of @p data to the message.
    /// @param[in] data   The bytes to add.
    /// @param[in] length How many bytes of @p data to add.
    constexpr void update( const std::uint8_t* data, std::size_t length ) noexcept {
        for ( std::size_t i = 0; i < length; ++i ) {
            block_[used_++] = data[i];
            if ( used_ == sizeof( block_ ) ) {
                compress();
                used_ = 0;
            }
        }
        bits_ += static_cast<std::uint64_t>( length ) * 8U;
    }

    /// @brief Pads the message, and writes its digest to @p out.
    /// @param[out] out The kDigestBytes-byte digest.
    constexpr void finish( std::uint8_t ( &out )[kDigestBytes] ) noexcept {
        const std::uint64_t bits = bits_;
        block_[used_++]          = 0x80U;
        if ( used_ > 56U ) {
            while ( used_ < sizeof( block_ ) ) {
                block_[used_++] = 0;
            }
            compress();
            used_ = 0;
        }
        while ( used_ < 56U ) {
            block_[used_++] = 0;
        }
        for ( std::uint32_t i = 0; i < 8U; ++i ) {
            block_[56U + i] = static_cast<std::uint8_t>( bits >> ( 56U - 8U * i ) );
        }
        compress();
        for ( std::uint32_t i = 0; i < 8U; ++i ) {
            for ( std::uint32_t b = 0; b < 4U; ++b ) {
                out[4U * i + b] = static_cast<std::uint8_t>( state_[i] >> ( 24U - 8U * b ) );
            }
        }
    }

   private:
    /// @brief Runs the compression function over the full 64-byte block in
    /// block_, updating state_.
    constexpr void compress() noexcept {
        std::uint32_t w[64] = {};
        for ( std::size_t t = 0; t < 16U; ++t ) {
            w[t] = ( static_cast<std::uint32_t>( block_[4U * t] ) << 24U )
                   | ( static_cast<std::uint32_t>( block_[4U * t + 1U] ) << 16U )
                   | ( static_cast<std::uint32_t>( block_[4U * t + 2U] ) << 8U )
                   | static_cast<std::uint32_t>( block_[4U * t + 3U] );
        }
        for ( std::uint32_t t = 16; t < 64U; ++t ) {
            const std::uint32_t s0 = detail::rotr( w[t - 15U], 7 ) ^ detail::rotr( w[t - 15U], 18 )
                                     ^ ( w[t - 15U] >> 3U );
            const std::uint32_t s1 = detail::rotr( w[t - 2U], 17 ) ^ detail::rotr( w[t - 2U], 19 )
                                     ^ ( w[t - 2U] >> 10U );
            w[t]                   = w[t - 16U] + s0 + w[t - 7U] + s1;
        }
        std::uint32_t a = state_[0];
        std::uint32_t b = state_[1];
        std::uint32_t c = state_[2];
        std::uint32_t d = state_[3];
        std::uint32_t e = state_[4];
        std::uint32_t f = state_[5];
        std::uint32_t g = state_[6];
        std::uint32_t h = state_[7];
        for ( std::uint32_t t = 0; t < 64U; ++t ) {
            const std::uint32_t s1 =
                detail::rotr( e, 6 ) ^ detail::rotr( e, 11 ) ^ detail::rotr( e, 25 );
            const std::uint32_t choose = ( e & f ) ^ ( ~e & g );
            const std::uint32_t t1     = h + s1 + choose + detail::kRound[t] + w[t];
            const std::uint32_t s0 =
                detail::rotr( a, 2 ) ^ detail::rotr( a, 13 ) ^ detail::rotr( a, 22 );
            const std::uint32_t majority = ( a & b ) ^ ( a & c ) ^ ( b & c );
            const std::uint32_t t2       = s0 + majority;
            h                            = g;
            g                            = f;
            f                            = e;
            e                            = d + t1;
            d                            = c;
            c                            = b;
            b                            = a;
            a                            = t1 + t2;
        }
        state_[0] += a;
        state_[1] += b;
        state_[2] += c;
        state_[3] += d;
        state_[4] += e;
        state_[5] += f;
        state_[6] += g;
        state_[7] += h;
    }

    /// The hash state, H0 to H7, from FIPS 180-4's initial value.
    std::uint32_t state_[8]  = { 0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
                                 0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U };
    std::uint8_t  block_[64] = {};  ///< The block being filled.
    std::size_t   used_      = 0;   ///< Its bytes so far.
    std::uint64_t bits_      = 0;   ///< The message length so far, in bits.
};

/// @}

}  // namespace kleidos::meter::sha256

#endif  // KLEIDOS_METER_SHA256_H
