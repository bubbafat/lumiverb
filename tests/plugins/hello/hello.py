"""A plug-in for Lumiverb's tests (ADR-023), written against the protocol
alone: Python's standard library, nothing of Lumiverb's. It listens on the
Unix socket Lumiverb names (LUMIVERB_PLUGIN_SOCKET) and answers its health."""

import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def address_string(self) -> str:  # a Unix socket's peer has no host
        return "lumiverb"

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        if self.path == "/v1/health":
            self._answer(200, {"state": "ready"})
        else:
            self._answer(404, {"error": "not found"})

    def _answer(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(socketserver.UnixStreamServer):
    def get_request(self):
        request, _ = super().get_request()
        return request, ("lumiverb", 0)


if __name__ == "__main__":
    path = os.environ["LUMIVERB_PLUGIN_SOCKET"]
    if os.path.exists(path):
        os.unlink(path)
    with Server(path, Handler) as server:
        server.serve_forever()
