"""Lumiverb's example plug-in (ADR-023): how one is written, against the
plug-in protocol alone. Python's standard library, nothing of Lumiverb's:
any language that can answer HTTP on a Unix socket would do.

Lumiverb starts it from its installed folder with a clean environment:
LUMIVERB_PLUGIN_SOCKET (where to listen), LUMIVERB_PLUGIN_DATA (its own
folder, the only one it may write), LUMIVERB_PLUGIN_ID and _PROTOCOL. It
answers:

  GET  /v1/health          {"state": "ready"} (or "preparing" with done,
                           total and detail, or "failed" with an error)
  POST /v1/settings/check  {"scope", "settings"} -> {"errors": {key: why}}:
                           what only it can know (Lumiverb has checked each
                           value's type, bounds and form already)

Its output goes to Lumiverb's log, a line at a time, named "plugin example".

Install it on the machine:
  lumiverb-server plugins install <this folder> --off
"""

import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def address_string(self) -> str:  # a Unix socket's peer has no host
        return "lumiverb"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — http.server's name
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        if self.path == "/v1/health":
            self._answer(200, {"state": "ready"})
        else:
            self._answer(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        sent = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/v1/settings/check":
            self._answer(404, {"error": "not found"})
            return
        settings = sent.get("settings") or {}
        errors = {}
        if str(settings.get("caption", "")).strip().lower() == "nothing":
            errors["caption"] = "The example won't say nothing: write something"
        print(f"checked {sent.get('scope')} settings: {'refused' if errors else 'fine'}", flush=True)
        self._answer(200, {"errors": errors})

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
    path = os.environ["LUMIVERB_PLUGIN_SOCKET"]
    if os.path.exists(path):
        os.unlink(path)
    print("the example is ready", flush=True)
    with Server(path, Handler) as server:
        server.serve_forever()
