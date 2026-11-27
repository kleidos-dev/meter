// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The timer groups' bus clock and reset, as ESP-IDF 6.0.3's hal/timg_ll.h for
// the ESP32-S3 offers them (components/esp_hal_timg/esp32s3/include): the
// underscored functions the macros of the same name without the underscore
// wrap, on the simulated board's groups (sim::World::timerGroups). Each counts
// a call made outside the peripheral clock lock (sim::TimerGroup::rccViolations);
// a reset puts the group's watchdog back to its reset values, with the flash
// boot protection off, as the device's function leaves it.

void _timg_ll_enable_bus_clock( int group_id, bool enable );
void _timg_ll_reset_register( int group_id );
