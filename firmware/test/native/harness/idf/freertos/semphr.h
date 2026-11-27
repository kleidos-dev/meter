// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "freertos/FreeRTOS.h"
SemaphoreHandle_t xSemaphoreCreateBinary();
SemaphoreHandle_t xSemaphoreCreateBinaryStatic( StaticSemaphore_t* buffer );
BaseType_t        xSemaphoreTake( SemaphoreHandle_t semaphore, TickType_t wait );
BaseType_t        xSemaphoreGive( SemaphoreHandle_t semaphore );
BaseType_t        xSemaphoreGiveFromISR( SemaphoreHandle_t semaphore, BaseType_t* woken );
