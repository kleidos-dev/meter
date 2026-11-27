# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's accident guards, as pure functions of a request."""

from __future__ import annotations

import pytest
from kleidos_meter.panel.guards import (
    check_host,
    check_mutation,
    host_name,
    is_loopback_bind,
    name_allowed,
)


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("localhost:8040", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        (" 127.0.0.1:8040 ", "127.0.0.1"),
        ("[::1]:8040", "::1"),
        ("[::1]", "::1"),
        ("evil.example", "evil.example"),
        ("", None),
        (":8040", None),
        ("[]", None),
        ("[::1", None),
        ("[::1]x", None),
        ("[::1]:80a", None),
        ("::1", None),  # an IPv6 literal is bracketed in a Host header
        ("127.0.0.1:port", None),
    ],
)
def test_a_host_header_names_its_host(value: str, name: str | None) -> None:
    assert host_name(value) == name


def test_the_loopback_alone_is_allowed_unless_serve_listens_beyond_it() -> None:
    for name in ("localhost", "LOCALHOST", "127.0.0.1", "127.8.9.1", "::1"):
        assert name_allowed(name, remote=False)
    for other in ("192.168.0.10", "10.0.0.1", "kleidos.local", "evil.example", None):
        assert not name_allowed(other, remote=False)
    assert name_allowed("192.168.0.10", remote=True)
    assert name_allowed("fe80::1", remote=True)
    assert not name_allowed("evil.example", remote=True)  # a name is never taken


def test_a_foreign_host_is_misdirected() -> None:
    assert check_host("127.0.0.1:8040") is None
    assert check_host("[::1]:8040") is None
    for host in ("evil.example:8040", "", None, "192.168.0.10:8040"):
        refusal = check_host(host)
        assert refusal is not None and refusal.status == 421 and refusal.error == "misdirected"
    assert check_host("192.168.0.10:8040", remote=True) is None


def test_a_change_needs_the_header_and_a_loopback_origin() -> None:
    assert check_mutation({"X-Kleidos-Meter": "1"}) is None
    assert check_mutation({"x-kleidos-meter": " 1 "}) is None
    assert check_mutation({"X-Kleidos-Meter": "1", "Origin": "http://127.0.0.1:8040"}) is None
    assert check_mutation({"X-Kleidos-Meter": "1", "Origin": "http://localhost:8040"}) is None
    for headers in ({}, {"X-Kleidos-Meter": "0"}, {"X-Kleidos-Meter": "yes"}):
        refusal = check_mutation(headers)
        assert refusal is not None and refusal.status == 403
        assert refusal.error == "header_missing"
    for origin in ("https://evil.example", "null", "http://192.168.0.10:8040"):
        refusal = check_mutation({"X-Kleidos-Meter": "1", "Origin": origin})
        assert refusal is not None and refusal.error == "foreign_origin"
    remote = {"X-Kleidos-Meter": "1", "Origin": "http://192.168.0.10:8040"}
    assert check_mutation(remote, remote=True) is None


def test_a_listening_address_is_on_the_loopback_or_not() -> None:
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("::1")
    assert is_loopback_bind("localhost")
    assert not is_loopback_bind("0.0.0.0")  # noqa: S104  the address the guard refuses
    assert not is_loopback_bind("::")
    assert not is_loopback_bind("192.168.0.10")
