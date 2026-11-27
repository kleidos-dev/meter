// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_startup_memory.cpp
 * @brief startup_memory.cpp: each idle task gets its own static slot of
 *        ESP-IDF's size, the wrapped hook hands them out in order and then
 *        nothing, and PSA's wrapped initialization answers success (FW-18);
 *        the startup tasks, the flash lock and the system interrupts are
 *        served from static memory or routed, once each, and anything else
 *        is refused (the review of 2026-11-24).
 */
#include "scope.h"
#include "startup_memory.h"
#include "unit.h"

#include <esp_intr_alloc.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <soc/interrupts.h>

#include <cstdint>

using namespace kleidos::meter;
using bench::Scope;

TEST( "startup_memory", "an idle stack is the size ESP-IDF's own hook took" ) {
    CHECK( startup_memory::kIdleTasks == 2U );
    CHECK( startup_memory::kIdleStackBytes == configMINIMAL_STACK_SIZE );
    CHECK( startup_memory::kIdleStackBytes == 1596U );
}

TEST( "startup_memory", "each idle task's slot is its own; none past the second (MC/DC)" ) {
    const startup_memory::IdleTaskMemory first  = startup_memory::idleTaskMemory( 0U );
    const startup_memory::IdleTaskMemory second = startup_memory::idleTaskMemory( 1U );
    CHECK( first.tcb != nullptr && first.stack != nullptr );
    CHECK( second.tcb != nullptr && second.stack != nullptr );
    CHECK( first.stackBytes == startup_memory::kIdleStackBytes );
    CHECK( second.stackBytes == startup_memory::kIdleStackBytes );
    CHECK( first.tcb != second.tcb );
    CHECK( first.stack != second.stack );
    // The same index is the same slot.
    CHECK( startup_memory::idleTaskMemory( 0U ).stack == first.stack );
    // The stacks do not overlap and are aligned as the heap's blocks were.
    const auto a = reinterpret_cast<std::uintptr_t>( first.stack );
    const auto b = reinterpret_cast<std::uintptr_t>( second.stack );
    CHECK( a + startup_memory::kIdleStackBytes <= b || b + startup_memory::kIdleStackBytes <= a );
    CHECK( a % 16U == 0U && b % 16U == 0U );
    // Past the last core: nothing, so the kernel's own assert stops it.
    const startup_memory::IdleTaskMemory none = startup_memory::idleTaskMemory( 2U );
    CHECK( none.tcb == nullptr && none.stack == nullptr && none.stackBytes == 0U );
    CHECK( startup_memory::idleTaskMemory( 0xFFFFFFFFU ).tcb == nullptr );
}

TEST( "startup_memory", "the wrapped hook hands out Core 0's slot, Core 1's, then nothing" ) {
    // The only case that calls the hook: its counter is the process's.
    StaticTask_t* tcb   = nullptr;
    StackType_t*  stack = nullptr;
    std::uint32_t bytes = 0U;
    __wrap_vApplicationGetIdleTaskMemory( &tcb, &stack, &bytes );
    CHECK( tcb == startup_memory::idleTaskMemory( 0U ).tcb );
    CHECK( stack == startup_memory::idleTaskMemory( 0U ).stack );
    CHECK( bytes == startup_memory::kIdleStackBytes );
    __wrap_vApplicationGetIdleTaskMemory( &tcb, &stack, &bytes );
    CHECK( tcb == startup_memory::idleTaskMemory( 1U ).tcb );
    CHECK( stack == startup_memory::idleTaskMemory( 1U ).stack );
    CHECK( bytes == startup_memory::kIdleStackBytes );
    // A third request gets no memory, and the counter stays where it was.
    __wrap_vApplicationGetIdleTaskMemory( &tcb, &stack, &bytes );
    CHECK( tcb == nullptr && stack == nullptr && bytes == 0U );
    CHECK( startup_memory::nextIdleTaskMemory().tcb == nullptr );
}

TEST( "startup_memory", "PSA's wrapped initialization answers PSA_SUCCESS" ) {
    CHECK( __wrap_psa_crypto_init() == startup_memory::kPsaSuccess );
    CHECK( startup_memory::kPsaSuccess == 0 );
}

