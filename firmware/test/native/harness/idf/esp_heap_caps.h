// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstddef>
#include <cstdint>
#define MALLOC_CAP_8BIT ( 1U << 2 )
#define MALLOC_CAP_SPIRAM ( 1U << 10 )
#define MALLOC_CAP_INTERNAL ( 1U << 11 )
void*       heap_caps_malloc( std::size_t size, std::uint32_t caps );
void        heap_caps_free( void* block );
std::size_t heap_caps_get_free_size( std::uint32_t caps );
std::size_t heap_caps_get_minimum_free_size( std::uint32_t caps );
std::size_t heap_caps_get_largest_free_block( std::uint32_t caps );
// CONFIG_HEAP_USE_HOOKS: the fake allocator calls these as ESP-IDF's does.
extern "C" void esp_heap_trace_alloc_hook( void* ptr, std::size_t size, std::uint32_t caps );
extern "C" void esp_heap_trace_free_hook( void* ptr );
