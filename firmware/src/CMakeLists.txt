# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The one component of the bench meter. The source list is explicit rather than
# globbed: a file added without a line here fails to link, loudly, instead of
# being dropped from the image in silence.

idf_component_register(
    SRCS
        "main.cpp"
        "status_led.cpp"
        "ina228.cpp"
        "sampler.cpp"
        "calibration.cpp"
        "console.cpp"
        "console_commands.cpp"
        "scpi.cpp"
        "relay.cpp"
        "identity.cpp"
        "heap_monitor.cpp"
        "interrupt.cpp"
        "watchdog.cpp"
        "uart0.cpp"
        "i2c_bus.cpp"
        "flash_log.cpp"
        "flash_port.cpp"
        "pin_boot.cpp"
        "pin_apply.cpp"
        "pinmap_store.cpp"
        "startup_memory.cpp"
        "brownout.cpp"
        "text.cpp"
    INCLUDE_DIRS "."

    # Public, because it appears in our own headers: driver/gpio.h in
    # status_led.h, sampler.h, relay.h and ina228.h. freertos, heap and log
    # are common requirements of every component and are not listed.
    REQUIRES
        esp_driver_gpio

    # Private, because only the .cpp files reach for these: the RMT's
    # low-level layer (hal/rmt_ll.h, header-only inline functions: the LED's
    # off frame is written on the registers, with no driver), the watchdog
    # timer's and its timer group's (hal/mwdt_ll.h, hal/timg_ll.h: the
    # instrument's own watchdog, with ESP-IDF's task watchdog off), UART0's
    # (hal/uart_ll.h: the console's line on its registers, with no driver),
    # I2C0's (hal/i2c_ll.h: the own I2C master, FW-13, which the INA228's
    # driver uses from FW-14), the brownout detector's (hal/brownout_hal.h,
    # esp_hal_pmu: armed by the firmware, FW-18, with ESP-IDF's brownout
    # handling off), esp_timer for the sampler's pacing,
    # spi_flash for the record logs' esp_flash_* calls (flash_port.cpp:
    # the calibration's from FW-16, which took NVS's place, and the pin
    # map's from FW-03), esp_psram for the
    # TRACE ring's place in PSRAM (main.cpp's EXT_RAM_BSS_ATTR, which
    # CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY, an option of esp_psram's,
    # makes real: the build holds only what this component requires, so the
    # requirement is written), and esp_app_format for
    # esp_app_get_description(), which is how the firmware reads back the
    # version that firmware/version.txt stamped into the image. The factory
    # MAC the serial is derived from (esp_mac.h) is esp_hw_support's, a common
    # requirement.
    PRIV_REQUIRES
        esp_hal_pmu
        esp_hal_rmt
        esp_hal_wdt
        esp_hal_timg
        esp_hal_uart
        esp_hal_i2c
        esp_timer
        spi_flash
        esp_psram
        esp_app_format
)
