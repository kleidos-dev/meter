// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file scpi.h
 * @ingroup scpi_parser
 * @brief The SCPI-1999 / IEEE 488.2 layer: program-message parsing, header
 *        matching, parameter decoding, the error queue and the status bytes.
 *
 * Pure C++ with no ESP-IDF dependency, so the whole grammar is exercised by
 * the host tests in `firmware/test/native/` and not only on the bench. The
 * command surface it serves, and every deviation from SCPI-1999 the
 * instrument makes, is `docs/scpi.md`.
 *
 * What it implements of the two standards:
 *
 *  - Program messages of one or more units separated by `;`, a unit being a
 *    header, optional whitespace and comma-separated parameters. A header
 *    with no leading `:` after a `;` is resolved against the path of the
 *    previous unit (SCPI-1999 vol. 1 SS 6.2.4); `*` common commands never
 *    change that path.
 *  - Mnemonics in long or short form, case-insensitive, optional nodes in
 *    square brackets and a numeric suffix (`ROUTe:RELay3`).
 *  - Numeric parameters as NR1/NR2/NR3 decoded **exactly** into a scaled
 *    integer (no float anywhere), the `#H`, `#Q` and `#B` non-decimal forms,
 *    booleans, character data and quoted string data.
 *  - The error queue with its standard numbering and overflow rule
 *    (`-350`), and the Standard Event Status Register bits each error class
 *    sets.
 */
#ifndef KLEIDOS_METER_SCPI_H
#define KLEIDOS_METER_SCPI_H

#include <cstddef>
#include <cstdint>
#include <span>
#include <string_view>

