// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// Where a pointer lies, as ESP-IDF 6.0.3's esp_memory_utils.h answers it: on
// the simulated board every host address is internal RAM but those a test
// places in PSRAM (sim::World::externalRam).

/// Whether @p p is in internal DRAM: false for an address of a range a test
/// placed in PSRAM.
bool esp_ptr_in_dram( const void* p );
