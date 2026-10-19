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
  POST /v1/work            {"settings", "clips": [{"id", <each input it
                           declares>}]} -> {"clips": {id: {"outputs": {key:
                           {"text", "url"?} or null}}}}, or for a clip
                           {"error": {"whose": "clip" or "plugin", "message"}}

Its work gives each clip three facts, from the inputs its manifest declares
(when it was taken, its metadata): the caption chosen in its settings, when
it was taken, and its shape.

Its output goes to Lumiverb's log, a line at a time, named "plugin example".

Install it on the machine:
  lumiverb-server plugins install <this folder> --off
"""

from __future__ import annotations  # it runs on whatever python3 the machine has (a Mac's is 3.9)

import json
import os
import socketserver
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

PARTS = ((5, "night"), (12, "morning"), (17, "afternoon"), (21, "evening"), (24, "night"))


def taken(when: dict) -> str:
    """"On a Sunday afternoon in May 2024", in the clip's own time when its offset is known."""
    at = datetime.fromisoformat(when["utc"].replace("Z", "+00:00"))
    if when.get("offset_min") is not None:
        at = at.astimezone(timezone(timedelta(minutes=when["offset_min"])))
    part = next(name for hour, name in PARTS if at.hour < hour)
    return f"On a {at:%A} {part} in {at:%B %Y}"


def shape(meta: dict) -> str | None:
    width, height = meta.get("width"), meta.get("height")
    if not width or not height:
        return None
    kind = "Square" if width == height else "Landscape" if width > height else "Portrait"
    if meta.get("media_type") == "video":
        seconds = round(meta.get("duration_sec") or 0)
        return f"{kind} video, {width} × {height}, {seconds // 60}:{seconds % 60:02d}"
    return f"{kind}, {width} × {height}"


def work(settings: dict, clip: dict) -> dict:
    caption = settings.get("caption") or "Seen by the example"
    if settings.get("loud"):
        caption = caption.upper()
    if settings.get("tag"):
        caption += f" #{settings['tag']}"
    made = shape(clip.get("metadata") or {})
    return {"outputs": {"caption": {"text": caption},
                        "taken": {"text": taken(clip["capture_time"])} if clip.get("capture_time") else None,
                        "shape": {"text": made} if made else None}}


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
        sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v1/work":
            settings, clips = sent.get("settings") or {}, sent.get("clips") or []
            self._answer(200, {"clips": {c["id"]: work(settings, c) for c in clips}})
            if settings.get("verbose"):
                print(f"made facts for {len(clips)} clip(s)", flush=True)
            return
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