namespace kleidos::meter::scpi {

/// @addtogroup scpi_parser
/// @{

// --- Errors -----------------------------------------------------------------

/// @brief Error numbers: the standard ones the instrument uses (SCPI-1999
/// vol. 2 SS 21.8) and the device-specific ones, which are positive.
namespace err {
constexpr std::int16_t kNone                = 0;     ///< "No error".
constexpr std::int16_t kCommand             = -100;  ///< "Command error".
constexpr std::int16_t kInvalidCharacter    = -101;  ///< "Invalid character".
constexpr std::int16_t kSyntax              = -102;  ///< "Syntax error".
constexpr std::int16_t kDataType            = -104;  ///< "Data type error".
constexpr std::int16_t kParameterNotAllowed = -108;  ///< "Parameter not allowed".
constexpr std::int16_t kMissingParameter    = -109;  ///< "Missing parameter".
constexpr std::int16_t kUndefinedHeader     = -113;  ///< "Undefined header".
constexpr std::int16_t kSuffixOutOfRange    = -114;  ///< "Header suffix out of range".
constexpr std::int16_t kNumericData         = -120;  ///< "Numeric data error".
constexpr std::int16_t kStringData          = -150;  ///< "String data error".
constexpr std::int16_t kExecution           = -200;  ///< "Execution error".
constexpr std::int16_t kSettingsConflict    = -221;  ///< "Settings conflict".
constexpr std::int16_t kDataOutOfRange      = -222;  ///< "Data out of range".
constexpr std::int16_t kIllegalParameter    = -224;  ///< "Illegal parameter value".
constexpr std::int16_t kDataStale           = -230;  ///< "Data corrupt or stale".
constexpr std::int16_t kHardware            = -240;  ///< "Hardware error".
constexpr std::int16_t kHardwareMissing     = -241;  ///< "Hardware missing".
constexpr std::int16_t kDeviceSpecific      = -300;  ///< "Device-specific error".
constexpr std::int16_t kQueueOverflow       = -350;  ///< "Queue overflow".
constexpr std::int16_t kInputOverrun        = -363;  ///< "Input buffer overrun".
constexpr std::int16_t kQuery               = -400;  ///< "Query error".

// Device-specific, positive (SCPI-1999 vol. 2 SS 21.8.1).
constexpr std::int16_t kRelaysUnavailable   = 100;  ///< "Relays unavailable".
constexpr std::int16_t kShuntBypassed       = 101;  ///< "Shunt bypassed".
constexpr std::int16_t kNullNotStored       = 102;  ///< "Null not stored".
constexpr std::int16_t kBypassCheckFailed   = 103;  ///< "Bypass check failed".
constexpr std::int16_t kLeadCheckFailed     = 104;  ///< "Lead check failed".
constexpr std::int16_t kMarksFull           = 105;  ///< "Marks full".
constexpr std::int16_t kCalibrationNotSaved = 106;  ///< "Calibration not saved".
constexpr std::int16_t kSamplerFailed       = 107;  ///< "Sampler not running".
constexpr std::int16_t kZeroNoSamples       = 108;  ///< "Null took no samples".
constexpr std::int16_t kOrderDisconnects    = 109;  ///< "Order disconnects the DUT".
constexpr std::int16_t kOrderBypasses       = 110;  ///< "Order bypasses the shunt".
constexpr std::int16_t kNotRunning          = 111;  ///< "Not running".
constexpr std::int16_t kWindowRunning       = 112;  ///< "Null window running".
constexpr std::int16_t kNullPathReleased    = 113;  ///< "Null path released" (0.12.0).
constexpr std::int16_t kLeadReclosed        = 114;  ///< "Lead reclosed" (0.13.0).
constexpr std::int16_t kBypassOpened        = 115;  ///< "Bypass opened" (0.16.0).
constexpr std::int16_t kResetReleased       = 116;  ///< "Relays released by reset" (0.16.0).
constexpr std::int16_t kPinMapReverted      = 117;  ///< "Pin map reverted" (0.18.0).
}  // namespace err

/// @brief The standard text of an error number, as SCPI-1999 vol. 2 SS 21.8
/// words it, or the instrument's own text for a positive number.
/// @param[in] code The error number, one of the @ref err values.
/// @return A view of a static text: `"Device-specific error"` for a
///         positive number with no text of its own, `"Error"` for any other
///         unknown number.
std::string_view errorText( std::int16_t code ) noexcept;

/// Room for an entry's device-dependent detail, after the standard text.
constexpr std::size_t kErrorDetailCapacity = 112;

/// The error queue's depth. The oldest entries are kept on overflow and the
/// newest is replaced by `-350` (SCPI-1999 vol. 2 SS 21.8.2).
constexpr std::size_t kErrorQueueDepth = 10;

/// @brief One entry of the error queue: an error number and its detail.
struct ErrorEntry {
    std::int16_t code                         = err::kNone;  ///< Error number (@ref err).
    char         detail[kErrorDetailCapacity] = {};  ///< NUL-terminated detail, may be empty.
};

/// A first-in, first-out queue of errors.
class ErrorQueue {
   public:
    /// @brief Appends an error.
    ///
    /// A `"` in @p detail is written as `'`, so that the string response it
    /// ends up in needs no escaping, and a byte that is not printable ASCII as
    /// `?`. A full queue keeps its oldest entries and replaces the newest by
    /// `-350`.
    /// @param[in] code   The error number (@ref err).
    /// @param[in] detail Device-dependent detail, truncated to fit; may be
    ///                   empty.
    void push( std::int16_t code, std::string_view detail ) noexcept;

    /// @brief Takes the oldest error.
    /// @return The oldest entry; `0` with no detail when the queue is empty.
    ErrorEntry pop() noexcept;

    /// @brief The number of errors queued.
    /// @return The entry count, at most @ref kErrorQueueDepth.
    std::size_t count() const noexcept { return count_; }
    /// @brief Empties the queue.
    void clear() noexcept { count_ = 0; }

