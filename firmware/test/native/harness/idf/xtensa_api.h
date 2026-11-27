// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// ESP-IDF's xtensa_api.h declares the Xtensa port's handler-table calls,
// which its esp_cpu.h wraps. interrupt.cpp includes it first, inside a C
// linkage block (the original has none); the harness's esp_cpu.h declares
// the wrappers themselves, so this one is empty.
