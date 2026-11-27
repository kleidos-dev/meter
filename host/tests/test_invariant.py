# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The package's checks of its own guarantees fail loudly, and only when broken."""

from __future__ import annotations

import pytest
from kleidos_meter.invariant import present


def test_present_hands_back_what_is_there_and_refuses_none() -> None:
    assert present(0, "a count") == 0
    assert present("", "a label") == ""
    with pytest.raises(RuntimeError, match="the session's start is missing"):
        present(None, "the session's start")
