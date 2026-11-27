// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include "esp_err.h"

#include <cstdint>
esp_err_t esp_efuse_mac_get_default( std::uint8_t* mac );
