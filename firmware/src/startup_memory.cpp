// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file startup_memory.cpp
 * @ingroup startup_memory
 * @brief What ESP-IDF's startup is served statically, and its wrapped calls.
 *
 * Nothing is allocated (FW-18, the review of 2026-11-24): the idle tasks', the
 * IPC tasks' and the main task's slots and the flash lock's queue are one
 * static object in internal RAM, reserved at link time, handed out by the
 * wrapped vApplicationGetIdleTaskMemory(), xTaskCreatePinnedToCore() and
 * xQueueCreateMutex(); the wrapped esp_intr_alloc() routes the startup's
 * interrupts without a handle, and PSA's wrapped initialization only
 * answers.
 */
#include "startup_memory.h"

#include "interrupt.h"

#include <soc/interrupts.h>

#include <array>
#include <cstdint>
#include <string_view>

namespace kleidos::meter::startup_memory {

namespace {

/// @brief One idle task's stack and control block. The stack is aligned to
///        16 bytes, the alignment the heap gave ESP-IDF's own hook.
struct Slot {
    alignas( 16 ) std::array<StackType_t, kIdleStackBytes> stack{};  ///< The stack.
    StaticTask_t tcb{};                                              ///< The control block.
};

/// @brief An IPC task's stack and control block, aligned as Slot's.
struct IpcSlot {
    alignas( 16 ) std::array<StackType_t, kIpcStackBytes> stack{};  ///< The stack.
    StaticTask_t tcb{};                                             ///< The control block.
};

/// @brief The main task's stack and control block, aligned as Slot's.
struct MainSlot {
    alignas( 16 ) std::array<StackType_t, kMainStackBytes> stack{};  ///< The stack.
    StaticTask_t tcb{};                                              ///< The control block.
};

/// @brief The startup tasks' names, in slot order.
constexpr std::array<std::string_view, kStartupTasks> kStartupTaskNames = { "ipc0", "ipc1",
                                                                            "main" };

/// @brief The interrupt sources ESP-IDF's startup registers: each core's
///        cross-core interrupt and each core's tick.
constexpr std::array<std::int32_t, 4> kStartupInterrupts = {
    static_cast<std::int32_t>( ETS_FROM_CPU_INTR0_SOURCE ),
    static_cast<std::int32_t>( ETS_FROM_CPU_INTR1_SOURCE ),
    static_cast<std::int32_t>( ETS_SYSTIMER_TARGET0_INTR_SOURCE ),
    static_cast<std::int32_t>( ETS_SYSTIMER_TARGET1_INTR_SOURCE ),
};

/// @brief The `ESP_INTR_FLAG_*` bits a routed request may carry: level 1
///        (or no level, which the allocator gave level 1 here) and IRAM.
constexpr std::uint32_t kRoutableFlags = static_cast<std::uint32_t>( ESP_INTR_FLAG_LEVEL1 )
                                         | static_cast<std::uint32_t>( ESP_INTR_FLAG_IRAM );

/// @brief The startup's memory and what was handed out. Plain fields: ESP-IDF
///        asks for each on its startup, one call at a time
///        (startup_memory.h).
struct State {
    std::array<Slot, kIdleTasks> slots{};                     ///< One per core, in index order.
    std::uint32_t                handedOut = 0;               ///< Slots nextIdleTaskMemory() gave.
    std::array<IpcSlot, 2>       ipc{};                       ///< `ipc0`'s and `ipc1`'s.
    MainSlot                     main{};                      ///< The main task's.
    std::uint32_t                tasksMade = 0;               ///< One bit per startup task created.
    StaticQueue_t                flashLock{};                 ///< The flash operations' mutex.
    bool                         flashLockMade    = false;    ///< flashLock is in use.
    std::uint32_t                interruptsRouted = 0;        ///< One bit per startup interrupt.
    char*                        environment      = nullptr;  ///< The C library's `environ` array.
    bool                         environServed    = false;    ///< environment is handed out.
};

/// @brief The one State, reached through a function so that no variable has
///        namespace scope (docs/misra.md, rule 6.7.2).
/// @return The slots and the counter.
State& state() noexcept {
    static constinit State instance;
    return instance;
}

}  // namespace

IdleTaskMemory idleTaskMemory( std::uint32_t index ) noexcept {
    if ( index >= kIdleTasks ) {
        return {};
    }
    Slot& slot = state().slots[index];
    return { &slot.tcb, slot.stack.data(), kIdleStackBytes };
}

IdleTaskMemory nextIdleTaskMemory() noexcept {
    State&               own    = state();
    const std::uint32_t  index  = own.handedOut;
    const IdleTaskMemory memory = idleTaskMemory( index );
    if ( memory.tcb != nullptr ) {
        own.handedOut = index + 1U;
    }
    return memory;
}

StartupTaskMemory startupTaskMemory( std::string_view name, std::uint32_t stackBytes ) noexcept {
    State& own = state();
    for ( std::uint32_t index = 0; index < kStartupTasks; ++index ) {
        if ( kStartupTaskNames[index] != name ) {
            continue;
        }
        const bool          isMain = index == 2U;
        const std::uint32_t bytes  = isMain ? kMainStackBytes : kIpcStackBytes;
        if ( stackBytes != bytes ) {
            return {};
        }
        if ( isMain ) {
            return { index, &own.main.tcb, own.main.stack.data(), bytes };
        }
        IpcSlot& slot = own.ipc[index];
        return { index, &slot.tcb, slot.stack.data(), bytes };
    }
    return {};
}

StaticQueue_t* flashLockMemory( std::uint8_t queueType ) noexcept {
    if ( static_cast<std::uint32_t>( queueType )
         != static_cast<std::uint32_t>( queueQUEUE_TYPE_RECURSIVE_MUTEX ) ) {
        return nullptr;
    }
    return &state().flashLock;
}

bool environmentRequest( std::size_t size ) noexcept {
    return size == sizeof( char* );
}

bool startupInterrupt( std::int32_t source, std::int32_t flags, bool wantsHandle ) noexcept {
    const auto bits = static_cast<std::uint32_t>( flags );
    if ( wantsHandle || ( bits & ~kRoutableFlags ) != 0U ) {
        return false;
    }
    for ( const std::int32_t known : kStartupInterrupts ) {
        if ( known == source ) {
            return true;
        }
    }
    return false;
}

namespace {

/// @brief The bit of startup interrupt @p source in State::interruptsRouted.
/// @param[in] source One of kStartupInterrupts.
/// @return Its bit; 0 for any other source.
std::uint32_t interruptBit( std::int32_t source ) noexcept {
    for ( std::uint32_t index = 0; index < kStartupInterrupts.size(); ++index ) {
        if ( kStartupInterrupts[index] == source ) {
            return 1U << index;
        }
    }
    return 0U;
}

}  // namespace

}  // namespace kleidos::meter::startup_memory

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" void __wrap_vApplicationGetIdleTaskMemory( StaticTask_t** tcb, StackType_t** stack,
                                                      std::uint32_t* stackBytes ) noexcept {
    const kleidos::meter::startup_memory::IdleTaskMemory memory =
        kleidos::meter::startup_memory::nextIdleTaskMemory();
    *tcb        = memory.tcb;
    *stack      = memory.stack;
    *stackBytes = memory.stackBytes;
}

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" std::int32_t __wrap_psa_crypto_init() noexcept {
    return kleidos::meter::startup_memory::kPsaSuccess;
}

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" BaseType_t __wrap_xTaskCreatePinnedToCore( TaskFunction_t entry, const char* name,
                                                      std::uint32_t stackBytes, void* argument,
                                                      UBaseType_t priority, TaskHandle_t* created,
                                                      BaseType_t core ) noexcept {
    using namespace kleidos::meter::startup_memory;
    const StartupTaskMemory memory =
        ( name == nullptr ) ? StartupTaskMemory{} : startupTaskMemory( name, stackBytes );
    State&              own = state();
    const std::uint32_t bit = ( memory.tcb == nullptr ) ? 0U : ( 1U << memory.index );
    if ( bit == 0U || ( own.tasksMade & bit ) != 0U ) {
        return errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY;
    }
    TaskHandle_t task = xTaskCreateStaticPinnedToCore( entry, name, stackBytes, argument, priority,
                                                       memory.stack, memory.tcb, core );
    if ( task == nullptr ) {
        return errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY;
    }
    own.tasksMade |= bit;
    if ( created != nullptr ) {
        *created = task;
    }
    return pdPASS;
}

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" QueueHandle_t __wrap_xQueueCreateMutex( std::uint8_t queueType ) noexcept {
    using namespace kleidos::meter::startup_memory;
    State&         own    = state();
    StaticQueue_t* buffer = flashLockMemory( queueType );
    if ( buffer == nullptr || own.flashLockMade ) {
        return nullptr;
    }
    QueueHandle_t mutex = xQueueCreateMutexStatic( queueType, buffer );
    own.flashLockMade   = mutex != nullptr;
    return mutex;
}

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" esp_err_t __wrap_esp_intr_alloc( std::int32_t source, std::int32_t flags,
                                            intr_handler_t handler, void* arg,
                                            intr_handle_t* retHandle ) noexcept {
    using namespace kleidos::meter;
    using namespace kleidos::meter::startup_memory;
    State&              own = state();
    const std::uint32_t bit = interruptBit( source );
    if ( !startupInterrupt( source, flags, retHandle != nullptr )
         || ( own.interruptsRouted & bit ) != 0U ) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    interrupt::Route route;
    const esp_err_t  routed =
        interrupt::routeHere( static_cast<std::uint32_t>( source ), handler, arg, route );
    if ( routed == ESP_OK ) {
        own.interruptsRouted |= bit;
    }
    return routed;
}

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" void* __wrap_malloc( std::size_t size ) noexcept {
    using namespace kleidos::meter::startup_memory;
    State& own = state();
    if ( !environmentRequest( size ) || own.environServed ) {
        return nullptr;
    }
    own.environServed = true;
    return static_cast<void*>( &own.environment );
}
