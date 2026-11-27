// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The GPIO matrix's signals the firmware routes, numbered as ESP-IDF 6.0.3's
// soc/esp32s3/include/soc/gpio_sig_map.h numbers them.
#define U0RXD_IN_IDX 12         ///< UART0's receive input.
#define U0TXD_OUT_IDX 12        ///< UART0's transmit output.
#define RMT_SIG_OUT0_IDX 81     ///< RMT transmit channel 0's output.
#define I2CEXT0_SCL_IN_IDX 89   ///< I2C0's clock line, in.
#define I2CEXT0_SCL_OUT_IDX 89  ///< I2C0's clock line, out.
#define I2CEXT0_SDA_IN_IDX 90   ///< I2C0's data line, in.
#define I2CEXT0_SDA_OUT_IDX 90  ///< I2C0's data line, out.
#define SIG_GPIO_OUT_IDX 256    ///< The GPIO's own output register.
