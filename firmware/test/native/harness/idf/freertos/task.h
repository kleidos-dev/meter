// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "freertos/FreeRTOS.h"
BaseType_t xTaskCreatePinnedToCore( TaskFunction_t entry, const char* name,
                                    std::uint32_t stackBytes, void* argument, UBaseType_t priority,
                                    TaskHandle_t* handle, BaseType_t core );
TaskHandle_t xTaskCreateStaticPinnedToCore( TaskFunction_t entry, const char* name,
                                            std::uint32_t stackBytes, void* argument,
                                            UBaseType_t priority, StackType_t* stack,
                                            StaticTask_t* tcb, BaseType_t core );
void         vTaskDelay( TickType_t ticks );
/// The fewest bytes of @p task's stack left free since it started (the
/// simulated board's World::stackFreeBytes).
UBaseType_t uxTaskGetStackHighWaterMark( TaskHandle_t task );
/// The calling task's handle (the harness: the handle a static create
/// returned for the calling thread; null for the test's own thread).
TaskHandle_t xTaskGetCurrentTaskHandle();
/// Sets @p task's priority, the calling task's when null (the harness keeps
/// it by the task's name, World::taskPriorities).
void vTaskPrioritySet( TaskHandle_t task, UBaseType_t priority );
