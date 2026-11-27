// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file unit.cpp
 * @brief The runner behind unit.h, and its JUnit XML writer.
 */
#include "unit.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

namespace unit {

namespace {

struct Case {
    const char* suite;
    const char* name;
    Body        body;
    const char* file;
    int         line;
};

std::vector<Case>& cases() {
    static std::vector<Case> all;
    return all;
}

int                       gChecks = 0;
std::vector<std::string>  gFailures;            // of the running case
std::vector<std::string>* gExpected = nullptr;  // an ExpectedFailures' own, while it lives

/// Records @p text as a failure of the running case, or keeps it for the
/// living ExpectedFailures without printing it.
void fail( std::string text ) {
    if ( gExpected != nullptr ) {
        gExpected->push_back( std::move( text ) );
        return;
    }
    std::printf( "    %s\n", text.c_str() );
    gFailures.push_back( std::move( text ) );
}

std::string escape( const std::string& text ) {
    std::string out;
    for ( char c : text ) {
        switch ( c ) {
            case '&':
                out += "&amp;";
                break;
            case '<':
                out += "&lt;";
                break;
            case '>':
                out += "&gt;";
                break;
            case '"':
                out += "&quot;";
                break;
            default:
                if ( static_cast<unsigned char>( c ) < 0x20 && c != '\n' && c != '\t' ) {
                    out += '?';
                } else {
                    out += c;
                }
        }
    }
    return out;
}

/// Prints each suite's name once, a line each, in registration order.
void listSuites() {
    std::vector<std::string> listed;
    for ( const Case& test : cases() ) {
        if ( std::find( listed.begin(), listed.end(), test.suite ) == listed.end() ) {
            listed.emplace_back( test.suite );
            std::printf( "%s\n", test.suite );
        }
    }
}

}  // namespace

Registrar::Registrar( const char* suite, const char* name, Body body, const char* file, int line ) {
    cases().push_back( Case{ suite, name, body, file, line } );
}

ExpectedFailures::ExpectedFailures() {
    if ( gExpected != nullptr ) {
        std::fprintf( stderr, "unit: ExpectedFailures nested\n" );
        std::abort();
    }
    gExpected = &failures_;
}

ExpectedFailures::~ExpectedFailures() {
    gExpected = nullptr;
}

const std::vector<std::string>& ExpectedFailures::failures() const {
    return failures_;
}

bool check( bool ok, const char* expression, const char* file, int line ) {
    ++gChecks;
    if ( !ok ) {
        char text[1024];
        std::snprintf( text, sizeof( text ), "%s:%d: CHECK failed: %s", file, line, expression );
        fail( text );
    }
    return ok;
}

bool checkEqual( const std::string& actual, const std::string& expected, const char* expression,
                 const char* file, int line ) {
    ++gChecks;
    if ( actual != expected ) {
        fail( std::string( file ) + ":" + std::to_string( line ) + ": CHECK failed: " + expression
              + "\n      actual:   " + actual + "\n      expected: " + expected );
    }
    return actual == expected;
}

}  // namespace unit

int main( int argc, char** argv ) {
    using namespace unit;
    std::setvbuf( stdout, nullptr, _IOLBF, 0 );
    const char* junit  = nullptr;
    const char* filter = nullptr;
    const char* only   = nullptr;
    for ( int i = 1; i < argc; ++i ) {
        if ( std::strcmp( argv[i], "--junit" ) == 0 && i + 1 < argc ) {
            junit = argv[++i];
        } else if ( std::strcmp( argv[i], "--suite" ) == 0 && i + 1 < argc ) {
            only = argv[++i];
        } else if ( std::strcmp( argv[i], "--suites" ) == 0 ) {
            listSuites();
            return 0;
        } else {
            filter = argv[i];
        }
    }

    struct Result {
        const Case*              test;
        double                   seconds;
        std::vector<std::string> failures;
    };
    std::vector<Result> results;
    int                 failed = 0;
    for ( const Case& test : cases() ) {
        const std::string full = std::string( test.suite ) + "/" + test.name;
        if ( filter != nullptr && full.find( filter ) == std::string::npos ) {
            continue;
        }
        if ( only != nullptr && std::strcmp( test.suite, only ) != 0 ) {
            continue;
        }
        gFailures.clear();
        const auto start = std::chrono::steady_clock::now();
        try {
            test.body();
        } catch ( const Abort& ) {
        } catch ( const std::exception& error ) {
            gFailures.push_back( std::string( "uncaught exception: " ) + error.what() );
        }
        const double seconds =
            std::chrono::duration<double>( std::chrono::steady_clock::now() - start ).count();
        std::printf( "[%s] %s\n", gFailures.empty() ? " ok " : "FAIL", full.c_str() );
        if ( !gFailures.empty() ) {
            ++failed;
        }
        results.push_back( Result{ &test, seconds, gFailures } );
    }
    std::printf( "%zu cases, %d failed, %d checks\n", results.size(), failed, gChecks );
    if ( only != nullptr && results.empty() ) {
        std::fprintf( stderr, "unit: no suite %s\n", only );
        return 2;
    }

    if ( junit != nullptr ) {
        FILE* out = std::fopen( junit, "w" );
        if ( out == nullptr ) {
            std::perror( junit );
            return 2;
        }
        std::fprintf( out, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<testsuites>\n" );
        // One <testsuite> per suite name, in registration order.
        std::vector<std::string> suites;
        for ( const Result& result : results ) {
            if ( std::find( suites.begin(), suites.end(), result.test->suite ) == suites.end() ) {
                suites.emplace_back( result.test->suite );
            }
        }
        for ( const std::string& suite : suites ) {
            int    count = 0, failures = 0;
            double time = 0;
            for ( const Result& result : results ) {
                if ( suite == result.test->suite ) {
                    ++count;
                    failures += result.failures.empty() ? 0 : 1;
                    time += result.seconds;
                }
            }
            std::fprintf( out,
                          "  <testsuite name=\"%s\" tests=\"%d\" failures=\"%d\" errors=\"0\" "
                          "time=\"%.6f\">\n",
                          escape( suite ).c_str(), count, failures, time );
            for ( const Result& result : results ) {
                if ( suite != result.test->suite ) {
                    continue;
                }
                std::fprintf( out,
                              "    <testcase classname=\"%s\" name=\"%s\" file=\"%s\" line=\"%d\" "
                              "time=\"%.6f\"",
                              escape( suite ).c_str(), escape( result.test->name ).c_str(),
                              escape( result.test->file ).c_str(), result.test->line,
                              result.seconds );
                if ( result.failures.empty() ) {
                    std::fprintf( out, "/>\n" );
                    continue;
                }
                std::string message;
                for ( const std::string& failure : result.failures ) {
                    message += failure + "\n";
                }
                std::fprintf(
                    out, ">\n      <failure message=\"%s\">%s</failure>\n    </testcase>\n",
                    escape( result.failures.front() ).c_str(), escape( message ).c_str() );
            }
            std::fprintf( out, "  </testsuite>\n" );
        }
        std::fprintf( out, "</testsuites>\n" );
        std::fclose( out );
    }
    return failed == 0 ? 0 : 1;
}
