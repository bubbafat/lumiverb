# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A Postgres that isn't in UTC.

Ubuntu's Postgres takes the machine's timezone; the brain's runs in
America/New_York. The tests' sessions default to that too (PGTZ, set in
conftest), so anything that leans on UTC fails here first.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.client.cli.ingest import _walk_library
from src.client.cli.scan import _fetch_existing_assets_with_sha, _record_file_stat, _split_files
from src.server.database import get_engine_for_url
from tests.test_preserve_human_data_slow import (  # noqa: F401 — the shared server fixture
    _image,
    env,
)


class _Api:
    """What the scan needs from LumiverbClient, over the test client."""

    def __init__(self, client, headers) -> None:
        self._client, self._headers = client, headers

    def get(self, path: str, **kwargs):
        r = self._client.get(path, headers=self._headers, **kwargs)
        assert r.status_code == 200, r.text
        return r

    def post(self, path: str, **kwargs):
        r = self._client.post(path, headers=self._headers, **kwargs)
        assert r.status_code == 200, r.text
        return r


@pytest.mark.slow
def test_the_server_talks_to_postgres_in_utc(env) -> None:
    *_, tenant_url = env
    with create_engine(tenant_url).connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar() == os.environ["PGTZ"] != "UTC"
    with get_engine_for_url(tenant_url).connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar() == "UTC"


@pytest.mark.slow
def test_a_second_scan_doesnt_hash_an_unchanged_file(env, tmp_path: Path) -> None:
    """What the brain's daily full scan does with a file it already has: its
    mtime and size match what the server stored, so it isn't read again."""
    client, headers, library_id, _ = env
    (tmp_path / "tz").mkdir()
    clip = tmp_path / "tz" / "clip.jpg"
    clip.write_bytes(_image())
    os.utime(clip, ns=(1_718_452_800_123_456_789,) * 2)
    local = _walk_library(tmp_path)
    [f] = local

    r = client.post(
        "/v1/ingest",
        headers=headers,
        files={"proxy": ("p.jpg", io.BytesIO(_image()), "image/jpeg")},
        data={"library_id": library_id, "rel_path": f["rel_path"], "file_size": str(f["file_size"]),
              "file_mtime": f["file_mtime"].isoformat(), "media_type": "image"},
    )
    assert r.status_code == 200, r.text

    existing = _fetch_existing_assets_with_sha(_Api(client, headers), library_id)
    new, needs_hash, fast_unchanged = _split_files(local, existing)
    assert not new
    assert not needs_hash
    assert [x["rel_path"] for x in fast_unchanged] == ["tz/clip.jpg"]


@pytest.mark.slow
def test_a_touched_file_is_hashed_once_then_matches_again(env, tmp_path: Path) -> None:
    """Same content, new mtime: the scan hashes it, records the new mtime,
    and the scan after that takes the fast path."""
    client, headers, library_id, _ = env
    (tmp_path / "touched").mkdir()
    clip = tmp_path / "touched" / "clip.jpg"
    clip.write_bytes(_image())
    os.utime(clip, ns=(1_700_000_000_000_000_000,) * 2)
    [before] = _walk_library(tmp_path)
    r = client.post(
        "/v1/ingest", headers=headers, files={"proxy": ("p.jpg", io.BytesIO(_image()), "image/jpeg")},
        data={"library_id": library_id, "rel_path": before["rel_path"], "file_size": str(before["file_size"]),
              "file_mtime": before["file_mtime"].isoformat(), "media_type": "image"},
    )
    assert r.status_code == 200, r.text

    os.utime(clip, ns=(1_720_000_000_987_654_321,) * 2)  # touched
    local = _walk_library(tmp_path)
    api = _Api(client, headers)
    _, needs_hash, _ = _split_files(local, _fetch_existing_assets_with_sha(api, library_id))
    assert [f["rel_path"] for f in needs_hash] == ["touched/clip.jpg"]
    _record_file_stat(api, library_id, local[0])

    _, needs_hash, fast_unchanged = _split_files(local, _fetch_existing_assets_with_sha(api, library_id))
    assert not needs_hash
    assert [f["rel_path"] for f in fast_unchanged] == ["touched/clip.jpg"]
