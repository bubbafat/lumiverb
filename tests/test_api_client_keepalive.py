"""The API client never reuses a connection near the server closing it
(src/processing/api.py KEEPALIVE_SEC): uvicorn closes an idle kept-alive
connection after 5 s, and a request sent on one just as it did was reset
("Connection reset by peer"; Robert's Mac, twice in two hours under load).
A real uvicorn server says which connection each request came on."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from src.processing.api import KEEPALIVE_SEC, ApiClient

pytestmark = pytest.mark.fast


async def _app(scope, receive, send):
    """Answers with the client's port: a new port is a new connection."""
    if scope["type"] != "http":
        return
    port = str(scope["client"][1]).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": port})


@pytest.fixture
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(_app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not srv.started:
        assert time.monotonic() < deadline, "the test server didn't start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}", config
    srv.should_exit = True
    thread.join(60)


def test_it_lets_go_of_an_idle_connection_well_before_the_server_does(server):
    url, config = server
    assert KEEPALIVE_SEC + 2 <= config.timeout_keep_alive, "a margin a busy machine's pause fits in"
    with ApiClient(url, None) as client:
        first = client.get("/").text
        idle_since = time.monotonic()
        gap = time.monotonic() - idle_since  # how long it sat idle, measured as the next is sent
        second = client.get("/").text
        # Reused only while it's fresh, whatever this machine's speed between the two.
        assert (second == first) is (gap < KEEPALIVE_SEC)
        time.sleep(KEEPALIVE_SEC + 0.5)
        third = client.get("/").text
    assert third != second, "idle past KEEPALIVE_SEC: a new connection, never the one the server may be closing"
