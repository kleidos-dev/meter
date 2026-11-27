// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
typedef int           BaseType_t;
typedef unsigned int  UBaseType_t;
typedef std::uint32_t TickType_t;
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define pdFAIL 0
#define errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY ( -1 )
#define portMAX_DELAY ( static_cast<TickType_t>( 0xFFFFFFFFUL ) )
#define pdMS_TO_TICKS( ms ) ( static_cast<TickType_t>( ms ) )
/// The device's tick rate (`CONFIG_FREERTOS_HZ`): one tick a millisecond, as
/// pdMS_TO_TICKS() above assumes.
#define configTICK_RATE_HZ 1000U
/// Asks for a context switch at the end of an interrupt; the harness has no
/// preemption, so it counts the request (sim.h, World::isrYields).
void simYieldFromIsr();
#define portYIELD_FROM_ISR() simYieldFromIsr()
/// Masks the calling core's interrupts and returns the previous mask; the
/// harness has no preemption, so it counts the nesting (sim.cpp), which every
/// Bench and Scope requires back at zero.
UBaseType_t xPortSetInterruptMaskFromISR();
void        vPortClearInterruptMaskFromISR( UBaseType_t previous );
#define portSET_INTERRUPT_MASK_FROM_ISR() xPortSetInterruptMaskFromISR()
#define portCLEAR_INTERRUPT_MASK_FROM_ISR( previous ) vPortClearInterruptMaskFromISR( previous )
typedef struct SimQueue* QueueHandle_t;
typedef struct SimQueue* SemaphoreHandle_t;
typedef struct SimTask*  TaskHandle_t;
typedef void ( *TaskFunction_t )( void* );
/// The core the caller runs on (the harness: sim::currentCore()).
BaseType_t xPortGetCoreID();
// ESP-IDF's port: a stack is counted in bytes.
typedef std::uint8_t StackType_t;
/// The idle task's stack, in bytes: the device's
/// `CONFIG_FREERTOS_IDLE_TASK_STACKSIZE` (1536) and its port's 60 bytes of
/// overhead, the 1,596 ESP-IDF's hook took for each idle task (FW-07's census).
#define configMINIMAL_STACK_SIZE 1596U
// The control blocks a static create writes into; their contents are the
// kernel's, so the harness keeps its own objects elsewhere.
struct StaticQueue_t {
    void* reserved[8];
};
typedef StaticQueue_t StaticSemaphore_t;
struct StaticTask_t {
    void* reserved[8];
};
