<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Notices

## In the firmware image

The firmware links the Espressif IoT Development Framework (ESP-IDF 6.0.3) and the components it builds with it (FreeRTOS, newlib/picolibc, mbedTLS through ESP-IDF), under the Apache License 2.0 and the licenses ESP-IDF lists for each component. A binary distributed from this repository carries those notices with it; ESP-IDF's own `COPYRIGHT` file is the list.

## In the documents

The INA228's register facts in `docs/ina228-reference.md` and `firmware/src/ina228_regs.h` are restated from Texas Instruments' datasheet (SBOSA20, cited by section) and are not a copy of it. No vendor datasheet is stored in this repository; `hardware/bom.md` names where each part's is published.

## The KiCad kit

`hardware/kicad/` is the Kleidos KiCad kit, byte copies of the brand repository's `kicad/` at the commit `hardware/kicad/KIT.json` records (<https://github.com/kleidos-dev/brand>, the same author). Its color themes, resolved style and helper are under the MIT License (`LICENSES/MIT.txt`); its two drawing sheets embed the Kleidos mark and are brand's artwork under the GNU GPL version 3 or later with brand's additional terms. `hardware/rig.pdf` and `hardware/rig.svg` show that mark in their title block placed unchanged under brand's press permission (<https://github.com/kleidos-dev/brand/blob/main/TRADEMARKS.md>), which does not make them covered works: they stay under CERN-OHL-S-2.0, and their metadata credits the mark. The Kleidos name and logo are subject to the terms in that TRADEMARKS.md.

## Nothing vendored

There is no third-party source code in this repository. The KiCad kit above is the project's own, copied from its brand repository.
