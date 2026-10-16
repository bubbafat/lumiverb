"""A scripted scans API for run_scan's unit tests.

The real ApiClient, over an httpx MockTransport that answers the scan's
calls (routers/scans.py) the way each test says and records every request.
What the server decides is tested against the real server
(tests/test_scans_api.py, tests/test_scan_outcomes.py); these fakes let a
test say what the client sends, and what it does with each answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from src.processing.api import ApiClient

SCAN_ID = "scan_1"
_FORM_FIELD = re.compile(rb'name="(\w+)"\r\n\r\n(.*?)\r\n', re.S)


def answer(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body) if body is not None else httpx.Response(status)


def mass_missing(count: int) -> httpx.Response:
    return answer(409, {"error": {"code": "mass_missing", "message": f"{count} clips would be archived as missing.",
                                  "details": {"count": count}}})


def finished(**fields) -> dict:
    return {"archived": 0, "moved": 0, "handed_over": 0, "unchanged": 0, "skipped": 0, "held_back": 0,
            "held_back_reason": None, "dry_run": False, **fields}


@dataclass
class Request:
    method: str
    path: str
    params: dict
    body: dict | None  # JSON, or an upload's form fields


@dataclass
class FakeScans:
    """plan: rel_path -> (action, reason, asset_id) for the files it names;
    every other file found that the server would plan (not settling, not
    empty, of the scan's media type) is planned `default`. gone: the clips
    the plan says weren't found. decide: a hashed file's decision, as the
    server returns it ({"decision", "asset_id", "moved_from"}). finish and
    moves: the answers, in turn (the last one again after that)."""

    plan: dict[str, tuple[str, str | None, str | None]] = field(default_factory=dict)
    default: tuple[str, str | None, str | None] | None = ("upload", "new", None)
    gone: int = 0
    follow_moves: bool = True
    decide: Callable[[str, str], dict] = lambda rel_path, sha: {"decision": "upload"}
    finish: list[httpx.Response] = field(default_factory=list)
    moves: list[httpx.Response] = field(default_factory=list)
    hashes: list[httpx.Response] = field(default_factory=list)  # answers before decide's (a 409 path_taken)
    requests: list[Request] = field(default_factory=list)
    _planned: list[dict] = field(default_factory=list)

    def client(self) -> ApiClient:
        client = ApiClient("http://lumiverb.test", "key")
        client._client = httpx.Client(transport=httpx.MockTransport(self._handle))
        return client

    # What the client sent

    def calls(self) -> list[str]:
        return [f"{r.method} {r.path}" for r in self.requests]

    def sent(self, method: str, path: str) -> list[Request]:
        return [r for r in self.requests if r.method == method and r.path == path]

    def started(self) -> dict:
        [start] = [r for r in self.requests if r.method == "POST" and r.path.endswith("/scans")]
        return start.body

    def found(self) -> list[dict]:
        return [f for r in self.sent("POST", f"/v1/scans/{SCAN_ID}/found") for f in r.body["files"]]

    def unlisted(self) -> list[str]:
        [plan] = self.sent("POST", f"/v1/scans/{SCAN_ID}/plan")
        return plan.body["unlisted"]

    def hashed(self) -> list[list[dict]]:
        """Each /hashes call's files, in order."""
        return [r.body["files"] for r in self.sent("POST", f"/v1/scans/{SCAN_ID}/hashes")]

    def ingested(self) -> list[str]:
        return [r.body["rel_path"] for r in self.sent("POST", "/v1/ingest")]

    def finishes(self) -> list[dict]:
        return [r.body for r in self.sent("POST", f"/v1/scans/{SCAN_ID}/finish")]

    def abandoned(self) -> bool:
        return bool(self.sent("DELETE", f"/v1/scans/{SCAN_ID}"))

    # The server

    def _handle(self, request: httpx.Request) -> httpx.Response:
        content = request.read()
        if request.headers.get("content-type", "").startswith("multipart/"):
            body = {k.decode(): v.decode() for k, v in _FORM_FIELD.findall(content)}
        else:
            body = json.loads(content) if content else None
        method, path = request.method, request.url.path
        self.requests.append(Request(method, path, dict(request.url.params), body))
        scan = f"/v1/scans/{SCAN_ID}"
        if method == "POST" and re.fullmatch(r"/v1/libraries/[^/]+/scans", path):
            return answer(200, {"scan_id": SCAN_ID, "follow_moves": self.follow_moves,
                                "expires_at": "2026-10-14T12:00:00Z"})
        if method == "GET" and (path == "/v1/tenant/filter-defaults"
                                or re.fullmatch(r"/v1/libraries/[^/]+/filters", path)):
            return answer(200, {"includes": [], "excludes": []})
        if method == "POST" and path == f"{scan}/found":
            return answer(200, {"received": len(body["files"])})
        if path == f"{scan}/plan":
            if method == "POST":
                self._planned = self._make_plan(body)
            return self._page(request.url.params, first=method == "POST")
        if method == "POST" and path == f"{scan}/hashes":
            if self.hashes:
                return self.hashes.pop(0)
            return answer(200, {"files": [{"rel_path": f["rel_path"], "asset_id": None, "moved_from": None,
                                           **self.decide(f["rel_path"], f["content_hash"])} for f in body["files"]]})
        if method == "POST" and path == f"{scan}/moves":
            return self._next(self.moves, answer(200, {"moved": 0, "skipped": 0, "upload": []}))
        if method == "POST" and path == f"{scan}/finish":
            return self._next(self.finish, answer(200, finished(archived=self.gone)))
        if method == "DELETE" and path == scan:
            return answer(204)
        if method == "POST" and path == "/v1/ingest":
            return answer(200, {"asset_id": f"ast_{len(self.sent('POST', '/v1/ingest'))}"})
        if method == "POST" and re.fullmatch(r"/v1/assets/[^/]+/artifacts/video_preview", path):
            return answer(200, {})
        if method == "GET" and re.fullmatch(r"/v1/assets/[^/]+/artifacts/proxy", path):
            return answer(404, {"error": {"code": "not_found", "message": "no proxy", "details": {}}})
        return answer(500, {"error": {"code": "unexpected", "message": f"{method} {path}", "details": {}}})

    @staticmethod
    def _next(answers: list[httpx.Response], otherwise: httpx.Response) -> httpx.Response:
        if not answers:
            return otherwise
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def _make_plan(self, body: dict) -> list[dict]:
        media_type = self.started().get("media_type", "all")
        planned = []
        for f in self.found():
            kind = "video" if f["rel_path"].lower().endswith((".mov", ".mp4")) else "image"
            plannable = not f["settling"] and f["file_size"] and media_type in ("all", kind)
            entry = self.plan.get(f["rel_path"], self.default if plannable else None)
            if entry is not None:
                action, reason, asset_id = entry
                planned.append({"rel_path": f["rel_path"], "action": action, "reason": reason, "asset_id": asset_id})
        return sorted(planned, key=lambda p: p["rel_path"])

    def _page(self, params, *, first: bool) -> httpx.Response:
        actions = (params.get("action") or "upload,hash,skipped").split(",")
        limit = int(params.get("limit") or 500)
        after = params.get("after")
        rows = [p for p in self._planned if p["action"] in actions and (after is None or p["rel_path"] > after)]
        page = rows[:limit]
        out = {"files": page, "next_cursor": page[-1]["rel_path"] if len(page) == limit else None}
        if first:
            found = self.found()
            out["counts"] = {
                "found": len(found),
                **{a: sum(p["action"] == a for p in self._planned) for a in ("upload", "hash", "unchanged", "skipped")},
                "settling": sum(f["settling"] for f in found),
                "empty": sum(f["file_size"] == 0 for f in found),
                "gone": self.gone,
            }
        return answer(200, out)