   private:
    ErrorEntry  entries_[kErrorQueueDepth] = {};  ///< The entries, a circular array.
    std::size_t head_                      = 0;   ///< The oldest entry's slot.
    std::size_t count_                     = 0;   ///< How many are queued.
};

/// @brief Writes @p entry as a SCPI error response,
/// `<code>,"<text>[;<detail>]"`.
///
/// A response cut short is closed with a `"` again, so it still parses.
/// @param[in]  entry The error to write.
/// @param[out] out   Buffer for the NUL-terminated response.
/// @return the length written, truncated to the size of @p out less one.
std::size_t formatError( const ErrorEntry& entry, std::span<char> out ) noexcept;

// --- Status bytes -----------------------------------------------------------

/// @brief Standard Event Status Register bits (IEEE 488.2 SS 11.5.1).
namespace esr {
constexpr std::uint32_t kOperationComplete = 0x01;  ///< OPC, operation complete.
constexpr std::uint32_t kQueryError        = 0x04;  ///< QYE, query error (-4xx).
constexpr std::uint32_t kDeviceError       = 0x08;  ///< DDE, device error (-3xx, positive).
constexpr std::uint32_t kExecutionError    = 0x10;  ///< EXE, execution error (-2xx).
constexpr std::uint32_t kCommandError      = 0x20;  ///< CME, command error (-1xx).
constexpr std::uint32_t kPowerOn           = 0x80;  ///< PON, power on.
}  // namespace esr

/// @brief Status Byte bits (IEEE 488.2 SS 11.2, SCPI-1999 vol. 1 SS 9.1).
namespace stb {
constexpr std::uint32_t kErrorAvailable   = 0x04;  ///< The error queue is not empty.
constexpr std::uint32_t kQuestionable     = 0x08;  ///< Questionable status summary.
constexpr std::uint32_t kEventSummary     = 0x20;  ///< ESB, standard event summary.
constexpr std::uint32_t kMasterSummary    = 0x40;  ///< MSS, master summary status.
constexpr std::uint32_t kOperationSummary = 0x80;  ///< Operation status summary.
}  // namespace stb

/// @brief The ESR bit an error of @p code sets: command, execution, device or
/// query error by its class; 0 for no error.
/// @param[in] code The error number (@ref err).
/// @return One of the @ref esr bits; a positive (device-specific) or
///         unclassified number sets @ref esr::kDeviceError, and @ref
///         err::kNone gives 0.
std::uint32_t esrBitFor( std::int16_t code ) noexcept;

// --- Program messages -------------------------------------------------------

/// One program message unit, cut out of a line in place.
struct Unit {
    char*       header       = nullptr;  ///< NUL-terminated, path not yet resolved.
    char*       parameters   = nullptr;  ///< NUL-terminated, possibly empty.
    bool        common       = false;    ///< `*IDN?` and the like.
    bool        query        = false;    ///< The header ended in `?`.
    std::size_t headerLength = 0;        ///< Length of @ref Unit::header in bytes.
};

/// Cuts a line into units at every `;` that is outside a quoted string, in
/// place. Call next() until it returns false.
class Splitter {
   public:
    /// @brief Starts cutting @p line.
    /// @param[in,out] line The NUL-terminated line, cut in place; it must
    ///                     outlive the splitter and the units it returns.
    explicit Splitter( char* line ) noexcept : cursor_( line ) {}

    /// @brief Cuts the next unit out of the line.
    ///
    /// A unit with an empty header is reported with header[0] == '\0'.
    /// @param[out] unit The unit; its pointers point into the line.
    /// @return false when the line is exhausted.
    bool next( Unit& unit ) noexcept;

   private:
    char* cursor_;        ///< Where the next unit starts.
    bool  done_ = false;  ///< The line is exhausted.
};

/// Longest resolved header the instrument accepts.
constexpr std::size_t kHeaderCapacity = 96;

/// The compound-header rule of SCPI-1999 vol. 1 SS 6.2.4: a header that does
/// not start with `:` is resolved against the path the previous unit left.
class PathResolver {
   public:
    /// @brief Resolves @p unit's header into @p out (no leading `:`).
    ///
    /// Common commands are copied as they are and leave the path alone; any
    /// other header leaves as the new path everything up to and including its
    /// last colon.
    /// @param[in]  unit     The unit whose header to resolve.
    /// @param[out] out      Buffer for the NUL-terminated resolved header.
    /// @param[in]  capacity Size of @p out in bytes.
    /// @return false when the result does not fit.
    bool resolve( const Unit& unit, char* out, std::size_t capacity ) noexcept;