TEST( "startup_memory", "each startup task has its slot, of the size ESP-IDF asks for (MC/DC)" ) {
    CHECK( startup_memory::kStartupTasks == 3U );
    CHECK( startup_memory::kIpcStackBytes == 4096U );
    CHECK( startup_memory::kMainStackBytes == 8192U + 512U );
    const auto ipc0 = startup_memory::startupTaskMemory( "ipc0", startup_memory::kIpcStackBytes );
    const auto ipc1 = startup_memory::startupTaskMemory( "ipc1", startup_memory::kIpcStackBytes );
    const auto main = startup_memory::startupTaskMemory( "main", startup_memory::kMainStackBytes );
    CHECK( ipc0.index == 0U && ipc1.index == 1U && main.index == 2U );
    CHECK( ipc0.tcb != nullptr && ipc1.tcb != nullptr && main.tcb != nullptr );
    CHECK( ipc0.tcb != ipc1.tcb && ipc1.tcb != main.tcb );
    CHECK( ipc0.stackBytes == startup_memory::kIpcStackBytes );
    CHECK( main.stackBytes == startup_memory::kMainStackBytes );
    for ( const auto* stack : { ipc0.stack, ipc1.stack, main.stack } ) {
        CHECK( reinterpret_cast<std::uintptr_t>( stack ) % 16U == 0U );
    }
    // Another size, another name: nothing.
    CHECK( startup_memory::startupTaskMemory( "ipc0", startup_memory::kMainStackBytes ).tcb
           == nullptr );
    CHECK( startup_memory::startupTaskMemory( "main", startup_memory::kIpcStackBytes ).tcb
           == nullptr );
    const auto other = startup_memory::startupTaskMemory( "tiT", startup_memory::kIpcStackBytes );
    CHECK( other.tcb == nullptr && other.stack == nullptr && other.stackBytes == 0U );
    CHECK( other.index == startup_memory::kStartupTasks );
}

TEST( "startup_memory", "the flash lock is a recursive mutex's, and nothing else's" ) {
    CHECK( startup_memory::flashLockMemory( queueQUEUE_TYPE_RECURSIVE_MUTEX ) != nullptr );
    CHECK( startup_memory::flashLockMemory( queueQUEUE_TYPE_MUTEX ) == nullptr );
}

TEST( "startup_memory", "the four startup interrupts are routed, nothing else (MC/DC)" ) {
    constexpr std::int32_t kIram   = ESP_INTR_FLAG_IRAM;
    constexpr std::int32_t kLevel1 = ESP_INTR_FLAG_LEVEL1;
    for ( const std::int32_t source :
          { static_cast<std::int32_t>( ETS_FROM_CPU_INTR0_SOURCE ),
            static_cast<std::int32_t>( ETS_FROM_CPU_INTR1_SOURCE ),
            static_cast<std::int32_t>( ETS_SYSTIMER_TARGET0_INTR_SOURCE ),
            static_cast<std::int32_t>( ETS_SYSTIMER_TARGET1_INTR_SOURCE ) } ) {
        CHECK( startup_memory::startupInterrupt( source, kIram, false ) );
        CHECK( startup_memory::startupInterrupt( source, kIram | kLevel1, false ) );
        CHECK( startup_memory::startupInterrupt( source, 0, false ) );
        // A handle, a shared or edge line, another level, a line left disabled.
        CHECK( !startup_memory::startupInterrupt( source, kIram, true ) );
        CHECK( !startup_memory::startupInterrupt( source, kIram | ESP_INTR_FLAG_SHARED, false ) );
        CHECK( !startup_memory::startupInterrupt( source, ESP_INTR_FLAG_EDGE, false ) );
        CHECK( !startup_memory::startupInterrupt( source, ESP_INTR_FLAG_LEVEL2, false ) );
        CHECK( !startup_memory::startupInterrupt( source, ESP_INTR_FLAG_INTRDISABLED, false ) );
    }
    CHECK( !startup_memory::startupInterrupt( static_cast<std::int32_t>( ETS_GPIO_INTR_SOURCE ),
                                              kIram, false ) );
}

namespace {

/// A startup task's body on the simulated board: it waits for good.
void restingTask( void* ) {
    for ( ;; ) {
        vTaskDelay( 1000 );
    }
}

/// A handler for the routes.
void routedHandler( void* ) {}

}  // namespace

