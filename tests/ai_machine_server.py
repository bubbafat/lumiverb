"""An AI machine on this computer, for tests: a real HTTP server on 127.0.0.1
(loopback, where an Ollama is allowed), or HTTPS with a certificate it's
given (`certificate`: one made as Lumiverb's AI worker makes its own), which
lists its models at GET /v1/models and answers every other request as it's
told. What it was asked is kept, so a test can say a request never reached
it.

`works` is an answer every kind of work reads as done (a description, a
transcript, a vector): a machine a redirect pointed at answers with it, so
a request that followed one would succeed and the test would see it.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL = "qwen3-vl:8b"

WORKS = {
    "data": [{"id": MODEL, "object": "model"}],
    "choices": [{"message": {"content": '{"description": "Followed a redirect.", "tags": []}'},
                 "finish_reason": "stop"}],
    "language": "en", "segments": [{"start": 0.0, "end": 1.0, "text": "followed a redirect"}],
    "model_id": "clip", "model_version": "ViT-B-32-openai",
    "results": [{"vector": [1.0, 0.0, 0.0], "faces": []}],
}


class LocalMachine:
    """lists: the models GET /v1/models lists (None: that's answered as everything else is). answer: (status,
    body: JSON or bytes, headers) for everything else, `delay` seconds after it's asked. tls: (certificate, key) files
    to serve HTTPS with. asked: [(method, path)] as they came; sent: what each sent, in that order."""

    def __init__(self, answer: tuple[int, Any, dict[str, str]] = (200, WORKS, {}),
                 lists: tuple[str, ...] | None = (MODEL,), tls: tuple[Path, Path] | None = None) -> None:
        self.answer = answer
        self.delay = 0.0
        self.asked: list[tuple[str, str]] = []
        self.sent: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.sent.append(self.rfile.read(length) if length else b"")
                outer.asked.append((self.command, self.path))
                if self.command == "GET" and self.path.endswith("/models") and lists is not None:
                    status, body, headers = 200, {"object": "list", "data": [{"id": m} for m in lists]}, {}
                else:
                    time.sleep(outer.delay)
                    status, body, headers = outer.answer
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, *_: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*map(str, tls))
            self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_port
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def certificate(folder: Path) -> tuple[tuple[Path, Path], str]:
    """A certificate made as Lumiverb's AI worker makes its own (src/server/ai_worker/), kept in folder: ((it, its
    key), its fingerprint)."""
    from src.server.ai_worker import certificate_files, ensure_certificate

    fingerprint = ensure_certificate(folder)
    return certificate_files(folder), fingerprint


@contextmanager
def local_machine(answer: tuple[int, Any, dict[str, str]] = (200, WORKS, {}),
                  lists: tuple[str, ...] | None = (MODEL,), tls: tuple[Path, Path] | None = None
                  ) -> Iterator[LocalMachine]:
    machine = LocalMachine(answer, lists, tls)
    try:
        yield machine
    finally:
        machine.close()


@contextmanager
def redirecting(status: int = 307, lists: tuple[str, ...] | None = (MODEL,)) -> Iterator[tuple[LocalMachine,
                                                                                                LocalMachine]]:
    """(the machine, elsewhere): the machine answers its work with a redirect to elsewhere, which would do it."""
    with local_machine() as elsewhere, \
            local_machine((status, b"", {"Location": elsewhere.url + "/somewhere"}), lists) as machine:
        yield machine, elsewhere
