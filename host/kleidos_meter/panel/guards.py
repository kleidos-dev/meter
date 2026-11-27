# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's accident guards: pure checks on a request, before any handler runs.

The panel is unauthenticated: whoever reaches its port can move the relays.
It listens on the loopback only, by default, and these guards keep what a
browser does on its own from reaching the instrument. They guard against
accidents and against other web pages, not against a person at the
machine. Every check is a function of the request alone, so the tests drive
them without a server, and :mod:`~kleidos_meter.panel.http` applies them in
this order:

1. **The Host header must name the loopback** (``localhost``, ``127.0.0.1``,
   ``[::1]``, any port), or any IP literal when serve was told to listen
   beyond it (``--allow-remote``); a name is never taken, so a page whose
   domain an attacker points at 127.0.0.1 (DNS rebinding) is answered 421.
2. **A GET never changes the instrument.** Routes that change it take POST
   only; a GET on one is answered 405, and the raw query route takes only
   the queries that read without acting (:func:`~kleidos_meter.panel.schema.get_refusal`).
3. **A change needs ``X-Kleidos-Meter: 1``.** A page of another origin
   cannot send a custom header without a CORS preflight, and the panel
   approves none: it never sends an ``Access-Control-Allow-*`` header, and
   answers ``OPTIONS`` 405. So a form or a ``fetch`` from elsewhere cannot
   fire a POST at it. A request that names an ``Origin`` other than the
   loopback's (or an IP literal's, as above) is refused too.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "MUTATION_HEADER",
    "MUTATION_VALUE",
    "Refusal",
    "check_host",
    "check_mutation",
    "host_name",
    "is_loopback_bind",
    "name_allowed",
]

#: The header every request that changes something must carry, and its value.
MUTATION_HEADER = "X-Kleidos-Meter"
MUTATION_VALUE = "1"


@dataclass(frozen=True)
class Refusal:
    """Why a request is refused, before any handler runs.

    :ivar status: The HTTP status.
    :ivar error: A short name for it, the JSON body's ``error``.
    :ivar detail: What the caller can do about it.
    """

    status: int
    error: str
    detail: str


def host_name(value: str) -> str | None:
    """The name a ``Host`` header (or an origin's netloc) gives, without its port.

    :param value: ``localhost:8040``, ``127.0.0.1``, ``[::1]:8040``.
    :returns: ``localhost``, ``127.0.0.1``, ``::1``; None when it is malformed.
    """
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        rest = value[end + 1 :] if end >= 0 else ""
        if end < 0 or (rest and not (rest[:1] == ":" and rest[1:].isdigit())):
            return None
        return value[1:end] or None
    name, colon, port = value.partition(":")
    if colon and not port.isdigit():
        return None  # a bad port, or an unbracketed IPv6 literal
    return name or None


def name_allowed(name: str | None, *, remote: bool) -> bool:
    """Whether a host *name* may reach the panel.

    :param name: As :func:`host_name` gives it.
    :param remote: Whether serve listens beyond the loopback (``--allow-remote``):
        then any IP literal is taken too, never a domain name.
    """
    if name is None:
        return False
    if name.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or remote


def check_host(host: str | None, *, remote: bool = False) -> Refusal | None:
    """The ``Host`` check: None when *host* may reach the panel.

    :param host: The request's ``Host`` header, None when it has none.
    :param remote: As :func:`name_allowed`.
    """
    if name_allowed(host_name(host or ""), remote=remote):
        return None
    return Refusal(
        421,
        "misdirected",
        "the panel answers requests to the loopback only (http://127.0.0.1:<port>/)",
    )


def check_mutation(headers: Mapping[str, str], *, remote: bool = False) -> Refusal | None:
    """The checks a request that changes something must pass: None when it does.

    :param headers: The request's headers, by name in any case.
    :param remote: As :func:`name_allowed`, for the ``Origin``.
    """
    lower = {key.lower(): value for key, value in headers.items()}
    if lower.get(MUTATION_HEADER.lower(), "").strip() != MUTATION_VALUE:
        return Refusal(
            403,
            "header_missing",
            f"a request that changes the instrument carries {MUTATION_HEADER}: {MUTATION_VALUE}",
        )
    origin = lower.get("origin")
    if origin is not None and not name_allowed(host_name(urlsplit(origin).netloc), remote=remote):
        return Refusal(403, "foreign_origin", "the panel takes changes from its own pages only")
    return None


def is_loopback_bind(address: str) -> bool:
    """Whether a listening *address* is on the loopback (``localhost`` counts).

    :param address: What ``--bind`` names.
    """
    return name_allowed(address, remote=False)
