// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file flash.cpp
 * @brief The model of the record logs' flash (sim::Flash). The port
 *        flash_log.h asks for is the device's own (flash_port.cpp, on
 *        `esp_flash_*`, FW-16), whose three calls sim.cpp fakes on this
 *        model.
 */
#include "flash_port.h"
#include "sim.h"

#include <algorithm>
#include <cstdlib>

static_assert( sim::Flash{}.base == kleidos::meter::flash_port::kLogBaseAddress,
               "the model's window starts at the calib partition" );
static_assert( sim::Flash::kSectors * sim::Flash::kSectorBytes
                   == kleidos::meter::flash_port::kPinLogBaseAddress
                          + kleidos::meter::flash_port::kLogBytes
                          - kleidos::meter::flash_port::kLogBaseAddress,
               "the model's window is the calib partition and the pins one after it" );

namespace sim {

bool Flash::inWindow( std::uint32_t address, std::size_t length ) const {
    const std::uint64_t first = address;
    const std::uint64_t last  = first + length;
    return first >= base && last <= std::uint64_t{ base } + bytes.size();
}

std::uint8_t& Flash::at( std::uint32_t address ) {
    if ( !inWindow( address, 1 ) ) {
        std::abort();  // a test's own mistake
    }
    return bytes[address - base];
}

bool Flash::read( std::uint32_t address, std::uint8_t* data, std::size_t length ) {
    ++reads;
    if ( !powered ) {
        return false;
    }
    if ( !inWindow( address, length ) ) {
        ++outOfWindow;
        return false;
    }
    if ( readsUntilFailure == 0 ) {
        readsUntilFailure = -1;
        return false;
    }
    if ( readsUntilFailure > 0 ) {
        --readsUntilFailure;
    }
    std::copy_n( bytes.begin() + static_cast<std::ptrdiff_t>( address - base ), length, data );
    return true;
}

bool Flash::program( std::uint32_t address, const std::uint8_t* data, std::size_t length ) {
    ++programs;
    if ( !powered ) {
        return false;
    }
    if ( !inWindow( address, length ) ) {
        ++outOfWindow;
        return false;
    }
    if ( failProgram ) {
        return false;
    }
    for ( std::size_t i = 0; i < length; ++i ) {
        if ( ( data[i] & ~bytes[address - base + i] ) != 0 ) {
            ++zeroToOne;
        }
    }
    if ( programLost ) {
        return true;
    }
    const bool        cut  = cutAt == static_cast<long long>( changes );
    const std::size_t land = cut ? std::min( cutKeep, length ) : length;
    ++changes;
    for ( std::size_t i = 0; i < land; ++i ) {
        if ( static_cast<long long>( address + i ) != stuckAddress ) {
            bytes[address - base + i] &= data[i];
        }
    }
    if ( cut ) {
        if ( cutTears && land < length ) {
            bytes[address - base + land] &= static_cast<std::uint8_t>( data[land] | 0xF0U );
        }
        powered = false;
        return false;
    }
    return true;
}

bool Flash::eraseSector( std::uint32_t address ) {
    if ( !powered ) {
        return false;
    }
    if ( !inWindow( address, kSectorBytes ) ) {
        ++outOfWindow;
        return false;
    }
    if ( address % kSectorBytes != 0 ) {
        ++unalignedErase;
        return false;
    }
    if ( failErase ) {
        return false;
    }
    if ( eraseLost ) {
        return true;
    }
    const std::size_t first = address - base;
    ++erases[first / kSectorBytes];
    const bool        cut  = cutAt == static_cast<long long>( changes );
    const std::size_t land = cut ? std::min<std::size_t>( cutKeep, kSectorBytes ) : kSectorBytes;
    ++changes;
    std::fill_n( bytes.begin() + static_cast<std::ptrdiff_t>( first ), land, 0xFF );
    if ( cut ) {
        if ( cutTears ) {
            for ( std::size_t i = land; i < kSectorBytes; ++i ) {
                bytes[first + i] |= 0xA5U;
            }
        }
        powered = false;
        return false;
    }
    return true;
}

void Flash::powerOn() {
    powered = true;
    cutAt   = -1;
}

}  // namespace sim
