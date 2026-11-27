// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file startup_memory.h
 * @ingroup startup_memory
 * @brief Static storage in place of ESP-IDF's allocations before
 *        `app_main()`: the idle, IPC and main tasks, the flash lock, the
 *        system interrupts' records, and PSA's initialization, which the
 *        image never uses.
 *
 * Both are ESP-IDF's startup, before the firmware's first line, and neither
 * has an option that stops it (FW-18, `docs/misra.md` D1), so the image is
 * linked with a wrap of each (`firmware/CMakeLists.txt`):
 *
 * - **The idle tasks.** FreeRTOS creates one idle task per core with
 *   `xTaskCreateStaticPinnedToCore()` over memory it asks the application
 *   for through `vApplicationGetIdleTaskMemory()`; ESP-IDF's implementation
 *   (`components/freertos/port_common.c`) takes a stack of
 *   `configMINIMAL_STACK_SIZE` bytes and a control block from the heap for
 *   each, four allocations. `-Wl,--wrap=vApplicationGetIdleTaskMemory`
 *   sends the kernel's calls to __wrap_vApplicationGetIdleTaskMemory(),
 *   which hands out the two static slots below, the same size, in internal
 *   RAM. The kernel calls it from `prvCreateIdleTasks()`, once per core in
 *   order, on Core 0 before the scheduler starts, so the counter needs no
 *   lock.
 * - **PSA.** mbedTLS's startup hook (`components/mbedtls/port/
 *   esp_psa_crypto_init.c`) calls `psa_crypto_init()`, which took PSA's key
 *   slots (1,408 bytes) and two mutexes (four allocations) from the heap.
 *   Nothing in the image calls a PSA function (`scripts/check_heap_objects.py
 *   --elf` fails an image that links one), so `-Wl,--wrap=psa_crypto_init`
 *   answers the hook with `PSA_SUCCESS` and initializes nothing; PSA's code
 *   then leaves the image with its only caller.
 *
 * And four more since the review of 2026-11-24, which left D1 no row:
 *
 * - **The startup tasks.** `esp_ipc_init()` (a constructor) creates the two
 *   IPC tasks, `ipc0` and `ipc1`, and `esp_startup_start_app()` the `main`
 *   task `app_main()` runs on, each with `xTaskCreatePinnedToCore()`, which
 *   took the stack and the control block from the heap (six allocations,
 *   17,196 bytes). `-Wl,--wrap=xTaskCreatePinnedToCore` sends both callers
 *   to __wrap_xTaskCreatePinnedToCore(), which creates each with
 *   `xTaskCreateStaticPinnedToCore()` over its slot below, of the size ESP-IDF
 *   asks for, and refuses any other task, or the same one twice, as out of
 *   memory: nothing after them creates a task that way.
 * - **The flash lock.** `spi_flash_init_lock()` creates the flash
 *   operations' recursive mutex with `xSemaphoreCreateRecursiveMutex()`, a
 *   macro for `xQueueCreateMutex()`, and keeps whatever handle it returns.
 *   `-Wl,--wrap=xQueueCreateMutex` makes that one over a static
 *   `StaticQueue_t` (`xQueueCreateMutexStatic()`) and refuses any other.
 * - **The system interrupts.** `esp_crosscore_int_init()` (each core's
 *   cross-core interrupt) and `vSystimerSetup()` (each core's tick) register
 *   their handler with `esp_intr_alloc()`, which took a vector descriptor
 *   and a handle for each and freed the handle (eight allocations, four
 *   frees). `-Wl,--wrap=esp_intr_alloc` routes those four sources with
 *   interrupt::routeHere() on the calling core instead, the router the
 *   firmware's own interrupts use, which applies the conditions ESP-IDF's
 *   allocator applies to such a request (a non-shared level-1 line), and
 *   refuses anything else with `ESP_ERR_NOT_SUPPORTED`.
 * - **The C library's `environ`.** `esp_libc_init()`, a startup hook, sets
 *   `environ = malloc( sizeof( char* ) )`, the image's one call of
 *   `malloc()` (ESP-IDF's own `calloc()` and `heap_caps_malloc()` reach the
 *   heap directly). `-Wl,--wrap=malloc` sends it to __wrap_malloc(), which
 *   serves that one pointer from a static word, once, and refuses every other
 *   request with null.
 *
 * All of it runs on ESP-IDF's startup, each call once and on one core at a
 * time, so the counters below need no lock; every slot is in internal RAM.
 */
#ifndef KLEIDOS_METER_STARTUP_MEMORY_H
#define KLEIDOS_METER_STARTUP_MEMORY_H

#include <esp_err.h>
#include <esp_intr_alloc.h>
#include <esp_task.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include <cstddef>
#include <cstdint>
#include <string_view>

namespace kleidos::meter::startup_memory {

/// @addtogroup startup_memory
/// @{

/// @brief How many idle tasks FreeRTOS creates: one per core.
constexpr std::uint32_t kIdleTasks = 2U;

/// @brief Each idle task's stack, in bytes (ESP-IDF's port counts a stack in
///        bytes): `configMINIMAL_STACK_SIZE`, the size ESP-IDF's own hook
///        took from the heap.
constexpr std::uint32_t kIdleStackBytes = configMINIMAL_STACK_SIZE;

/// @brief `PSA_SUCCESS` (`psa/crypto_values.h`), the status the wrapped
///        `psa_crypto_init()` answers; `psa_status_t` is a 32-bit signed
///        integer.
constexpr std::int32_t kPsaSuccess = 0;

/// @brief One idle task's memory, as FreeRTOS asks for it.
struct IdleTaskMemory {
    StaticTask_t* tcb        = nullptr;  ///< The task's control block.
    StackType_t*  stack      = nullptr;  ///< Its stack's lowest byte.
    std::uint32_t stackBytes = 0U;       ///< Its stack's size, in bytes.
};

/// @brief The static memory of idle task @p index.
/// @param[in] index Which idle task: 0 for Core 0's, 1 for Core 1's.
/// @return Its control block, stack and the stack's size; both pointers
///         null and the size 0 for an index of kIdleTasks or more.
IdleTaskMemory idleTaskMemory( std::uint32_t index ) noexcept;

/// @brief The next idle task's static memory, in the order FreeRTOS creates
///        them; what __wrap_vApplicationGetIdleTaskMemory() hands out.
/// @return idleTaskMemory() of the next index: the first call Core 0's
///         slot, the second Core 1's, and a third or later the empty answer,
///         so that a kernel asking for more than kIdleTasks fails its own
///         `configASSERT` on the null buffers rather than share a stack.
IdleTaskMemory nextIdleTaskMemory() noexcept;

/// @brief The tasks ESP-IDF's startup creates with `xTaskCreatePinnedToCore()`:
///        the two IPC tasks and the main task.
constexpr std::uint32_t kStartupTasks = 3U;

/// @brief Each IPC task's stack, in bytes: `IPC_STACK_SIZE` of
///        `components/esp_system/esp_ipc.c`, which is the configured size.
constexpr std::uint32_t kIpcStackBytes = CONFIG_ESP_IPC_TASK_STACK_SIZE;

/// @brief The main task's stack, in bytes: `ESP_TASK_MAIN_STACK`, the
///        configured size and ESP-IDF's extra 512.
constexpr std::uint32_t kMainStackBytes = ESP_TASK_MAIN_STACK;

/// @brief A startup task's memory, or none.
struct StartupTaskMemory {
    std::uint32_t index      = kStartupTasks;  ///< Its slot, kStartupTasks for none.
    StaticTask_t* tcb        = nullptr;        ///< Its control block.
    StackType_t*  stack      = nullptr;        ///< Its stack's lowest byte.
    std::uint32_t stackBytes = 0U;             ///< Its stack's size, in bytes.
};

/// @brief The static memory of the startup task called @p name, when it asks
///        for the stack its slot holds.
/// @param[in] name       The task's name: `ipc0`, `ipc1` or `main`.
/// @param[in] stackBytes The stack it asks for, in bytes.
/// @return Its slot; the empty answer for any other name or size.
StartupTaskMemory startupTaskMemory( std::string_view name, std::uint32_t stackBytes ) noexcept;

/// @brief The flash lock's control block, for a request of @p queueType.
/// @param[in] queueType The kind of mutex asked for (`queueQUEUE_TYPE_*`).
/// @return The static `StaticQueue_t` for a recursive mutex
///         (`queueQUEUE_TYPE_RECURSIVE_MUTEX`); null for any other kind.
StaticQueue_t* flashLockMemory( std::uint8_t queueType ) noexcept;

/// @brief Whether ESP-IDF's startup's request to register interrupt
///        @p source is one __wrap_esp_intr_alloc() routes itself.
///
/// The four sources ESP-IDF's startup registers (each core's cross-core
/// interrupt and tick), not shared, not edge-triggered, at level 1 or at
/// no level named, enabled, and with no handle asked for: the handle is the
/// allocator's record, which a route does not make.
/// @param[in] source     The interrupt matrix's source.
/// @param[in] flags      The `ESP_INTR_FLAG_*` asked for.
/// @param[in] wantsHandle Whether the caller asked for a handle back.
/// @return True when the request is one of those four.
bool startupInterrupt( std::int32_t source, std::int32_t flags, bool wantsHandle ) noexcept;

/// @brief Whether a `malloc()` of @p size is the C library's request for its
///        `environ` array: one pointer, the empty list's terminator.
/// @param[in] size The bytes asked for.
/// @return True for `sizeof( char* )`.
bool environmentRequest( std::size_t size ) noexcept;

/// @}

}  // namespace kleidos::meter::startup_memory

/// @addtogroup startup_memory
/// @{

/// @brief FreeRTOS's request for an idle task's memory, as the image links
///        it: `-Wl,--wrap=vApplicationGetIdleTaskMemory` sends every call of
///        `vApplicationGetIdleTaskMemory()` here.
///
/// The name is the linker's convention for a wrapped symbol and the
/// signature FreeRTOS's (`task.h`), neither of them ours to choose
/// (`docs/misra.md` D7).
///
/// @param[out] tcb        Where the control block's address goes.
/// @param[out] stack      Where the stack's address goes.
/// @param[out] stackBytes Where the stack's size, in bytes, goes.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" void __wrap_vApplicationGetIdleTaskMemory( StaticTask_t** tcb, StackType_t** stack,
                                                      std::uint32_t* stackBytes ) noexcept;

/// @brief PSA's initialization, as the image links it:
///        `-Wl,--wrap=psa_crypto_init` sends mbedTLS's startup hook here.
///
/// Initializes nothing and allocates nothing: the image uses no PSA
/// function. The name is the linker's convention and the signature PSA's
/// (`psa/crypto.h`, `psa_status_t psa_crypto_init(void)`), neither ours to
/// choose (`docs/misra.md` D7).
///
/// @return kleidos::meter::startup_memory::kPsaSuccess, so that ESP-IDF's
///         startup goes on.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" std::int32_t __wrap_psa_crypto_init() noexcept;

/// @brief FreeRTOS's dynamic task creation, as the image links it:
///        `-Wl,--wrap=xTaskCreatePinnedToCore` sends ESP-IDF's startup's calls
///        here.
///
/// Creates the task over its static slot (kleidos::meter::startup_memory::
/// startupTaskMemory(), each slot once) with `xTaskCreateStaticPinnedToCore()`,
/// and refuses every other request as out of memory. The name is the
/// linker's convention and the signature FreeRTOS's (`task.h`), neither ours
/// to choose (`docs/misra.md` D7).
///
/// @param[in]  entry      The task's function.
/// @param[in]  name       Its name.
/// @param[in]  stackBytes Its stack, in bytes.
/// @param[in]  argument   Passed to @p entry.
/// @param[in]  priority   Its priority.
/// @param[out] created    Where its handle goes, when not null.
/// @param[in]  core       The core it is pinned to.
/// @retval pdPASS Created.
/// @retval errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY Not a startup task, asked
///         for twice, or of another stack size: nothing created.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" BaseType_t __wrap_xTaskCreatePinnedToCore( TaskFunction_t entry, const char* name,
                                                      std::uint32_t stackBytes, void* argument,
                                                      UBaseType_t priority, TaskHandle_t* created,
                                                      BaseType_t core ) noexcept;

/// @brief FreeRTOS's dynamic mutex creation, as the image links it:
///        `-Wl,--wrap=xQueueCreateMutex` sends `spi_flash_init_lock()`'s
///        call here.
///
/// The first recursive mutex is made over the static control block
/// (kleidos::meter::startup_memory::flashLockMemory()); any other request,
/// or a second, is refused. The name is the linker's convention and the
/// signature FreeRTOS's (`queue.h`), neither ours to choose (`docs/misra.md`
/// D7).
///
/// @param[in] queueType The kind of mutex (`queueQUEUE_TYPE_*`).
/// @return The mutex; null when refused.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" QueueHandle_t __wrap_xQueueCreateMutex( std::uint8_t queueType ) noexcept;

/// @brief ESP-IDF's interrupt allocator, as the image links it:
///        `-Wl,--wrap=esp_intr_alloc` sends the startup's registrations of
///        the cross-core and tick interrupts here.
///
/// Routes each of those four sources once, on the calling core, with
/// kleidos::meter::interrupt::routeHere(), and refuses anything else. The
/// name is the linker's convention and the signature ESP-IDF's
/// (`esp_intr_alloc.h`), neither ours to choose (`docs/misra.md` D7).
///
/// @param[in]  source     The interrupt matrix's source.
/// @param[in]  flags      The `ESP_INTR_FLAG_*` asked for.
/// @param[in]  handler    The handler.
/// @param[in]  arg        Passed to @p handler.
/// @param[out] retHandle  Must be null: no handle is made.
/// @retval ESP_OK                Routed and enabled.
/// @retval ESP_ERR_NOT_SUPPORTED Not one of the four, routed already, or a
///                               handle asked for: nothing routed.
/// @retval ESP_ERR_NOT_FOUND     No free line on this core.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" esp_err_t __wrap_esp_intr_alloc( std::int32_t source, std::int32_t flags,
                                            intr_handler_t handler, void* arg,
                                            intr_handle_t* retHandle ) noexcept;

/// @brief The C library's allocator, as the image links it:
///        `-Wl,--wrap=malloc` sends every call of `malloc()` here.
///
/// The image's one call is `esp_libc_init()`'s, a startup hook's, for the
/// C library's `environ` array: one pointer
/// (kleidos::meter::startup_memory::environmentRequest()), served once from
/// a static word. Every other request is refused with null, as an
/// allocator out of memory answers. ESP-IDF's own allocators (`calloc()`,
/// `heap_caps_malloc()`) call the heap directly and are not affected. The
/// name is the linker's convention and the signature the C library's
/// (`stdlib.h`), neither ours to choose (`docs/misra.md` D7).
///
/// @param[in] size The bytes asked for.
/// @return The static word for the first request of one pointer; null for
///         any other, and for a second.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" void* __wrap_malloc( std::size_t size ) noexcept;

/// @}

#endif  // KLEIDOS_METER_STARTUP_MEMORY_H
