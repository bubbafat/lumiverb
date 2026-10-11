"""Who sent a request, as nginx saw it (src/server/api/client_address.py):
the rate limits and first-run setup's "only from this machine" both rely
on it. nginx's own entry is the right-most; a request that didn't come
through this machine's nginx is its own address, whatever it claims."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from src.server.api.client_address import client_address, is_loopback, on_this_machine

pytestmark = pytest.mark.fast


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "client": (peer, 50000), "headers": headers})


@pytest.mark.parametrize(("host", "loopback"), [
    ("127.0.0.1", True), ("127.0.0.5", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("192.168.86.20", False), ("::ffff:192.168.86.20", False), ("testclient", False), ("", False),
])
def test_loopback(host, loopback):
    assert is_loopback(host) is loopback


def test_through_this_machines_nginx_its_entry_counts():
    assert client_address(_request("127.0.0.1", "10.0.0.9, 192.168.86.20")) == "192.168.86.20"
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1")) is True


def test_from_anywhere_else_the_header_is_ignored():
    assert client_address(_request("192.168.86.20", "127.0.0.1")) == "192.168.86.20"
    assert on_this_machine(_request("192.168.86.20", "127.0.0.1")) is False


def test_without_a_header_the_connection_counts():
    assert on_this_machine(_request("127.0.0.1")) is True
    assert on_this_machine(_request("192.168.86.20")) is False