    /// @brief Forgets the path, so the next header resolves from the root.
    void reset() noexcept {
        path_[0]    = '\0';
        pathLength_ = 0;
    }

   private:
    char        path_[kHeaderCapacity] = {};  ///< The path the last unit left, with its colon.
    std::size_t pathLength_            = 0;   ///< The length of path_, its terminator left out.
};

// --- Header matching --------------------------------------------------------

/// The numeric suffix a header carried, and whether it carried one.
struct Suffix {
    std::int32_t value   = 1;      ///< The suffix; 1 when the header carried none.
    bool         present = false;  ///< True when the header carried a suffix.
};

/// @brief Matches a resolved header (no leading colon, `?` included) against a
/// pattern such as `SENSe:CURRent:RANGe[:UPPer]?` or `ROUTe:RELay#`.
///
/// In a pattern, upper case marks the short form, `[:NODE]` an optional node
/// and a trailing `#` a node that takes a numeric suffix. A common command
/// pattern (`*IDN?`) is compared whole, ignoring case.
/// @param[in]  pattern The pattern to match against.
/// @param[in]  header  The resolved header.
/// @param[out] suffix  The numeric suffix the header carried; reset first.
/// @retval true  The header matches the pattern.
/// @retval false It does not, or the pattern is malformed.
bool matches( std::string_view pattern, std::string_view header, Suffix& suffix ) noexcept;

/// @brief Matches one mnemonic token (no colon) against a long form such as
/// `CLOSe`: the full long form or its upper-case short form, in any case.
/// @param[in] longForm    The long form, short form in upper case.
/// @param[in] token       The token, not necessarily NUL-terminated.
/// @param[in] tokenLength Length of @p token in bytes.
/// @retval true  The token is the long or the short form.
/// @retval false It is neither, or either pointer is null.
bool mnemonicMatches( const char* longForm, const char* token, std::size_t tokenLength ) noexcept;

/// @brief mnemonicMatches() on views.
/// @param[in] longForm The long form, short form in upper case.
/// @param[in] token    The token.
/// @retval true  The token is the long or the short form.
/// @retval false It is neither.
bool mnemonicMatches( std::string_view longForm, std::string_view token ) noexcept;

// --- Parameters -------------------------------------------------------------

/// Most parameters any command takes.
constexpr std::size_t kMaxParameters = 8;

/// The comma-separated parameters of one unit, split in place.
class Parameters {
   public:
    /// @brief Splits @p text at every comma outside a quoted string and trims
    /// each parameter.
    /// @param[in,out] text The NUL-terminated parameter text, split in place;
    ///                     null or blank is no parameters.
    /// @return false (and sets error()) on an unterminated string, an empty
    ///         parameter or more than kMaxParameters parameters.
    bool parse( char* text ) noexcept;

    /// @brief The number of parameters the last parse() found.
    /// @return The parameter count, at most @ref kMaxParameters.
    std::size_t count() const noexcept { return count_; }
    /// @brief One parameter as it was written, trimmed.
    /// @param[in] index Zero-based parameter index.
    /// @return The NUL-terminated parameter, or null past the last one.
    const char* raw( std::size_t index ) const noexcept;

    /// @brief The error a failed parse or decode left: a `-1xx` number.
    /// @return The error number (@ref err), or @ref err::kNone after a parse
    ///         with no error.
    std::int16_t error() const noexcept { return error_; }

    /// @brief Character data: the index into @p choices (long forms with their
    /// short form in upper case) of the one @p index matches.
    /// @param[in]  index   Zero-based parameter index.
    /// @param[in]  choices The long forms to match against.
    /// @param[in]  count   Number of entries in @p choices.
    /// @param[out] which   The index of the matching choice.
    /// @return false (and sets error()) when the parameter is missing (-109),
    ///         is not character data (-104) or matches no choice (-224).
    bool choice( std::size_t index, const char* const* choices, std::size_t count,
                 std::size_t& which ) noexcept;

