# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the guards that read a device build share: a missing build skips, or fails when required.

A guard over the build's outputs (the generated sdkconfig, the compile
database, CMake's code model) has nothing to read on a checkout that was never
built, and skips there. Where the image was just built, CI's firmware job,
``METER_BUILD_REQUIRED=1`` turns that skip into a failure: a guard that skips
where it should have run passes while checking nothing.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

#: The environment variable that makes a missing build output a failure.
REQUIRED = "METER_BUILD_REQUIRED"


def build_output(path: Path, how: str) -> Path:
    """Return *path*, or skip (fail with ``METER_BUILD_REQUIRED=1``) when it does not exist.

    :param path: the build output the guard reads.
    :param how: the command that writes it, for the message.
    :returns: *path*, which exists.
    """
    if not path.exists():
        message = f"{path.name} does not exist: {how} writes it, and this guard reads it"
        if os.environ.get(REQUIRED) == "1":
            pytest.fail(f"{message} ({REQUIRED}=1, so a missing build is a failure)")
        pytest.skip(message)
    return path


@pytest.fixture
def built() -> Callable[[Path, str], Path]:
    """The :func:`build_output` check, as a fixture for the guards that read a build."""
    return build_output
