# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# ESP-IDF project file for the Kleidos bench power meter.
# See platformio.ini for what this project is and how it is built.

cmake_minimum_required(VERSION 3.16.0)

# --- Single version source --------------------------------------------------
# The instrument carries its OWN version (docs/architecture.md SS 10,
# CHANGELOG.md): firmware/version.txt. ESP-IDF reads it natively --
# __project_get_revision_from_version_file() in tools/cmake/project.cmake
# stamps it into esp_app_desc_t.version -- so the firmware reads its own version
# back through esp_app_get_description() and no second copy exists in the
# sources.
#
# The file is read when CMake configures, and the configure dependency below
# re-runs that step only under CMake's own build tool. PlatformIO's espidf
# builder never runs it: it configures when build.ninja is missing or older
# than a CMakeLists.txt or sdkconfig, so scripts/meter_version_reconfigure.py
# (a pre: extra script in platformio.ini) is what makes an incremental build
# take a changed version.txt.
set(METER_VERSION_FILE "${CMAKE_CURRENT_LIST_DIR}/version.txt")
if(NOT EXISTS "${METER_VERSION_FILE}")
    message(FATAL_ERROR
        "firmware/version.txt is missing. It is the single source of the "
        "instrument's version; without it ESP-IDF falls back to a `git "
        "describe` string and every capture header would report it as fw=.")
endif()
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${METER_VERSION_FILE}")

# --- The components the firmware needs, and no other -------------------------
# ESP-IDF builds every component of its tree unless told otherwise, and a
# component that is built can be linked by another's optional dependency:
# NVS was, after the firmware stopped calling it (FW-16), because mbedTLS
# compiles its PSA storage onto NVS whenever nvs_flash is in the build
# ($IDF/components/mbedtls/CMakeLists.txt, "Only compile esp_psa_its.c if
# nvs_flash component is available"), and psa_crypto_init(), which ESP-IDF's
# startup runs, reaches it; NVS then brought operator new, the C++ exception
# runtime and its eh_globals (which takes a thread key from the heap before
# app_main()). The build is the firmware's component and what it requires,
# directly or through ESP-IDF's own requirements, and nothing else: no NVS,
# no Wi-Fi, Bluetooth, PHY or coexistence, which the image never linked.
# PlatformIO's espidf builder adds a component of its own, __pio_env (empty
# sources, in the framework's components directory), whose compile settings
# it reads for files outside any component; it must be in the list.
set(COMPONENTS src __pio_env)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# --- Linker warnings are errors ---------------------------------------------
# A warning from the link (a section that does not fit where linker.lf puts
# it, a symbol defined twice, a deprecated option) is an error, as the
# compiler's are (platformio.ini build_src_flags). ESP-IDF's LINK_OPTIONS
# reach the image's link command, which PlatformIO's espidf builder takes
# from CMake's code model; tests/firmware/test_effective_compile_flags.py
# holds the option in this file and in that link command.
idf_build_set_property(LINK_OPTIONS "-Wl,--fatal-warnings" APPEND)

# --- The image keeps its relocations ----------------------------------------
# scripts/check_heap_reachability.py builds the image's call graph from its
# relocation records (each call, literal load and address in data, as the
# linker resolved it, wraps included) and fails when an allocator is
# reachable from the firmware's own code (FW-19, docs/misra.md). The records
# go into non-loaded sections of firmware.elf: the loaded sections, and so
# firmware.bin, are the bytes the link writes without them.
# tests/firmware/test_effective_compile_flags.py holds the option here and in
# the image's link command.
idf_build_set_property(LINK_OPTIONS "-Wl,--emit-relocs" APPEND)

# --- ESP-IDF's flash region check, on a static table ------------------------
# esp_flash_write() and esp_flash_erase_region() ask
# esp_partition_is_flash_region_writable() before they touch the main flash,
# and it walks the partition table with an iterator it allocates (loading the
# table into a list it allocates the first time). The wrap sends every call
# of it to __wrap_esp_partition_is_flash_region_writable() in
# src/flash_port.cpp, a comparison with the two record logs' sectors, and
# CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED (sdkconfig.defaults) leaves out
# ESP-IDF's second check, which needed the running partition (FW-16,
# docs/architecture.md). tests/firmware/test_effective_compile_flags.py
# holds the option here and in the image's link command.
idf_build_set_property(LINK_OPTIONS
    "-Wl,--wrap=esp_partition_is_flash_region_writable" APPEND)

# --- What ESP-IDF's startup would allocate, on static storage ----------------
# Two of ESP-IDF's startup calls allocate before app_main() and offer no
# option that stops them (FW-18, docs/misra.md D1). FreeRTOS takes each idle
# task's stack and control block from vApplicationGetIdleTaskMemory(), which
# ESP-IDF's port_common.c implements with pvPortMalloc(): the wrap hands out
# two static ones instead (src/startup_memory.cpp). And mbedTLS's startup
# hook calls psa_crypto_init(), which allocated PSA's key slots and two
# mutexes; nothing in the image uses PSA (scripts/check_heap_objects.py
# --elf fails an image that holds a psa_ function), so the wrap answers
# PSA_SUCCESS without initializing it. tests/firmware/
# test_effective_compile_flags.py holds both options here and in the image's
# link command.
idf_build_set_property(LINK_OPTIONS
    "-Wl,--wrap=vApplicationGetIdleTaskMemory" APPEND)
idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=psa_crypto_init" APPEND)
# And three more (the review of 2026-11-24): the IPC and main tasks
# ESP-IDF's startup creates with xTaskCreatePinnedToCore(), the flash lock it
# creates with xQueueCreateMutex(), and the cross-core and tick interrupts it
# registers with esp_intr_alloc(); each is a call from another object, so
# the wrap reaches it, and src/startup_memory.cpp serves it from static
# storage, or routes it with the firmware's own interrupt router, and
# refuses anything else.
idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=xTaskCreatePinnedToCore" APPEND)
idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=xQueueCreateMutex" APPEND)
idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=esp_intr_alloc" APPEND)
# And the last (the review of 2026-11-24, HM2-04): the C library's environ,
# the one malloc() call the image makes (esp_libc_init(), a startup hook).
# src/startup_memory.cpp serves that one pointer from a static word and
# refuses every other request; calloc() and heap_caps_malloc() call the heap
# directly and are not wrapped.
idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=malloc" APPEND)

project(kleidos-meter)

# --- Explicit C++26 pin -----------------------------------------------------
# The decode helpers in scale.h rest on guarantees C++20 made normative: the
# conversion to a signed type is two's complement by definition and `>>` on a
# signed value is arithmetic. The fleet targets C++26 and so does the
# instrument, and for the same reason the fleet's root CMakeLists.txt gives:
# the standard is OUR choice, not whatever the framework happens to default to.
# IDF's own `-std` is applied first and ours is appended after, and GCC honors
# the last one on the command line.
#
# The `src` IDF library target is `__idf_src` (double underscore); it is created
# by idf_component_register() in src/CMakeLists.txt, which runs during the
# project() call above.
if(TARGET __idf_src)
    target_compile_options(__idf_src PRIVATE
        "$<$<COMPILE_LANGUAGE:CXX>:-std=gnu++26>")
endif()

# --- Strict-warning hygiene -------------------------------------------------
# Our component is compiled with the fleet's strict warning set (-Werror plus
# -Wold-style-cast, -Wconversion, -Wsign-conversion, -Wdouble-promotion, ...).
# The ESP-IDF headers we include are not under our control and legitimately trip
# those warnings (FreeRTOS's pdTRUE expands a C-style cast inside our translation
# units, for one), so they are reclassified as system headers for our targets
# only. This mirrors the fleet's root CMakeLists.txt, including its two
# hard-won constraints:
#
#   * add the directories as raw glued `-isystem<dir>` flags on our own target
#     rather than setting CMake's SYSTEM property, which would empty
#     INTERFACE_INCLUDE_DIRECTORIES and break PlatformIO's include extraction;
#   * emit one token with no space, because PlatformIO re-parses each target's
#     compile options through SCons ParseFlags, which would otherwise mistake
#     the bare directory path for a library file and abort.
#
# A space-bearing path is redirected through a space-free symlink in the build
# tree, because ParseFlags runs the flag through shlex.split().
set(METER_OWN_COMPONENTS src)

function(_meter_add_system_include _dir)
    if(_dir MATCHES " ")
        set(_link_root "${CMAKE_BINARY_DIR}/meter_system_includes")
        string(MD5 _hash "${_dir}")
        set(_link "${_link_root}/${_hash}")
        file(MAKE_DIRECTORY "${_link_root}")
        if(NOT EXISTS "${_link}")
            file(CREATE_LINK "${_dir}" "${_link}" SYMBOLIC)
        endif()
        set(_dir "${_link}")
    endif()
    foreach(_own IN LISTS METER_OWN_COMPONENTS)
        if(TARGET "__idf_${_own}")
            target_compile_options("__idf_${_own}" PRIVATE "-isystem${_dir}")
        endif()
    endforeach()
endfunction()

idf_build_get_property(_meter_components BUILD_COMPONENTS)
foreach(_meter_component IN LISTS _meter_components)
    if(NOT _meter_component IN_LIST METER_OWN_COMPONENTS)
        set(_meter_lib "__idf_${_meter_component}")
        if(TARGET ${_meter_lib})
            get_target_property(_meter_inc ${_meter_lib} INTERFACE_INCLUDE_DIRECTORIES)
            if(_meter_inc)
                foreach(_meter_dir IN LISTS _meter_inc)
                    # Skip generator expressions; IDF interface includes are
                    # almost always plain absolute paths, but guard anyway.
                    if(NOT _meter_dir MATCHES "\\$<")
                        _meter_add_system_include("${_meter_dir}")
                    endif()
                endforeach()
            endif()
        endif()
    endif()
endforeach()
