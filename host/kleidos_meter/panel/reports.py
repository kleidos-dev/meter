# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The instrument's reports and errors as the panel's JSON, and the HTTP status of a reply.

Shared by the HTTP API (:mod:`~kleidos_meter.panel.http`), the live events
(:mod:`~kleidos_meter.panel.live`) and the recorder
(:mod:`~kleidos_meter.panel.recorder`), so a report reads the same in a
route's answer, an event and a run:

- A value in canonical integer form becomes a JSON integer in the unit its
  key names (``i_na``, ``vbus_uv``); ``-`` becomes ``null``; anything else,
  and every key of :data:`~kleidos_meter.panel.schema.TEXT_KEYS`, stays the
  string the instrument wrote. An integer beyond 2^53 stays a string too, so
  a browser never rounds it.
- The instrument's errors are a list of ``{"code", "text"}``, oldest first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..protocol import ErrorEntry, parse_fields
from .schema import SCHEMA

__all__ = ["errors_json", "report_json", "status_for", "typed_fields"]

#: An integer as a report writes one, with no leading zero or sign but the minus.
_CANONICAL_INT = re.compile(r"-?(?:0|[1-9]\d*)")

#: The largest integer a browser's Number holds exactly.
_SAFE_INT = 2**53 - 1


def typed_fields(
    fields: Mapping[str, str], text_keys: Iterable[str] = SCHEMA.text_keys
) -> dict[str, Any]:
    """A report's fields as JSON: the module's rules for integers, ``-`` and text.

    :param fields: ``key=value`` pairs, as :func:`~kleidos_meter.protocol.parse_fields` gives them.
    :param text_keys: The keys whose values stay strings.
    """
    keep = frozenset(text_keys)
    typed: dict[str, Any] = {}
    for key, value in fields.items():
        if value == "-":
            typed[key] = None
        elif key not in keep and _CANONICAL_INT.fullmatch(value) and abs(int(value)) <= _SAFE_INT:
            typed[key] = int(value)
        else:
            typed[key] = value
    return typed


def report_json(unit: str, text_keys: Iterable[str] = SCHEMA.text_keys) -> dict[str, Any]:
    """A report unit as a JSON object (:func:`typed_fields` of its fields)."""
    return typed_fields(parse_fields(unit), text_keys)


def errors_json(errors: Iterable[ErrorEntry]) -> list[dict[str, Any]]:
    """The instrument's errors as ``{"code", "text"}`` objects, oldest first."""
    return [{"code": entry.code, "text": entry.text} for entry in errors]


def status_for(codes: Iterable[int]) -> int:
    """The HTTP status of a typed route's reply: 200, 400 for ``-1xx``, else 409.

    :param codes: The error numbers the instrument queued (``0`` is none).
    """
    found = [code for code in codes if code != 0]
    if not found:
        return 200
    if any(-199 <= code <= -100 for code in found):
        return 400
    return 409
