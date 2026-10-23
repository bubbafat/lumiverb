"""A plug-in for Lumiverb's tests (ADR-023 phase 2), written against the
protocol alone: Python's standard library, nothing of Lumiverb's. Its work
(POST /v1/work) gives each clip a fact or two from what it's handed, its
place lookup (POST /v1/lookups/places) finds a few places by name or id,
and its "behave" setting says how it answers, so the tests can try each way
a plug-in's work or lookup can go wrong:

  well        where (with a link), when, what; nothing to say where an input is missing
  clip-error  each clip's error, the clip's
  own-error   each clip's error, its own
  nonsense    an output it doesn't declare
  html        a fact with a tag in it
  leave-out   says nothing of the first clip
  slow        takes a minute (longer than the test lets it)
  quiet       well, but nothing to say of where (its manifest's empty line shows)

It writes the last request it was handed to its data folder (last-work.json,
last-lookup.json).
"""

import json
import os
import socketserver
import time
from http.server import BaseHTTPRequestHandler

DATA = os.environ.get("LUMIVERB_PLUGIN_DATA", ".")


def facts(clip: dict, word: str) -> dict:
    where = clip.get("location")
    when = clip.get("capture_time")
    what = clip.get("metadata") or {}
    return {
        "where": {"text": f"{word} at {where['lat']:.3f}, {where['lon']:.3f} ({where['source']})",
                  "url": f"https://www.openstreetmap.org/?mlat={where['lat']}&mlon={where['lon']}"} if where else None,
        "when": {"text": f"{word} {when['utc']}"} if when else None,
        "what": {"text": f"{word}: {what.get('media_type')} {what.get('width')}x{what.get('height')}"}
        if what.get("media_type") else None,
    }


PLACES = [
    {"id": "harbour", "name": "Harbour Point", "region": "Testshire", "country": "Testland", "lat": 41.1,
     "lon": -73.2, "kind": "lighthouse"},
    {"id": "hill", "name": "Hill Top", "country": "Testland", "lat": 41.3, "lon": -73.4, "population": 120},
]


def lookup(sent: dict) -> dict:
    behave = (sent.get("settings") or {}).get("behave", "well")
    if behave == "slow":
        time.sleep(60)
    if behave == "nonsense":
        return {"places": [{"id": "x", "name": "Nowhere", "lat": 999, "lon": 0}]}
    if "id" in sent:
        return {"places": [p for p in PLACES if p["id"] == sent["id"]]}
    q = str(sent.get("q", "")).lower()
    return {"places": [p for p in PLACES if p["name"].lower().startswith(q)][: int(sent.get("limit", 10))]}


def work(sent: dict) -> dict:
    settings, clips = sent.get("settings") or {}, sent.get("clips") or []
    behave, word = settings.get("behave", "well"), settings.get("word", "Seen")
    if behave == "slow":
        time.sleep(60)
    out = {}
    for n, clip in enumerate(clips):
        if behave == "leave-out" and n == 0:
            continue
        if behave == "clip-error":
            out[clip["id"]] = {"error": {"whose": "clip", "message": "can't make sense of this one"}}
        elif behave == "own-error":
            out[clip["id"]] = {"error": {"whose": "plugin", "message": "its list isn't loaded"}}
        elif behave == "nonsense":
            out[clip["id"]] = {"outputs": {"elsewhere": {"text": "not one it declares"}}}
        elif behave == "html":
            out[clip["id"]] = {"outputs": {"when": {"text": "<b>bold</b>"}}}
        elif behave == "quiet":
            out[clip["id"]] = {"outputs": {**facts(clip, word), "where": None}}
        else:
            out[clip["id"]] = {"outputs": facts(clip, word)}
    return {"clips": out}


def write_whole(path: str, text: str) -> None:
    """The file whole or not yet there: written beside it, then renamed in (a test reads it while it's written)."""
    part = path + ".part"
    with open(part, "w") as f:
        f.write(text)
    os.replace(part, path)


class Handler(BaseHTTPRequestHandler):
    def address_string(self) -> str:  # a Unix socket's peer has no host
        return "lumiverb"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — http.server's name
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        self._answer(200, {"state": "ready"} if self.path == "/v1/health" else {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v1/lookups/places":
            write_whole(os.path.join(DATA, "last-lookup.json"), json.dumps(sent))
            self._answer(200, lookup(sent))
            return
        if self.path != "/v1/work":
            self._answer(404, {"error": "not found"})
            return
        write_whole(os.path.join(DATA, "last-work.json"), json.dumps(sent))
        self._answer(200, work(sent))

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
    print("facts ready", flush=True)
    with Server(path, Handler) as server:
        server.serve_forever()
