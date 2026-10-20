# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A Postgres that isn't in UTC.

Ubuntu's Postgres takes the machine's timezone; prod's runs in
America/New_York. The tests' sessions default to that too (PGTZ, set in
conftest), so anything that leans on UTC fails here first. A scan's times
are compared on the server now (src/server/scans.py): the real run_scan
against the real API.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from rich.console import Console
from sqlalchemy import create_engine, text

from src.processing.originals import Folder
from src.processing.scan import run_scan
from src.server.database import get_engine_for_url
from tests.machine_lineage import ingest_made
from tests.test_preserve_human_data_slow import (  # noqa: F401 — the shared server fixture
    _image,
    env,
)


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _scanner(env) -> tuple[object, list[str]]:
    """The CLI's client over the app, and the requests it makes ("METHOD /path")."""
    from fastapi.testclient import TestClient

    from src.client.cli.client import LumiverbClient
    from src.server.api.main import app

    _, headers, *_ = env
    api = LumiverbClient(base_url="http://testserver", token="x")
    api._client = TestClient(app, headers=headers)
    seen: list[str] = []
    api._client.event_hooks["request"].append(lambda req: seen.append(f"{req.method} {req.url.path}"))
    return api, seen


def _ingest(env, root: Path, f: dict) -> None:
    """The clip as a scan made it: its content, size and modified time."""
    client, headers, library_id, _ = env
    sha = "sha256:" + hashlib.sha256((root / f["rel_path"]).read_bytes()).hexdigest()
    r = client.post(
        "/v1/ingest", headers=headers, files={"lumiverb.preview": ("p.jpg", io.BytesIO(_image()), "image/jpeg")},
        data={"library_id": library_id, "rel_path": f["rel_path"], "file_size": str(f["file_size"]),
              "file_mtime": f["file_mtime"].isoformat(), "media_type": "image", "lineage": ingest_made(sha),
              "exif": json.dumps({"content_hash": sha})},
    )
    assert r.status_code == 200, r.text


def _scan(env, api, root: Path, folder: str):
    _, _, library_id, _ = env
    return run_scan(api, {"library_id": library_id, "root_path": str(root)}, path_prefix=folder,
                    allow_moves=True, console=Console(quiet=True))


@pytest.mark.slow
def test_the_server_talks_to_postgres_in_utc(env) -> None:
    *_, tenant_url = env
    with create_engine(tenant_url).connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar() == os.environ["PGTZ"] != "UTC"
    with get_engine_for_url(tenant_url).connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar() == "UTC"


@pytest.mark.slow
def test_a_second_scan_doesnt_hash_an_unchanged_file(env, tmp_path: Path) -> None:
    """What the server's full scan (every 15 minutes) does with a file it already has: its
    mtime and size match what the server stored, so it isn't read again."""
    (tmp_path / "tz").mkdir()
    clip = tmp_path / "tz" / "clip.jpg"
    clip.write_bytes(_image())
    os.utime(clip, ns=(1_718_452_800_123_456_789,) * 2)
    [f] = Folder(tmp_path).walk()
    _ingest(env, tmp_path, f)
    api, seen = _scanner(env)

    stats = _scan(env, api, tmp_path, "tz")

    assert stats.unchanged == 1 and (stats.new, stats.changed) == (0, 0)
    assert not any(s.endswith("/hashes") or s == "POST /v1/ingest" for s in seen), seen


@pytest.mark.slow
def test_a_touched_file_is_hashed_once_then_matches_again(env, tmp_path: Path) -> None:
    """Same content, new mtime: the scan hashes it, the server records the
    new mtime, and the scan after that takes the fast path."""
    (tmp_path / "touched").mkdir()
    clip = tmp_path / "touched" / "clip.jpg"
    clip.write_bytes(_image())
    os.utime(clip, ns=(1_700_000_000_000_000_000,) * 2)
    [before] = Folder(tmp_path).walk()
    _ingest(env, tmp_path, before)

    os.utime(clip, ns=(1_720_000_000_987_654_321,) * 2)  # touched
    api, seen = _scanner(env)
    stats = _scan(env, api, tmp_path, "touched")
    assert stats.unchanged == 1 and (stats.new, stats.changed) == (0, 0)
    assert [s for s in seen if s.endswith("/hashes")] and "POST /v1/ingest" not in seen

    seen.clear()
    stats = _scan(env, api, tmp_path, "touched")
    assert stats.unchanged == 1
    assert not any(s.endswith("/hashes") or s == "POST /v1/ingest" for s in seen), seen
