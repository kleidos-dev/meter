// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_sweep.cpp
 * @brief The command table's sweep (FW-30): every entry of
 *        Console::commands() held to the rules every command keeps.
 *
 * The other suites test what each command does; this one tests what they all
 * do, entry by entry, over the table itself:
 *
 *  - its long form, every optional node written, and its short form, none
 *    written, in any case, reach the same entry and no other;
 *  - given valid parameters in the state it needs, a query answers exactly
 *    one unit and a command none, with no error, in either form;
 *  - one parameter more than it takes is `-108` and one required parameter
 *    less is `-109`, each alone in the queue, and neither changes what its
 *    query answers;
 *  - its D, R and C refuse it where they say and nowhere else: no INA228, a
 *    capture or a null window running, one that ended by itself;
 *  - nothing it does allocates or frees, and it asks ESP-IDF for no entry
 *    point that does (`DIAGnostic:HEAP?`, sim::World::idfLedger).
 *
 * kFixtures gives each entry its parameters and the state it needs, in the
 * table's order, and the first case fails when the two differ, so a command
 * added to the table without a fixture fails here. kSweptEntries is the count
 * tests/firmware/test_sweep_covers_the_table.py holds to what
 * scripts/scpi_table.py reads from the source.
 */
#include "bench.h"
#include "unit.h"

#include <cctype>
#include <cstddef>
#include <functional>
#include <iterator>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

using bench::Bench;
using kleidos::meter::Console;
using kleidos::meter::scpi::Suffix;

