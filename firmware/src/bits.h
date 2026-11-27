// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file bits.h
 * @ingroup bit_work
 * @brief Flag tests and register fields on `std::uint32_t`, so no narrow
 *        unsigned operand is promoted to `int`.
 *
 * A `std::uint8_t` or `std::uint16_t` that meets another narrow operand in a
 * bitwise, shift, arithmetic or comparison operator is promoted to `int`
 * first, which changes its signedness: MISRA C++:2023 7.0.5 forbids that. The
 * firmware's masks and bit positions are declared `std::uint32_t`, so a narrow
 * register value that meets one is converted to `std::uint32_t` (unsigned to
 * unsigned) instead, and the tests below take every operand as
 * `std::uint32_t`, so the conversion happens at the call, where the parameter
 * type is the target. `scripts/misra_rules.py` fails an operand whose
 * signedness promotion or the usual arithmetic conversions change
 * (`docs/misra.md`, "Held").
 */
#ifndef KLEIDOS_METER_BITS_H
#define KLEIDOS_METER_BITS_H

#include <cstdint>

namespace kleidos::meter::bits {

/// @addtogroup bit_work
/// @{

/// @brief Whether any bit of @p mask is set in @p value.
/// @param[in] value  A register value or a set of flags.
/// @param[in] mask   The bits to test.
/// @retval true   at least one bit of @p mask is set in @p value.
/// @retval false  none is, or @p mask is empty.
[[nodiscard]] constexpr bool hasBit( std::uint32_t value, std::uint32_t mask ) noexcept {
    return ( value & mask ) != 0U;
}

/// @brief The field of @p word that @p mask selects, moved down by @p shift.
/// @param[in] word   The register value.
/// @param[in] mask   The field's bits, in place.
/// @param[in] shift  The position of the field's lowest bit; below 32.
/// @return `( word & mask ) >> shift`.
[[nodiscard]] constexpr std::uint32_t field( std::uint32_t word, std::uint32_t mask,
                                             std::uint32_t shift ) noexcept {
    return ( word & mask ) >> shift;
}

/// @}

}  // namespace kleidos::meter::bits

#endif  // KLEIDOS_METER_BITS_H