    /// @brief choice() over a table of views, as the command handlers keep
    /// their keywords.
    /// @param[in]  index   Zero-based parameter index.
    /// @param[in]  choices The long forms to match against.
    /// @param[out] which   The index of the matching choice.
    /// @return false (and sets error()) when the parameter is missing (-109),
    ///         is not character data (-104) or matches no choice (-224).
    bool choice( std::size_t index, std::span<const std::string_view> choices,
                 std::size_t& which ) noexcept;

    /// @brief A boolean: `ON`, `OFF`, `1` or `0`.
    /// @param[in]  index Zero-based parameter index.
    /// @param[out] value True for `ON` or `1`, false for `OFF` or `0`.
    /// @return false (and sets error()) when the parameter is missing (-109)
    ///         or is none of the four (-224).
    bool boolean( std::size_t index, bool& value ) noexcept;

    /// @brief An unsigned integer in NR1, or `#H`, `#Q`, `#B` form.
    ///
    /// A decimal point, an exponent or a sign is refused (-104 / -120).
    /// @param[in]  index Zero-based parameter index.
    /// @param[out] value The decoded integer.
    /// @return false (and sets error()) when the parameter is missing (-109),
    ///         malformed (-104 / -120), negative or above 32 bits (-222).
    bool unsignedInteger( std::size_t index, std::uint32_t& value ) noexcept;

    /// @brief A decimal number (NR1, NR2 or NR3) as an integer count of
    /// 10^-@p decimals units, rounded half away from zero.
    ///
    /// `0.1000125` with @p decimals 6 is 100013. Exact: no binary floating
    /// point is involved.
    /// @param[in]  index    Zero-based parameter index.
    /// @param[in]  decimals Decimal places of the unit @p value counts.
    /// @param[out] value    The scaled integer.
    /// @return false (and sets error()) when the parameter is missing (-109),
    ///         malformed (-104 / -120) or out of the 64-bit range (-222).
    bool scaled( std::size_t index, std::uint32_t decimals, std::int64_t& value ) noexcept;

    /// @brief A string in `"..."` or `'...'`, the doubled quote unescaped,
    /// copied into @p out.
    ///
    /// Refused (-150) when it does not fit.
    /// @param[in]  index    Zero-based parameter index.
    /// @param[out] out      Buffer for the NUL-terminated string.
    /// @param[in]  capacity Size of @p out in bytes.
    /// @return false (and sets error()) when the parameter is missing (-109),
    ///         is not a well-formed quoted string or does not fit (-150).
    bool string( std::size_t index, char* out, std::size_t capacity ) noexcept;

   private:
    /// @brief The parameter at @p index, when it is character data: neither
    /// a string nor a number.
    /// @param[in]  index Zero-based parameter index.
    /// @param[out] text  The parameter, when it is character data.
    /// @return false (and sets error()) when the parameter is missing (-109)
    ///         or starts as a string or a number does (-104).
    bool characterData( std::size_t index, std::string_view& text ) noexcept;

    /// @brief Records @p code as error() and refuses.
    /// @param[in] code The SCPI error code.
    /// @return false, always, for the caller to return.
    bool fail( std::int16_t code ) noexcept {
        error_ = code;
        return false;
    }

    char*        items_[kMaxParameters] = {};          ///< Each parameter's text, in the line.
    std::size_t  count_                 = 0;           ///< How many parameters.
    std::int16_t error_                 = err::kNone;  ///< The last refusal's code.
};

// --- Response formatting ----------------------------------------------------

/// @brief ASCII case-insensitive equality of two NUL-terminated strings.
/// @param[in] a First string.
/// @param[in] b Second string.
/// @retval true  The strings are equal ignoring ASCII case.
/// @retval false They differ, or either is null.
bool equalsIgnoreCase( const char* a, const char* b ) noexcept;

/// @}

}  // namespace kleidos::meter::scpi

#endif  // KLEIDOS_METER_SCPI_H
