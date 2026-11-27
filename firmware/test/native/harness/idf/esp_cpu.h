// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The CPU's interrupt lines, as ESP-IDF 6.0.3's esp_cpu.h offers them, on
// the simulated board's two cores (sim::World::cpus): the descriptor table,
// the port's handler table, the lines' enables, and the calling core.
#include <cstdint>

typedef enum {
    ESP_CPU_INTR_TYPE_LEVEL = 0,
    ESP_CPU_INTR_TYPE_EDGE,
    ESP_CPU_INTR_TYPE_NA,
} esp_cpu_intr_type_t;

typedef struct {
    int                 priority;
    esp_cpu_intr_type_t type;
    std::uint32_t       flags;
} esp_cpu_intr_desc_t;

#define ESP_CPU_INTR_DESC_FLAG_SPECIAL 0x01
#define ESP_CPU_INTR_DESC_FLAG_RESVD 0x02

typedef void ( *esp_cpu_intr_handler_t )( void* arg );

/// The core the caller runs on (sim::currentCore()).
int esp_cpu_get_core_id( void );
/// Line @p intr_num's entry in core @p core_id's descriptor table.
void esp_cpu_intr_get_desc( int core_id, int intr_num, esp_cpu_intr_desc_t* intr_desc_ret );
/// Whether the calling core's handler table has a handler on the line.
bool esp_cpu_intr_has_handler( int intr_num );
/// Sets the calling core's handler on the line.
void esp_cpu_intr_set_handler( int intr_num, esp_cpu_intr_handler_t handler, void* handler_arg );
/// Enables, on the calling core, the lines of @p intr_mask.
void esp_cpu_intr_enable( std::uint32_t intr_mask );
/// Disables, on the calling core, the lines of @p intr_mask.
void esp_cpu_intr_disable( std::uint32_t intr_mask );
