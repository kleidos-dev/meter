// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_golden.cpp
 * @brief The golden transcript: every byte the instrument writes to a
 *        scripted host session, compared with golden/console.transcript in
 *        every run (FW-24).
 *
 * The formatter rewrite (FW-24 to FW-26) replaces every `snprintf` of the
 * firmware with the bounded writer of text.h, and the wire format is a
 * contract (docs/scpi.md): the rewrite must change no byte. The sessions
 * below are what holds it. They boot simulated instruments, keep the boot's
 * announcement, ask every query of the console's command table
 * (tests/firmware/test_golden_session_covers_the_table.py holds the sessions
 * to `kCommands`), provoke every error the host harness can reach, stream
 * every record of both formats (samples, marks, threshold marks) and dump
 * the ring in both formats, plain and under a level trigger; each step's
 * output is recorded byte for byte. The harness's world runs on virtual time
 * and is deterministic, so the transcript is the same on every host, at
 * every optimization and at 32 bits.
 *
 * A change that means to change the wire regenerates the file and shows the
 * difference in its diff:
 *
 *     METER_GOLDEN_WRITE=1 ./build/test_firmware golden   # from firmware/test/native
 *     git diff firmware/test/native/golden/console.transcript
 *
 * The transcript's format: `## <session>` opens a session, `> <message>` is
 * a program message sent (with its newline), `! <note>` a step that changed
 * the world, `< <line>` a line the instrument wrote (its newline removed),
 * and `<+ <text>` a step's output that did not end in a newline; an empty
 * line is the mark alone. A byte that is not printable ASCII is written
 * `\xHH`, a backslash `\\`, and a space that ends a line `\x20`, so that
 * no line of the file ends in white space.
 */
#include "bench.h"
#include "pinmap.h"
#include "pinmap_store.h"
#include "unit.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <span>
#include <sstream>
#include <string>
#include <string_view>

#ifndef METER_GOLDEN_FILE
    #error "the Makefile passes the golden transcript's path as METER_GOLDEN_FILE"
#endif

using bench::Bench;

namespace {

/// What a step may do to the world before its message is sent.
using Action = void ( * )( sim::World& );

/// One step of a session: a program message, how long the world runs after
/// it, and what changes in the world first, if anything.
struct Step {
    const char* message;           ///< The program message ("" sends none).
    sim::Micros runUs;             ///< How long the world runs after it.
    const char* note   = nullptr;  ///< The transcript's line for @ref before.
    Action      before = nullptr;  ///< Applied before the message is sent.
};

/// @p bytes as the transcript writes them: printable ASCII as it is, a
/// backslash doubled, any other byte as `\xHH`.
std::string escaped( std::string_view bytes ) {
    std::string out;
    for ( const char c : bytes ) {
        const auto byte = static_cast<unsigned char>( c );
        if ( c == '\\' ) {
            out += "\\\\";
        } else if ( byte >= 0x20 && byte <= 0x7E ) {
            out += c;
        } else {
            char hex[8] = {};
            std::snprintf( hex, sizeof( hex ), "\\x%02X", static_cast<unsigned>( byte ) );
            out += hex;
        }
    }
    // A space that ends a line is written as a byte, so that no line of the
    // file ends in white space the editors and hooks would strip.
    if ( !out.empty() && out.back() == ' ' ) {
        out.pop_back();
        out += "\\x20";
    }
    return out;
}

/// One line of the transcript: @p mark, then @p body after a space if it
/// is not empty.
std::string line( std::string_view mark, std::string_view body ) {
    std::string out( mark );
    if ( !body.empty() ) {
        out += ' ';
        out += body;
    }
    out += '\n';
    return out;
}

/// The transcript being written.
class Transcript {
   public:
    void session( std::string_view name ) {
        text_ += "## ";
        text_ += name;
        text_ += '\n';
    }

    void note( std::string_view what ) {
        text_ += "! ";
        text_ += what;
        text_ += '\n';
    }

    void sent( std::string_view message ) { text_ += line( ">", escaped( message ) ); }

    void received( std::string_view bytes ) {
        while ( !bytes.empty() ) {
            const std::size_t end = bytes.find( '\n' );
            if ( end == std::string_view::npos ) {
                text_ += line( "<+", escaped( bytes ) );
                return;
            }
            text_ += line( "<", escaped( bytes.substr( 0, end ) ) );
            bytes.remove_prefix( end + 1 );
        }
    }

