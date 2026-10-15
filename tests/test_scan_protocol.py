# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The scanner asks the server (ADR-018 decision 12, phase 2).

run_scan says what it found and the server decides (src/server/scans.py):
the scan never lists the library's clips to compare them itself, never
reads what a person removed, never moves or archives clips itself. The
real run_scan against the real API, its requests recorded; and the CLI's
question about moves, which the scheduler never asks (what a scan leaves
is pinned by tests/test_scan_outcomes.py, always as the scheduler scans).
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from rich.console import Console

from src.processing.scan import run_scan
from tests.machine_lineage import ingest_made
from tests.producer_fakes import scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_scan_outcomes import Lib, _image, _in_sight, _missing, _move, _note, _rows

pytestmark = pytest.mark.slow

# How a scan compared before the server did: the library's whole listing,
# the account's settings, and archiving sent clip by clip. (What a person
# removed, moves and new times had endpoints of their own, which answer 410
# now: tests/test_retired_endpoints.py.)
OLD_COMPARISON = {"GET /v1/assets/page", "GET /v1/tenant/settings", "DELETE /v1/assets"}
_ID = re.compile(r"/(ast|scan|lib)_[A-Za-z0-9]+")


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def lib(env, tmp_path: Path):
    client, headers, *_ = env
    root = tmp_path / "library"
    root.mkdir()
    r = client.post("/v1/libraries", json={"name": f"Ask {uuid.uuid4().hex[:10]}", "root_path": str(root)},
                    headers=headers)
    assert r.status_code == 200, r.text
    settings = client.patch("/v1/tenant/settings", json={"follow_moves": True}, headers=headers)
    assert settings.status_code == 200, settings.text
    return Lib(root, r.json())


@pytest.fixture
def api(env):
    """The scheduler's client, and the requests it makes ("METHOD /path", ids as {id})."""
    client = scheduler_client(env[4])
    client.seen = []
    client._client.event_hooks["request"].append(
        lambda req: client.seen.append(f"{req.method} {_ID.sub('/{id}', req.url.path)}"))
    return client


def _scan(api, lib: Lib, **options):
    kwargs = {"allow_moves": True, "allow_mass_delete": False, "concurrency": 1, "console": Console(quiet=True)}
    stats = run_scan(api, lib.library, **{**kwargs, **options})
    assert stats.failed_paths == [], stats.failed_paths
    return stats


def test_a_scan_never_compares_on_its_own(env, api, lib):
    for n in range(3):
        _image(lib, f"inbox/{n}.png", n)
    _image(lib, "keep.png", 9)
    _scan(api, lib)  # new
    first = list(api.seen)
    _move(lib, "inbox/0.png", "sorted/0.png")  # moved
    (lib.root / "inbox/1.png").unlink()  # gone
    os.utime(lib.root / "keep.png", (time.time() - 1800,) * 2)  # touched
    _scan(api, lib)
    _scan(api, lib)  # nothing changed

    assert not OLD_COMPARISON & set(api.seen), sorted(OLD_COMPARISON & set(api.seen))
    assert [s for s in first if "scans" in s] == ["POST /v1/libraries/{id}/scans", "POST /v1/scans/{id}/found",
                                                   "POST /v1/scans/{id}/plan", "POST /v1/scans/{id}/finish"]
    assert api.seen.count("POST /v1/scans/{id}/finish") == 3
    clips = _in_sight(env, lib)
    assert set(clips) == {"sorted/0.png", "inbox/2.png", "keep.png"}
    assert set(_missing(env, lib)) == {"inbox/1.png"}


def test_the_first_of_two_files_with_a_gone_clips_content_takes_it(env, api, lib):
    # In walk order, as the scanner always took them: the other is a clip of its own.
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _note(env, a["asset_id"], "keeper")
    _move(lib, "a.png", "x/a.png")
    (lib.root / "y").mkdir()
    (lib.root / "y/a.png").write_bytes((lib.root / "x/a.png").read_bytes())
    os.utime(lib.root / "y/a.png", (time.time() - 3600,) * 2)

    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert clips["x/a.png"]["asset_id"] == a["asset_id"] and clips["x/a.png"]["note"] == "keeper"
    assert clips["y/a.png"]["asset_id"] != a["asset_id"]
    assert _missing(env, lib) == {}


