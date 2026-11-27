// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// On the device these place code in IRAM, constant data in internal RAM,
// zero-initialized data in PSRAM's .ext_ram.bss and data a reset leaves alone
// in RTC memory; on the host all four are ordinary.
#define IRAM_ATTR
#define DRAM_ATTR
#define EXT_RAM_BSS_ATTR
#define RTC_NOINIT_ATTR