TEST( "startup_memory", "the wraps serve each startup request once and refuse the rest" ) {
    // The only case that calls these wraps: their counters are the process's.
    Scope scope;
    using startup_memory::kIpcStackBytes;
    using startup_memory::kMainStackBytes;
    TaskHandle_t ipc0 = nullptr;
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "ipc0", kIpcStackBytes, nullptr, 24U,
                                           &ipc0, 0 )
           == pdPASS );
    CHECK( ipc0 != nullptr );
    // Twice, another task, no name, another size: refused, nothing created.
    TaskHandle_t again = nullptr;
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "ipc0", kIpcStackBytes, nullptr, 24U,
                                           &again, 0 )
           == errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY );
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "esp_timer", kIpcStackBytes, nullptr, 22U,
                                           &again, 0 )
           == errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY );
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, nullptr, kIpcStackBytes, nullptr, 1U,
                                           &again, 0 )
           == errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY );
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "main", kIpcStackBytes, nullptr, 1U,
                                           &again, 0 )
           == errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY );
    CHECK( again == nullptr );
    // No handle asked for: still created.
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "ipc1", kIpcStackBytes, nullptr, 24U,
                                           nullptr, 1 )
           == pdPASS );
    // The kernel's own refusal is passed on, and the slot stays free.
    scope->faults.failTask     = true;
    scope->faults.failTaskName = "main";
    TaskHandle_t main          = nullptr;
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "main", kMainStackBytes, nullptr, 1U,
                                           &main, 0 )
           == errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY );
    CHECK( main == nullptr );
    scope->faults.failTask = false;
    CHECK( __wrap_xTaskCreatePinnedToCore( &restingTask, "main", kMainStackBytes, nullptr, 1U,
                                           &main, 0 )
           == pdPASS );
    CHECK( main != nullptr && main != ipc0 );

    // The flash lock, once; a plain mutex never.
    CHECK( __wrap_xQueueCreateMutex( queueQUEUE_TYPE_MUTEX ) == nullptr );
    CHECK( __wrap_xQueueCreateMutex( queueQUEUE_TYPE_RECURSIVE_MUTEX ) != nullptr );
    CHECK( __wrap_xQueueCreateMutex( queueQUEUE_TYPE_RECURSIVE_MUTEX ) == nullptr );

    // Core 0's cross-core interrupt, routed on this core, once.
    const auto crossCore = static_cast<std::int32_t>( ETS_FROM_CPU_INTR0_SOURCE );
    CHECK( __wrap_esp_intr_alloc( crossCore, ESP_INTR_FLAG_IRAM, &routedHandler, nullptr, nullptr )
           == ESP_OK );
    CHECK( scope->cpus[0].routes.size() == 1U );
    CHECK( __wrap_esp_intr_alloc( crossCore, ESP_INTR_FLAG_IRAM, &routedHandler, nullptr, nullptr )
           == ESP_ERR_NOT_SUPPORTED );
    // A handle asked for, and a source of no startup's: refused, nothing routed.
    intr_handle_t handle = nullptr;
    const auto    tick   = static_cast<std::int32_t>( ETS_SYSTIMER_TARGET0_INTR_SOURCE );
    CHECK( __wrap_esp_intr_alloc( tick, ESP_INTR_FLAG_IRAM, &routedHandler, nullptr, &handle )
           == ESP_ERR_NOT_SUPPORTED );
    CHECK( __wrap_esp_intr_alloc( static_cast<std::int32_t>( ETS_GPIO_INTR_SOURCE ),
                                  ESP_INTR_FLAG_IRAM, &routedHandler, nullptr, nullptr )
           == ESP_ERR_NOT_SUPPORTED );
    // The router's own refusal is passed on, and the source stays unrouted.
    CHECK( __wrap_esp_intr_alloc( tick, ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL1, nullptr, nullptr,
                                  nullptr )
           == ESP_ERR_INVALID_ARG );
    CHECK( __wrap_esp_intr_alloc( tick, ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL1, &routedHandler,
                                  nullptr, nullptr )
           == ESP_OK );
    CHECK( scope->cpus[0].routes.size() == 2U );
}

TEST( "startup_memory", "the C library's environ word is served once, and nothing else (MC/DC)" ) {
    // The only case that calls this wrap: its record is the process's.
    CHECK( startup_memory::environmentRequest( sizeof( char* ) ) );
    CHECK( !startup_memory::environmentRequest( sizeof( char* ) + 1U ) );
    CHECK( !startup_memory::environmentRequest( 0U ) );
    CHECK( __wrap_malloc( 16U ) == nullptr );  // any other request: refused
    void* const word = __wrap_malloc( sizeof( char* ) );
    REQUIRE( word != nullptr );
    CHECK( reinterpret_cast<std::uintptr_t>( word ) % alignof( char* ) == 0U );
    static_cast<char**>( word )[0] = nullptr;              // what esp_libc_init() writes
    CHECK( __wrap_malloc( sizeof( char* ) ) == nullptr );  // once
}
