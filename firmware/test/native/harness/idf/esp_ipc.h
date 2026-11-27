// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <esp_err.h>

#include <cstdint>

/// The callback esp_ipc_call_blocking() runs on another core.
using esp_ipc_func_t = void ( * )( void* arg );

/// Runs @p func on @p cpu_id's IPC task and waits for it: the harness runs it
/// in place and records the core it was asked for (sim::Faults::ipcCpu).
esp_err_t esp_ipc_call_blocking( std::uint32_t cpu_id, esp_ipc_func_t func, void* arg );
