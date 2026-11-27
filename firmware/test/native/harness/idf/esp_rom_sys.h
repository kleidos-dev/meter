// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
void esp_rom_delay_us( std::uint32_t us );
/// Routes peripheral interrupt source @p periph_intr_id to CPU line
/// @p cpu_intr_num of core @p cpu_core in the interrupt matrix.
void esp_rom_route_intr_matrix( int cpu_core, std::uint32_t periph_intr_id,
                                std::uint32_t cpu_intr_num );
