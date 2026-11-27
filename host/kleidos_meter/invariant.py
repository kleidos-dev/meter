# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the package's own logic guarantees, said so the program checks it.

Where a value cannot be ``None`` because of what ran before (a session that
was started, a setting the schema declares, a capture that is running), the
code says so with :func:`present`. Unlike an ``assert``, it does not go away
under ``python -O``: a broken guarantee is a :class:`RuntimeError` that names
it, never an ``AttributeError`` further on.
"""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def present(value: T | None, what: str) -> T:
    """Return *value*, which the caller's own logic guarantees is not ``None``.

    :param value: The value.
    :param what: What it is, for the error.
    :returns: *value*.
    :raises RuntimeError: When it is ``None`` after all.
    """
    if value is None:
        raise RuntimeError(f"{what} is missing, which the code before this point rules out")
    return value
