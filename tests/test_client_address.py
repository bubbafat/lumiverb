"""Who sent a request, as nginx saw it (src/server/api/client_address.py):
the rate limits and every "only from this machine" choice (first-run setup,
a Mac's password reset, folders, Settings › Network) rely on it. nginx's own
entry is the right-most; a request that didn't come through this machine's
nginx is its own address, whatever it claims. "This machine" is also
addressed as localhost, from no page or a page at localhost."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from src.server.api.client_address import (
    client_address,
    is_local_name,
    is_loopback,
    on_this_machine,
)

pytestmark = pytest.mark.fast


def _request(peer: str, forwarded: str | None = None, *, host: str | None = "localhost:6464",
             origin: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    headers += [(b"host", host.encode())] if host is not None else []
    headers += [(b"origin", origin.encode())] if origin is not None else []
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


@pytest.mark.parametrize(("value", "local"), [
    ("localhost:6464", True), ("localhost", True), ("LOCALHOST:6464", True), ("localhost.:6464", True),
    ("127.0.0.1:6464", True), ("127.0.0.1", True), ("[::1]:6464", True), ("[::1]", True),
    ("http://localhost:6464", True), ("http://127.0.0.1:6464", True), ("http://[::1]:6464", True),
    ("macstudio.local:6464", False), ("192.168.86.20:6464", False), ("lumiverb.example.com", False),
    ("localhost.example.com:6464", False), ("http://evil.example:6464", False), ("null", False), ("", False),
])
def test_local_names(value, local):
    assert is_local_name(value) is local


@pytest.mark.parametrize("host", ["localhost:6464", "127.0.0.1:6464", "[::1]:6464", "localhost"])
def test_addressed_as_localhost_from_here_is_this_machine(host):
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", host=host)) is True
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", host=host, origin=f"http://{host}")) is True


def test_a_page_whose_name_now_points_here_is_not_this_machine():
    # Its requests reach nginx from 127.0.0.1, but they carry the page's own name.
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", host="rebind.example:6464")) is False
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", host="rebind.example:6464",
                                    origin="http://rebind.example:6464")) is False


def test_another_site_posting_to_localhost_is_not_this_machine():
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", origin="https://elsewhere.example")) is False
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", origin="null")) is False


def test_the_macs_own_name_or_address_is_not_localhost():
    # Opened as macstudio.local the connection comes from the Mac's LAN address anyway.
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", host="macstudio.local:6464")) is False
    assert on_this_machine(_request("127.0.0.1", "127.0.0.1", host="192.168.86.55:6464")) is False


def test_no_host_is_not_this_machine():
    assert on_this_machine(_request("127.0.0.1", host=None)) is False
