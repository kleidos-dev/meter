// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
typedef struct {
    char         version[32];
    char         project_name[32];
    std::uint8_t app_elf_sha256[32];
} esp_app_desc_t;
const esp_app_desc_t* esp_app_get_description();
