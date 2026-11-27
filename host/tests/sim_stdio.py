# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulator on standard input and output, as the firmware's native bridge runs.

The conformance tests start this as a stand-in for the bridge binary
(``KLEIDOS_METER_NATIVE_BRIDGE``), so the process path
(:class:`conformance.ProcessTransport`) is tested in every run, bridge or
not. It ends when its standard input closes.

Usage: ``python sim_stdio.py``
"""

from __future__ import annotations

import os
import select
import sys

from kleidos_meter.sim import SimulatedMeter


def main() -> int:
    """Serve one simulated instrument until standard input closes; the exit status."""
    meter = SimulatedMeter()
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    while True:
        ready, _, _ = select.select([stdin], [], [], 0.01)
        if ready:
            data = os.read(stdin, 65536)
            if not data:
                return 0
            meter.write(data)
        out = meter.read(1 << 20)
        while out:
            written = os.write(stdout, out)
            out = out[written:]


if __name__ == "__main__":
    raise SystemExit(main())
