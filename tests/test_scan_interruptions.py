# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A scan survives a dropped connection and the API restarting (review, Oct 13).

The real run_scan, as the scheduler calls it, over a real folder against
the real API, with the first try of each of the scan's calls interrupted
on its way: its answer lost after the API did it (a connection reset), or
the call never arriving (refused, or nginx's 503 while the API restarts).
Before, any of them failed the whole scan. Now each is tried again, and
the API answers a repeat as it answered the first: the server ends up as a
scan with nothing interrupted leaves it, in a twin library.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from src.processing import api as api_module
from src.processing.scan import run_scan
from tests.producer_fakes import scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_scan_outcomes import HOUR, Lib, _ago, _copy, _image, _move, _rows, _set_mtime

pytestmark = pytest.mark.slow

SCAN_CALL = re.compile(r"^/v1/scans/[^/]+/(found|plan|hashes|moves|finish)$")
CALLS = ("found", "plan", "hashes", "finish")


class Interrupted:
    """The app in-process, with the first try of each named scan call
    interrupted: "lost", done by the API and its answer lost on the way back;
    "refused", never arriving; "restarting", answered 503 by nginx."""

    def __init__(self, app, how: dict[str, str]) -> None:
        self._app = app
        self._how = dict(how)
        self.interrupted: list[str] = []

    def request(self, method: str, url, **kwargs):
        m = SCAN_CALL.match(httpx.URL(str(url)).path)
        how = self._how.pop(m.group(1), None) if m and method == "POST" else None
        if how is not None:
            self.interrupted.append(f"{m.group(1)} {how}")
        if how == "refused":
            raise httpx.ConnectError("[Errno 61] Connection refused")
        if how == "restarting":
            return httpx.Response(503, text="<html>503 Service Temporarily Unavailable</html>")
        response = self._app.request(method, url, **kwargs)
        if how == "lost":
            raise httpx.ReadError("[Errno 54] Connection reset by peer")
        return response

    def __getattr__(self, name: str):
        return getattr(self._app, name)


@pytest.fixture(scope="module")
def api(env):
    return scheduler_client(env[4])


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan's proxy cache in the test's own folder; tries again without waiting."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(api_module, "_sleep", lambda seconds: None)


def _library(env, root: Path) -> Lib:
    client, headers, *_ = env
    root.mkdir()
    r = client.post("/v1/libraries", json={"name": f"Interrupted {uuid.uuid4().hex[:10]}", "root_path": str(root)},
                    headers=headers)
    assert r.status_code == 200, r.text
    return Lib(root, r.json())


def _scan(api, lib: Lib):
    stats = run_scan(api, lib.library, allow_moves=True, allow_mass_delete=False, concurrency=1,
                     console=Console(quiet=True))
    assert stats.failed_paths == [], stats.failed_paths
    return stats


def _before(env, api, lib: Lib) -> dict[str, str]:
    """The library as two scans left it, then changed on disk: a.png touched
    (its time, not its content), b.png moved, c.png deleted, d.png new, and
    Ingest/e.png deleted after it was copied to Projects (copy, then delete).
    Returns each clip's first path, by id."""
    for rel, n in (("a.png", 1), ("b.png", 2), ("c.png", 3), ("Ingest/e.png", 5)):
        _image(lib, rel, n)
    _scan(api, lib)
    _copy(lib, "Ingest/e.png", "Projects/e.png")
    _scan(api, lib)  # the copy: a clip of its own
    first = {r["asset_id"]: r["rel_path"] for r in _rows(env, lib)}
    _set_mtime(lib.root / "a.png", _ago(2 * HOUR))
    _move(lib, "b.png", "moved/b.png")
    os.remove(lib.root / "c.png")
    _image(lib, "d.png", 4)
    os.remove(lib.root / "Ingest/e.png")
    return first


def _outcome(env, lib: Lib, first: dict[str, str]) -> list[tuple]:
    """Each clip: where it is, where it was first (None: made by this scan), and whether it's in sight."""
    return sorted((r["rel_path"], first.get(r["asset_id"]), r["deleted_reason"]) for r in _rows(env, lib))


@pytest.mark.parametrize("how", ["lost", "refused", "restarting"])
def test_a_scan_interrupted_at_every_call_ends_as_one_that_wasnt(env, api, tmp_path, how):
    clean, interrupted = _library(env, tmp_path / "clean"), _library(env, tmp_path / "interrupted")
    first_clean, first = _before(env, api, clean), _before(env, api, interrupted)
    expected = _scan(api, clean)

    app = api._client
    api._client = Interrupted(app, dict.fromkeys(CALLS, how))
    try:
        stats = _scan(api, interrupted)
    finally:
        interrupted_calls, api._client = api._client.interrupted, app
    assert interrupted_calls == [f"{call} {how}" for call in CALLS]  # each was interrupted, once
    assert _outcome(env, interrupted, first) == _outcome(env, clean, first_clean) == [
        ("Projects/e.png", "Ingest/e.png", None),  # handed over to its copy, which is gone
        ("a.png", "a.png", None),
        ("c.png", "c.png", "missing"),
        ("d.png", None, None),
        ("moved/b.png", "b.png", None),
    ]
    assert (stats.new, stats.unchanged, stats.moved, stats.deleted) == (
        expected.new, expected.unchanged, expected.moved, expected.deleted) == (1, 2, 2, 1)
