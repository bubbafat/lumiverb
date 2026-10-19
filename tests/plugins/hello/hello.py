"""A plug-in for Lumiverb's tests (ADR-023), written against the protocol
alone: Python's standard library, nothing of Lumiverb's. It listens on the
Unix socket Lumiverb names (LUMIVERB_PLUGIN_SOCKET) and answers its health.

What it does is its command's argument, so the runner's tests can try a
plug-in going wrong in each way it might (tests/test_plugin_runner.py):

  (none)      ready
  preparing   preparing, 3 of 10
  failing     failed, with why
  crash       says why and exits 3 at once
  hang        listens but never answers
  goes-quiet  answers its health once, then never again
  garbage     answers its health with what the protocol doesn't say
  memory      takes 300 MB first
  big-file    tries to write a 20 MB file (more than its disk_mb lets one file be), says what happened
  fill        writes 9 MB files into its data folder, one every half second
  env         writes the environment it was given to its data folder (env.json)

Whatever it does, it checks new settings (POST /v1/settings/check): a
greeting of "rude" isn't one it takes.
"""

import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
ANSWERED = threading.Event()
DATA = os.environ.get("LUMIVERB_PLUGIN_DATA", ".")


class Handler(BaseHTTPRequestHandler):
    def address_string(self) -> str:  # a Unix socket's peer has no host
        return "lumiverb"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — http.server's name
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        if self.path != "/v1/health":
            self._answer(404, {"error": "not found"})
        elif MODE == "hang" or (MODE == "goes-quiet" and ANSWERED.is_set()):
            time.sleep(3600)
        elif MODE == "goes-quiet":
            ANSWERED.set()
            self._answer(200, {"state": "ready"})
        elif MODE == "garbage":
            self._answer(200, {"state": "dancing"})
        elif MODE == "preparing":
            self._answer(200, {"state": "preparing", "done": 3, "total": 10, "detail": "Reading the list"})
        elif MODE == "failing":
            self._answer(200, {"state": "failed", "error": "Its list is broken"})
        else:
            self._answer(200, {"state": "ready"})

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/v1/settings/check":
            self._answer(404, {"error": "not found"})
        elif sent.get("settings", {}).get("greeting") == "rude":
            self._answer(200, {"errors": {"greeting": "Not a nice greeting"}})
        else:
            self._answer(200, {"errors": {}})

    def _answer(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("lumiverb", 0)


def _fill() -> None:
    n = 0
    while True:
        n += 1
        with open(os.path.join(DATA, f"part-{n}"), "wb") as f:
            f.write(b"x" * (9 * 1024 * 1024))
        time.sleep(0.5)


def main() -> int:
    if MODE == "crash":
        print("crashing on purpose", flush=True)
        return 3
    if MODE == "env":
        with open(os.path.join(DATA, "env.json"), "w") as f:
            json.dump(dict(os.environ), f)
    if MODE == "memory":
        held = bytearray(300 * 1024 * 1024)
        for i in range(0, len(held), 4096):
            held[i] = 1
        globals()["HELD"] = held
    if MODE == "big-file":
        try:
            with open(os.path.join(DATA, "big"), "wb") as f:
                for _ in range(20):
                    f.write(b"x" * (1024 * 1024))
            print("wrote 20 MB", flush=True)
        except OSError as exc:
            print(f"couldn't write it: {exc.strerror}", flush=True)
    if MODE == "fill":
        threading.Thread(target=_fill, daemon=True).start()
    path = os.environ["LUMIVERB_PLUGIN_SOCKET"]
    if os.path.exists(path):
        os.unlink(path)
    print(f"hello {MODE or 'ready'}", flush=True)
    with Server(path, Handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
