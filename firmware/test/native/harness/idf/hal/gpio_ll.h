// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The GPIO's registers, as ESP-IDF 6.0.3's hal/gpio_ll.h for the ESP32-S3
// reaches them, on the simulated board (sim::World's GPIO model): a pin's
// interrupt type and enable, and the status register, which a pin's edge
// latches and a write of ones clears (on the ESP32-S3 one enable and one
// status serve both cores, and the calls ignore their core); and a pad's
// function, input and pulls, output enable, output register and the
// matrix's output signal (sim::Pad, FW-10), and the pad an input signal
// is read from through the matrix (FW-13).
#include "driver/gpio.h"

#include <cstdint>

/// The GPIO peripheral's register block (`GPIO`, soc/gpio_struct.h).
struct gpio_dev_t {
    int unused;
};
extern gpio_dev_t GPIO;

void gpio_ll_set_intr_type( gpio_dev_t* hw, std::uint32_t gpio_num, gpio_int_type_t intr_type );
void gpio_ll_intr_enable_on_core( gpio_dev_t* hw, std::uint32_t core_id, std::uint32_t gpio_num );
void gpio_ll_intr_disable( gpio_dev_t* hw, std::uint32_t gpio_num );
/// GPIO0 to 31's latched status, of the pins whose interrupt is enabled.
void gpio_ll_get_intr_status( gpio_dev_t* hw, std::uint32_t core_id, std::uint32_t* status );
/// Clears the status bits of @p mask.
void gpio_ll_clear_intr_status( gpio_dev_t* hw, std::uint32_t mask );

/// The pad's output register.
void gpio_ll_set_level( gpio_dev_t* hw, std::uint32_t gpio_num, std::uint32_t level );
/// The pad's IO MUX function (PIN_FUNC_GPIO: the matrix; 0 on GPIO43 and 44:
/// UART0's TX and RX).
void gpio_ll_func_sel( gpio_dev_t* hw, std::uint8_t gpio_num, std::uint32_t func );
void gpio_ll_input_enable( gpio_dev_t* hw, std::uint32_t gpio_num );
/// Whether input signal @p signal_idx comes through the GPIO matrix or, when
/// @p from_gpio_matrix is false, straight from its IO MUX pin.
void gpio_ll_set_input_signal_from( gpio_dev_t* hw, std::uint32_t signal_idx,
                                    bool from_gpio_matrix );
void gpio_ll_input_disable( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_pullup_dis( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_pullup_en( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_pulldown_dis( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_od_disable( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_od_enable( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_output_enable( gpio_dev_t* hw, std::uint32_t gpio_num );
void gpio_ll_output_disable( gpio_dev_t* hw, std::uint32_t gpio_num );
/// Whether the output enable comes from the peripheral (@p ctrl_by_periph)
/// or from the GPIO's enable register.
void gpio_ll_set_output_enable_ctrl( gpio_dev_t* hw, std::uint8_t gpio_num, bool ctrl_by_periph,
                                     bool one_inv );
/// The signal the matrix drives the pad with (SIG_GPIO_OUT_IDX: the output
/// register).
void gpio_ll_set_output_signal_matrix_source( gpio_dev_t* hw, std::uint32_t gpio_num,
                                              std::uint32_t signal_idx, bool out_inv );
/// Input signal @p signal_idx read from pad @p gpio_num through the GPIO
/// matrix (and taken off its IO MUX pin).
void gpio_ll_set_input_signal_matrix_source( gpio_dev_t* hw, std::uint32_t signal_idx,
                                             std::uint32_t gpio_num, bool in_inv );