    const std::string& text() const { return text_; }

   private:
    std::string text_;
};

/// Runs @p steps on @p b, recording each and what the instrument wrote.
void play( Bench& b, Transcript& t, std::span<const Step> steps ) {
    for ( const Step& step : steps ) {
        if ( step.note != nullptr ) {
            t.note( step.note );
        }
        if ( step.before != nullptr ) {
            step.before( b.world() );
        }
        const std::string message = step.message;
        if ( !message.empty() ) {
            t.sent( message );
            b.world().send( message );
        }
        b.world().run( step.runUs );
        t.received( b.drain() );
    }
}

/// Boots a Bench arranged by @p setup, records its boot, then @p steps.
void session( Transcript& t, std::string_view name, const Bench::Setup& setup,
              std::span<const Step> steps ) {
    t.session( name );
    Bench b( setup );
    t.received( b.bootOutput() );
    play( b, t, steps );
}

// --- The sessions ------------------------------------------------------------

/// Every query of the command table, and every setting beside the query that
/// reads it back, on an instrument whose DUT draws 5 mA through the shunt.
constexpr Step kGoldenQueries[] = {
    // IEEE 488.2's common commands.
    { "*IDN?", 20'000 },
    { "*ESR?;*ESR?", 20'000 },
    { "*ESE 36;*SRE 48", 20'000 },
    { "*ESE?;*SRE?", 20'000 },
    { "*STB?", 20'000 },
    { "*OPC;*OPC?", 20'000 },
    { "*WAI;*ESR?", 20'000 },
    { "*TST?", 500'000 },
    { "*CLS;*ESE 0;*SRE 0", 20'000 },
    // SYSTem.
    { "SYST:ERR?", 20'000 },
    { "SYST:ERR:COUN?", 20'000 },
    { "SYST:ERR:ALL?", 20'000 },
    { "SYST:VERS?", 20'000 },
    { "SYST:INF?", 20'000 },
    { "SYST:STAT?", 20'000 },
    { "SYST:HEAL?", 20'000 },
    { "SYST:LED?", 20'000 },
    { "SYST:COMM:I2C:FREQ?", 20'000 },
    { "SYST:COMM:I2C:FREQ 137000;:SYST:COMM:I2C:FREQ?", 20'000 },
    { "SYST:COMM:I2C:FREQ 400000;:SYST:COMM:I2C:FREQ?", 20'000 },
    // STATus.
    { "STAT:OPER?", 20'000 },
    { "STAT:OPER:COND?", 20'000 },
    { "STAT:OPER:ENAB 16;:STAT:OPER:ENAB?", 20'000 },
    { "STAT:QUES?", 20'000 },
    { "STAT:QUES:COND?", 20'000 },
    { "STAT:QUES:ENAB 256;:STAT:QUES:ENAB?", 20'000 },
    { "STAT:QUES:PTR 32767;:STAT:QUES:PTR?", 20'000 },
    { "STAT:QUES:NTR 256;:STAT:QUES:NTR?", 20'000 },
    { "STAT:OPER:PTR 32767;:STAT:OPER:PTR?", 20'000 },
    { "STAT:OPER:NTR 16;:STAT:OPER:NTR?", 20'000 },
    { "*SRE 255;*STB?", 20'000 },
    { "STAT:PRES;:STAT:QUES:ENAB?;:STAT:OPER:ENAB?;*SRE 0", 20'000 },
    // SENSe.
    { "SENS:PROF STATE;:SENS:PROF?", 20'000 },
    { "SENS:PROF:REP?", 20'000 },
    { "SENS:CURR:RANG?", 20'000 },
    { "SENS:CURR:RANG 0.04;:SENS:CURR:RANG?;:SENS:CURR:RANG:IND?", 20'000 },
    { "SENS:CURR:RANG:IND 0;:SENS:CURR:RANG:IND?", 20'000 },
    { "SENS:CURR:RANG:REP?", 20'000 },
    { "SENS:CURR:SHUN?", 20'000 },
    { "SENS:CURR:SHUN 0.100116;:SENS:CURR:SHUN?", 20'000 },
    { "SENS:CURR:SHUN:RANG1 0.100144;:SENS:CURR:SHUN:RANG1?;:SENS:CURR:SHUN:RANG0?", 20'000 },
    { "SENS:CURR:APER?", 20'000 },
    { "SENS:VOLT:APER?", 20'000 },
    { "SENS:TEMP:APER?", 20'000 },
    { "SENS:AVER:COUN?", 20'000 },
    { R"(SENS:FUNC:ON "CURR","VOLT","POW","TEMP";:SENS:FUNC:ON?)", 20'000 },
    { "SENS:FUNC:ON:DEF;:SENS:FUNC:ON?", 20'000 },
    { "SENS:CHAR?", 1'200'000 },
    { "SENS:CHAR:RES;:SENS:CHAR?", 1'200'000 },
    { "SENS:CAPT?", 20'000 },
    { "SENS:CAPT STAT;:SENS:CAPT?;:SENS:CAPT DATA", 20'000 },
    { "SENS:CAPT:LIM?", 20'000 },
    { "SENS:CAPT:LIM ON;:SENS:CAPT:LIM?;:SENS:CAPT:LIM OFF", 20'000 },
    { "SENS:CURR:THR?", 20'000 },
    { "SENS:CURR:THR 0.002;:SENS:CURR:THR?", 20'000 },
    { "SENS:CURR:THR:HYST?", 20'000 },
    { "SENS:CURR:THR:HYST 0.0005;:SENS:CURR:THR:HYST?", 20'000 },
    { "SENS:CURR:THR:MARK?", 20'000 },
    { "SENS:CURR:THR:MARK ON;:SENS:CURR:THR:MARK?", 20'000 },
    { "SENS:CURR:THR:MARK OFF;:SENS:CURR:THR OFF;:SENS:CURR:THR?", 20'000 },
    // SAMPle, FORMat, TRIGger.
    { "SAMP:TIM?", 20'000 },
    { "SAMP:COUN?", 20'000 },
    { "SAMP:COUN 20;:SAMP:COUN?", 20'000 },
    { "FORM?", 20'000 },
    { "FORM CSV;:FORM?;:FORM TEXT", 20'000 },
    { "TRIG:SOUR?;:TRIG:LEV?;:TRIG:SLOP?;:TRIG:PRET?", 20'000 },
    { "TRIG:SOUR LEV;:TRIG:LEV -0.0125;:TRIG:SLOP NEG;:TRIG:PRET 1000", 20'000 },
    { "TRIG:SOUR?;:TRIG:LEV?;:TRIG:SLOP?;:TRIG:PRET?", 20'000 },
    { "TRIG:SOUR IMM;:TRIG:LEV 0;:TRIG:SLOP POS;:TRIG:PRET 0", 20'000 },
    // CONFigure, MEASure, READ, FETCh.
    { "CONF?", 20'000 },
    { "CONF:VOLT;:CONF?", 20'000 },
    { "CONF:POW;:CONF?", 20'000 },
    { "CONF:ENER;:CONF?", 20'000 },
    { "CONF:TEMP;:CONF?", 20'000 },
    { "CONF:CURR;:CONF?", 20'000 },
    { "MEAS:CURR?", 2'000'000 },
    { "MEAS:VOLT?", 2'000'000 },
    { "MEAS:POW?", 2'000'000 },
    { "MEAS:ENER?", 2'000'000 },
    { "MEAS:TEMP?", 2'000'000 },
    { "CONF:CURR", 20'000 },
    { "READ?", 2'000'000 },
    { "READ:CURR?", 2'000'000 },
    { "READ:VOLT?", 2'000'000 },
    { "READ:POW?", 2'000'000 },
    { "READ:ENER?", 2'000'000 },
    { "READ:TEMP?", 2'000'000 },
    { "FETC?", 20'000 },
    { "FETC:CURR?", 20'000 },
    { "FETC:VOLT?", 20'000 },
    { "FETC:POW?", 20'000 },
    { "FETC:ENER?", 20'000 },
    { "FETC:CHAR?", 20'000 },
    { "FETC:TEMP?", 20'000 },
    { "FETC:STAT?", 20'000 },
    { "FETC:STAT:BATC?", 20'000 },
    // A short STATE stream in TEXT: its header, its samples and a mark.
    { "SENS:PROF STATE;:INIT;:FETC:HEAD?", 12'000 },
    { "TRAC:MARK golden", 6'000 },
    { "ABOR;:FETC:STAT?", 20'000 },
    { "FETC:STAT:BATC?", 20'000 },
    // The same in CSV, forced.
    { "FORM CSV;:INIT:FORC", 12'000 },
    { "TRAC:MARK csv", 6'000 },
    { "ABOR;:FORM TEXT", 20'000 },
    // TRACe: a full ring and its dumps.
    { "TRAC:POIN?;:TRAC:POIN:MAX?", 20'000 },
    { "SENS:PROF TRAC;:INIT", 1'400'000 },
    { "TRAC:POIN?", 20'000 },
    { "TRAC:DATA? RAW,0,20", 300'000 },
    { "TRAC:DATA? SCAL,100,5", 300'000 },
    { "TRAC:DATA? RAW,1020,8", 300'000 },
    { "TRAC:DATA? SCAL,4090,10", 300'000 },
    { "ABOR;:SENS:PROF STATE", 50'000 },
    // CALibration.
    { "CAL:DATA?", 20'000 },
    { "CAL:TAG?", 20'000 },
    { R"(CAL:TAG "KM-GOLDEN";:CAL:TAG?)", 20'000 },
    { "CAL:DATE?", 20'000 },
    { "CAL:DATE 2026,11,22;:CAL:DATE?", 20'000 },
    { "CAL:VBUS?", 20'000 },
    { "CAL:VBUS SOUR;:CAL:VBUS?", 20'000 },
    { "CAL:SHUN:REF?", 20'000 },
    { "CAL:SHUN:REF 100;:CAL:SHUN:REF?", 20'000 },
    { "CAL:ZERO:RES?", 20'000 },
    { "ROUT:NULL ON", 500'000 },
    { "CAL:ZERO 1", 1'500'000 },
    { "ABOR;:CAL:ZERO:RES?", 100'000 },
    { "ROUT:NULL OFF", 500'000 },
    { "CAL:SHUN:RES?", 20'000 },
    { "SAMP:COUN 200;:CAL:SHUN", 1'000'000 },
    { "CAL:SHUN:RES?", 20'000 },
    { "CAL:DATA?", 20'000 },
    // DIAGnostic.
    { "DIAG:ALER?", 20'000 },
    { "DIAG:I2C:SCAN?", 200'000 },
    { "DIAG:REG? #H0;:DIAG:REG? #H4;:DIAG:REG? #H3E;:DIAG:REG? #H3F", 20'000 },
    { "DIAG:HEAP?", 20'000 },
    { "DIAG:WATC?", 20'000 },
    // ROUTe.
    { "ROUT:STAT?", 20'000 },
    { "ROUT:RES?", 20'000 },
    { "ROUT:BYP ON;:ROUT:STAT?", 500'000 },
    { "ROUT:BYP OFF;:ROUT:RES?", 500'000 },
    { "ROUT:LEAD OPEN,FORC;:ROUT:STAT?", 500'000 },
    { "ROUT:LEAD CLOS;:ROUT:RES?", 500'000 },
    { "ROUT:LEAD:CYCL 100,FORC", 600'000 },
    { "ROUT:RES?", 20'000 },
    { "ROUT:REL2 ON;:ROUT:REL2 OFF;:ROUT:RES?", 600'000 },
    // A reset, and what it left.
    { "*RST", 100'000 },
    { "SENS:PROF?;:FORM?;:SAMP:COUN?;:CONF?;:SYST:ERR:ALL?", 20'000 },
};

/// One error of every kind the line and the commands give, each read back.
constexpr Step kGoldenErrors[] = {
    // The command errors.
    { "*CLS;CAL:TAG \"a\x7F"
      "b\"",
      20'000 },
    { "SYST:ERR?", 20'000 },
    { "SAMP:COUN 1,,2;:SYST:ERR?", 20'000 },
    { "SENS:PROF 7;:SYST:ERR?", 20'000 },
    { "*IDN? 1;:SYST:ERR?", 20'000 },
    { "SAMP:COUN;:SYST:ERR?", 20'000 },
    { "BOGUS;:FOO:BAR?;:SYST:ERR:ALL?", 20'000 },
    { "ROUT:REL1 ON;:ROUT:REL5 ON;:SYST:ERR:ALL?", 20'000 },
    { "SAMP:COUN 1x;:SYST:ERR?", 20'000 },
    { R"(CAL:TAG "open;:SYST:ERR?)", 20'000 },
    { "SYST:ERR?", 20'000 },
    // The execution errors.
    { "SAMP:COUN 0;:SYST:ERR?", 20'000 },
    { "SENS:PROF FAST;:SYST:ERR?", 20'000 },
    { "*RST;FETC:STAT?;:SYST:ERR?", 100'000 },
    { "ROUT:REL3 ON;:SYST:ERR?", 20'000 },
    { "SENS:PROF STATE;:INIT;:SENS:PROF SLEEP;:SYST:ERR?", 20'000 },
    { "ABOR;:ABOR;:SYST:ERR?", 50'000 },
    { "SYST:COMM:I2C:FREQ 50000;:SENS:PROF TRAC;:INIT;:SYST:ERR:ALL?", 50'000 },
    { "SYST:COMM:I2C:FREQ 400000;:SENS:PROF STATE", 20'000 },
    { "SYST:COMM:I2C:FREQ 400000;:SYST:ERR?", 20'000, "the bus's core refuses its IPC call",
      []( sim::World& w ) { w.faults.failIpc = true; } },
    { "SYST:ERR?", 20'000, "the bus's core takes its IPC calls again",
      []( sim::World& w ) { w.faults.failIpc = false; } },
    // The device-specific errors.
    { "", 20'000, "a 300-byte program message",
      []( sim::World& w ) { w.send( std::string( 300, 'A' ) ); } },
    { "SYST:ERR:ALL?", 20'000 },
    { "NOPE;NOPE;NOPE;NOPE;NOPE;NOPE;NOPE;NOPE;NOPE;NOPE;NOPE;NOPE", 50'000 },
    { "SYST:ERR:ALL?", 20'000 },
    { "CAL:TAG unsaved;:SYST:ERR?", 50'000, "the flash refuses to program",
      []( sim::World& w ) { w.flash.failProgram = true; } },
    { "SYST:ERR?", 20'000, "the flash programs again",
      []( sim::World& w ) { w.flash.failProgram = false; } },
    // The device-defined errors.
    { "ROUT:BYP ON", 500'000 },
    { "INIT;:SYST:ERR?", 50'000 },
    { "ROUT:BYP OFF", 500'000 },
    { "ROUT:LEAD OPEN;:SYST:ERR?", 500'000 },
    { "SENS:PROF STATE;:INIT", 20'000 },
    { "TRAC:MARK m1;:TRAC:MARK m2;:TRAC:MARK m3;:TRAC:MARK m4;:TRAC:MARK m5;:TRAC:MARK m6",
      20'000 },
    { "TRAC:MARK m7;:TRAC:MARK m8;:TRAC:MARK m9;:TRAC:MARK m10;:TRAC:MARK m11;:TRAC:MARK m12",
      20'000 },
    { "TRAC:MARK m13;:TRAC:MARK m14;:TRAC:MARK m15;:TRAC:MARK m16;:TRAC:MARK m17", 20'000 },
    { "ROUT:BYP ON;:SYST:ERR:ALL?", 50'000 },
    { "ABOR;:SYST:ERR:ALL?", 50'000 },
    { "SENS:PROF SLEEP;:CAL:ZERO 5;:ROUT:BYP ON;:SYST:ERR?", 100'000 },
    { "ABOR", 300'000 },
    { "ROUT:NULL ON;:SYST:ERR:ALL?", 500'000 },
    { "", 121'000'000, "the hold's 120 s run out" },
    { "SYST:ERR:ALL?", 20'000 },
    { "ROUT:LEAD OPEN,FORC", 500'000 },
    { "", 121'000'000, "the hold's 120 s run out" },
    { "SYST:ERR:ALL?", 20'000 },
    { "ROUT:BYP ON", 500'000 },
    { "", 121'000'000, "the hold's 120 s run out" },
    { "SYST:ERR:ALL?;:ROUT:STAT?", 20'000 },
};

/// A level trigger that fires inside a full ring, and the dump that shows it.
constexpr Step kGoldenTrigger[] = {
    { "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.05;"
      ":TRIG:PRET 1000;:INIT",
      10'000 },
    { "FETC:HEAD?;:SYST:STAT?", 20'000 },
    { "", 3'100'000, "the burst at 2 s and the samples after it" },
    { "SYST:STAT?;:TRAC:POIN?", 20'000 },
    { "TRAC:DATA? RAW,0,24", 300'000 },
    { "TRAC:DATA? SCAL,0,4", 300'000 },
    { "ABOR;:FETC:STAT?", 50'000 },
};

/// Threshold marks in both formats, on a current that crosses the level.
constexpr Step kGoldenThreshold[] = {
    { "SENS:CURR:THR 0.04;:SENS:CURR:THR:MARK ON;:SENS:PROF STAT;:INIT", 60'000 },
    { "ABOR;:FETC:STAT?", 50'000 },
    { "FORM CSV;:INIT", 60'000 },
    { "ABOR;:FETC:STAT?", 50'000 },
};

/// The INA228 absent: the announcement and the refusals by name.
constexpr Step kGoldenNoDevice[] = {
    { "SYST:ERR:ALL?", 20'000 },
    { "*TST?;:SYST:INF?", 500'000 },
    { "SENS:PROF STATE;:MEAS:CURR?;:SYST:ERR:ALL?", 100'000 },
};

/// A sampler and relays that did not start.
constexpr Step kGoldenBrokenBoot[] = {
    { "SYST:ERR:ALL?", 20'000 },
    { "*TST?", 500'000 },
    { "ROUT:STAT?;:ROUT:RES?;:ROUT:REL2 ON;:SENS:PROF STATE;:INIT;:SYST:ERR:ALL?", 100'000 },
};

/// A null path whose contacts do not move.
constexpr Step kGoldenStuckLead[] = {
    { "ROUT:NULL ON;:SYST:ERR:ALL?", 500'000 },
    { "ROUT:BYP ON;:ROUT:LEAD OPEN", 300'000 },
    { "SENS:PROF SLEEP;:CAL:ZERO 1", 2'000'000 },
    { "ABOR;:SYST:ERR:ALL?;:CAL:ZERO:RES?", 300'000 },
    { "ROUT:NULL OFF", 300'000 },
    { "CAL:ZERO 10;:ABOR;:SYST:ERR:ALL?", 1'500'000 },
};

/// A bypass whose contact does not close.
constexpr Step kGoldenStuckBypass[] = {
    { "ROUT:NULL ON;:SYST:ERR:ALL?;:ROUT:STAT?", 500'000 },
};

/// The boot after a watchdog reset that found K1 and K2 energized.
constexpr Step kGoldenWarmBoot[] = {
    { "SYST:ERR:ALL?", 20'000 },
    { "SYST:HEAL?;:ROUT:STAT?", 20'000 },
};

/// The pin map and the averaging override (0.18.0): the live map, the staged
/// one, the three refusals its contract names, each a byte the simulator must
/// match, the orders that try a map and keep it or put it back (FW-05), and the
/// averaging override off (its own capture's bytes are those of 0.17.0, in the
/// sessions above), on, refused and off again (FW-06).
constexpr Step kGoldenPins[] = {
    { "SYST:PINS?", 20'000 },
    { "SYST:PINS:PEND?", 20'000 },
    { "SYST:PINS:STAG SDA,45;:SYST:PINS:PEND?", 20'000 },
    { "SYST:PINS:CLE;:SYST:PINS:STAG SDA,5;:SYST:PINS:PEND?", 20'000 },
    { "SYST:PINS:CLE;:SYST:PINS:STAG ALER,39;:SYST:PINS:PEND?", 20'000 },
    { "SYST:PINS:CLE;:SYST:PINS:STAG REL2,16,LOW;:SYST:PINS:ADDR #H41;:SYST:PINS:PEND?", 20'000 },
    { "SYST:PINS:CLE;:SYST:PINS:RES?", 20'000 },
    { "SYST:PINS:STAG SDA,64;:SYST:PINS:ADDR 0;:SYST:ERR:ALL?", 20'000 },
    // FW-05: a map the INA228 is not on, put back; one that breaks a rule;
    // one that is kept; and the defaults.
    { "SYST:PINS:STAG SDA,18;:SYST:PINS:APPL;:SYST:ERR:ALL?;:SYST:PINS:RES?", 2'000'000 },
    { "SYST:PINS:STAG SDA,45;:SYST:PINS:APPL;:SYST:ERR:ALL?;:SYST:PINS:RES?", 20'000 },
    { "SYST:PINS:CLE;:SYST:PINS:CLOC 100000;:SYST:PINS:APPL;:SYST:PINS:RES?;:SYST:PINS?",
      2'000'000 },
    { "SYST:PINS:DEF;:SYST:PINS:RES?;:SYST:PINS?;:SYST:ERR:ALL?", 2'000'000 },
    { "SENS:AVER?;:SENS:AVER:COUN 4;:SENS:CURR:APER 0.00028;:SENS:PROF:REP?;:SYST:ERR:ALL?",
      20'000 },
    { "SENS:AVER ON;:SENS:PROF:REP?;:SENS:AVER:COUN 1024;:SENS:CURR:APER 0.002074;:SYST:ERR:ALL?",
      20'000 },
    { "SENS:PROF BURS;:SENS:AVER ON;:SENS:PROF STAT;:SENS:AVER:COUN 4;:SENS:AVER ON;:SYST:ERR:ALL?",
      20'000 },
    { "INIT", 200'000 },
    { "ABOR;:FETC:HEAD?", 50'000 },
    { "SENS:AVER OFF;:SENS:PROF:REP?;:SYST:ERR:ALL?", 20'000 },
};

/// A boot whose stored map's bus the INA228 is not on (FW-04): what it says
/// and what it runs.
constexpr Step kGoldenSilentBus[] = {
    { "SYST:ERR:ALL?", 20'000 },
    { "SYST:PINS?;:SYST:PINS:PEND?", 20'000 },
    { "SYST:INF?;:ROUT:STAT?", 20'000 },
};

/// Stores the default map with its bus on GPIO18 and GPIO21, and Relay 1 on
/// @p relay1, as an earlier boot's apply would have.
void storeMovedBus( std::int32_t relay1 ) {
    kleidos::meter::pinmap::PinMap map = kleidos::meter::pinmap::kDefaultMap;
    map.pins                           = { 18, 21, 6, relay1, 15 };
    REQUIRE( kleidos::meter::pinmap_store::save( map ) == kleidos::meter::pinmap_store::Saved::Ok );
}

/// Every session, appended to @p t in order.
void record( Transcript& t ) {
    session(
        t, "every query",
        []( sim::World& w ) {
            w.ringSamples   = 4096;
            w.circuit.dutNa = 5'000'000;
        },
        kGoldenQueries );
    session(
        t, "errors",
        []( sim::World& w ) {
            w.ringSamples   = 4096;
            w.circuit.dutNa = 5'000'000;
        },
        kGoldenErrors );
    session(
        t, "a level trigger",
        []( sim::World& w ) {
            w.ringSamples     = 4096;
            w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
                return ( now >= 2'000'000 && now < 2'001'000 ) ? 100'000'000 : 1'000'000;
            };
        },
        kGoldenTrigger );
    session(
        t, "threshold marks",
        []( sim::World& w ) {
            w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
                return ( now % 20'000 ) < 5'000 ? 80'000'000 : 2'000'000;
            };
        },
        kGoldenThreshold );
    session( t, "no INA228", []( sim::World& w ) { w.ina.present = false; }, kGoldenNoDevice );
    session(
        t, "a sampler and relays that did not start",
        []( sim::World& w ) {
            w.faults.failTask          = true;
            w.faults.failTaskName      = "meter-sample";
            w.faults.failGpioConfig    = true;
            w.faults.failGpioConfigPin = 7;
        },
        kGoldenBrokenBoot );
    session(
        t, "a lead that does not open",
        []( sim::World& w ) {
            w.circuit.dutNa           = 5'000'000;
            w.circuit.leadStuckClosed = true;
        },
        kGoldenStuckLead );
    session(
        t, "a bypass that does not close",
        []( sim::World& w ) {
            w.circuit.dutNa           = 5'000'000;
            w.circuit.bypassStuckOpen = true;
        },
        kGoldenStuckBypass );

    // The warm boot needs the record a first instrument's null path left in
    // RTC memory when its watchdog fired.
    using kleidos::meter::relay::detail::resetRecord;
    kleidos::meter::relay::ResetRecord left = {};
    {
        Bench first( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
        first.tell( "ROUT:NULL ON", 400'000 );
        left = resetRecord();
    }
    session(
        t, "a warm boot that released K1 and K2",
        [&]( sim::World& w ) {
            w.resetReason = 6;
            resetRecord() = left;
        },
        kGoldenWarmBoot );
    session( t, "the pin map", []( sim::World& ) {}, kGoldenPins );
    session(
        t, "a stored bus that falls back",
        []( sim::World& w ) {
            w.wires.lead = 10;
            storeMovedBus( 10 );
        },
        kGoldenSilentBus );
    session(
        t, "a stored bus with nothing to fall back to",
        []( sim::World& w ) {
            w.wires.lead = 4;
            storeMovedBus( 4 );
        },
        kGoldenSilentBus );
}

/// The file's text, or "" when it cannot be read.
std::string readFile( const char* path ) {
    std::ifstream in( path, std::ios::binary );
    return std::string( std::istreambuf_iterator<char>( in ), std::istreambuf_iterator<char>() );
}

/// The first line at which @p a and @p b differ, both sides shown.
std::string firstDifference( const std::string& expected, const std::string& actual ) {
    std::istringstream e( expected );
    std::istringstream a( actual );
    std::string        left;
    std::string        right;
    for ( int line = 1;; ++line ) {
        const bool more = static_cast<bool>( std::getline( e, left ) );
        const bool also = static_cast<bool>( std::getline( a, right ) );
        if ( !more && !also ) {
            return "";
        }
        if ( more != also || left != right ) {
            return "line " + std::to_string( line ) + ":\n  golden: " + ( more ? left : "<end>" )
                   + "\n  now:    " + ( also ? right : "<end>" );
        }
    }
}

/// The header the file starts with: its holder and license (spelled in
/// pieces, so that REUSE does not take these literals for this file's own
/// tags), and what it is.
constexpr std::string_view kHeader =
    "# SPDX-"
    "FileCopyrightText: 2026 Jos\u00e9 M. Requena Plens\n"
    "# SPDX-"
    "License-Identifier: GPL-3.0-or-later\n"
    "#\n"
    "# The golden transcript of firmware/test/native/test_golden.cpp: every byte the\n"
    "# instrument wrote to its sessions. Generated; never edit it by hand\n"
    "# (METER_GOLDEN_WRITE=1, test_golden.cpp says how).\n";

}  // namespace

TEST( "golden", "every byte of the sessions is the golden transcript's" ) {
    Transcript t;
    record( t );
    const std::string actual = std::string( kHeader ) + t.text();
    if ( std::getenv( "METER_GOLDEN_WRITE" ) != nullptr ) {
        std::ofstream out( METER_GOLDEN_FILE, std::ios::binary | std::ios::trunc );
        out << actual;
        REQUIRE( static_cast<bool>( out ) );
        std::printf( "  wrote %s (%zu bytes)\n", METER_GOLDEN_FILE, actual.size() );
        return;
    }
    const std::string golden = readFile( METER_GOLDEN_FILE );
    REQUIRE( !golden.empty() );
    CHECK_EQ( firstDifference( golden, actual ), "" );
}

TEST( "golden", "the sessions write the same bytes twice" ) {
    // The harness is deterministic: a second recording in the same process,
    // after every other case of the binary has run or not, is the first.
    Transcript first;
    Transcript second;
    session(
        first, "every query",
        []( sim::World& w ) {
            w.ringSamples   = 4096;
            w.circuit.dutNa = 5'000'000;
        },
        std::span<const Step>( kGoldenQueries ).first( 40 ) );
    session(
        second, "every query",
        []( sim::World& w ) {
            w.ringSamples   = 4096;
            w.circuit.dutNa = 5'000'000;
        },
        std::span<const Step>( kGoldenQueries ).first( 40 ) );
    CHECK_EQ( firstDifference( first.text(), second.text() ), "" );
    // And the transcript's own escapes and fragments read as documented.
    Transcript t;
    t.received( std::string( "a\\b\x01" ) + "\n\nend \n" );
    CHECK_EQ( t.text(), "< a\\\\b\\x01\n<\n< end\\x20\n" );
}
