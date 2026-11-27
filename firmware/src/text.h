// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file text.h
 * @ingroup text_output
 * @brief A bounded writer over a caller's buffer: typed appends of text,
 *        characters and integers in the forms the wire uses, with no format
 *        string, no variadic call and nothing allocated.
 *
 * MISRA C++:2023 21.10.1 forbids `<cstdarg>` and 30.0.1 the C library's
 * formatted output, the printf family included (docs/misra.md, "Held"). The
 * firmware's responses, errors and records are written through this writer
 * instead (FW-24 to FW-26), each piece by the append of its type:
 *
 *     text::Writer w( buffer );       // a char array, or a std::span<char>
 *     w.text( "blanked=" );
 *     w.signedDecimal( 48 );
 *     w.field( "pixels", 4U );
 *     // buffer: "blanked=48 pixels=4"
 *
 * Each append is a statement of its own and returns nothing, so no result is
 * ever discarded (MISRA C++:2023 0.1.2, docs/misra.md "Held").
 *
 * The numbers are converted by `std::to_chars` (`<charconv>`), which formats
 * an integer into a range it is given and needs no locale, no allocation and
 * no exception; a scaled value (`scaled()`, `thousandths()`) is two integers
 * and a point, so no float is ever formatted.
 *
 * **Bounded.** The buffer always holds a NUL-terminated string. An append
 * that does not fit writes the part of it that does, as the C library's
 * bounded printf did, and marks the writer truncated (`truncated()`); no byte
 * goes past the buffer, and once it is full every later append writes nothing.
 * An empty buffer stays untouched. `size()` is the length written, which a
 * truncated writer holds at the buffer's size less the NUL.
 *
 * The wire's bytes did not change with it: the golden transcript of
 * `firmware/test/native/test_golden.cpp` holds every response, error and
 * record byte for byte (docs/testing.md).
 */
#ifndef KLEIDOS_METER_TEXT_H
#define KLEIDOS_METER_TEXT_H

#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace kleidos::meter::text {

/// @addtogroup text_output
/// @{

/// @brief The case of the letters a hexadecimal number is written with.
enum class LetterCase : std::uint8_t {
    Upper,  ///< `A` to `F`, as a register's value is written (`0x5449`).
    Lower,  ///< `a` to `f`, as a digest or a CRC is written (`crc32=36b3aab0`).
};

/// @brief The most decimals `Writer::scaled()` writes: 10^18 is the largest
///        power of ten a `std::uint64_t` holds.
constexpr std::uint32_t kMaxDecimals = 18U;

/// @brief A bounded writer of NUL-terminated text over a caller's buffer.
///
/// The writer owns nothing: the buffer is the caller's and must outlive it.
class Writer {
   public:
    /// @brief Starts an empty string in @p out.
    /// @param[out] out The buffer; its first byte becomes the NUL when it has
    ///                 one, and an empty one is never written.
    explicit Writer( std::span<char> out ) noexcept;

    Writer( const Writer& )            = delete;  ///< One writer per buffer.
    Writer& operator=( const Writer& ) = delete;  ///< One writer per buffer.
    Writer( Writer&& )                 = delete;  ///< Never moved.
    Writer& operator=( Writer&& )      = delete;  ///< Never moved.
    ~Writer()                          = default;

    /// @brief Appends @p piece, or as much of it as fits.
    /// @param[in] piece The characters, as they are.
    void text( std::string_view piece ) noexcept;

    /// @brief Appends one character.
    /// @param[in] c The character.
    void character( char c ) noexcept;

    /// @brief Appends @p value in decimal, with no sign and no padding.
    /// @param[in] value The value.
    void unsignedDecimal( std::uint64_t value ) noexcept;

    /// @brief Appends @p value in decimal, a `-` ahead of a negative one.
    /// @param[in] value The value, `INT64_MIN` included.
    void signedDecimal( std::int64_t value ) noexcept;

    /// @brief Appends @p value in decimal, padded with leading zeros to
    ///        @p width digits.
    /// @param[in] value The value.
    /// @param[in] width The fewest digits written, at most 24; a value with
    ///                  more digits is written whole.
    void zeroPadded( std::uint64_t value, std::size_t width ) noexcept;

    /// @brief Appends @p value in hexadecimal, with no prefix, padded with
    ///        leading zeros to @p width digits.
    /// @param[in] value       The value.
    /// @param[in] width       The fewest digits written, at most 24; a value
    ///                        with more digits is written whole.
    /// @param[in] letterCase  The case of the digits `A` to `F`.
    void hex( std::uint64_t value, std::size_t width, LetterCase letterCase ) noexcept;

    /// @brief Appends @p value divided by 10^@p decimals, as the SCPI
    ///        responses write a value carried in a smaller unit: the whole
    ///        part, then, when @p decimals is not 0, a point and the fraction
    ///        with its trailing zeros removed down to one digit.
    ///
    /// 88637490694 at 12 decimals is `0.088637490694`, 25000 at 3 is `25.0`,
    /// 0 at 9 is `0.0`, 42 at 0 is `42`, -1500 at 3 is `-1.5`.
    /// @param[in] value    The value, in units of 10^-@p decimals.
    /// @param[in] decimals The decimals the unit carries; above kMaxDecimals,
    ///                     kMaxDecimals.
    void scaled( std::int64_t value, std::uint32_t decimals ) noexcept;

    /// @brief Appends @p value divided by 1000 with all three decimals, as a
    ///        stream record writes a value (`5000.449`, `25.000`, `-0.500`).
    /// @param[in] value The value, in thousandths.
    void thousandths( std::int64_t value ) noexcept;

    /// @brief Opens a report's `key=value` pair: a space when the string is
    ///        not empty, @p name and `=`; the value is the next append.
    /// @param[in] name The key.
    void key( std::string_view name ) noexcept;

    /// @brief Appends a report's `key=value` pair whose value is a number:
    ///        key(), then unsignedDecimal().
    /// @param[in] name  The key.
    /// @param[in] value The value.
    void field( std::string_view name, std::uint64_t value ) noexcept;

    /// @brief Appends a report's `key=value` pair whose value is a word:
    ///        key(), then text().
    /// @param[in] name  The key.
    /// @param[in] value The value.
    void field( std::string_view name, std::string_view value ) noexcept;

    /// @brief The length of the string written, excluding its NUL.
    /// @return At most the buffer's size less one; 0 for an empty buffer.
    std::size_t size() const noexcept;

    /// @brief The string written, as a view of the caller's buffer.
    /// @return The first size() characters of the buffer, which a NUL
    ///         follows; empty for an empty buffer.
    std::string_view view() const noexcept;

    /// @brief Whether an append did not fit whole.
    /// @retval true  Some append was cut short or dropped.
    /// @retval false Everything appended is in the buffer.
    bool truncated() const noexcept;

   private:
    /// @brief How many more characters fit before the NUL.
    /// @return The buffer's size less one less what is written; 0 for an
    ///         empty buffer.
    std::size_t room() const noexcept;

    std::span<char> out_;                ///< The caller's buffer.
    std::size_t     used_      = 0;      ///< Characters written, before the NUL.
    bool            truncated_ = false;  ///< An append did not fit whole.
};

/// @}

}  // namespace kleidos::meter::text

#endif  // KLEIDOS_METER_TEXT_H
