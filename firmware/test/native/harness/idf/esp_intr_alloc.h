// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include "esp_err.h"

/// The flags esp_intr_alloc() takes, as ESP-IDF 6.0.3 defines them.
#define ESP_INTR_FLAG_LEVEL1 ( 1 << 1 )
#define ESP_INTR_FLAG_LEVEL2 ( 1 << 2 )
#define ESP_INTR_FLAG_LEVEL3 ( 1 << 3 )
#define ESP_INTR_FLAG_SHARED ( 1 << 8 )
#define ESP_INTR_FLAG_EDGE ( 1 << 9 )
#define ESP_INTR_FLAG_IRAM ( 1 << 10 )
#define ESP_INTR_FLAG_INTRDISABLED ( 1 << 11 )

/// An interrupt handler, as ESP-IDF's interrupt allocator takes it.
typedef void ( *intr_handler_t )( void* arg );

/// The handle esp_intr_alloc() returns.
typedef struct intr_handle_data_t* intr_handle_t;

/// ESP-IDF's interrupt allocator, which allocates a record per CPU line and
/// handler on the device. The firmware calls it nowhere (the drivers it uses
/// call it inside); the harness has no interrupt matrix, so the fake records
/// the call on the world's ledger (idf_ledger.h), where no case may leave
/// it, and refuses with ESP_ERR_NOT_SUPPORTED.
esp_err_t esp_intr_alloc( int source, int flags, intr_handler_t handler, void* arg,
                          intr_handle_t* ret_handle );

/// Frees what esp_intr_alloc() allocated: recorded on the ledger, then
/// ESP_ERR_NOT_SUPPORTED like the allocation.
esp_err_t esp_intr_free( intr_handle_t handle );
