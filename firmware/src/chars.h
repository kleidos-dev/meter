// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file chars.h
 * @ingroup characters
 * @brief Character classes, case folding, digit values and the bytes a
 *        character stands for, with no character's numerical value used.
 *
 * MISRA C++:2023 7.0.3 forbids using the numerical value of a character: no
 * `c - '0'`, no `c >= 'A' && c <= 'Z'`, no `static_cast<unsigned char>( c )`,
 * no `static_cast<char>( byte )`. What the firmware needs of a character
 * comes from here instead:
 *
 *  - the class of an ASCII character (white space, digit, letter, printable)
 *    and its value as a digit, read from kTable, a constexpr table of 128
 *    entries built at compile time from strings that spell each class
 *    (kDigits, kLowerLetters, kSpaces), so no entry is computed from a
 *    character code;
 *  - the upper case of a letter, the letter of kDigits at its value;
 *  - the byte a character stands for on the wire and back, through
 *    `std::char_traits<char>`'s conversions, the ones the rule's
 *    implementations accept (MathWorks' page for 7.0.3).
 *
 * The table is indexed by `std::char_traits<char>::to_int_type()`, the
 * character's byte as a non-negative int, checked against the table's size
 * first: a byte above 0x7F belongs to no class. `scripts/misra_rules.py`
 * fails a conversion between a character type and any other type anywhere
 * in `firmware/src`, this file included, whose conversions are all
 * `std::char_traits` calls (`docs/misra.md`, "Held").
 */
#ifndef KLEIDOS_METER_CHARS_H
#define KLEIDOS_METER_CHARS_H

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace kleidos::meter::chars {

/// @addtogroup characters
/// @{

/// The digits of every base up to 36, in order: a digit's value is its index.
/// `0` to `9`, then the upper-case letters, so a letter's upper case is
/// kDigits at its value.
inline constexpr std::string_view kDigits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";

/// The lower-case letters, in order: `a` stands at index 0, the value of `A`
/// less 10.
inline constexpr std::string_view kLowerLetters = "abcdefghijklmnopqrstuvwxyz";

/// What separates SCPI elements: space, tab, carriage return, newline.
inline constexpr std::string_view kSpaces = " \t\r\n";

/// The value of the first letter in kDigits, `A`.
constexpr std::uint8_t kFirstLetterValue = 10U;

/// How many decimal digits kDigits starts with.
constexpr std::size_t kDecimalDigits = 10U;

/// The characters the table describes: 7-bit ASCII.
constexpr std::size_t kAsciiCount = 128U;

/// The first printable ASCII character, the space, by code.
constexpr std::size_t kFirstPrintable = 0x20U;

/// The last printable ASCII character, the tilde, by code: 0x7F (DEL) is a
/// control character.
constexpr std::size_t kLastPrintable = 0x7EU;

/// What kTable says of one ASCII character.
struct Traits {
    bool         space     = false;  ///< In kSpaces.
    bool         digit     = false;  ///< `0` to `9`.
    bool         upper     = false;  ///< `A` to `Z`.
    bool         lower     = false;  ///< `a` to `z`.
    bool         printable = false;  ///< 0x20 to 0x7E: the space and the graphic characters.
    std::uint8_t value = 0U;  ///< Its value as a digit of base 36: 0 to 35 for a digit or a letter.
};

/// @brief The index of @p c in a table of kAsciiCount entries, at compile
/// time: its byte, through `std::char_traits<char>::to_int_type()`.
/// @param[in] c An ASCII character.
/// @return Its code, below kAsciiCount for every character this file spells.
consteval std::size_t indexOf( char c ) noexcept {
    return static_cast<std::size_t>( std::char_traits<char>::to_int_type( c ) );
}

/// @brief Builds kTable from the strings that spell each class.
/// @return Every ASCII character's traits.
consteval std::array<Traits, kAsciiCount> makeTable() noexcept {
    std::array<Traits, kAsciiCount> table{};
    for ( std::size_t code = kFirstPrintable; code <= kLastPrintable; ++code ) {
        table[code].printable = true;
    }
    for ( const char space : kSpaces ) {
        table[indexOf( space )].space = true;
    }
    for ( std::size_t i = 0U; i < kDigits.size(); ++i ) {
        Traits& traits = table[indexOf( kDigits[i] )];
        traits.digit   = i < kDecimalDigits;
        traits.upper   = i >= kDecimalDigits;
        traits.value   = static_cast<std::uint8_t>( i );
    }
    for ( std::size_t i = 0U; i < kLowerLetters.size(); ++i ) {
        Traits& traits = table[indexOf( kLowerLetters[i] )];
        traits.lower   = true;
        traits.value   = static_cast<std::uint8_t>( i + kFirstLetterValue );
    }
    return table;
}

/// Every ASCII character's class and digit value, built at compile time.
inline constexpr std::array<Traits, kAsciiCount> kTable = makeTable();

/// @brief What kTable says of @p c.
/// @param[in] c Any character.
/// @return Its entry, or an entry of no class for a byte above 0x7F.
constexpr Traits traitsOf( char c ) noexcept {
    const auto code = static_cast<std::size_t>( std::char_traits<char>::to_int_type( c ) );
    return ( code < kAsciiCount ) ? kTable[code] : Traits{};
}

/// @brief Whether @p c separates SCPI elements.
/// @param[in] c The character.
/// @retval true  A space, tab, carriage return or newline.
/// @retval false Anything else.
constexpr bool isSpace( char c ) noexcept {
    return traitsOf( c ).space;
}

/// @brief Whether @p c is a decimal digit.
/// @param[in] c The character.
/// @retval true  `0` to `9`.
/// @retval false Anything else.
constexpr bool isDigit( char c ) noexcept {
    return traitsOf( c ).digit;
}

/// @brief Whether @p c is an upper-case ASCII letter.
/// @param[in] c The character.
/// @retval true  `A` to `Z`.
/// @retval false Anything else.
constexpr bool isUpper( char c ) noexcept {
    return traitsOf( c ).upper;
}

/// @brief Whether @p c is an ASCII letter of either case.
/// @param[in] c The character.
/// @retval true  `A` to `Z` or `a` to `z`.
/// @retval false Anything else.
constexpr bool isLetter( char c ) noexcept {
    const Traits traits = traitsOf( c );
    return traits.upper || traits.lower;
}

/// @brief Whether @p c is printable ASCII: the space or a graphic character.
/// @param[in] c The character.
/// @retval true  0x20 to 0x7E.
/// @retval false A control character, DEL, or a byte above 0x7F.
constexpr bool isPrintable( char c ) noexcept {
    return traitsOf( c ).printable;
}

/// @brief Whether @p c is a graphic ASCII character: printable and not the
/// space.
/// @param[in] c The character.
/// @retval true  0x21 to 0x7E.
/// @retval false The space, a control character, DEL, or a byte above 0x7F.
constexpr bool isGraphic( char c ) noexcept {
    return isPrintable( c ) && ( c != ' ' );
}

/// @brief @p c in upper case, when it is an ASCII letter.
/// @param[in] c The character.
/// @return The upper-case letter, or @p c unchanged.
constexpr char toUpper( char c ) noexcept {
    const Traits traits = traitsOf( c );
    return traits.lower ? kDigits[traits.value] : c;
}

/// @brief The byte @p c stands for on the wire.
/// @param[in] c The character.
/// @return Its byte, 0 to 255.
constexpr std::uint8_t byteOf( char c ) noexcept {
    return static_cast<std::uint8_t>( std::char_traits<char>::to_int_type( c ) );
}

/// @brief The character @p byte stands for.
/// @param[in] byte A byte received.
/// @return The character of that code.
constexpr char fromByte( std::uint8_t byte ) noexcept {
    return std::char_traits<char>::to_char_type(
        static_cast<std::char_traits<char>::int_type>( byte ) );
}

/// @brief The NUL-terminated text a character array holds, as a view.
///
/// Read through a `std::span` of the whole array, so the array is never
/// handed on as a bare pointer (MISRA C++:2023 7.11.2) and a missing NUL
/// cannot take the view past its end: the view then is the whole array.
/// @tparam N The array's size.
/// @param[in] text The array.
/// @return Its characters up to the first NUL, or all of them.
template <std::size_t N>
constexpr std::string_view untilNul( const char ( &text )[N] ) noexcept {
    const std::span<const char> all( text );
    const auto                  end = std::find( all.begin(), all.end(), '\0' );
    return { all.data(), static_cast<std::size_t>( end - all.begin() ) };
}

/// @brief untilNul() of a `std::array` of characters.
/// @tparam N The array's size.
/// @param[in] text The array.
/// @return Its characters up to the first NUL, or all of them.
template <std::size_t N>
constexpr std::string_view untilNul( const std::array<char, N>& text ) noexcept {
    const std::span<const char> all( text );
    const auto                  end = std::find( all.begin(), all.end(), '\0' );
    return { all.data(), static_cast<std::size_t>( end - all.begin() ) };
}

/// @}

}  // namespace kleidos::meter::chars

#endif  // KLEIDOS_METER_CHARS_H
