// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file unit.h
 * @brief A small test runner for the host tests: registered cases, checks
 *        that count and report, and a JUnit XML file the test report reads.
 *
 *     TEST( "suite", "what it shows" ) { CHECK( 1 + 1 == 2 ); }
 *
 * Each binary links unit.cpp's main(): `./test_x [--junit out.xml] [filter]`.
 * A case fails when any CHECK in it fails; REQUIRE also ends the case.
 */
#pragma once

#include <string>
#include <vector>

namespace unit {

using Body = void ( * )();

struct Registrar {
    Registrar( const char* suite, const char* name, Body body, const char* file, int line );
};

/// Records one check; returns @p ok.
bool check( bool ok, const char* expression, const char* file, int line );

/// Records a string comparison; returns whether they are equal.
bool checkEqual( const std::string& actual, const std::string& expected, const char* expression,
                 const char* file, int line );

/// Thrown by REQUIRE to end a case.
struct Abort {};

/// While one lives, a failing check is kept here instead of failing the
/// running case, and nothing is printed: for a case that shows that one of
/// the harness's own checks (a bench::Bench destructor's) fails when it
/// should. One at a time.
class ExpectedFailures {
   public:
    ExpectedFailures();
    ~ExpectedFailures();
    ExpectedFailures( const ExpectedFailures& )            = delete;
    ExpectedFailures& operator=( const ExpectedFailures& ) = delete;
    ExpectedFailures( ExpectedFailures&& )                 = delete;
    ExpectedFailures& operator=( ExpectedFailures&& )      = delete;

    /// Every failure kept so far, each as the case would have printed it.
    /// Out of line (unit.cpp), as every member of the runner is: test_scpi
    /// never calls it, and an inline body only one binary emits would be
    /// llvm-cov's "mismatched data" (docs/testing.md).
    const std::vector<std::string>& failures() const;

   private:
    std::vector<std::string> failures_;
};

}  // namespace unit

#define UNIT_CAT2( a, b ) a##b
#define UNIT_CAT( a, b ) UNIT_CAT2( a, b )

#define TEST( suite, name )                                                                        \
    static void                    UNIT_CAT( unitCase_, __LINE__ )();                              \
    static const ::unit::Registrar UNIT_CAT( unitRegistrar_, __LINE__ )(                           \
        suite, name, &UNIT_CAT( unitCase_, __LINE__ ), __FILE__, __LINE__ );                       \
    static void UNIT_CAT( unitCase_, __LINE__ )()

#define CHECK( expression )                                                                        \
    ( (void)::unit::check( static_cast<bool>( expression ), #expression, __FILE__, __LINE__ ) )
#define REQUIRE( expression )                                                                      \
    do {                                                                                           \
        if ( !::unit::check( static_cast<bool>( expression ), #expression, __FILE__,               \
                             __LINE__ ) ) {                                                        \
            throw ::unit::Abort{};                                                                 \
        }                                                                                          \
    } while ( 0 )
#define CHECK_EQ( actual, expected )                                                               \
    ( (void)::unit::checkEqual( ( actual ), ( expected ), #actual " == " #expected, __FILE__,      \
                                __LINE__ ) )
#define CHECK_CONTAINS( haystack, needle )                                                         \
    ( (void)::unit::check( std::string( haystack ).find( needle ) != std::string::npos,            \
                           #haystack " contains " #needle, __FILE__, __LINE__ ) )
