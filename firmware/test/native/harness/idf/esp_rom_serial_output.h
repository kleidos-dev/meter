// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
/// Writes @p c to the ROM's console UART (the fake appends it to UART0's
/// transmitted bytes).
void esp_rom_output_putc( char c );