namespace {

/// Entries of the command table, each of them swept.
constexpr std::size_t kSweptEntries = 157;

/// What an entry needs done before it can be given its parameters and succeed.
enum class State {
    Idle,        ///< Nothing but the baseline.
    Running,     ///< A STATE capture streaming.
    Captured,    ///< A capture closed, its figures to fetch.
    RingEnded,   ///< A TRACE ring that filled and is not closed.
    Nulled,      ///< The range has a null of its own.
    ShuntPoint,  ///< A point of the shunt calibration taken.
};

/// One entry's valid call.
struct Fixture {
    std::string_view pattern;          ///< The entry's pattern.
    std::string_view parameters = "";  ///< Valid, and as many as it takes.
    std::size_t      required   = 0;   ///< How many of them cannot be left out.
    State            state      = State::Idle;
    std::string_view suffix     = "";  ///< What its `#` node is given.
    /// The one error its valid call queues on this rig, when it cannot
    /// succeed: a spare relay is not wired (ROUTe:RELay#).
    std::string_view refusal = "";
};

// clang-format off
constexpr Fixture kFixtures[] = {
    { "*IDN?" },
    { "*RST" },
    { "*CLS" },
    { "*ESE", "0", 1 },
    { "*ESE?" },
    { "*ESR?" },
    { "*SRE", "0", 1 },
    { "*SRE?" },
    { "*STB?" },
    { "*OPC" },
    { "*OPC?" },
    { "*WAI" },
    { "*TST?" },
    { "SYSTem:ERRor[:NEXT]?" },
    { "SYSTem:ERRor:COUNt?" },
    { "SYSTem:ERRor:ALL?" },
    { "SYSTem:VERSion?" },
    { "SYSTem:INFormation?" },
    { "SYSTem:STATus?" },
    { "SYSTem:HEALth?" },
    { "SYSTem:COMMunicate:I2C:FREQuency", "400000", 1 },
    { "SYSTem:COMMunicate:I2C:FREQuency?" },
    { "SYSTem:LED?" },
    { "SYSTem:PINS?" },
    { "SYSTem:PINS:PENDing?" },
    { "SYSTem:PINS:STAGe", "RELay1,7,LOW", 2 },
    { "SYSTem:PINS:ADDRess", "AUTO", 1 },
    { "SYSTem:PINS:CLOCk", "400000", 1 },
    { "SYSTem:PINS:CLEar" },
    { "SYSTem:PINS:APPLy" },
    { "SYSTem:PINS:DEFault" },
    { "SYSTem:PINS:RESult?" },
    { "STATus:OPERation[:EVENt]?" },
    { "STATus:OPERation:CONDition?" },
    { "STATus:OPERation:ENABle", "0", 1 },
    { "STATus:OPERation:ENABle?" },
    { "STATus:QUEStionable[:EVENt]?" },
    { "STATus:QUEStionable:CONDition?" },
    { "STATus:QUEStionable:ENABle", "0", 1 },
    { "STATus:QUEStionable:ENABle?" },
    { "STATus:PRESet" },
    { "STATus:QUEStionable:PTRansition", "32767", 1 },
    { "STATus:QUEStionable:PTRansition?" },
    { "STATus:QUEStionable:NTRansition", "0", 1 },
    { "STATus:QUEStionable:NTRansition?" },
    { "STATus:OPERation:PTRansition", "32767", 1 },
    { "STATus:OPERation:PTRansition?" },
    { "STATus:OPERation:NTRansition", "0", 1 },
    { "STATus:OPERation:NTRansition?" },
    { "SENSe:PROFile", "STATE", 1 },
    { "SENSe:PROFile?" },
    { "SENSe:PROFile:REPort?" },
    { "SENSe:CURRent:RANGe[:UPPer]", "1.6", 1 },
    { "SENSe:CURRent:RANGe[:UPPer]?" },
    { "SENSe:CURRent:RANGe:INDex", "0", 1 },
    { "SENSe:CURRent:RANGe:INDex?" },
    { "SENSe:CURRent:RANGe:REPort?" },
    { "SENSe:CURRent:SHUNt", "0.100116", 1 },
    { "SENSe:CURRent:SHUNt?" },
    { .pattern = "SENSe:CURRent:SHUNt:RANGe#", .parameters = "0.100144", .required = 1, .state = State::Idle, .suffix = "1" },
    { .pattern = "SENSe:CURRent:SHUNt:RANGe#?", .parameters = "", .required = 0, .state = State::Idle, .suffix = "1" },
    { .pattern = "SENSe:CURRent:APERture", .parameters = "0.00028", .required = 1 },
    { "SENSe:CURRent:APERture?" },
    { "SENSe:VOLTage:APERture?" },
    { "SENSe:TEMPerature:APERture?" },
    { "SENSe:AVERage:COUNt?" },
    { .pattern = "SENSe:AVERage:COUNt", .parameters = "16", .required = 1 },
    { .pattern = "SENSe:AVERage[:STATe]", .parameters = "OFF", .required = 1 },
    { "SENSe:AVERage[:STATe]?" },
    { "SENSe:FUNCtion:ON", R"("CURR","VOLT",POW,TEMP)", 1 },
    { "SENSe:FUNCtion:ON?" },
    { "SENSe:FUNCtion:ON:DEFault" },
    { "SENSe:CHARge?" },
    { "SENSe:CHARge:RESet" },
    { "SENSe:CAPTure", "DATA", 1 },
    { "SENSe:CAPTure?" },
    { "SENSe:CAPTure:LIMit", "OFF", 1 },
    { "SENSe:CAPTure:LIMit?" },
    { "SENSe:CURRent:THReshold:MARK", "OFF", 1 },
    { "SENSe:CURRent:THReshold:MARK?" },
    { "SENSe:CURRent:THReshold", "0.002", 1 },
    { "SENSe:CURRent:THReshold?" },
    { "SENSe:CURRent:THReshold:HYSTeresis", "0.001", 1 },
    { "SENSe:CURRent:THReshold:HYSTeresis?" },
    { "SAMPle:TIMer?" },
    { "SAMPle:COUNt", "20", 1 },
    { "SAMPle:COUNt?" },
    { "FORMat[:DATA]", "TEXT", 1 },
    { "FORMat[:DATA]?" },
    { "TRIGger:SOURce", "IMM", 1 },
    { "TRIGger:SOURce?" },
    { "TRIGger:LEVel", "0.001", 1 },
    { "TRIGger:LEVel?" },
    { "TRIGger:SLOPe", "NEG", 1 },
    { "TRIGger:SLOPe?" },
    { "TRIGger:PRETrigger", "16", 1 },
    { "TRIGger:PRETrigger?" },
    { "CONFigure[:SCALar]:CURRent[:DC]", "0.1", 0 },
    { "CONFigure[:SCALar]:VOLTage[:DC]" },
    { "CONFigure[:SCALar]:POWer[:DC]" },
    { "CONFigure[:SCALar]:ENERgy" },
    { "CONFigure[:SCALar]:TEMPerature" },
    { "CONFigure?" },
    { "MEASure[:SCALar]:CURRent[:DC]?", "0.1", 0 },
    { "MEASure[:SCALar]:VOLTage[:DC]?" },
    { "MEASure[:SCALar]:POWer[:DC]?" },
    { "MEASure[:SCALar]:ENERgy?" },
    { "MEASure[:SCALar]:TEMPerature?" },
    { "READ?" },
    { "READ[:SCALar]:CURRent[:DC]?" },
    { "READ[:SCALar]:VOLTage[:DC]?" },
    { "READ[:SCALar]:POWer[:DC]?" },
    { "READ[:SCALar]:ENERgy?" },
    { "READ[:SCALar]:TEMPerature?" },
    { "FETCh?", "", 0, State::Captured },
    { "FETCh[:SCALar]:CURRent[:DC]?", "", 0, State::Captured },
    { "FETCh[:SCALar]:VOLTage[:DC]?", "", 0, State::Captured },
    { "FETCh[:SCALar]:POWer[:DC]?", "", 0, State::Captured },
    { "FETCh[:SCALar]:ENERgy?", "", 0, State::Captured },
    { "FETCh[:SCALar]:CHARge?", "", 0, State::Captured },
    { "FETCh[:SCALar]:TEMPerature?", "", 0, State::Captured },
    { "FETCh:STATistics?", "", 0, State::Captured },
    { "FETCh:STATistics:BATCh?", "", 0, State::Captured },
    { "FETCh:HEADer?", "", 0, State::Captured },
    { "INITiate[:IMMediate]" },
    { "INITiate:FORCe" },
    { "ABORt", "", 0, State::Running },
    { "TRACe:MARK", "sweep", 1, State::Running },
    { "TRACe:DATA?", "RAW,0,32", 0, State::RingEnded },
    { "TRACe:POINts?" },
    { "TRACe:POINts:MAXimum?" },
    { "CALibration:ZERO", "1", 1 },
    { "CALibration:ZERO:RESult?", "", 0, State::Nulled },
    { "CALibration:TAG", R"("KM-SWEEP")", 1 },
    { "CALibration:TAG?" },
    { "CALibration:DATE", "2026,11,23", 3 },
    { "CALibration:DATE?" },
    { "CALibration:VBUSside", "SOUR", 1 },
    { "CALibration:VBUSside?" },
    { "CALibration:DATA?" },
    { "CALibration:SHUNt:REFerence", "100", 1 },
    { "CALibration:SHUNt:REFerence?" },
    { "CALibration:SHUNt[:MEASure]", "", 0, State::Nulled },
    { "CALibration:SHUNt:RESult?", "", 0, State::ShuntPoint },
    { "DIAGnostic:ALERt?" },
    { "DIAGnostic:I2C:SCAN?" },
    { "DIAGnostic:REGister?", "#H3E", 1 },
    { "DIAGnostic:HEAP?" },
    { "DIAGnostic:WATChdog?" },
    { "DIAGnostic:WATChdog:TEST" },
    { "ROUTe:LEAD", "CLOS,FORC,LIVE", 1 },
    { "ROUTe:LEAD:CYCLe", "100,FORC,LIVE", 1 },
    { "ROUTe:BYPass", "OFF,FORC,LIVE", 1 },
    { "ROUTe:NULLpath", "OFF,LIVE,FORC", 1 },
    { .pattern = "ROUTe:RELay#", .parameters = "ON,LIVE", .required = 1, .state = State::Idle, .suffix = "3", .refusal = "-241" },
    { "ROUTe:STATe?" },
    { "ROUTe:RESult?" },
};
// clang-format on

/// How long a command's message is let run: an acquisition of 20 samples on
/// STATE, the clock's and the relays' settles, all within it.
constexpr sim::Micros kAnswerUs = 2'000'000;

/// The fixture's parameters, split where the console splits them.
std::vector<std::string> split( std::string_view parameters ) {
    std::vector<std::string> out;
    bool                     quoted = false;
    std::string              item;
    for ( const char c : parameters ) {
        quoted = quoted != ( c == '"' );
        if ( c == ',' && !quoted ) {
            out.push_back( item );
            item.clear();
        } else {
            item += c;
        }
    }
    if ( !item.empty() ) {
        out.push_back( item );
    }
    return out;
}

/// The first @p count of @p parameters, joined.
std::string join( const std::vector<std::string>& parameters, std::size_t count ) {
    std::string out;
    for ( std::size_t i = 0; i < count; ++i ) {
        out += ( i == 0 ) ? parameters[i] : "," + parameters[i];
    }
    return out;
}

/// The header of @p fixture in its long form with every optional node, or in
/// its short form with none.
std::string headerOf( const Fixture& fixture, bool longForm ) {
    std::string out;
    bool        optional = false;
    for ( const char c : fixture.pattern ) {
        if ( c == '[' || c == ']' ) {
            optional = c == '[';
        } else if ( optional && !longForm ) {
            continue;
        } else if ( c == '#' ) {
            out += fixture.suffix;
        } else if ( longForm || std::islower( static_cast<unsigned char>( c ) ) == 0 ) {
            out += c;
        }
    }
    return out;
}

/// @p text in lower case.
std::string lower( std::string text ) {
    for ( char& c : text ) {
        c = static_cast<char>( std::tolower( static_cast<unsigned char>( c ) ) );
    }
    return text;
}

/// The unit of @p header with @p parameters, from the root.
std::string unitOf( const std::string& header, const std::string& parameters ) {
    const std::string root = ( header[0] == '*' ) ? header : ":" + header;
    return parameters.empty() ? root : root + " " + parameters;
}

/// The response units of one message in @p wire, its stream records left
/// out, or nothing while the message's line is not whole. A definite-length
/// block is one unit, whatever its bytes are.
std::optional<std::vector<std::string>> responseUnits( std::string_view wire ) {
    constexpr std::string_view kRecords[] = { "METER ", "METER_MARK ", "S,", "K," };
    std::size_t                at         = 0;
    const auto                 record     = [&] {
        for ( const std::string_view prefix : kRecords ) {
            if ( wire.substr( at ).starts_with( prefix ) ) {
                return true;
            }
        }
        return false;
    };
    while ( record() ) {
        const std::size_t end = wire.find( '\n', at );
        if ( end == std::string_view::npos ) {
            return std::nullopt;
        }
        at = end + 1;
    }
    std::vector<std::string> units( 1 );
    bool                     quoted = false;
    while ( at < wire.size() ) {
        if ( units.back().empty() && wire[at] == '#' ) {
            const std::string_view rest = wire.substr( at + 1 );
            const std::size_t width = rest.empty() ? 0 : static_cast<std::size_t>( rest[0] - '0' );
            if ( rest.size() < 1 + width ) {
                return std::nullopt;
            }
            const std::size_t bytes = std::stoul( std::string( rest.substr( 1, width ) ) );
            if ( rest.size() < 1 + width + bytes ) {
                return std::nullopt;
            }
            units.back() = "#";
            at += 2 + width + bytes;
            continue;
        }
        const char c = wire[at++];
        quoted       = quoted != ( c == '"' );
        if ( c == '\n' ) {
            return units;
        }
        if ( c == ';' && !quoted ) {
            units.emplace_back();
        } else {
            units.back() += c;
        }
    }
    return std::nullopt;
}

/// What one unit of a message came to.
struct Outcome {
    std::size_t units = 0;  ///< The response units it gave.
    std::string answer;     ///< The first of them.
    std::string errors;     ///< The queue after it, as `SYSTem:ERRor:ALL?` reads it.
};

/// Sends @p unit and, in the same message, the query of the queue; empties
/// the queue first.
Outcome give( Bench& b, const std::string& unit ) {
    b.tell( "*CLS", 1'000 );
    (void)b.drain();
    b.world().send( unit + ";:SYST:ERR:ALL?" );
    std::string                             wire;
    std::optional<std::vector<std::string>> units;
    (void)b.world().runUntil(
        [&] {
            wire += b.drain();
            units = responseUnits( wire );
            return units.has_value();
        },
        kAnswerUs );
    if ( !units.has_value() ) {
        return { 0, "", "no answer" };
    }
    return { units->size() - 1, units->size() > 1 ? units->front() : "", units->back() };
}

/// Whether @p errors is one error alone, of @p code.
bool only( const std::string& errors, std::string_view code ) {
    return errors.starts_with( std::string( code ) + ",\"" )
           && errors.find( "\",", code.size() + 2 ) == std::string::npos;
}

/// The instrument put back where every entry starts: no window, the relays
/// at rest, STATE, 20 samples an acquisition.
void baseline( Bench& b ) {
    b.tell( "*RST;:SENS:PROF STATE;:SAMP:COUN 20;*CLS", 100'000 );
    (void)b.drain();
}

/// Puts the instrument in @p state, from the baseline.
void reach( Bench& b, State state ) {
    baseline( b );
    switch ( state ) {
        case State::Idle:
            return;
        case State::Running:
            b.tell( "INIT", 100'000 );
            return;
        case State::Captured:
            b.tell( "INIT", 200'000 );
            b.tell( "ABOR", 20'000 );
            return;
        case State::RingEnded:
            b.tell( "SENS:PROF TRAC;:INIT", 1'500'000 );
            return;
        case State::ShuntPoint:
        case State::Nulled:
            b.tell( "CAL:ZERO 1", 1'500'000 );
            b.tell( "ABOR", 50'000 );
            if ( state == State::ShuntPoint ) {
                b.tell( "CAL:SHUN", 500'000 );
            }
            return;
    }
}

/// The board every case of the sweep runs on.
void board( sim::World& w ) {
    w.ringSamples   = 4096;
    w.circuit.dutNa = 5'000'000;
}

/// Each entry with its fixture, in the table's order; REQUIREs the two lists
/// to be the same.
void eachEntry( const std::function<void( const Console::Command&, const Fixture& )>& body ) {
    const auto table = Console::commands();
    REQUIRE( table.size() == std::size( kFixtures ) );
    std::size_t swept = 0;
    for ( std::size_t i = 0; i < table.size(); ++i ) {
        REQUIRE( table[i].pattern == kFixtures[i].pattern );
        body( table[i], kFixtures[i] );
        ++swept;
    }
    REQUIRE( swept == kSweptEntries );
}

/// True when the entry's flags have @p flag.
bool has( const Console::Command& entry, std::uint32_t flag ) {
    return ( entry.flags & flag ) != 0U;
}

/// Fails, naming @p header, unless @p got is @p want.
void same( const std::string& header, const std::string& got, const std::string& want ) {
    CHECK_EQ( header + ": " + got, header + ": " + want );
}

/// The refusals of the table's flags, as the console words them.
constexpr std::string_view kNoDevice = "-241,\"Hardware missing;no device (scan found none)\"";
constexpr std::string_view kCaptureRuns =
    "-221,\"Settings conflict;capture running (ABORt first)\"";
constexpr std::string_view kCaptureEnded =
    "-221,\"Settings conflict;capture ended, not closed "
    "(ABORt first)\"";
constexpr std::string_view kNullRuns =
    "112,\"Null window running;null window running "
    "(ABORt first)\"";
constexpr std::string_view kNullEnded =
    "112,\"Null window running;null window ended, not "
    "closed (ABORt first)\"";

/// Where a window stands, as `SYSTem:STATus?` tells it.
struct Window {
    std::string_view sink;     ///< `stream`, `ring` or `null`.
    std::string_view running;  ///< `1`, or `0` for one that ended by itself.
};

/// Gives every entry its valid call in a window that @p open opens, opened
/// again whenever an entry closed it, and requires @p refusal of exactly the
/// entries with one of @p flags. `ROUTe:LEAD:CYCLe` is the one entry whose
/// own interlock words its refusal as @p refusal does, so @p interlock says
/// it does in this window.
void sweepWindow( const Window& window, const std::function<void( Bench& )>& open,
                  std::uint32_t flags, std::string_view refusal, bool interlock ) {
    Bench b( board );
    eachEntry( [&]( const Console::Command& entry, const Fixture& fixture ) {
        std::string status = b.ask( "SYST:STAT?" );
        if ( bench::field( status, "sink" ) != window.sink
             || bench::field( status, "running" ) != window.running ) {
            baseline( b );
            open( b );
            status = b.ask( "SYST:STAT?" );
        }
        const std::string header = headerOf( fixture, false );
        same( header, bench::field( status, "sink" ), std::string( window.sink ) );
        same( header, bench::field( status, "running" ), std::string( window.running ) );
        const bool refused = has( entry, flags );
        if ( !refused && entry.pattern == "DIAGnostic:WATChdog:TEST" ) {
            CHECK( false );  // it resets the instrument, whose window it would lose
            return;
        }
        const Outcome outcome = give( b, unitOf( header, std::string( fixture.parameters ) ) );
        if ( refused ) {
            same( header, outcome.errors, std::string( refusal ) );
            CHECK( outcome.units == 0 );
        } else if ( !interlock || entry.pattern != "ROUTe:LEAD:CYCLe" ) {
            CHECK( outcome.errors.find( refusal ) == std::string::npos );
        } else {
            same( header, outcome.errors, std::string( refusal ) );
        }
        CHECK( b.world().heapAllocs == 0 );
        CHECK( b.world().heapFrees == 0 );
        same( header, b.unlistedIdfCalls(), "" );
    } );
}

}  // namespace

TEST( "sweep", "every entry of the table has its fixture, valid and whole" ) {
    eachEntry( []( const Console::Command& entry, const Fixture& fixture ) {
        const std::size_t given = split( fixture.parameters ).size();
        same( std::string( entry.pattern ), std::to_string( given ),
              std::to_string( entry.mostParameters() ) );
        CHECK( fixture.required <= given );
        CHECK( entry.handler != nullptr );
        const bool numbered = entry.pattern.find( '#' ) != std::string_view::npos;
        CHECK( numbered == !fixture.suffix.empty() );
    } );
}

TEST( "sweep", "the long and the short form of every header reach its entry, in any case" ) {
    eachEntry( []( const Console::Command& entry, const Fixture& fixture ) {
        const std::string longForm  = headerOf( fixture, true );
        const std::string shortForm = headerOf( fixture, false );
        for ( const std::string& header :
              { longForm, shortForm, lower( longForm ), lower( shortForm ) } ) {
            Suffix                  suffix = {};
            const Console::Command* found =
                Console::findCommand( Console::commands(), header, suffix );
            REQUIRE( found != nullptr );
            same( header, std::string( found->pattern ), std::string( entry.pattern ) );
            CHECK( found->handler == entry.handler );
            CHECK( suffix.present == !fixture.suffix.empty() );
            // One more character is no header at all.
            CHECK( Console::findCommand( Console::commands(), header + "X", suffix ) == nullptr );
        }
        // No two entries answer one header.
        Suffix      suffix  = {};
        std::size_t matched = 0;
        for ( const Console::Command& other : Console::commands() ) {
            matched += kleidos::meter::scpi::matches( other.pattern, shortForm, suffix ) ? 1U : 0U;
        }
        same( shortForm, std::to_string( matched ), "1" );
    } );
}

TEST( "sweep", "every query answers one unit, every command none, and nothing allocates" ) {
    kleidos::meter::heap::disarm();
    Bench b( board );
    eachEntry( [&]( const Console::Command& entry, const Fixture& fixture ) {
        if ( entry.pattern == "DIAGnostic:WATChdog:TEST" ) {
            return;  // it answers nothing and resets the instrument: the next case
        }
        for ( const bool longForm : { false, true } ) {
            reach( b, fixture.state );
            const std::string header = headerOf( fixture, longForm );
            const Outcome outcome = give( b, unitOf( header, std::string( fixture.parameters ) ) );
            same( header, std::to_string( outcome.units ),
                  entry.pattern.ends_with( '?' ) ? "1" : "0" );
            if ( fixture.refusal.empty() ) {
                same( header, outcome.errors, "0,\"No error\"" );
            } else if ( !only( outcome.errors, fixture.refusal ) ) {
                same( header, outcome.errors, std::string( fixture.refusal ) );
            }
            CHECK( !entry.pattern.ends_with( '?' ) || !outcome.answer.empty() );
            b.world().run( 300'000 );  // a lead cycle's reclose
            const std::string report = b.ask( "DIAG:HEAP?" );
            same( header,
                  bench::field( report, "allocs_since_main" ) + ","
                      + bench::field( report, "frees_since_main" ),
                  "0,0" );
            CHECK( b.world().heapAllocs == 0 );
            CHECK( b.world().heapFrees == 0 );
            same( header, b.unlistedIdfCalls(), "" );
        }
    } );
}

TEST( "sweep", "the watchdog's test, which nothing answers after" ) {
    for ( const bool longForm : { false, true } ) {
        Bench b( board );
        b.world().mwdt.resetExpected = true;
        b.tell( longForm ? "DIAGnostic:WATChdog:TEST" : "DIAG:WATC:TEST", 7'000'000 );
        b.world().noteMwdt();
        CHECK( b.world().mwdt.resetAtUs >= 0 );
        CHECK( b.world().heapAllocs == 0 );
        CHECK( b.world().heapFrees == 0 );
    }
}

TEST( "sweep", "a parameter too many is -108 and one missing -109, and neither changes anything" ) {
    Bench b( board );
    eachEntry( [&]( const Console::Command&, const Fixture& fixture ) {
        baseline( b );
        const std::string              header     = headerOf( fixture, false );
        const std::vector<std::string> parameters = split( fixture.parameters );
        // The setting this header writes, when the table has its query.
        Suffix            suffix = {};
        const std::string query  = header + "?";
        const bool setting = Console::findCommand( Console::commands(), query, suffix ) != nullptr;
        const auto read    = [&] { return setting ? b.ask( query ) : std::string(); };
        const std::string before = read();

        const std::string full    = join( parameters, parameters.size() );
        const std::string more    = full.empty() ? "1" : full + ",1";
        Outcome           outcome = give( b, unitOf( header, more ) );
        if ( !only( outcome.errors, "-108" ) ) {
            same( header + " " + more, outcome.errors, "-108 alone" );
        }
        CHECK( outcome.units == 0 );
        same( header, read(), before );

        if ( fixture.required > 0 ) {
            const std::string fewer = join( parameters, fixture.required - 1 );
            outcome                 = give( b, unitOf( header, fewer ) );
            if ( !only( outcome.errors, "-109" ) ) {
                same( header + " " + fewer, outcome.errors, "-109 alone" );
            }
            CHECK( outcome.units == 0 );
            same( header, read(), before );
        }
        CHECK( b.world().heapAllocs == 0 );
        same( header, b.unlistedIdfCalls(), "" );
    } );
}

TEST( "sweep", "the parameter counts a refusal names" ) {
    Bench b( board );
    CHECK_EQ( give( b, "*IDN? 1" ).errors,
              "-108,\"Parameter not allowed;*IDN? takes no parameter\"" );
    CHECK_EQ( give( b, "CONF:VOLT 1" ).errors,
              "-108,\"Parameter not allowed;CONF:VOLT takes no parameter\"" );
    CHECK_EQ( give( b, "MEAS:POW? 1" ).errors,
              "-108,\"Parameter not allowed;MEAS:POW? takes no parameter\"" );
    CHECK_EQ( give( b, "SENS:PROF STATE,SLEEP" ).errors,
              "-108,\"Parameter not allowed;SENS:PROF takes at most 1 parameter\"" );
    CHECK_EQ( give( b, "CAL:DATE 2026,11,23,1" ).errors,
              "-108,\"Parameter not allowed;CAL:DATE takes at most 3 parameters\"" );
    // With no form written, the place and the count are all the dump takes.
    reach( b, State::RingEnded );
    CHECK( give( b, "TRAC:DATA? 0,8" ).units == 1 );
    CHECK_EQ( give( b, "TRAC:DATA? 0,8,8" ).errors,
              "-108,\"Parameter not allowed;use TRACe:DATA? [SCALed|RAW][,<from>[,<count>]]\"" );
}

TEST( "sweep", "D: without an INA228 exactly the entries that need it are refused" ) {
    Bench b( []( sim::World& w ) {
        board( w );
        w.ina.present = false;
    } );
    eachEntry( [&]( const Console::Command& entry, const Fixture& fixture ) {
        if ( entry.pattern == "DIAGnostic:WATChdog:TEST" ) {
            CHECK( !has( entry, Console::kNeedsDevice ) );  // it would reset the instrument
            return;
        }
        const std::string header  = headerOf( fixture, false );
        const Outcome     outcome = give( b, unitOf( header, std::string( fixture.parameters ) ) );
        if ( has( entry, Console::kNeedsDevice ) ) {
            same( header, outcome.errors, std::string( kNoDevice ) );
            CHECK( outcome.units == 0 );
        } else {
            CHECK( outcome.errors.find( kNoDevice ) == std::string::npos );
        }
        CHECK( b.world().heapAllocs == 0 );
        same( header, b.unlistedIdfCalls(), "" );
    } );
}

TEST( "sweep", "R: exactly the entries that say so are refused under a capture" ) {
    sweepWindow(
        { "stream", "1" }, []( Bench& b ) { b.tell( "INIT", 100'000 ); }, Console::kRefusedOpen,
        kCaptureRuns, false );
}

TEST( "sweep", "R: exactly the entries that say so are refused under a null window" ) {
    sweepWindow(
        { "null", "1" }, []( Bench& b ) { b.tell( "CAL:ZERO 60", 50'000 ); }, Console::kRefusedOpen,
        kNullRuns, true );
}

TEST( "sweep", "C: exactly the entries that say so are refused while a full ring is not closed" ) {
    sweepWindow(
        { "ring", "0" }, []( Bench& b ) { b.tell( "SENS:PROF TRAC;:INIT", 1'500'000 ); },
        Console::kRefusedWhileOpen, kCaptureEnded, false );
}

TEST( "sweep",
      "C: exactly the entries that say so are refused while an ended null is not closed" ) {
    sweepWindow(
        { "null", "0" }, []( Bench& b ) { b.tell( "CAL:ZERO 1", 1'500'000 ); },
        Console::kRefusedWhileOpen, kNullEnded, false );
}