def _reads(lib: Lib):
    """Patch the scan's hashing to record the files it reads (rel paths), reading them all the same."""
    from src.processing import scan

    read: list[str] = []
    real = scan.compute_sha256

    def sha(path: Path):
        read.append(Path(path).relative_to(lib.root.resolve()).as_posix())
        return real(path)

    return read, patch("src.processing.scan.compute_sha256", side_effect=sha)


def test_only_files_of_a_gone_clips_size_are_read_for_a_move(env, api, lib):
    _image(lib, "inbox/a.png", 1)
    _scan(api, lib)
    (lib.root / "inbox/a.png").unlink()
    _image(lib, "new/same-size.png", 5)  # every _image is the same size
    bigger = lib.root / "new/bigger.png"
    bigger.write_bytes((lib.root / "new/same-size.png").read_bytes() + b"\0" * 64)
    os.utime(bigger, (time.time() - 3600,) * 2)
    read, hashing = _reads(lib)
    with hashing:
        _scan(api, lib)
    assert read == ["new/same-size.png"]
    assert set(_in_sight(env, lib)) == {"new/same-size.png", "new/bigger.png"}
    assert set(_missing(env, lib)) == {"inbox/a.png"}


def test_a_gone_clip_whose_content_isnt_known_is_no_moves_source(env, api, lib):
    # Made without its content (an older client's ingest): nothing to compare a new file with.
    client, headers, *_ = env
    path = _image(lib, "inbox/a.png", 1)
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post("/v1/ingest", headers=headers, files={"proxy": ("p.jpg", buf, "image/jpeg")}, data={
        "library_id": lib.id, "rel_path": "inbox/a.png", "file_size": str(path.stat().st_size),
        "file_mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(), "media_type": "image",
        "width": "16", "height": "16", "exif": json.dumps({}), "lineage": ingest_made()})
    assert r.status_code == 200, r.text
    old = r.json()["asset_id"]
    assert _in_sight(env, lib)["inbox/a.png"]["sha256"] is None
    _move(lib, "inbox/a.png", "sorted/a.png")

    read, hashing = _reads(lib)
    with hashing:
        _scan(api, lib)

    assert read == []  # uploaded as new without being read first
    assert _in_sight(env, lib)["sorted/a.png"]["asset_id"] != old
    assert _missing(env, lib)["inbox/a.png"]["asset_id"] == old


# ---------------------------------------------------------------------------
# The CLI's question about moves (moves "ask")
# ---------------------------------------------------------------------------


def _moved(env, api, lib) -> dict:
    _image(lib, "inbox/a.png", 1)
    _image(lib, "inbox/b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["inbox/a.png"]
    _note(env, a["asset_id"], "keeper")
    _move(lib, "inbox/a.png", "sorted/a.png")
    return a


def _asked(api, lib, answer: str):
    with patch("builtins.input", side_effect=[answer]) as asked:
        stats = _scan(api, lib, allow_moves=False)
    assert asked.call_count == 1
    return stats


def test_moves_asked_about_and_applied(env, api, lib):
    a = _moved(env, api, lib)
    stats = _asked(api, lib, "1")
    clips = _in_sight(env, lib)
    assert set(clips) == {"sorted/a.png", "inbox/b.png"}
    assert clips["sorted/a.png"]["asset_id"] == a["asset_id"] and clips["sorted/a.png"]["note"] == "keeper"
    assert stats.moved == 1 and len(_rows(env, lib)) == 2
    with patch("builtins.input", side_effect=AssertionError("nothing to ask")):
        _scan(api, lib, allow_moves=False)


def test_moves_asked_about_and_skipped_take_no_part(env, api, lib):
    a = _moved(env, api, lib)
    before = _rows(env, lib)
    stats = _asked(api, lib, "2")
    # Neither the new path uploaded nor the old clip archived.
    assert _rows(env, lib) == before and stats.moved == 0
    _scan(api, lib)  # the scheduler's next scan applies it
    assert _in_sight(env, lib)["sorted/a.png"]["asset_id"] == a["asset_id"]


def test_an_aborted_scan_changes_nothing(env, api, lib):
    _moved(env, api, lib)
    (lib.root / "inbox/b.png").unlink()
    _image(lib, "new.png", 7)
    before = _rows(env, lib)
    _asked(api, lib, "3")
    assert _rows(env, lib) == before
