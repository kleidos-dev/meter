// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// ESP-IDF 6.0.3's peripheral clock lock (esp_hw_support's
// esp_private/periph_ctrl.h), the lock PERIPH_RCC_ATOMIC() and
// PERIPH_RCC_ACQUIRE_ATOMIC() take, on the simulated board: its depth
// (sim::World::rccDepth), which every Bench and Scope requires back at 0, and
// the reference counts of the shared modules (sim::World::rccReferences).
#include <soc/periph_defs.h>

#include <cstdint>

void periph_rcc_enter( void );
void periph_rcc_exit( void );
/// Takes the lock and answers @p periph's references before this one.
std::uint8_t periph_rcc_acquire_enter( shared_periph_module_t periph );
/// Counts one more reference on @p periph than @p ref_count and lets the lock go.
void periph_rcc_acquire_exit( shared_periph_module_t periph, std::uint8_t ref_count );
