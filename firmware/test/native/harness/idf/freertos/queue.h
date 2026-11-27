// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "freertos/FreeRTOS.h"
QueueHandle_t xQueueCreate( UBaseType_t length, UBaseType_t itemSize );
QueueHandle_t xQueueCreateStatic( UBaseType_t length, UBaseType_t itemSize, std::uint8_t* storage,
                                  StaticQueue_t* buffer );
/// A mutex's kinds, as FreeRTOS numbers them.
#define queueQUEUE_TYPE_MUTEX ( static_cast<std::uint8_t>( 1U ) )
#define queueQUEUE_TYPE_RECURSIVE_MUTEX ( static_cast<std::uint8_t>( 4U ) )
/// A mutex over @p buffer; the harness makes a binary semaphore given once.
QueueHandle_t xQueueCreateMutexStatic( std::uint8_t type, StaticQueue_t* buffer );
BaseType_t    xQueueSend( QueueHandle_t queue, const void* item, TickType_t wait );
BaseType_t    xQueueReceive( QueueHandle_t queue, void* item, TickType_t wait );
