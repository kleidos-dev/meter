// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "esp_err.h"

#include <cstdint>
typedef enum {
    GPIO_NUM_NC  = -1,
    GPIO_NUM_0   = 0,
    GPIO_NUM_1   = 1,
    GPIO_NUM_2   = 2,
    GPIO_NUM_4   = 4,
    GPIO_NUM_5   = 5,
    GPIO_NUM_6   = 6,
    GPIO_NUM_7   = 7,
    GPIO_NUM_8   = 8,
    GPIO_NUM_9   = 9,
    GPIO_NUM_10  = 10,
    GPIO_NUM_11  = 11,
    GPIO_NUM_12  = 12,
    GPIO_NUM_13  = 13,
    GPIO_NUM_14  = 14,
    GPIO_NUM_15  = 15,
    GPIO_NUM_16  = 16,
    GPIO_NUM_17  = 17,
    GPIO_NUM_18  = 18,
    GPIO_NUM_21  = 21,
    GPIO_NUM_38  = 38,
    GPIO_NUM_39  = 39,
    GPIO_NUM_40  = 40,
    GPIO_NUM_41  = 41,
    GPIO_NUM_42  = 42,
    GPIO_NUM_47  = 47,
    GPIO_NUM_48  = 48,
    GPIO_NUM_MAX = 49,
} gpio_num_t;
typedef enum {
    GPIO_MODE_DISABLE      = 0,
    GPIO_MODE_INPUT        = 1,
    GPIO_MODE_OUTPUT       = 2,
    GPIO_MODE_INPUT_OUTPUT = 3,
} gpio_mode_t;
typedef enum {
    GPIO_PULLUP_DISABLE = 0,
    GPIO_PULLUP_ENABLE  = 1
} gpio_pullup_t;
typedef enum {
    GPIO_PULLDOWN_DISABLE = 0,
    GPIO_PULLDOWN_ENABLE  = 1
} gpio_pulldown_t;
typedef enum {
    GPIO_INTR_DISABLE = 0,
    GPIO_INTR_POSEDGE = 1,
    GPIO_INTR_NEGEDGE = 2,
} gpio_int_type_t;
typedef struct {
    std::uint64_t   pin_bit_mask;
    gpio_mode_t     mode;
    gpio_pullup_t   pull_up_en;
    gpio_pulldown_t pull_down_en;
    gpio_int_type_t intr_type;
} gpio_config_t;
// No GPIO interrupt driver: the firmware routes the ALERT pin's interrupt
// itself and reaches the pin's interrupt registers through hal/gpio_ll.h
// (FW-09), so the ISR service and gpio_intr_enable() are not faked.
esp_err_t gpio_config( const gpio_config_t* config );
esp_err_t gpio_set_level( gpio_num_t pin, std::uint32_t level );
int       gpio_get_level( gpio_num_t pin );
