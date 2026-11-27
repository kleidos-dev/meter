# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host client for the Kleidos bench power meter's SCPI protocol (``docs/scpi.md``)."""

from .client import Capture, Meter, MeterError, MeterTimeout, PortBusyError, SerialTransport
from .protocol import (
    BlockProgress,
    ErrorEntry,
    LineRouter,
    Mark,
    Overflow,
    Response,
    Sample,
    parse_errors,
    parse_fields,
    parse_record,
)
from .sim import SimulatedMeter

__all__ = [
    "BlockProgress",
    "Capture",
    "ErrorEntry",
    "LineRouter",
    "Mark",
    "Meter",
    "MeterError",
    "MeterTimeout",
    "Overflow",
    "PortBusyError",
    "Response",
    "Sample",
    "SerialTransport",
    "SimulatedMeter",
    "parse_errors",
    "parse_fields",
    "parse_record",
]

__version__ = "0.18.0"
