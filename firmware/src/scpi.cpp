// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file scpi.cpp
 * @ingroup scpi_parser
 * @brief The SCPI / IEEE 488.2 layer. See scpi.h; the command surface is
 *        `docs/scpi.md`.
 */
#include "scpi.h"

#include "chars.h"
#include "text.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <span>
#include <string_view>
#include <system_error>

namespace kleidos::meter::scpi {

namespace {

// --- Characters -------------------------------------------------------------

/// @brief Whether @p c opens or closes a SCPI string.
/// @param[in] c The character.
/// @retval true  A double or a single quote.
/// @retval false Anything else.
constexpr bool isQuote( char c ) noexcept {
    return ( c == '"' ) || ( c == '\'' );
}

/// @brief Copies at most @p capacity - 1 bytes of @p text and terminates.
/// @param[out] out      The destination.
/// @param[in]  capacity Its size in bytes, the terminator included; at least 1
///                      (PathResolver's two calls: resolve() refuses 0 first,
///                      and its own path buffer is a std::array).
/// @param[in]  text     The bytes to copy.
/// @return The length copied.
std::size_t copyBounded( char* out, std::size_t capacity, std::string_view text ) noexcept {
    const std::size_t n = std::min( text.size(), capacity - 1U );
    (void)std::copy_n( text.cbegin(), n, out );
    out[n] = '\0';
    return n;
}

/// @brief The @p length characters of @p text from @p start.
///
/// Not `std::string_view::substr()`, whose range check throws
/// `std::out_of_range` and so links libstdc++'s exception runtime and its
/// allocator into the image (`scripts/check_heap_objects.py --elf` fails
/// it); every caller has bounded both already.
/// @param[in] text   The text.
/// @param[in] start  The first character's index, at most `text.size()`.
/// @param[in] length How many, at most `text.size() - start`.
/// @return The view of those characters.
constexpr std::string_view slice( std::string_view text, std::size_t start,
                                  std::size_t length ) noexcept {
    text.remove_prefix( start );
    text.remove_suffix( text.size() - length );
    return text;
}

/// @brief The run of decimal digits @p text starts with.
/// @param[in] text The text.
/// @return The digits, possibly none.
constexpr std::string_view leadingDigits( std::string_view text ) noexcept {
    std::size_t length = 0;
    while ( ( length < text.size() ) && chars::isDigit( text[length] ) ) {
        ++length;
    }
    return slice( text, 0, length );
}

/// @brief Parses all of @p text as an unsigned number in @p base, with
/// `std::from_chars`.
/// @tparam Number The unsigned type to parse into.
/// @param[in]  text  The text.
/// @param[out] value The number, when the result says it was parsed.
/// @param[in]  base  The base, 2 to 36.
/// @return `std::from_chars`'s result: where it stopped, and why.
template <typename Number>
std::from_chars_result parseNumber( std::string_view text, Number& value,
                                    std::int32_t base ) noexcept {
    return std::from_chars( text.data(), text.data() + text.size(), value, base );
}

/// @brief ASCII case-insensitive equality of two views.
/// @param[in] a First text.
/// @param[in] b Second text.
/// @retval true  The texts have the same length and letters, ignoring case.
/// @retval false They differ.
constexpr bool sameIgnoringCase( std::string_view a, std::string_view b ) noexcept {
    if ( a.size() != b.size() ) {
        return false;
    }
    for ( std::size_t k = 0; k < a.size(); ++k ) {
        if ( chars::toUpper( a[k] ) != chars::toUpper( b[k] ) ) {
            return false;
        }
    }
    return true;
}

// --- Errors -----------------------------------------------------------------

/// One error code and its SCPI text.
struct ErrorName {
    std::int16_t     code = err::kNone;  ///< The code (err).
    std::string_view text = {};          ///< Its text, as `SYSTem:ERRor?` answers it.
};

/// A lookup table rather than a switch, so the numbering is data.
constexpr std::array<ErrorName, 41> kErrorNames = { {
    { err::kNone, "No error" },
    { err::kCommand, "Command error" },
    { err::kInvalidCharacter, "Invalid character" },
    { err::kSyntax, "Syntax error" },
    { err::kDataType, "Data type error" },
    { err::kParameterNotAllowed, "Parameter not allowed" },
    { err::kMissingParameter, "Missing parameter" },
    { err::kUndefinedHeader, "Undefined header" },
    { err::kSuffixOutOfRange, "Header suffix out of range" },
    { err::kNumericData, "Numeric data error" },
    { err::kStringData, "String data error" },
    { err::kExecution, "Execution error" },
    { err::kSettingsConflict, "Settings conflict" },
    { err::kDataOutOfRange, "Data out of range" },
    { err::kIllegalParameter, "Illegal parameter value" },
    { err::kDataStale, "Data corrupt or stale" },
    { err::kHardware, "Hardware error" },
    { err::kHardwareMissing, "Hardware missing" },
    { err::kDeviceSpecific, "Device-specific error" },
    { err::kQueueOverflow, "Queue overflow" },
    { err::kInputOverrun, "Input buffer overrun" },
    { err::kQuery, "Query error" },
    { err::kRelaysUnavailable, "Relays unavailable" },
    { err::kShuntBypassed, "Shunt bypassed" },
    { err::kNullNotStored, "Null not stored" },
    { err::kBypassCheckFailed, "Bypass check failed" },
    { err::kLeadCheckFailed, "Lead check failed" },
    { err::kMarksFull, "Marks full" },
    { err::kCalibrationNotSaved, "Calibration not saved" },
    { err::kSamplerFailed, "Sampler not running" },
    { err::kZeroNoSamples, "Null took no samples" },
    { err::kOrderDisconnects, "Order disconnects the DUT" },
    { err::kOrderBypasses, "Order bypasses the shunt" },
    { err::kNotRunning, "Not running" },
    { err::kWindowRunning, "Null window running" },
    { err::kNullPathReleased, "Null path released" },
    { err::kLeadReclosed, "Lead reclosed" },
    { err::kBypassOpened, "Bypass opened" },
    { err::kResetReleased, "Relays released by reset" },
    { err::kPinMapReverted, "Pin map reverted" },
    { err::kCommand, "Command error" },
} };

// --- Patterns ---------------------------------------------------------------

/// The most nodes a header or a pattern may have.
constexpr std::size_t kMaxSegments = 8;

/// One node of a command pattern.
struct Segment {
    std::string_view name     = {};     ///< The mnemonic, its short form in capitals, no `#`.
    bool             optional = false;  ///< In brackets: the header may leave it out.
    bool             suffix   = false;  ///< Ends in `#`: it takes a numeric suffix.
};

/// @brief Cuts a pattern (no `?`) into its nodes.
/// @param[in]  pattern The pattern.
/// @param[out] out     The nodes, kMaxSegments at most.
/// @return The node count, or 0 when the pattern is malformed or has too
///         many nodes.
std::size_t splitPattern( std::string_view                 pattern,
                          std::span<Segment, kMaxSegments> out ) noexcept {
    std::size_t count = 0;
    std::size_t i     = 0;
    while ( i < pattern.size() ) {
        Segment segment;
        if ( pattern[i] == '[' ) {
            segment.optional = true;
            ++i;
        }
        if ( ( i < pattern.size() ) && ( pattern[i] == ':' ) ) {
            ++i;
        }
        const std::size_t start = i;
        while ( ( i < pattern.size() ) && ( pattern[i] != ':' ) && ( pattern[i] != '[' )
                && ( pattern[i] != ']' ) ) {
            ++i;
        }
        segment.name = slice( pattern, start, i - start );
        if ( segment.name.ends_with( '#' ) ) {
            segment.suffix = true;
            segment.name.remove_suffix( 1 );
        }
        if ( segment.optional ) {
            if ( ( i >= pattern.size() ) || ( pattern[i] != ']' ) ) {
                return 0;
            }
            ++i;
        }
        if ( segment.name.empty() || ( count == kMaxSegments ) ) {
            return 0;
        }
        out[count] = segment;
        ++count;
    }
    return count;
}

/// @brief Whether @p token (alpha part only) is @p name's long or short form.
/// @param[in] name  The pattern's mnemonic.
/// @param[in] token The header's node.
/// @retval true  The token spells either form, in any case.
/// @retval false It spells neither.
bool formMatches( std::string_view name, std::string_view token ) noexcept {
    std::size_t shortLength = 0;
    while ( ( shortLength < name.size() )
            && ( chars::isUpper( name[shortLength] ) || chars::isDigit( name[shortLength] ) ) ) {
        ++shortLength;
    }
    return sameIgnoringCase( name, token )
           || ( ( shortLength > 0U ) && sameIgnoringCase( slice( name, 0, shortLength ), token ) );
}

/// The largest numeric suffix a header node may carry: six digits. The
/// parser read digits while the value so far was at most 99999, so the
/// sixth digit was the last one taken.
constexpr std::uint32_t kMaxSuffix = 999999U;

/// @brief Matches one header node against one pattern node, reading its
/// suffix.
/// @param[in]     segment The pattern's node.
/// @param[in]     token   The header's node.
/// @param[in,out] suffix  Set when the node carries a numeric suffix.
/// @retval true  The node matches.
/// @retval false It does not, or its suffix exceeds kMaxSuffix.
bool nodeMatches( const Segment& segment, std::string_view token, Suffix& suffix ) noexcept {
    std::size_t alpha = token.size();
    while ( ( alpha > 0U ) && chars::isDigit( token[alpha - 1U] ) ) {
        --alpha;
    }
    // A mnemonic whose own spelling ends in a digit is not a suffix: try the
    // whole token first.
    if ( formMatches( segment.name, token ) ) {
        return true;
    }
    if ( ( alpha == token.size() ) || !segment.suffix || ( alpha == 0U ) ) {
        return false;
    }
    if ( !formMatches( segment.name, slice( token, 0, alpha ) ) ) {
        return false;
    }
    // Digits only, at least one: from_chars stops at nothing but a value past
    // 32 bits, which is past kMaxSuffix too.
    std::uint32_t value  = 0;
    const auto    result = parseNumber( slice( token, alpha, token.size() - alpha ), value, 10 );
    if ( ( result.ec != std::errc{} ) || ( value > kMaxSuffix ) ) {
        return false;
    }
    suffix.value   = static_cast<std::int32_t>( value );
    suffix.present = true;
    return true;
}

/// A header cut into its nodes, as matchFrom() reads it.
struct HeaderTokens {
    std::array<std::string_view, kMaxSegments> node  = {};  ///< Each node.
    std::size_t                                count = 0;   ///< How many nodes.
};

/// One level of matchFrom()'s backtracking: a pattern node, the header node
/// it is tried against, and which way it is tried next.
struct Choice {
    /// The ways a pattern node is tried, in order.
    enum class Next : std::uint8_t {
        Consume,  ///< Match it against the header node, then go on.
        Skip,     ///< Leave an optional node out, then go on.
        Done,     ///< Both tried: back up to the node before.
    };
    std::size_t t      = 0;              ///< The header node this level starts at.
    Suffix      suffix = {};             ///< The suffix the match has so far.
    Next        next   = Next::Consume;  ///< The next way to try.
};

/// @brief Whether the header's nodes match the pattern's, trying each optional
/// node first present and then absent, and the suffix of the first match.
///
/// A depth-first search by iterative backtracking over a fixed stack, one
/// level per pattern node (MISRA C++:2023 8.2.10: no recursion): level @c s
/// holds where node @c s starts in the header and which way it is tried next.
/// A level that matches pushes the next one; one with no way left pops back
/// to the one before. The order is the recursion's it replaced, so the match
/// and its suffix are the same: a node consumes its header node before it is
/// skipped. Every push is one node further, so the stack holds at most
/// kMaxSegments + 1 levels, and the search ends: each level is tried at most
/// twice per visit of the level before.
/// @param[in]     segments     The pattern's nodes.
/// @param[in]     segmentCount How many, at most kMaxSegments.
/// @param[in]     tokens       The header's nodes.
/// @param[in,out] suffix       The suffix of the match, when it has one.
/// @retval true  The header matches the pattern.
/// @retval false It does not.
bool matchFrom( std::span<const Segment> segments, std::size_t segmentCount,
                const HeaderTokens& tokens, Suffix& suffix ) noexcept {
    std::array<Choice, kMaxSegments + 1> stack = {};
    std::size_t                          s     = 0;
    stack[0].suffix                            = suffix;
    while ( true ) {
        Choice& level   = stack[s];
        bool    pushed  = false;
        Choice  advance = {};
        if ( s == segmentCount ) {
            if ( level.t == tokens.count ) {
                suffix = level.suffix;
                return true;
            }
        } else if ( level.next == Choice::Next::Consume ) {
            level.next   = Choice::Next::Skip;
            Suffix tried = level.suffix;
            if ( level.t < tokens.count ) {
                const bool matched = nodeMatches( segments[s], tokens.node[level.t], tried );
                if ( matched ) {
                    advance.t      = level.t + 1U;
                    advance.suffix = tried;
                    pushed         = true;
                }
            }
        } else if ( level.next == Choice::Next::Skip ) {
            level.next = Choice::Next::Done;
            if ( segments[s].optional ) {
                advance.t      = level.t;
                advance.suffix = level.suffix;
                pushed         = true;
            }
        } else {
            // Both ways tried at this node: backtrack below.
        }
        if ( pushed ) {
            ++s;
            stack[s] = advance;
        } else if ( level.next == Choice::Next::Done || s == segmentCount ) {
            if ( s == 0U ) {
                return false;
            }
            --s;
        } else {
            // A way is left at this node: try it on the next pass.
        }
    }
}

// --- Numbers ----------------------------------------------------------------

/// The powers of ten from 10^0 to 10^18, every one an int64 can hold.
constexpr std::array<std::uint64_t, 19> kPow10 = { 1ULL,
                                                   10ULL,
                                                   100ULL,
                                                   1000ULL,
                                                   10000ULL,
                                                   100000ULL,
                                                   1000000ULL,
                                                   10000000ULL,
                                                   100000000ULL,
                                                   1000000000ULL,
                                                   10000000000ULL,
                                                   100000000000ULL,
                                                   1000000000000ULL,
                                                   10000000000000ULL,
                                                   100000000000000ULL,
                                                   1000000000000000ULL,
                                                   10000000000000000ULL,
                                                   100000000000000000ULL,
                                                   1000000000000000000ULL };

/// The largest exponent kPow10 holds.
constexpr std::size_t kMaxPow10 = kPow10.size() - 1U;
/// INT64_MAX, unsigned: the largest magnitude a parsed value may reach.
constexpr std::uint64_t kInt64Magnitude = 9223372036854775807ULL;
/// The base of NR1, NR2 and NR3, and of an exponent.
constexpr std::int32_t kDecimalBase = 10;
/// The most significant digits a decimal parameter may have: every digit
/// after the leading zeros counts, trailing zeros included.
constexpr std::size_t kMaxSignificantDigits = 18;
static_assert( kMaxSignificantDigits <= kMaxPow10, "kPow10 must hold 10^kMaxSignificantDigits" );
/// The largest exponent magnitude a decimal parameter may carry.
constexpr std::uint32_t kMaxExponent = 999U;

}  // namespace

// --- Errors -----------------------------------------------------------------

std::string_view errorText( std::int16_t code ) noexcept {
    for ( const ErrorName& entry : kErrorNames ) {
        if ( entry.code == code ) {
            return entry.text;
        }
    }
    if ( code > 0 ) {
        return "Device-specific error";
    }
    return "Error";
}

std::uint32_t esrBitFor( std::int16_t code ) noexcept {
    if ( code == err::kNone ) {
        return 0;
    }
    if ( code > 0 || ( code <= -300 && code > -400 ) ) {
        return esr::kDeviceError;
    }
    if ( code <= -100 && code > -200 ) {
        return esr::kCommandError;
    }
    if ( code <= -200 && code > -300 ) {
        return esr::kExecutionError;
    }
    if ( code <= -400 && code > -500 ) {
        return esr::kQueryError;
    }
    return esr::kDeviceError;
}

namespace {

/// One character of an error's detail as the queue keeps it: a `"` as `'`, so
/// that the string response needs no escaping, and a byte that is not
/// printable ASCII as `?`, so that none reaches the line inside a response.
/// @param[in] c The character of the detail.
/// @return The character the queue keeps.
char detailCharacter( char c ) noexcept {
    if ( !chars::isPrintable( c ) ) {
        return '?';
    }
    return ( c == '"' ) ? '\'' : c;
}

}  // namespace

void ErrorQueue::push( std::int16_t code, std::string_view detail ) noexcept {
    if ( count_ == kErrorQueueDepth ) {
        ErrorEntry& newest = entries_[( head_ + kErrorQueueDepth - 1U ) % kErrorQueueDepth];
        newest             = ErrorEntry{};
        newest.code        = err::kQueueOverflow;
        return;
    }
    ErrorEntry& entry = entries_[( head_ + count_ ) % kErrorQueueDepth];
    entry             = ErrorEntry{};
    entry.code        = code;
    std::size_t i     = 0;
    while ( i < detail.size() && i + 1U < kErrorDetailCapacity ) {
        entry.detail[i] = detailCharacter( detail[i] );
        ++i;
    }
    entry.detail[i] = '\0';
    ++count_;
}

ErrorEntry ErrorQueue::pop() noexcept {
    if ( count_ == 0U ) {
        return ErrorEntry{};
    }
    const ErrorEntry entry = entries_[head_];
    head_                  = ( head_ + 1U ) % kErrorQueueDepth;
    --count_;
    return entry;
}

std::size_t formatError( const ErrorEntry& entry, std::span<char> out ) noexcept {
    text::Writer w( out );
    w.signedDecimal( entry.code );
    w.text( ",\"" );
    w.text( errorText( entry.code ) );
    if ( entry.detail[0] != '\0' ) {
        w.character( ';' );
        w.text( chars::untilNul( entry.detail ) );
    }
    w.character( '"' );
    // Truncated inside the string: close it again, so the response still parses.
    if ( w.truncated() && ( out.size() >= 2U ) ) {
        out[out.size() - 2U] = '"';
    }
    return w.size();
}

// --- Program messages -------------------------------------------------------

bool Splitter::next( Unit& unit ) noexcept {
    if ( done_ || cursor_ == nullptr ) {
        return false;
    }
    char* start = cursor_;
    char* end   = start;
    char  quote = '\0';
    while ( *end != '\0' ) {
        if ( quote != '\0' ) {
            if ( *end == quote ) {
                quote = '\0';
            }
        } else if ( isQuote( *end ) ) {
            quote = *end;
        } else if ( *end == ';' ) {
            break;
        } else {
            // Any other character, outside a quote, is part of the unit.
        }
        ++end;
    }
    if ( *end == ';' ) {
        *end    = '\0';
        cursor_ = end + 1;
    } else {
        done_ = true;
    }

    while ( ( start < end ) && chars::isSpace( *start ) ) {
        ++start;
    }
    // Trailing whitespace belongs to no parameter. The unit ends where the
    // scan stopped, at its `;` (cut above) or at the line's end, and the
    // leading whitespace stopped at that end at the latest.
    auto length = static_cast<std::size_t>( end - start );
    while ( ( length > 0U ) && chars::isSpace( start[length - 1U] ) ) {
        --length;
        start[length] = '\0';
    }

    unit        = Unit{};
    unit.header = start;
    char* p     = start;
    while ( ( *p != '\0' ) && !chars::isSpace( *p ) ) {
        ++p;
    }
    unit.headerLength = static_cast<std::size_t>( p - start );
    if ( *p != '\0' ) {
        *p = '\0';
        ++p;
        while ( chars::isSpace( *p ) ) {
            ++p;
        }
    }
    unit.parameters = p;
    unit.common     = unit.headerLength > 0U && unit.header[0] == '*';
    unit.query      = unit.headerLength > 0U && unit.header[unit.headerLength - 1U] == '?';
    return true;
}

bool PathResolver::resolve( const Unit& unit, char* out, std::size_t capacity ) noexcept {
    if ( capacity == 0U ) {
        return false;
    }
    const std::string_view header( unit.header, unit.headerLength );
    if ( unit.common ) {
        return copyBounded( out, capacity, header ) == header.size();
    }
    const bool             absolute = header.starts_with( ':' );
    const std::string_view body     = absolute ? slice( header, 1, header.size() - 1U ) : header;
    const std::size_t      prefixLength = absolute ? 0U : pathLength_;
    if ( prefixLength + body.size() + 1U > capacity ) {
        return false;
    }
    const std::span<char> path( path_ );
    (void)std::copy_n( path.begin(), prefixLength, out );
    (void)std::copy_n( body.cbegin(), body.size(), out + prefixLength );
    out[prefixLength + body.size()] = '\0';

    // The new path is everything up to and including the last colon.
    std::size_t cut = prefixLength + body.size();
    while ( ( cut > 0U ) && ( out[cut - 1U] != ':' ) ) {
        --cut;
    }
    pathLength_ = copyBounded( path.data(), path.size(), std::string_view( out, cut ) );
    return true;
}

// --- Header matching --------------------------------------------------------

bool mnemonicMatches( const char* longForm, const char* token, std::size_t tokenLength ) noexcept {
    if ( ( longForm == nullptr ) || ( token == nullptr ) ) {
        return false;
    }
    return formMatches( longForm, std::string_view( token, tokenLength ) );
}

bool mnemonicMatches( std::string_view longForm, std::string_view token ) noexcept {
    return formMatches( longForm, token );
}

bool matches( std::string_view pattern, std::string_view header, Suffix& suffix ) noexcept {
    suffix = Suffix{};
    if ( pattern.starts_with( '*' ) ) {
        return sameIgnoringCase( pattern, header );
    }

    const bool patternQuery = pattern.ends_with( '?' );
    const bool headerQuery  = header.ends_with( '?' );
    if ( patternQuery != headerQuery ) {
        return false;
    }
    if ( patternQuery ) {
        pattern.remove_suffix( 1 );
        header.remove_suffix( 1 );
    }

    std::array<Segment, kMaxSegments> segments{};
    const std::size_t                 segmentCount = splitPattern( pattern, segments );
    if ( segmentCount == 0U ) {
        return false;
    }

    HeaderTokens tokens;
    std::size_t  i = 0;
    while ( i <= header.size() ) {
        const std::size_t start = i;
        while ( ( i < header.size() ) && ( header[i] != ':' ) ) {
            ++i;
        }
        if ( ( i == start ) || ( tokens.count == kMaxSegments ) ) {
            return false;  // an empty node, or too many
        }
        tokens.node[tokens.count] = slice( header, start, i - start );
        ++tokens.count;
        ++i;
    }
    return matchFrom( segments, segmentCount, tokens, suffix );
}

// --- Parameters -------------------------------------------------------------

bool Parameters::parse( char* text ) noexcept {
    count_ = 0;
    error_ = err::kNone;
    if ( text == nullptr ) {
        return true;
    }
    while ( chars::isSpace( *text ) ) {
        ++text;
    }
    if ( *text == '\0' ) {
        return true;
    }
    char* item  = text;
    char  quote = '\0';
    // Each pass reads one character and steps past it at its end: a
    // character inside a quote or an item is passed over, a separator or the
    // end closes the item before it.
    char* p = text;
    while ( true ) {
        const char c = *p;
        if ( quote != '\0' ) {
            if ( c == '\0' ) {
                return fail( err::kStringData );
            }
            if ( c == quote ) {
                quote = '\0';
            }
            ++p;
            continue;
        }
        if ( isQuote( c ) ) {
            quote = c;
            ++p;
            continue;
        }
        if ( ( c != ',' ) && ( c != '\0' ) ) {
            ++p;
            continue;
        }
        *p = '\0';
        // Trim this item on both sides. It ends at p, where its separator
        // was, and its leading whitespace stops at p at the latest.
        while ( ( item < p ) && chars::isSpace( *item ) ) {
            ++item;
        }
        auto length = static_cast<std::size_t>( p - item );
        while ( ( length > 0U ) && chars::isSpace( item[length - 1U] ) ) {
            --length;
            item[length] = '\0';
        }
        if ( length == 0U ) {
            return fail( err::kSyntax );
        }
        if ( count_ == kMaxParameters ) {
            return fail( err::kParameterNotAllowed );
        }
        items_[count_] = item;
        ++count_;
        if ( c == '\0' ) {
            return true;
        }
        item = p + 1;
        ++p;
    }
}

const char* Parameters::raw( std::size_t index ) const noexcept {
    return ( index < count_ ) ? items_[index] : nullptr;
}

bool Parameters::characterData( std::size_t index, std::string_view& text ) noexcept {
    const char* item = raw( index );
    if ( item == nullptr ) {
        return fail( err::kMissingParameter );
    }
    text             = item;
    const char first = text.front();
    if ( isQuote( first ) || chars::isDigit( first ) || ( first == '#' ) || ( first == '+' )
         || ( first == '-' ) || ( first == '.' ) ) {
        return fail( err::kDataType );
    }
    return true;
}

bool Parameters::choice( std::size_t index, const char* const* choices, std::size_t count,
                         std::size_t& which ) noexcept {
    std::string_view text;
    if ( !characterData( index, text ) ) {
        return false;
    }
    for ( std::size_t i = 0; i < count; ++i ) {
        if ( mnemonicMatches( choices[i], text.data(), text.size() ) ) {
            which = i;
            return true;
        }
    }
    return fail( err::kIllegalParameter );
}

bool Parameters::choice( std::size_t index, std::span<const std::string_view> choices,
                         std::size_t& which ) noexcept {
    std::string_view text;
    if ( !characterData( index, text ) ) {
        return false;
    }
    for ( std::size_t i = 0; i < choices.size(); ++i ) {
        if ( mnemonicMatches( choices[i], text ) ) {
            which = i;
            return true;
        }
    }
    return fail( err::kIllegalParameter );
}

bool Parameters::boolean( std::size_t index, bool& value ) noexcept {
    const char* text = raw( index );
    if ( text == nullptr ) {
        return fail( err::kMissingParameter );
    }
    const std::string_view word( text );
    if ( equalsIgnoreCase( text, "ON" ) || ( word == "1" ) ) {
        value = true;
        return true;
    }
    if ( equalsIgnoreCase( text, "OFF" ) || ( word == "0" ) ) {
        value = false;
        return true;
    }
    return fail( err::kIllegalParameter );
}

bool Parameters::unsignedInteger( std::size_t index, std::uint32_t& value ) noexcept {
    const char* item = raw( index );
    if ( item == nullptr ) {
        return fail( err::kMissingParameter );
    }
    std::string_view text( item );
    std::int32_t     base = kDecimalBase;
    if ( text.starts_with( '#' ) ) {
        const char kind = ( text.size() > 1U ) ? chars::toUpper( text[1] ) : '\0';
        base            = ( kind == 'H' ) ? 16 : ( kind == 'Q' ) ? 8 : ( kind == 'B' ) ? 2 : 0;
        if ( base == 0 ) {
            return fail( err::kNumericData );
        }
        text.remove_prefix( 2 );
    } else if ( text.starts_with( '+' ) ) {
        text.remove_prefix( 1 );
    } else if ( text.starts_with( '-' ) ) {
        return fail( err::kDataOutOfRange );
    } else {
        // An unsigned decimal: no sign or base prefix to skip.
    }
    if ( text.empty() ) {
        return fail( err::kNumericData );
    }
    std::uint32_t parsed = 0;
    const auto    result = parseNumber( text, parsed, base );
    if ( result.ec == std::errc::result_out_of_range ) {
        return fail( err::kDataOutOfRange );
    }
    const auto used = static_cast<std::size_t>( result.ptr - text.data() );
    if ( used < text.size() ) {
        // A decimal point or an exponent is NR2/NR3 where NR1 was asked for:
        // a data type error. Anything else that is not a digit of the base is
        // a numeric data error.
        const char stop = chars::toUpper( text[used] );
        if ( ( base == kDecimalBase ) && ( ( stop == '.' ) || ( stop == 'E' ) ) ) {
            return fail( err::kDataType );
        }
        return fail( err::kNumericData );
    }
    value = parsed;
    return true;
}

bool Parameters::scaled( std::size_t index, std::uint32_t decimals, std::int64_t& value ) noexcept {
    const char* item = raw( index );
    if ( item == nullptr ) {
        return fail( err::kMissingParameter );
    }
    std::string_view text( item );
    bool             negative = false;
    if ( text.starts_with( '+' ) || text.starts_with( '-' ) ) {
        negative = text.starts_with( '-' );
        text.remove_prefix( 1 );
    }
    const std::string_view whole = leadingDigits( text );
    text.remove_prefix( whole.size() );
    std::string_view fraction{};
    if ( text.starts_with( '.' ) ) {
        text.remove_prefix( 1 );
        fraction = leadingDigits( text );
        text.remove_prefix( fraction.size() );
    }
    if ( whole.empty() && fraction.empty() ) {
        return fail( err::kDataType );
    }
    // The significant digits: every digit after the leading zeros, of the
    // whole part and, while it is all zeros, of the fraction.
    std::size_t leading = std::min( whole.find_first_not_of( '0' ), whole.size() );
    if ( leading == whole.size() ) {
        leading += std::min( fraction.find_first_not_of( '0' ), fraction.size() );
    }
    if ( whole.size() + fraction.size() - leading > kMaxSignificantDigits ) {
        return fail( err::kNumericData );
    }
    // Each part fits 64 bits: at most kMaxSignificantDigits digits after its
    // leading zeros, which from_chars reads past. An empty part reads as 0.
    std::uint64_t wholeValue    = 0;
    std::uint64_t fractionValue = 0;
    (void)parseNumber( whole, wholeValue, kDecimalBase );
    (void)parseNumber( fraction, fractionValue, kDecimalBase );
    // With a non-zero whole part every fraction digit is significant, so
    // the fraction has fewer than kMaxSignificantDigits of them.
    const std::uint64_t mantissa =
        ( wholeValue == 0U ) ? fractionValue
                             : ( ( wholeValue * kPow10[fraction.size()] ) + fractionValue );
    std::int32_t exponent = 0;
    if ( text.starts_with( 'e' ) || text.starts_with( 'E' ) ) {
        text.remove_prefix( 1 );
        bool expNegative = false;
        if ( text.starts_with( '+' ) || text.starts_with( '-' ) ) {
            expNegative = text.starts_with( '-' );
            text.remove_prefix( 1 );
        }
        const std::string_view digits = leadingDigits( text );
        std::uint32_t          parsed = 0;
        const auto             result = parseNumber( digits, parsed, kDecimalBase );
        if ( ( result.ec != std::errc{} ) || ( parsed > kMaxExponent ) ) {
            return fail( err::kNumericData );
        }
        text.remove_prefix( digits.size() );
        exponent = expNegative ? -static_cast<std::int32_t>( parsed )
                               : static_cast<std::int32_t>( parsed );
    }
    if ( !text.empty() ) {
        return fail( err::kDataType );
    }

    const std::int32_t shift     = exponent - static_cast<std::int32_t>( fraction.size() )
                                   + static_cast<std::int32_t>( decimals );
    std::uint64_t      magnitude = 0;
    // Two ways to zero, kept apart: a zero mantissa (any exponent, even one
    // past kMaxPow10, is a zero) and a value below the unit's resolution.
    if ( mantissa == 0U ) {  // NOLINT(bugprone-branch-clone): two zeros, kept apart
        magnitude = 0;
    } else if ( shift >= 0 ) {
        if ( ( static_cast<std::size_t>( shift ) > kMaxPow10 )
             || ( mantissa > ( kInt64Magnitude / kPow10[static_cast<std::size_t>( shift )] ) ) ) {
            return fail( err::kDataOutOfRange );
        }
        magnitude = mantissa * kPow10[static_cast<std::size_t>( shift )];
    } else if ( static_cast<std::size_t>( -shift ) > kMaxPow10 ) {
        magnitude = 0;
    } else {
        const std::uint64_t divisor   = kPow10[static_cast<std::size_t>( -shift )];
        const std::uint64_t quotient  = mantissa / divisor;
        const std::uint64_t remainder = mantissa % divisor;
        magnitude                     = quotient + ( ( remainder * 2U >= divisor ) ? 1U : 0U );
    }
    // Every way above leaves the magnitude within INT64_MAX: a product past it
    // is refused, and a quotient, rounded up or not, is no larger than the
    // mantissa, which has at most kMaxSignificantDigits digits.
    static_assert( kPow10[kMaxSignificantDigits] - 1U <= kInt64Magnitude,
                   "a mantissa of kMaxSignificantDigits digits must fit int64" );
    value =
        negative ? -static_cast<std::int64_t>( magnitude ) : static_cast<std::int64_t>( magnitude );
    return true;
}

bool Parameters::string( std::size_t index, char* out, std::size_t capacity ) noexcept {
    const char* item = raw( index );
    if ( item == nullptr ) {
        return fail( err::kMissingParameter );
    }
    const std::string_view text( item );
    if ( ( text.size() < 2U ) || !isQuote( text.front() ) || ( text.back() != text.front() )
         || ( capacity == 0U ) ) {
        return fail( err::kStringData );
    }
    const std::array<char, 2> doubled = { text.front(), text.front() };
    const std::string_view    pair( doubled.data(), doubled.size() );
    std::string_view          inside = slice( text, 1, text.size() - 2U );
    std::size_t               used   = 0;
    while ( !inside.empty() ) {
        const char c = inside.front();
        if ( c == doubled[0] ) {
            // Only a doubled quote can sit inside: Parameters::parse() would
            // otherwise have ended the string here.
            if ( !inside.starts_with( pair ) ) {
                return fail( err::kStringData );
            }
            inside.remove_prefix( 1 );
        }
        if ( used + 1U >= capacity ) {
            return fail( err::kStringData );
        }
        out[used] = c;
        ++used;
        inside.remove_prefix( 1 );
    }
    out[used] = '\0';
    return true;
}

// --- Response formatting ----------------------------------------------------

bool equalsIgnoreCase( const char* a, const char* b ) noexcept {
    if ( ( a == nullptr ) || ( b == nullptr ) ) {
        return false;
    }
    return sameIgnoringCase( a, b );
}

}  // namespace kleidos::meter::scpi
