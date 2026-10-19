"""A plug-in for the tester's acceptance tests (ADR-023 phase 3), written
against the protocol alone: Python's standard library, nothing of
Lumiverb's, Python 3.9's grammar.

At start it writes what it was started with to its data folder
(started.json): its whole environment, and the server settings file the
runner names (LUMIVERB_PLUGIN_SETTINGS), with that file's mode. Its health
says what its server setting `health` asks: "ready", "busy" (ready, with
work in the background: detail, done, total), "preparing" or "failed". Its
work gives each clip `seen` (a fact) and `nothing` (always null: its empty
line shows); its place lookup finds one place.
"""

from __future__ import annotations

import json
import os
import socketserver
import stat
import sys
from http.server import BaseHTTPRequestHandler

DATA = os.environ.get("LUMIVERB_PLUGIN_DATA", ".")


def server_settings() -> dict:
    path = os.environ.get("LUMIVERB_PLUGIN_SETTINGS")
    if not path:
        return {"path": None}
    try:
        with open(path) as f:
            values = json.load(f)
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except (OSError, ValueError) as exc:
        return {"path": path, "error": str(exc)}
    return {"path": path, "values": values, "mode": mode}


STARTED = {"environment": dict(os.environ), "server_settings": server_settings(), "pid": os.getpid(),
           "python": ".".join(str(n) for n in sys.version_info[:3]), "executable": sys.executable}

HEALTH = {
    "ready": {"state": "ready"},
    "busy": {"state": "ready", "detail": "Fetching a newer list", "done": 3, "total": 10},
    "preparing": {"state": "preparing", "detail": "Reading", "done": 1, "total": 4},
    "failed": {"state": "failed", "error": "It can't reach its list"},
}


def health() -> dict:
    asked = ((STARTED["server_settings"].get("values") or {}).get("health")) or "ready"
    return HEALTH.get(asked, HEALTH["ready"])


def work(sent: dict) -> dict:
    word = (sent.get("settings") or {}).get("word", "Probed")
    return {"clips": {c["id"]: {"outputs": {"seen": {"text": f"{word} {c['id']}"}, "nothing": None}}
                      for c in sent.get("clips") or []}}


def lookup(sent: dict) -> dict:
    place = {"id": "lab", "name": "Probe Lab", "country": "Testland", "lat": 10.5, "lon": -61.5}
    if "id" in sent:
        return {"places": [place] if sent["id"] == "lab" else []}
    q = str(sent.get("q", "")).lower()
    return {"places": [place] if "probe lab".startswith(q) else []}


class Handler(BaseHTTPRequestHandler):
    def address_string(self) -> str:  # a Unix socket's peer has no host
        return "lumiverb"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — http.server's name
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        self._answer(200, health() if self.path == "/v1/health" else {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v1/work":
            self._answer(200, work(sent))
        elif self.path == "/v1/lookups/places":
            self._answer(200, lookup(sent))
        else:
            self._answer(404, {"error": "not found"})

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


if __name__ == "__main__":
    with open(os.path.join(DATA, "started.json"), "w") as f:
        json.dump(STARTED, f)
    path = os.environ["LUMIVERB_PLUGIN_SOCKET"]
    if os.path.exists(path):
        os.unlink(path)
    print("probe ready", flush=True)
    with Server(path, Handler) as server:
        server.serve_forever()
