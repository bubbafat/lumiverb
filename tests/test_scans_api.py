"""The server compares what a scan found (ADR-018 decision 12, phase 2).

Each test drives the scan calls the way a client would, over real server
state: start, send what's on disk, plan, hash what the plan asks for, answer
moves, finish, then upload what was decided (POST /v1/ingest, after the
finish, as today's scanner archives before it ingests). The outcomes are
today's scanner's (src/processing/scan.py), rule by rule, except where the
module's docstring says otherwise.

Real Postgres (testcontainers), two accounts, a temp LocalStorage.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_engine_for_url
from src.server.models.tenant import Asset
from src.server.storage.local import LocalStorage
from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made, made_json
from tests.stored_in import stored_in
from tests.videos import playable

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def _tenant(client: TestClient, name: str) -> tuple[str, str]:
    r = client.post("/v1/admin/tenants", json={"name": name, "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-scans"})
    assert r.status_code == 200, r.text
    return r.json()["tenant_id"], r.json()["api_key"]


def _route(tenant_id: str, url: str) -> None:
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantDbRoutingRepository

    with get_control_session() as session:
        row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
        row.connection_string = url
        session.add(row)
        session.commit()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Two accounts, each with its own database: the first's admin, editor
    and viewer keys, and the second's admin key."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("scans_storage")))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-scans"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.accounts.provision_tenant_database"), TestClient(app) as client:
            tenant_id, api_key = _tenant(client, "ScansTenant")
            other_id, other_key = _tenant(client, "OtherScansTenant")

        with PostgresContainer(PG_IMAGE) as tenant_pg, PostgresContainer(PG_IMAGE) as other_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            other_url = _ensure_psycopg2(other_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            _provision_tenant_db(other_url, project_root)
            _route(tenant_id, tenant_url)
            _route(other_id, other_url)

            with ExitStack() as stack:
                stack.enter_context(stored_in(storage))
                with TestClient(app) as client:
                    admin = {"Authorization": f"Bearer {api_key}"}

                    def key(role: str) -> dict:
                        r = client.post("/v1/keys", json={"label": role, "role": role}, headers=admin)
                        assert r.status_code == 200, r.text
                        return {"Authorization": f"Bearer {r.json()['plaintext']}"}

                    db = create_engine(tenant_url)
                    yield SimpleNamespace(client=client, admin=admin, editor=key("editor"), viewer=key("viewer"),
                                          other={"Authorization": f"Bearer {other_key}"}, tenant_url=tenant_url,
                                          db=db)
                    db.dispose()
        _engines.clear()


@pytest.fixture(autouse=True)
def _moves_followed(env):
    yield
    r = env.client.patch("/v1/tenant/settings", json={"follow_moves": True}, headers=env.admin)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# A client: files on disk, ingest, and the scan's calls
# ---------------------------------------------------------------------------


@dataclass
class File:
    """A file on disk: its path, content (its hash and, unless given, its size) and modified time."""

    rel_path: str
    content: str
    mtime: datetime = T0
    size: int | None = None

    @property
    def sha(self) -> str:
        return "sha256:" + hashlib.sha256(self.content.encode()).hexdigest()

    @property
    def file_size(self) -> int:
        return self.size if self.size is not None else 1000 + int(self.sha.removeprefix("sha256:")[:5], 16)

    def at(self, rel_path: str) -> File:
        return File(rel_path, self.content, self.mtime, self.size)

    def touched(self) -> File:
        return File(self.rel_path, self.content, T1, self.size)


def _library(env) -> str:
    name = f"scan-{uuid.uuid4().hex[:8]}"
    r = env.client.post("/v1/libraries", json={"name": name, "root_path": f"/Volumes/{name}"}, headers=env.admin)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


def _ingest(env, library_id: str, f: File, *, preview: bool = True, headers: dict | None = None) -> str:
    """Upload a file as the scanner does (a video with its 10-second preview), as an editor unless headers say."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    video = f.rel_path.endswith(".mov")
    r = env.client.post("/v1/ingest", headers=headers or env.editor, files={"lumiverb.preview": ("p.jpg", buf, "image/jpeg")}, data={
        "library_id": library_id, "rel_path": f.rel_path, "file_size": str(f.file_size),
        "file_mtime": f.mtime.isoformat(), "media_type": "video" if video else "image", "width": "64",
        "height": "36", "exif": json.dumps({"content_hash": f.sha}), "lineage": ingest_made(f.sha)})
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]
    if video and preview:
        r = env.client.post(f"/v1/assets/{asset_id}/artifacts/lumiverb.video.preview", headers=headers or env.editor,
                            files={"file": ("p.mp4", io.BytesIO(playable()), "video/mp4")},
                            data={"lineage": made_json("lumiverb.video.preview", f.sha)})
        assert r.status_code == 200, r.text
    return asset_id


def _clips(env, library_id: str) -> dict[str, str]:
    """The library's clips in sight: {rel_path: asset_id}."""
    with env.db.connect() as conn:
        return dict(conn.execute(text("SELECT rel_path, asset_id FROM assets"
                                      " WHERE library_id = :l AND deleted_at IS NULL"), {"l": library_id}).all())


def _clip(env, asset_id: str) -> dict:
    with env.db.connect() as conn:
        return dict(conn.execute(text("SELECT rel_path, deleted_reason, file_size, file_mtime FROM assets"
                                      " WHERE asset_id = :a"), {"a": asset_id}).mappings().one())


def _settings(env, **body) -> None:
    r = env.client.patch("/v1/tenant/settings", json=body, headers=env.admin)
    assert r.status_code == 200, r.text


def _ok(r, status: int = 200) -> dict:
    assert r.status_code == status, r.text
    return r.json() if r.content else {}


def _error(r, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code, err
    return err


def _start(env, library_id: str, headers: dict | None = None, **opts) -> str:
    return _ok(env.client.post(f"/v1/libraries/{library_id}/scans", json=opts,
                               headers=headers or env.editor))["scan_id"]


def _listing(disk: list[File], settling=(), empty=()) -> list[dict]:
    return [{"rel_path": f.rel_path, "file_size": 0 if f.rel_path in empty else f.file_size,
             "file_mtime": f.mtime.isoformat(), "settling": f.rel_path in settling} for f in disk]


@dataclass
class Run:
    scan_id: str
    counts: dict = field(default_factory=dict)
    plan: dict[str, dict] = field(default_factory=dict)  # rel_path -> planned file
    decisions: dict[str, dict] = field(default_factory=dict)
    moves: dict | None = None
    finish: dict | None = None
    finish_error: object = None  # the response to a finish the server refused
    uploaded: dict[str, str] = field(default_factory=dict)  # rel_path -> asset_id


def _scan(env, library_id: str, disk: list[File], *, settling=(), empty=(), unlisted=(), answer: bool | None = None,
          allow_mass_missing: bool = False, page: int = 500, finish: bool = True, headers: dict | None = None,
          **opts) -> Run:
    """A whole scan as a client makes it, as an editor unless headers say.
    Stops at a finish the server refuses (run.finish is None), uploading
    nothing; before the finish when finish is False (the scan's listing
    still there)."""
    c, h = env.client, headers or env.editor
    run = Run(_start(env, library_id, headers=h, **opts))
    listing = _listing(disk, settling, empty)
    for i in range(0, len(listing), 1000):
        _ok(c.post(f"/v1/scans/{run.scan_id}/found", json={"files": listing[i:i + 1000]}, headers=h))
    first = _ok(c.post(f"/v1/scans/{run.scan_id}/plan", params={"limit": page},
                       json={"unlisted": list(unlisted)}, headers=h))
    run.counts = first["counts"]
    files, cursor = first["files"], first["next_cursor"]
    while cursor:
        more = _ok(c.get(f"/v1/scans/{run.scan_id}/plan", params={"after": cursor, "limit": page}, headers=h))
        files, cursor = files + more["files"], more["next_cursor"]
    run.plan = {f["rel_path"]: f for f in files}
    on_disk = {f.rel_path: f for f in disk}
    to_hash = [p for p, f in run.plan.items() if f["action"] == "hash"]
    for i in range(0, len(to_hash), 1000):
        sent = [{"rel_path": p, "content_hash": on_disk[p].sha} for p in to_hash[i:i + 1000]]
        for d in _ok(c.post(f"/v1/scans/{run.scan_id}/hashes", json={"files": sent}, headers=h))["files"]:
            run.decisions[d["rel_path"]] = d
    if answer is not None and any(d["decision"] == "move_candidate" for d in run.decisions.values()):
        run.moves = _ok(c.post(f"/v1/scans/{run.scan_id}/moves", json={"apply": answer}, headers=h))
    if not finish:
        return run
    r = c.post(f"/v1/scans/{run.scan_id}/finish", json={"allow_mass_missing": allow_mass_missing}, headers=h)
    if r.status_code != 200:
        run.finish_error = r
        return run
    run.finish = r.json()
    if not opts.get("dry_run"):
        uploads = [p for p, f in run.plan.items() if f["action"] == "upload"]
        uploads += [p for p, d in run.decisions.items() if d["decision"] == "upload"]
        uploads += (run.moves or {}).get("upload", [])
        for p in uploads:
            run.uploaded[p] = _ingest(env, library_id, on_disk[p], headers=h)
    return run


def _planned(run: Run) -> dict[str, tuple[str, str | None]]:
    return {p: (f["action"], f["reason"]) for p, f in run.plan.items()}


def _decided(run: Run) -> dict[str, str]:
    return {p: d["decision"] for p, d in run.decisions.items()}


# ---------------------------------------------------------------------------
# New, unchanged, touched, changed
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_new_file_is_uploaded_without_hashing_first(env):
    lib = _library(env)
    run = _scan(env, lib, [File("a.jpg", "a")])
    assert _planned(run) == {"a.jpg": ("upload", "new")}
    assert run.counts == {"found": 1, "upload": 1, "hash": 0, "unchanged": 0, "skipped": 0, "settling": 0,
                          "empty": 0, "gone": 0}
    assert set(_clips(env, lib)) == {"a.jpg"}


@pytest.mark.slow
def test_a_file_whose_size_and_time_match_is_unchanged_without_hashing(env):
    lib = _library(env)
    a = File("a.jpg", "a")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a])
    assert run.plan == {} and run.counts["unchanged"] == 1 and run.counts["hash"] == 0
    assert run.finish["unchanged"] == 1 and run.finish["archived"] == 0
    assert _clips(env, lib) == {"a.jpg": asset}


@pytest.mark.slow
def test_the_unchanged_files_are_listed_with_their_clips_on_request(env):
    # A client keeping a cache of previews by clip (today's scanner fills its cache for unchanged files).
    lib = _library(env)
    a = File("a.jpg", "a")
    asset = _ingest(env, lib, a)
    scan_id = _start(env, lib)
    _ok(env.client.post(f"/v1/scans/{scan_id}/found", json={"files": _listing([a])}, headers=env.editor))
    first = _ok(env.client.post(f"/v1/scans/{scan_id}/plan", json={}, headers=env.editor))
    assert first["files"] == []
    page = _ok(env.client.get(f"/v1/scans/{scan_id}/plan", params={"action": "unchanged"}, headers=env.editor))
    assert page["files"] == [{"rel_path": "a.jpg", "action": "unchanged", "reason": None, "asset_id": asset,
                              "legacy": False}]
    _error(env.client.get(f"/v1/scans/{scan_id}/plan", params={"action": "gone"}, headers=env.editor),
           422, "invalid_request")


@pytest.mark.slow
def test_a_touched_file_is_unchanged_and_its_new_time_is_recorded(env):
    lib = _library(env)
    a = File("a.jpg", "a")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.touched()])
    assert _planned(run) == {"a.jpg": ("hash", "changed")}
    assert run.decisions["a.jpg"] == {"rel_path": "a.jpg", "decision": "unchanged", "asset_id": asset,
                                      "moved_from": None}
    assert run.uploaded == {}
    assert _clip(env, asset)["file_mtime"] == T1
    # So the next scan doesn't read it again.
    assert _scan(env, lib, [a.touched()]).counts["unchanged"] == 1


@pytest.mark.slow
def test_changed_content_is_uploaded_and_becomes_a_new_clip(env):
    lib = _library(env)
    old = _ingest(env, lib, File("a.jpg", "before"))
    run = _scan(env, lib, [File("a.jpg", "after", mtime=T1)])
    assert _planned(run) == {"a.jpg": ("hash", "changed")}
    assert run.decisions["a.jpg"]["decision"] == "upload" and run.decisions["a.jpg"]["asset_id"] == old
    new = _clips(env, lib)["a.jpg"]
    assert new != old and _clip(env, old)["deleted_reason"] == "missing"  # ingest's rule: a new file is a new clip


@pytest.mark.slow
def test_a_video_whose_preview_never_arrived_is_uploaded_again(env):
    lib = _library(env)
    without, with_ = File("v1.mov", "v1"), File("v2.mov", "v2")
    asset = _ingest(env, lib, without, preview=False)
    _ingest(env, lib, with_)
    run = _scan(env, lib, [without, with_])
    assert run.plan == {"v1.mov": {"rel_path": "v1.mov", "action": "upload", "reason": "preview_missing",
                                   "asset_id": asset, "legacy": False}}
    assert run.counts["unchanged"] == 1
    assert run.uploaded == {"v1.mov": asset}


def _lose_preview(env, asset_id: str) -> None:
    """The clip's preview file goes; a GET finds it gone and upkeep's
    missing-file repair clears its key (src/server/missing_artifacts.py)."""
    from src.server import missing_artifacts
    from src.server.storage.store import get_store

    with env.db.connect() as conn:
        key = conn.execute(text("SELECT proxy_key FROM assets WHERE asset_id = :a"), {"a": asset_id}).scalar()
    get_store().delete(key)
    _error(env.client.get(f"/v1/assets/{asset_id}/artifacts/lumiverb.preview", headers=env.editor), 404, "artifact_missing")
    with Session(env.db) as session:
        assert missing_artifacts.repair(session)["cleared"] == 1


@pytest.mark.slow
def test_a_photo_whose_preview_was_lost_is_uploaded_again(env):
    # Its reader makes it again (src/server/reader_work.py): before, only a video's preview was.
    lib = _library(env)
    lost, kept, touched = File("a.jpg", "a"), File("b.jpg", "b"), File("c.jpg", "c")
    asset, _, changed = _ingest(env, lib, lost), _ingest(env, lib, kept), _ingest(env, lib, touched)
    _lose_preview(env, asset)
    _lose_preview(env, changed)
    run = _scan(env, lib, [lost, kept, touched.touched()])
    assert run.plan["a.jpg"] == {"rel_path": "a.jpg", "action": "upload", "reason": "preview_missing",
                                 "asset_id": asset, "legacy": False}
    assert _planned(run)["c.jpg"] == ("upload", "preview_missing")  # touched too: read once, not hashed first
    assert run.counts["unchanged"] == 1
    assert run.uploaded == {"a.jpg": asset, "c.jpg": changed}
    for clip in (asset, changed):
        assert env.client.get(f"/v1/assets/{clip}/artifacts/lumiverb.preview", headers=env.editor).status_code == 200
    assert _scan(env, lib, [lost, kept, touched.touched()]).plan == {}  # made: nothing more to do


@pytest.mark.slow
def test_thorough_hashes_every_known_file(env):
    lib = _library(env)
    a, b = File("a.jpg", "a"), File("b.jpg", "b")
    _ingest(env, lib, a)
    _ingest(env, lib, b)
    run = _scan(env, lib, [a, File("b.jpg", "b changed")], thorough=True)
    assert _planned(run) == {"a.jpg": ("hash", "thorough"), "b.jpg": ("hash", "thorough")}
    assert _decided(run) == {"a.jpg": "unchanged", "b.jpg": "upload"}


@pytest.mark.slow
def test_force_uploads_every_known_file_and_looks_for_no_moves(env):
    lib = _library(env)
    a, b = File("a.jpg", "a"), File("b.jpg", "b")
    _ingest(env, lib, a)
    b_asset = _ingest(env, lib, b)
    run = _scan(env, lib, [a, b.at("moved/b.jpg")], force=True)
    assert _planned(run) == {"a.jpg": ("upload", "force"), "moved/b.jpg": ("upload", "new")}
    assert run.finish["archived"] == 1 and run.finish["moved"] == 0
    # Archived first, then ingested: restored at its new path by content, as today under --force.
    assert _clips(env, lib)["moved/b.jpg"] == b_asset


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_moved_file_keeps_its_clip(env):
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="apply")
    assert _planned(run) == {"b/1.jpg": ("hash", "move")} and run.counts["gone"] == 1
    assert run.decisions["b/1.jpg"] == {"rel_path": "b/1.jpg", "decision": "moved", "asset_id": asset,
                                        "moved_from": "a/1.jpg"}
    assert run.finish["archived"] == 0 and run.finish["moved"] == 1
    assert _clips(env, lib) == {"b/1.jpg": asset}
    assert run.uploaded == {}


@pytest.mark.slow
def test_a_renamed_folder_moves_every_clip_in_it(env):
    lib = _library(env)
    trip = [File("Trip/1.jpg", "t1"), File("Trip/2.jpg", "t2"), File("Trip/3.mov", "t3")]
    ids = {f.rel_path.split("/")[1]: _ingest(env, lib, f) for f in trip}
    run = _scan(env, lib, [f.at(f.rel_path.replace("Trip/", "Trip 2024/")) for f in trip], moves="apply")
    assert set(_decided(run).values()) == {"moved"}
    assert _clips(env, lib) == {f"Trip 2024/{name}": asset for name, asset in ids.items()}
    assert run.finish["archived"] == 0 and run.finish["moved"] == 3


@pytest.mark.slow
def test_a_copy_is_not_a_move(env):
    # The original's file is still there: the copy is a new clip.
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a, a.at("b/1.jpg")], moves="apply")
    assert _planned(run) == {"b/1.jpg": ("upload", "new")} and run.counts["gone"] == 0
    clips = _clips(env, lib)
    assert clips["a/1.jpg"] == asset and clips["b/1.jpg"] != asset


@pytest.mark.slow
def test_two_files_with_one_gone_clips_content_take_it_once(env):
    lib = _library(env)
    a = File("a.jpg", "same")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("x/a.jpg"), a.at("y/a.jpg")], moves="apply")
    assert _decided(run) == {"x/a.jpg": "moved", "y/a.jpg": "upload"}  # the first sent takes it
    clips = _clips(env, lib)
    assert clips["x/a.jpg"] == asset and clips["y/a.jpg"] != asset


@pytest.mark.slow
def test_moves_off_a_moved_file_is_a_new_clip_and_the_old_one_is_archived(env):
    _settings(env, follow_moves=False)
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="apply")
    assert _planned(run) == {"b/1.jpg": ("upload", "new")}
    assert run.finish["archived"] == 1
    assert _clip(env, asset)["deleted_reason"] == "missing"
    assert _clips(env, lib)["b/1.jpg"] != asset  # off: nothing is matched by content


@pytest.mark.slow
def test_moves_asked_about_wait_for_an_answer_then_apply(env):
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="ask")
    assert _decided(run) == {"b/1.jpg": "move_candidate"}
    assert _clip(env, asset)["rel_path"] == "a/1.jpg"  # not yet
    err = _error(run.finish_error, 409, "moved_files")
    assert err["details"] == {"count": 1, "moves": [{"from": "a/1.jpg", "to": "b/1.jpg"}]}
    c, h = env.client, env.editor
    assert _ok(c.post(f"/v1/scans/{run.scan_id}/moves", json={"apply": True}, headers=h)) == {
        "moved": 1, "skipped": 0, "upload": []}
    finish = _ok(c.post(f"/v1/scans/{run.scan_id}/finish", json={}, headers=h))
    assert finish["archived"] == 0 and finish["moved"] == 1
    assert _clips(env, lib) == {"b/1.jpg": asset}


@pytest.mark.slow
def test_moves_turned_off_before_the_answer_are_refused_and_skipping_still_works(env):
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="ask")
    assert _decided(run) == {"b/1.jpg": "move_candidate"}
    _settings(env, follow_moves=False)  # an admin, while the scan waits for its answer
    c, h = env.client, env.editor
    err = _error(c.post(f"/v1/scans/{run.scan_id}/moves", json={"apply": True}, headers=h), 409, "moves_off")
    assert err["details"] == {"follow_moves": False}
    assert _clip(env, asset)["rel_path"] == "a/1.jpg"  # nothing moved
    assert _ok(c.post(f"/v1/scans/{run.scan_id}/moves", json={"apply": False}, headers=h))["skipped"] == 1


@pytest.mark.slow
def test_moves_asked_about_and_skipped_take_no_part(env):
    # Today's prompt, "Skip moves": the new path isn't uploaded, the old clip isn't archived.
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="ask", answer=False)
    assert run.moves == {"moved": 0, "skipped": 1, "upload": []}
    assert run.finish["archived"] == 0 and run.uploaded == {}
    assert _clips(env, lib) == {"a/1.jpg": asset}


@pytest.mark.slow
def test_skipping_moves_looks_for_none_and_archives_nothing(env):
    # --skip-moves as today (tests/test_scan_outcomes.py pins it as is): no
    # moves are looked for, so the moved file is a new clip and the old one
    # stays in sight; anything gone may be half a move, so nothing is archived.
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    gone = _ingest(env, lib, File("a/2.jpg", "two"))
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="skip")
    assert _planned(run) == {"b/1.jpg": ("upload", "new")}
    assert run.finish["archived"] == 0
    assert (run.finish["held_back"], run.finish["held_back_reason"]) == (2, "skip_moves")
    clips = _clips(env, lib)
    assert set(clips) == {"a/1.jpg", "a/2.jpg", "b/1.jpg"} and clips["b/1.jpg"] != asset
    assert clips["a/1.jpg"] == asset and clips["a/2.jpg"] == gone
    # The next scan that looks for moves puts it right: the clip moves to its new copy.
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="apply")
    assert run.finish["archived"] == 1 and run.finish["handed_over"] == 1
    assert _clips(env, lib) == {"b/1.jpg": asset}


@pytest.mark.slow
def test_skipping_moves_where_moves_are_off_still_archives(env):
    # Nothing gone can be half a move when a new path is always a new clip.
    _settings(env, follow_moves=False)
    lib = _library(env)
    asset = _ingest(env, lib, File("a/1.jpg", "one"))
    run = _scan(env, lib, [File("b/2.jpg", "two")], moves="skip")
    assert run.finish["archived"] == 1 and run.finish["held_back_reason"] is None
    assert _clip(env, asset)["deleted_reason"] == "missing"


@pytest.mark.slow
def test_hashes_sent_again_keep_their_decision(env):
    lib = _library(env)
    a = File("a.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b.jpg")], moves="ask")
    again = _ok(env.client.post(f"/v1/scans/{run.scan_id}/hashes",
                                json={"files": [{"rel_path": "b.jpg", "content_hash": a.sha}]}, headers=env.editor))
    assert again["files"] == [{"rel_path": "b.jpg", "decision": "move_candidate", "asset_id": asset,
                               "moved_from": "a.jpg"}]


# ---------------------------------------------------------------------------
# Gone and back
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_file_gone_is_archived_as_missing(env):
    lib = _library(env)
    a, b = File("a.jpg", "a"), File("b.jpg", "b")
    _ingest(env, lib, a)
    b_asset = _ingest(env, lib, b)
    run = _scan(env, lib, [a])
    assert run.counts["gone"] == 1
    assert run.finish == {"archived": 1, "moved": 0, "handed_over": 0, "unchanged": 1, "skipped": 0,
                          "held_back": 0, "held_back_reason": None, "dry_run": False}
    assert _clip(env, b_asset)["deleted_reason"] == "missing"


@pytest.mark.slow
def test_a_file_coming_back_restores_its_clip(env):
    lib = _library(env)
    a, b, c = File("a.jpg", "a"), File("b.jpg", "b"), File("c.jpg", "c")
    _ingest(env, lib, a)
    b_asset, c_asset = _ingest(env, lib, b), _ingest(env, lib, c)
    assert _scan(env, lib, [a]).finish["archived"] == 2
    # Back where it was, and back at a new path (restore by content).
    run = _scan(env, lib, [a, b, c.at("elsewhere/c.jpg")])
    assert _planned(run) == {"b.jpg": ("upload", "new"), "elsewhere/c.jpg": ("upload", "new")}
    assert _clips(env, lib) == {"a.jpg": _clips(env, lib)["a.jpg"], "b.jpg": b_asset, "elsewhere/c.jpg": c_asset}


@pytest.mark.slow
def test_an_original_gone_moves_to_its_empty_newer_copy(env):
    # Copy, then delete the original: the clip moves to its copy, as DELETE /v1/assets hands it over.
    lib = _library(env)
    a = File("Ingest/a.jpg", "a")
    original = _ingest(env, lib, a)
    copy = _ingest(env, lib, a.at("Projects/a.jpg"))
    run = _scan(env, lib, [a.at("Projects/a.jpg")])
    assert run.finish["archived"] == 0 and run.finish["handed_over"] == 1 and run.finish["moved"] == 1
    assert _clips(env, lib) == {"Projects/a.jpg": original}
    with env.db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assets WHERE asset_id = :a"), {"a": copy}).scalar() == 0


@pytest.mark.slow
def test_a_copy_someone_worked_on_keeps_its_clip_when_the_original_goes(env):
    lib = _library(env)
    a = File("Ingest/a.jpg", "a")
    original = _ingest(env, lib, a)
    copy = _ingest(env, lib, a.at("Projects/a.jpg"))
    _ok(env.client.put(f"/v1/assets/{copy}/rating", json={"stars": 3}, headers=env.editor))
    run = _scan(env, lib, [a.at("Projects/a.jpg")])
    assert run.finish["archived"] == 1 and run.finish["handed_over"] == 0
    assert _clips(env, lib) == {"Projects/a.jpg": copy}
    assert _clip(env, original)["deleted_reason"] == "missing"


@pytest.mark.slow
def test_a_copy_made_then_the_original_overwritten_takes_the_clip_with_it(env):
    lib = _library(env)
    a = File("a.jpg", "original")
    original = _ingest(env, lib, a)
    run = _scan(env, lib, [File("a.jpg", "overwritten", mtime=T1), a.at("copies/a.jpg")])
    assert _planned(run) == {"a.jpg": ("hash", "changed"), "copies/a.jpg": ("upload", "new")}
    clips = _clips(env, lib)
    assert set(clips) == {"a.jpg", "copies/a.jpg"}
    assert clips["copies/a.jpg"] == original and clips["a.jpg"] != original


@pytest.mark.slow
def test_a_move_seen_first_by_a_scan_of_its_new_folder_is_a_move_by_the_next_full_scan(env):
    # The folder's scan can't see the old path go: the file is a new clip
    # there, and the next full scan hands the clip over to it.
    lib = _library(env)
    a = File("inbox/a.jpg", "a")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("sorted/a.jpg")], scope="sorted", moves="apply")
    assert _planned(run) == {"sorted/a.jpg": ("upload", "new")} and run.finish["archived"] == 0
    assert set(_clips(env, lib)) == {"inbox/a.jpg", "sorted/a.jpg"}
    run = _scan(env, lib, [a.at("sorted/a.jpg")], moves="apply")
    assert run.finish["handed_over"] == 1
    assert _clips(env, lib) == {"sorted/a.jpg": asset}


@pytest.mark.slow
def test_a_file_moved_while_its_new_copy_settles_goes_missing_then_comes_back_there(env):
    lib = _library(env)
    a, b = File("a.jpg", "a"), File("b.jpg", "b")
    asset = _ingest(env, lib, a)
    _ingest(env, lib, b)
    run = _scan(env, lib, [b, a.at("elsewhere/a.jpg")], settling={"elsewhere/a.jpg"}, moves="apply")
    assert run.plan == {} and run.finish["archived"] == 1
    assert _clip(env, asset)["deleted_reason"] == "missing"
    run = _scan(env, lib, [b, a.at("elsewhere/a.jpg")], moves="apply")  # settled
    assert _planned(run) == {"elsewhere/a.jpg": ("upload", "new")}
    assert _clips(env, lib)["elsewhere/a.jpg"] == asset  # restored by content at ingest


@pytest.mark.slow
def test_unlisted_folders_protect_their_clips_and_hold_back_archiving(env):
    lib = _library(env)
    x, y1, y2, z = File("x/1.jpg", "x1"), File("y/1.jpg", "y1"), File("y/2.jpg", "y2"), File("z/1.jpg", "z1")
    for f in (x, y1, y2, z):
        _ingest(env, lib, f)
    run = _scan(env, lib, [x, y2.at("w/2.jpg")], unlisted=["y"])
    # y's clips are neither gone nor where a move comes from; z's is gone but not archived.
    assert run.counts["gone"] == 1
    assert _planned(run) == {"w/2.jpg": ("upload", "new")}
    assert (run.finish["archived"], run.finish["held_back"], run.finish["held_back_reason"]) == (0, 1, "unlisted")
    assert set(_clips(env, lib)) == {"x/1.jpg", "y/1.jpg", "y/2.jpg", "z/1.jpg", "w/2.jpg"}


@pytest.mark.slow
def test_unlisted_folders_still_let_moves_happen(env):
    lib = _library(env)
    x, z = File("x/1.jpg", "x1"), File("z/1.jpg", "z1")
    _ingest(env, lib, x)
    z_asset = _ingest(env, lib, z)
    run = _scan(env, lib, [x, z.at("z2/1.jpg")], unlisted=["y"], moves="apply")
    assert _decided(run) == {"z2/1.jpg": "moved"}
    assert _clips(env, lib)["z2/1.jpg"] == z_asset


@pytest.mark.slow
def test_most_of_a_library_gone_asks_first_then_archives_when_allowed(env):
    lib = _library(env)
    files = [File(f"f{i:02}.jpg", f"f{i}") for i in range(60)]
    for f in files:  # no content known, as an older scan left them
        make_clip(env.tenant_url, lib, f.rel_path, file_size=f.file_size, file_mtime=f.mtime)
    run = _scan(env, lib, files[:5])
    err = _error(run.finish_error, 409, "mass_missing")
    assert err["details"]["count"] == 55 and "allow_mass_missing: true" in err["message"]
    assert len(_clips(env, lib)) == 60  # nothing taken
    finish = _ok(env.client.post(f"/v1/scans/{run.scan_id}/finish", json={"allow_mass_missing": True},
                                 headers=env.editor))
    assert finish["archived"] == 55
    assert len(_clips(env, lib)) == 5


@pytest.mark.slow
def test_a_whole_library_scan_that_finds_nothing_archives_nothing(env):
    # An empty library is more likely an unmounted volume than a deleted one.
    lib = _library(env)
    asset = _ingest(env, lib, File("a.jpg", "a"))
    run = _scan(env, lib, [])
    assert (run.finish["archived"], run.finish["held_back"], run.finish["held_back_reason"]) == (0, 1, "nothing_found")
    assert _clip(env, asset)["deleted_reason"] is None
    # Unless forced, or a folder: an empty folder is just empty.
    assert _scan(env, lib, [], force=True).finish["archived"] == 1


@pytest.mark.slow
def test_a_folder_scan_takes_only_that_folders_clips_for_gone(env):
    lib = _library(env)
    a = _ingest(env, lib, File("a/1.jpg", "a1"))
    b = _ingest(env, lib, File("b/1.jpg", "b1"))
    ab = _ingest(env, lib, File("ab/1.jpg", "ab1"))
    run = _scan(env, lib, [], scope="a")
    assert run.counts["gone"] == 1 and run.finish["archived"] == 1
    assert _clip(env, a)["deleted_reason"] == "missing"
    assert _clip(env, b)["deleted_reason"] is None and _clip(env, ab)["deleted_reason"] is None
    scan_id = _start(env, lib, scope="a")
    r = env.client.post(f"/v1/scans/{scan_id}/found", json={"files": _listing([File("b/2.jpg", "b2")])},
                        headers=env.editor)
    _error(r, 400, "bad_request")


@pytest.mark.slow
def test_a_media_type_scan_takes_only_that_media_for_gone(env):
    lib = _library(env)
    photo = _ingest(env, lib, File("p.jpg", "p"))
    video = _ingest(env, lib, File("v.mov", "v"))
    run = _scan(env, lib, [File("q.jpg", "q"), File("w.mov", "w")], media_type="image")
    assert _planned(run) == {"q.jpg": ("upload", "new")}  # the video on disk isn't this scan's
    assert run.counts["gone"] == 1 and run.finish["archived"] == 1
    assert _clip(env, photo)["deleted_reason"] == "missing" and _clip(env, video)["deleted_reason"] is None


@pytest.mark.slow
def test_settling_and_empty_files_are_present_but_not_planned(env):
    lib = _library(env)
    a, b = File("a.jpg", "a"), File("b.jpg", "b")
    _ingest(env, lib, a)
    _ingest(env, lib, b)
    disk = [a.touched(), b, File("c.jpg", "c"), File("d.jpg", "d")]
    run = _scan(env, lib, disk, settling={"a.jpg", "c.jpg"}, empty={"b.jpg", "d.jpg"})
    assert run.plan == {}
    assert run.counts == {"found": 4, "upload": 0, "hash": 0, "unchanged": 0, "skipped": 0, "settling": 2,
                          "empty": 2, "gone": 0}
    assert run.finish["archived"] == 0 and set(_clips(env, lib)) == {"a.jpg", "b.jpg"}


@pytest.mark.slow
def test_a_scan_doesnt_archive_what_another_scan_made_or_moved_meanwhile(env):
    lib = _library(env)
    a, m = File("a.jpg", "a"), File("m.jpg", "m")
    _ingest(env, lib, a)
    m_asset = _ingest(env, lib, m)
    scan_id = _start(env, lib, moves="apply")
    # While this scan walks, a new file is ingested and another scan moves m.
    made = _ingest(env, lib, File("new.jpg", "new"))
    other = _scan(env, lib, [a, m.at("m2.jpg"), File("new.jpg", "new")], moves="apply")
    assert _decided(other) == {"m2.jpg": "moved"} and other.finish["archived"] == 0
    c, h = env.client, env.editor
    _ok(c.post(f"/v1/scans/{scan_id}/found", json={"files": _listing([a, m])}, headers=h))  # walked before
    plan = _ok(c.post(f"/v1/scans/{scan_id}/plan", json={}, headers=h))
    assert plan["counts"]["gone"] == 0
    assert _ok(c.post(f"/v1/scans/{scan_id}/finish", json={}, headers=h))["archived"] == 0
    assert _clips(env, lib) == {"a.jpg": _clips(env, lib)["a.jpg"], "new.jpg": made, "m2.jpg": m_asset}


# ---------------------------------------------------------------------------
# Files a person removed
# ---------------------------------------------------------------------------


def _removed(env, lib: str) -> dict[str, File]:
    """t.jpg trashed, r.jpg archived, e.jpg deleted for good; k.jpg kept."""
    files = {n: File(f"{n}.jpg", n) for n in ("t", "r", "e", "k")}
    ids = {n: _ingest(env, lib, f) for n, f in files.items()}
    c = env.client
    _ok(c.request("DELETE", "/v1/assets", json={"asset_ids": [ids["t"], ids["e"]], "reason": "user"},
                  headers=env.editor))
    _ok(c.post("/v1/assets/archive", json={"asset_ids": [ids["r"]]}, headers=env.editor))
    assert _ok(c.request("DELETE", "/v1/trash/empty", json={"asset_ids": [ids["e"]]}, headers=env.admin))["deleted"] == 1
    return files


@pytest.mark.slow
def test_files_a_person_removed_are_skipped_by_size_and_time(env):
    lib = _library(env)
    files = _removed(env, lib)
    run = _scan(env, lib, list(files.values()))
    assert _planned(run) == {"t.jpg": ("skipped", "removed"), "r.jpg": ("skipped", "removed"),
                             "e.jpg": ("skipped", "removed")}
    assert run.finish["skipped"] == 3 and run.finish["unchanged"] == 1 and run.finish["archived"] == 0
    assert set(_clips(env, lib)) == {"k.jpg"}


@pytest.mark.slow
def test_a_removed_file_touched_is_hashed_and_skipped_for_its_content(env):
    lib = _library(env)
    files = _removed(env, lib)
    run = _scan(env, lib, [f.touched() for f in files.values()])
    assert {p: a for p, (a, _) in _planned(run).items()} == {"t.jpg": "hash", "r.jpg": "hash", "e.jpg": "hash",
                                                             "k.jpg": "hash"}
    assert _decided(run) == {"t.jpg": "skipped", "r.jpg": "skipped", "e.jpg": "skipped", "k.jpg": "unchanged"}
    assert run.uploaded == {}


@pytest.mark.slow
def test_another_file_at_a_removed_path_is_a_new_clip(env):
    lib = _library(env)
    _removed(env, lib)
    disk = [File(f"{n}.jpg", f"another {n}", mtime=T1) for n in ("t", "r", "e")]
    run = _scan(env, lib, disk)
    assert _decided(run) == {"t.jpg": "upload", "r.jpg": "upload", "e.jpg": "upload"}
    assert set(_clips(env, lib)) == {"t.jpg", "r.jpg", "e.jpg"}  # k.jpg isn't on disk: gone


def _every_clip(env, library_id: str) -> dict[str, tuple[str, str | None]]:
    """Every clip of the library, in sight or not: {rel_path: (asset_id, deleted_reason)}, the latest per path."""
    with env.db.connect() as conn:
        rows = conn.execute(text("SELECT rel_path, asset_id, deleted_reason FROM assets WHERE library_id = :l"
                                 " ORDER BY created_at"), {"l": library_id}).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _forgotten(env, library_id: str, rel_path: str) -> bool:
    with env.db.connect() as conn:
        return conn.execute(text("SELECT 1 FROM ignored_files WHERE library_id = :l AND rel_path = :p"),
                            {"l": library_id, "p": rel_path}).first() is None


@pytest.mark.slow
def test_force_add_brings_back_what_a_person_removed(env):
    """A last resort (lumiverb scan --force-add): a clip trashed or archived
    comes back into sight with everything it had; a file deleted for good
    comes back as a new clip and is forgotten as deleted, so later scans take
    it as any other file."""
    lib = _library(env)
    files = _removed(env, lib)
    before = _every_clip(env, lib)
    run = _scan(env, lib, list(files.values()), force_add=True)
    assert _planned(run) == {"t.jpg": ("hash", "removed"), "r.jpg": ("hash", "removed"),
                             "e.jpg": ("hash", "removed")}
    assert _decided(run) == {"t.jpg": "restored", "r.jpg": "restored", "e.jpg": "upload"}
    assert run.decisions["t.jpg"]["asset_id"] == before["t.jpg"][0]
    assert run.decisions["r.jpg"]["asset_id"] == before["r.jpg"][0]
    assert run.finish["archived"] == 0 and run.finish["skipped"] == 0
    clips = _clips(env, lib)
    assert set(clips) == {"t.jpg", "r.jpg", "e.jpg", "k.jpg"}
    assert clips["t.jpg"] == before["t.jpg"][0] and clips["r.jpg"] == before["r.jpg"][0]
    assert clips["e.jpg"] == run.uploaded["e.jpg"]
    assert _forgotten(env, lib, "e.jpg")
    again = _scan(env, lib, list(files.values()))
    assert _planned(again) == {} and again.finish["unchanged"] == 4


@pytest.mark.slow
def test_force_add_brings_a_clip_archived_then_trashed_into_sight(env):
    lib = _library(env)
    f = File("a.jpg", "a")
    asset_id = _ingest(env, lib, f)
    _ok(env.client.post("/v1/assets/archive", json={"asset_ids": [asset_id]}, headers=env.editor))
    _ok(env.client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "user"},
                           headers=env.editor))
    run = _scan(env, lib, [f.touched()], force_add=True)
    assert _decided(run) == {"a.jpg": "restored"}
    clip = _clip(env, asset_id)
    assert clip["deleted_reason"] is None and clip["file_mtime"] == T1  # in sight, the file as found


@pytest.mark.slow
def test_force_add_brings_back_a_clip_whose_content_was_never_known(env):
    """Removed before its content was known: a scan skips the file by its
    path alone, and force_add brings the clip back by its path alone."""
    lib = _library(env)
    f = File("old.jpg", "old")
    asset_id = _ingest(env, lib, f)
    _ok(env.client.delete(f"/v1/assets/{asset_id}", headers=env.editor), 204)
    with env.db.begin() as conn:
        conn.execute(text("UPDATE assets SET content_hash = NULL WHERE asset_id = :a"), {"a": asset_id})
    assert _planned(_scan(env, lib, [f], dry_run=True)) == {"old.jpg": ("skipped", "removed")}
    run = _scan(env, lib, [f], force_add=True)
    assert _decided(run) == {"old.jpg": "restored"}
    assert _clips(env, lib) == {"old.jpg": asset_id}


@pytest.mark.slow
def test_force_add_another_file_at_a_removed_path_is_a_new_clip_and_the_old_stays_removed(env):
    lib = _library(env)
    _removed(env, lib)
    before = _every_clip(env, lib)
    disk = [File(f"{n}.jpg", f"another {n}", mtime=T1) for n in ("t", "r")]
    run = _scan(env, lib, disk, force_add=True)
    assert _decided(run) == {"t.jpg": "upload", "r.jpg": "upload"}
    assert set(_clips(env, lib)) == {"t.jpg", "r.jpg"}
    assert _clip(env, before["t.jpg"][0])["deleted_reason"] == "user"
    assert _clip(env, before["r.jpg"][0])["deleted_reason"] == "archived"


@pytest.mark.slow
def test_force_add_takes_only_the_scans_folder(env):
    lib = _library(env)
    here, there = File("here/a.jpg", "here"), File("there/a.jpg", "there")
    ids = {f.rel_path: _ingest(env, lib, f) for f in (here, there)}
    _ok(env.client.request("DELETE", "/v1/assets", json={"asset_ids": list(ids.values()), "reason": "user"},
                           headers=env.editor))
    run = _scan(env, lib, [here], scope="here", force_add=True)
    assert _decided(run) == {"here/a.jpg": "restored"}
    assert _clips(env, lib) == {"here/a.jpg": ids["here/a.jpg"]}
    assert _clip(env, ids["there/a.jpg"])["deleted_reason"] == "user"


@pytest.mark.slow
def test_force_add_in_a_dry_run_decides_and_changes_nothing(env):
    lib = _library(env)
    files = _removed(env, lib)
    before = _every_clip(env, lib)
    run = _scan(env, lib, list(files.values()), force_add=True, dry_run=True)
    assert _decided(run) == {"t.jpg": "restored", "r.jpg": "restored", "e.jpg": "upload"}
    assert _every_clip(env, lib) == before
    assert not _forgotten(env, lib, "e.jpg")
    assert _planned(_scan(env, lib, list(files.values()), dry_run=True)) == {
        "t.jpg": ("skipped", "removed"), "r.jpg": ("skipped", "removed"), "e.jpg": ("skipped", "removed")}


@pytest.mark.slow
def test_force_add_hashes_sent_again_keep_their_decision(env):
    lib = _library(env)
    f = File("a.jpg", "a")
    asset_id = _ingest(env, lib, f)
    _ok(env.client.delete(f"/v1/assets/{asset_id}", headers=env.editor), 204)
    c, h = env.client, env.editor
    scan_id = _start(env, lib, force_add=True)
    _ok(c.post(f"/v1/scans/{scan_id}/found", json={"files": _listing([f])}, headers=h))
    _ok(c.post(f"/v1/scans/{scan_id}/plan", json={}, headers=h))
    sent = {"files": [{"rel_path": f.rel_path, "content_hash": f.sha}]}
    first = _ok(c.post(f"/v1/scans/{scan_id}/hashes", json=sent, headers=h))
    second = _ok(c.post(f"/v1/scans/{scan_id}/hashes", json=sent, headers=h))
    assert first == second == {"files": [{"rel_path": "a.jpg", "decision": "restored", "asset_id": asset_id,
                                          "moved_from": None}]}
    assert _clips(env, lib) == {"a.jpg": asset_id}


# ---------------------------------------------------------------------------
# Listed before the finish (lumiverb scan --explain): the files a scan skips
# because a person removed them, and the clips it didn't find
# ---------------------------------------------------------------------------


def _all(env, scan_id: str, what: str, key: str, limit: int = 500) -> list[dict]:
    """Every item of GET /v1/scans/{id}/{what} (skipped, gone), a page at a time."""
    items: list[dict] = []
    params: dict = {"limit": limit}
    while True:
        page = _ok(env.client.get(f"/v1/scans/{scan_id}/{what}", params=params, headers=env.editor))
        items += page[key]
        if page["next_cursor"] is None:
            return items
        params["after"] = page["next_cursor"]


def _removed_ids(env, lib: str) -> dict[str, str]:
    """The library's clips a person trashed or archived: {rel_path: asset_id}."""
    with env.db.connect() as conn:
        return dict(conn.execute(text("SELECT rel_path, asset_id FROM assets WHERE library_id = :l"
                                      " AND deleted_reason IN ('user', 'archived')"), {"l": lib}).all())


def _trash(env, asset_ids: list[str]) -> None:
    _ok(env.client.request("DELETE", "/v1/assets", json={"asset_ids": asset_ids, "reason": "user"},
                           headers=env.editor))


def _empty_trash(env, asset_ids: list[str]) -> None:
    assert _ok(env.client.request("DELETE", "/v1/trash/empty", json={"asset_ids": asset_ids},
                                  headers=env.admin))["deleted"] == len(asset_ids)


def _archive(env, asset_ids: list[str]) -> None:
    _ok(env.client.post("/v1/assets/archive", json={"asset_ids": asset_ids}, headers=env.editor))


def _drop(env, scan_id: str) -> None:
    _ok(env.client.delete(f"/v1/scans/{scan_id}", headers=env.editor), 204)


@pytest.mark.slow
@pytest.mark.parametrize("touched", [False, True], ids=["by size and time", "by content"])
def test_the_files_a_scan_skips_say_how_a_person_removed_them(env, touched):
    lib = _library(env)
    files = _removed(env, lib)  # t.jpg trashed, r.jpg archived, e.jpg deleted for good; k.jpg kept
    ids = _removed_ids(env, lib)
    run = _scan(env, lib, [f.touched() if touched else f for f in files.values()], finish=False)
    if touched:  # read, and skipped for their content
        assert _decided(run) == {"t.jpg": "skipped", "r.jpg": "skipped", "e.jpg": "skipped", "k.jpg": "unchanged"}
    else:  # skipped for their size and time, unread
        assert {p: a for p, (a, _) in _planned(run).items()} == {"t.jpg": "skipped", "r.jpg": "skipped",
                                                                 "e.jpg": "skipped"}
    assert _all(env, run.scan_id, "skipped", "files") == [
        {"rel_path": "e.jpg", "removed_as": "deleted", "asset_id": None},
        {"rel_path": "r.jpg", "removed_as": "archived", "asset_id": ids["r.jpg"]},
        {"rel_path": "t.jpg", "removed_as": "trash", "asset_id": ids["t.jpg"]},
    ]
    assert _all(env, run.scan_id, "gone", "clips") == []
    _drop(env, run.scan_id)


@pytest.mark.slow
def test_of_several_removals_that_are_the_file_the_trash_is_named_then_the_archive(env):
    # One path removed three times over, by clips whose content was never
    # known (an older scan's): each of them is whatever file is there now.
    lib = _library(env)
    deleted = make_clip(env.tenant_url, lib, "p.jpg")
    _trash(env, [deleted])
    _empty_trash(env, [deleted])
    archived = make_clip(env.tenant_url, lib, "p.jpg")
    _archive(env, [archived])
    trashed = make_clip(env.tenant_url, lib, "p.jpg")
    _trash(env, [trashed])

    def named() -> list[dict]:
        run = _scan(env, lib, [File("p.jpg", "whatever is there", mtime=T1)], dry_run=True, finish=False)
        assert _planned(run) == {"p.jpg": ("skipped", "removed")}
        try:
            return _all(env, run.scan_id, "skipped", "files")
        finally:
            _drop(env, run.scan_id)

    assert named() == [{"rel_path": "p.jpg", "removed_as": "trash", "asset_id": trashed}]
    _empty_trash(env, [trashed])  # the path's deleted for good twice now, one record
    assert named() == [{"rel_path": "p.jpg", "removed_as": "archived", "asset_id": archived}]


@pytest.mark.slow
def test_the_removal_named_is_the_one_that_is_the_file(env):
    # By its content, its size and time, or a content never known; another
    # removal at the same path that isn't this file is never named, trash or not.
    lib = _library(env)
    # c.jpg: trashed with other content, then archived with this one: read, the archive's by content.
    old_c = _ingest(env, lib, File("c.jpg", "old c"))
    _trash(env, [old_c])
    new_c = _ingest(env, lib, File("c.jpg", "new c", mtime=T1))
    _archive(env, [new_c])
    # s.jpg: archived with this size and time, then trashed with others: unread, the archive's by size and time.
    s = File("s.jpg", "s")
    s_archived = _ingest(env, lib, s)
    _archive(env, [s_archived])
    _trash(env, [_ingest(env, lib, File("s.jpg", "other s", mtime=T1))])
    # u.jpg: trashed before its content was known: whatever is there now.
    unknown = make_clip(env.tenant_url, lib, "u.jpg")
    _trash(env, [unknown])

    disk = [File("c.jpg", "new c", mtime=T1 + timedelta(hours=1)), s, File("u.jpg", "u", mtime=T1)]
    run = _scan(env, lib, disk, finish=False)
    assert _planned(run) == {"c.jpg": ("hash", "removed"), "s.jpg": ("skipped", "removed"),
                             "u.jpg": ("skipped", "removed")}
    assert _decided(run) == {"c.jpg": "skipped"}
    assert _all(env, run.scan_id, "skipped", "files") == [
        {"rel_path": "c.jpg", "removed_as": "archived", "asset_id": new_c},
        {"rel_path": "s.jpg", "removed_as": "archived", "asset_id": s_archived},
        {"rel_path": "u.jpg", "removed_as": "trash", "asset_id": unknown},
    ]
    _drop(env, run.scan_id)


@pytest.mark.slow
def test_a_dry_run_lists_what_it_skips_and_whats_gone_less_its_moves(env):
    lib = _library(env)
    files = _removed(env, lib)
    ids = _removed_ids(env, lib)
    photo = _ingest(env, lib, File("gone/p.jpg", "p"))
    video = _ingest(env, lib, File("gone/v.mov", "v"))
    moving = File("m.jpg", "m")
    _ingest(env, lib, moving)
    before = _clips(env, lib)
    run = _scan(env, lib, [*files.values(), moving.at("moved/m.jpg")], dry_run=True, finish=False)
    assert _decided(run) == {"moved/m.jpg": "move_candidate"}
    # m.jpg would move, so it isn't gone; k.jpg was found.
    assert _all(env, run.scan_id, "gone", "clips") == [
        {"asset_id": photo, "rel_path": "gone/p.jpg", "media_type": "image"},
        {"asset_id": video, "rel_path": "gone/v.mov", "media_type": "video"},
    ]
    assert _all(env, run.scan_id, "skipped", "files") == [
        {"rel_path": "e.jpg", "removed_as": "deleted", "asset_id": None},
        {"rel_path": "r.jpg", "removed_as": "archived", "asset_id": ids["r.jpg"]},
        {"rel_path": "t.jpg", "removed_as": "trash", "asset_id": ids["t.jpg"]},
    ]
    finish = _ok(env.client.post(f"/v1/scans/{run.scan_id}/finish", json={}, headers=env.editor))
    assert (finish["archived"], finish["moved"], finish["skipped"], finish["dry_run"]) == (2, 1, 3, True)
    assert _clips(env, lib) == before and _removed_ids(env, lib) == ids  # nothing changed


@pytest.mark.slow
def test_what_a_scan_skips_and_whats_gone_come_a_page_at_a_time_by_path(env):
    lib = _library(env)
    on_disk = [File(f"t{i}.jpg", f"t{i}") for i in range(5)]
    trashed = [_ingest(env, lib, f) for f in on_disk]
    _trash(env, trashed)
    gone = [_ingest(env, lib, File(f"g{i}.jpg", f"g{i}")) for i in range(5)]
    run = _scan(env, lib, on_disk, finish=False)
    for what, key, ids in (("skipped", "files", trashed), ("gone", "clips", gone)):
        first = _ok(env.client.get(f"/v1/scans/{run.scan_id}/{what}", params={"limit": 2}, headers=env.editor))
        assert len(first[key]) == 2 and first["next_cursor"] == first[key][-1]["rel_path"]
        every = _all(env, run.scan_id, what, key, limit=2)
        assert [i["asset_id"] for i in every] == ids  # by path, each once
        assert _all(env, run.scan_id, what, key) == every
        _error(env.client.get(f"/v1/scans/{run.scan_id}/{what}", params={"limit": 501}, headers=env.editor),
               422, "invalid_request")
    _drop(env, run.scan_id)


@pytest.mark.slow
def test_what_a_scan_skips_and_whats_gone_are_listed_after_the_plan_until_the_finish(env):
    lib = _library(env)
    _ingest(env, lib, File("gone.jpg", "gone"))
    c, h = env.client, env.editor
    s = _start(env, lib, dry_run=True)
    for what in ("skipped", "gone"):
        _error(c.get(f"/v1/scans/{s}/{what}", headers=h), 409, "scan_not_planned")
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": _listing([File("new.jpg", "new")])}, headers=h))
    _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))
    assert _ok(c.get(f"/v1/scans/{s}/skipped", headers=h)) == {"files": [], "next_cursor": None}
    assert [g["rel_path"] for g in _ok(c.get(f"/v1/scans/{s}/gone", headers=h))["clips"]] == ["gone.jpg"]
    _error(c.get(f"/v1/scans/{s}/gone", headers=env.other), 404, "not_found")
    _ok(c.post(f"/v1/scans/{s}/finish", json={}, headers=h))
    for what in ("skipped", "gone"):  # the finish dropped its listing
        _error(c.get(f"/v1/scans/{s}/{what}", headers=h), 409, "scan_finished")


# ---------------------------------------------------------------------------
# Dry runs, expiry, the scan's own rules
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_dry_run_decides_everything_and_changes_nothing(env):
    lib = _library(env)
    a, b, c = File("a.jpg", "a"), File("b.jpg", "b"), File("c.jpg", "c")
    a_asset, b_asset, c_asset = (_ingest(env, lib, f) for f in (a, b, c))
    run = _scan(env, lib, [a.touched(), b.at("moved/b.jpg"), File("new.jpg", "new")], moves="apply", dry_run=True)
    assert _planned(run) == {"a.jpg": ("hash", "changed"), "moved/b.jpg": ("hash", "move"),
                             "new.jpg": ("upload", "new")}
    assert _decided(run) == {"a.jpg": "unchanged", "moved/b.jpg": "moved"}
    assert run.finish == {"archived": 1, "moved": 1, "handed_over": 0, "unchanged": 1, "skipped": 0,
                          "held_back": 0, "held_back_reason": None, "dry_run": True}
    assert _clips(env, lib) == {"a.jpg": a_asset, "b.jpg": b_asset, "c.jpg": c_asset}
    assert _clip(env, a_asset)["file_mtime"] == T0


@pytest.mark.slow
def test_a_dry_run_asks_nothing_about_moves(env):
    lib = _library(env)
    a = File("a.jpg", "a")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b.jpg")], moves="ask", dry_run=True)
    assert _decided(run) == {"b.jpg": "move_candidate"}
    assert run.finish["moved"] == 1 and run.finish["archived"] == 0
    assert _clips(env, lib) == {"a.jpg": asset}


@pytest.mark.slow
def test_a_finished_scan_cant_be_used_again(env):
    lib = _library(env)
    run = _scan(env, lib, [File("a.jpg", "a")])
    c, h, s = env.client, env.editor, run.scan_id
    _error(c.post(f"/v1/scans/{s}/found", json={"files": []}, headers=h), 409, "scan_finished")
    _error(c.get(f"/v1/scans/{s}/plan", headers=h), 409, "scan_finished")
    _error(c.post(f"/v1/scans/{s}/plan", json={}, headers=h), 409, "scan_finished")
    _error(c.post(f"/v1/scans/{s}/hashes", json={"files": []}, headers=h), 409, "scan_finished")
    _error(c.post(f"/v1/scans/{s}/moves", json={"apply": True}, headers=h), 409, "scan_finished")
    _ok(c.delete(f"/v1/scans/{s}", headers=h), 204)
    _error(c.post(f"/v1/scans/{s}/finish", json={}, headers=h), 404, "not_found")
    _error(c.delete(f"/v1/scans/{s}", headers=h), 404, "not_found")  # sent again: it's gone


@pytest.mark.slow
def test_each_step_comes_in_its_turn(env):
    lib = _library(env)
    c, h = env.client, env.editor
    s = _start(env, lib)
    _error(c.post(f"/v1/scans/{s}/hashes", json={"files": []}, headers=h), 409, "scan_not_planned")
    _error(c.post(f"/v1/scans/{s}/moves", json={"apply": True}, headers=h), 409, "scan_not_planned")
    _error(c.post(f"/v1/scans/{s}/finish", json={}, headers=h), 409, "scan_not_planned")
    _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))
    _error(c.post(f"/v1/scans/{s}/found", json={"files": []}, headers=h), 409, "scan_planned")
    # A plan sent again is the same plan; for another listing (other folders unlisted), it's too late.
    _error(c.post(f"/v1/scans/{s}/plan", json={"unlisted": ["Day 1"]}, headers=h), 409, "scan_planned")


# ---------------------------------------------------------------------------
# Sent again: an answer lost on the way back (a dropped connection, the API
# restarting) and the client tries the call again (src/processing/api.py)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_finish_sent_again_gets_the_answer_the_first_got(env):
    """Review, Oct 13: a finish whose answer was lost was 409 scan_finished
    when sent again, and the client skipped that run's uploads."""
    lib = _library(env)
    a, gone = File("a.jpg", "a"), File("gone.jpg", "gone")
    _ingest(env, lib, a)
    gone_asset = _ingest(env, lib, gone)
    original = _ingest(env, lib, File("Ingest/b.jpg", "b"))
    copy = _ingest(env, lib, File("Projects/b.jpg", "b"))
    run = _scan(env, lib, [a, File("Projects/b.jpg", "b")])
    assert run.finish == {"archived": 1, "moved": 1, "handed_over": 1, "unchanged": 2, "skipped": 0,
                          "held_back": 0, "held_back_reason": None, "dry_run": False}
    clips = _clips(env, lib)
    again = _ok(env.client.post(f"/v1/scans/{run.scan_id}/finish", json={}, headers=env.editor))
    assert again == run.finish
    # And it did nothing again: the clips are as the first left them.
    assert _clips(env, lib) == clips == {"a.jpg": clips["a.jpg"], "Projects/b.jpg": original}
    assert _clip(env, gone_asset)["deleted_reason"] == "missing"
    with env.db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM assets WHERE asset_id = :a"), {"a": copy}).scalar() == 0
    # Whatever it's sent with: the first one's answer stands.
    assert _ok(env.client.post(f"/v1/scans/{run.scan_id}/finish", json={"allow_mass_missing": True},
                               headers=env.editor)) == run.finish


@pytest.mark.slow
def test_a_dry_runs_finish_sent_again_gets_the_same_answer(env):
    lib = _library(env)
    _ingest(env, lib, File("a.jpg", "a"))
    run = _scan(env, lib, [], scope="", dry_run=True, force=True)  # force: an empty listing still archives
    assert _ok(env.client.post(f"/v1/scans/{run.scan_id}/finish", json={}, headers=env.editor)) == run.finish
    assert run.finish["archived"] == 1 and set(_clips(env, lib)) == {"a.jpg"}


@pytest.mark.slow
def test_a_plan_sent_again_for_the_same_listing_is_the_same_plan(env):
    lib = _library(env)
    a, b = File("a/1.jpg", "one"), File("a/2.jpg", "two")
    asset = _ingest(env, lib, a)
    _ingest(env, lib, b)
    c, h = env.client, env.editor
    s = _start(env, lib, moves="apply")
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": _listing([a.at("b/1.jpg"), File("new.jpg", "new")])},
               headers=h))
    first = _ok(c.post(f"/v1/scans/{s}/plan", params={"limit": 1}, json={"unlisted": ["x", "x"]}, headers=h))
    assert first["counts"]["gone"] == 2 and first["next_cursor"]
    assert _ok(c.post(f"/v1/scans/{s}/plan", params={"limit": 1}, json={"unlisted": ["x"]}, headers=h)) == first
    _error(c.post(f"/v1/scans/{s}/plan", json={"unlisted": []}, headers=h), 409, "scan_planned")
    # The plan made once: the scan goes on as if it had been sent once.
    moved = _ok(c.post(f"/v1/scans/{s}/hashes", json={"files": [{"rel_path": "b/1.jpg", "content_hash": a.sha}]},
                       headers=h))
    assert moved["files"][0]["decision"] == "moved" and _clips(env, lib)["b/1.jpg"] == asset
    # Sent again after that, still the counts it answered (the move made since isn't a clip gone less).
    assert _ok(c.post(f"/v1/scans/{s}/plan", json={"unlisted": ["x"]}, headers=h))["counts"] == first["counts"]


@pytest.mark.slow
def test_moves_answered_again_get_the_answer_the_first_got(env):
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run = _scan(env, lib, [a.at("b/1.jpg")], moves="ask")  # its finish asks about the move first
    c, h, s = env.client, env.editor, run.scan_id
    first = _ok(c.post(f"/v1/scans/{s}/moves", json={"apply": True}, headers=h))
    assert first == {"moved": 1, "skipped": 0, "upload": []}
    again = _ok(c.post(f"/v1/scans/{s}/moves", json={"apply": True}, headers=h))
    assert again == first  # was {"moved": 0, ...}: nothing left waiting
    assert _ok(c.post(f"/v1/scans/{s}/finish", json={}, headers=h))["moved"] == 1
    assert _clips(env, lib) == {"b/1.jpg": asset}


@pytest.mark.slow
def test_moves_that_couldnt_be_made_are_named_again_when_answered_again(env):
    """The upload list is the client's only word that a move fell through:
    lost, those files would never be uploaded."""
    lib = _library(env)
    a = File("a/1.jpg", "one")
    asset = _ingest(env, lib, a)
    run_moves = _scan(env, lib, [a.at("b/1.jpg")], moves="ask")
    s = run_moves.scan_id
    with env.db.begin() as conn:  # meanwhile the clip moved elsewhere (another scan)
        conn.execute(text("UPDATE assets SET rel_path = 'c/1.jpg' WHERE asset_id = :a"), {"a": asset})
    first = _ok(env.client.post(f"/v1/scans/{s}/moves", json={"apply": True}, headers=env.editor))
    assert first == {"moved": 0, "skipped": 0, "upload": ["b/1.jpg"]}
    assert _ok(env.client.post(f"/v1/scans/{s}/moves", json={"apply": True}, headers=env.editor)) == first


# ---------------------------------------------------------------------------
# A finish is one transaction: an API that dies halfway leaves nothing done
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_finish_that_dies_handing_over_archives_nothing_and_can_finish_again(env):
    """Review, Oct 13: the archive (and the scan's close) committed before the
    handover to a copy. An API that died between them left the clip archived,
    its notes, people and projects with it, and the empty copy in its place;
    nothing later put that right, and the scan couldn't be finished again."""
    lib = _library(env)
    original = _ingest(env, lib, File("Ingest/b.jpg", "b"))
    copy = _ingest(env, lib, File("Projects/b.jpg", "b"))
    gone = _ingest(env, lib, File("gone.jpg", "gone"))
    c, h = env.client, env.editor
    s = _start(env, lib, moves="apply")
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": _listing([File("Projects/b.jpg", "b")])}, headers=h))
    _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))
    with (patch("src.server.api.routers.scans.hand_over_to_copies", side_effect=RuntimeError("the API died")),
          pytest.raises(RuntimeError, match="the API died")):
        c.post(f"/v1/scans/{s}/finish", json={}, headers=h)
    # Nothing archived, nothing handed over, the scan still open.
    assert _clips(env, lib) == {"Ingest/b.jpg": original, "Projects/b.jpg": copy, "gone.jpg": gone}
    with env.db.connect() as conn:
        assert conn.execute(text("SELECT finished_at FROM scan_sessions WHERE scan_id = :s"), {"s": s}).scalar() is None
    finish = _ok(c.post(f"/v1/scans/{s}/finish", json={}, headers=h))
    assert (finish["archived"], finish["handed_over"]) == (1, 1)
    assert _clips(env, lib) == {"Projects/b.jpg": original}


@pytest.mark.slow
def test_a_handover_that_fails_on_its_own_leaves_that_clip_archived_and_the_rest_done(env):
    """One clip's handover failing (its copy busy, say) is no failed finish:
    that clip stays archived, as without moves, and the others go ahead."""
    lib = _library(env)
    first = _ingest(env, lib, File("Ingest/b.jpg", "b"))
    _ingest(env, lib, File("Projects/b.jpg", "b"))
    second = _ingest(env, lib, File("Ingest/c.jpg", "c"))
    c_copy = _ingest(env, lib, File("Projects/c.jpg", "c"))
    from src.server.api.routers import trash

    real = trash._hand_over

    def hand_over(session, request, asset_id, after):
        if asset_id == first:
            raise RuntimeError("its copy was busy")
        return real(session, request, asset_id, after)

    with patch.object(trash, "_hand_over", side_effect=hand_over):
        run = _scan(env, lib, [File("Projects/b.jpg", "b"), File("Projects/c.jpg", "c")])
    assert (run.finish["archived"], run.finish["handed_over"]) == (1, 1)
    clips = _clips(env, lib)
    assert clips["Projects/c.jpg"] == second and c_copy not in clips.values()
    assert _clip(env, first)["deleted_reason"] == "missing" and clips["Projects/b.jpg"] != first


# ---------------------------------------------------------------------------
# Scans a killed client left behind
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_upkeep_deletes_expired_scans_and_what_they_found(env):
    """A client killed mid-scan (SIGKILL, the power cut) never drops its own:
    upkeep deletes it once it has expired, a day after its last call."""
    lib = _library(env)
    c, h = env.client, env.editor
    stale, live = _start(env, lib), _start(env, lib)
    for s in (stale, live):
        _ok(c.post(f"/v1/scans/{s}/found", json={"files": _listing([File("a.jpg", "a")])}, headers=h))
    with env.db.begin() as conn:
        conn.execute(text("UPDATE scan_sessions SET expires_at = now() - interval '1 minute' WHERE scan_id = :s"),
                     {"s": stale})
    r = _ok(c.post("/v1/upkeep", headers=env.admin))
    assert r["expired_scans"]["deleted"] >= 1
    with env.db.connect() as conn:
        left = dict(conn.execute(text(
            "SELECT s.scan_id, (SELECT count(*) FROM scan_found f WHERE f.scan_id = s.scan_id)"
            " FROM scan_sessions s WHERE s.scan_id = ANY(:s)"), {"s": [stale, live]}).all())
        found = conn.execute(text("SELECT count(*) FROM scan_found WHERE scan_id = :s"), {"s": stale}).scalar()
    assert left == {live: 1} and found == 0


@pytest.mark.slow
def test_what_a_client_sends_is_checked(env):
    lib = _library(env)
    a = File("a.jpg", "a")
    _ingest(env, lib, a)
    c, h = env.client, env.editor
    s = _start(env, lib)
    found = f"/v1/scans/{s}/found"
    for bad in ("../a.jpg", "/a.jpg", "a/./b.jpg", "a\nb.jpg"):
        _error(c.post(found, json={"files": [{"rel_path": bad, "file_size": 1}]}, headers=h), 400, "bad_request")
    _error(c.post(found, json={"files": [{"rel_path": "notes.txt", "file_size": 1}]}, headers=h), 400, "bad_request")
    _error(c.post(found, json={"files": [{"rel_path": "a.jpg", "file_size": -1}]}, headers=h), 422, "invalid_request")
    many = [{"rel_path": f"{i}.jpg", "file_size": 1} for i in range(1001)]
    _error(c.post(found, json={"files": many}, headers=h), 422, "invalid_request")
    _ok(c.post(found, json={"files": _listing([a.touched()])}, headers=h))
    _error(c.post(f"/v1/scans/{s}/plan", json={"unlisted": ["../x"]}, headers=h), 400, "bad_request")
    _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))
    hashes = f"/v1/scans/{s}/hashes"
    _error(c.post(hashes, json={"files": [{"rel_path": "a.jpg", "content_hash": "XYZ"}]}, headers=h), 422, "invalid_request")
    err = _error(c.post(hashes, json={"files": [{"rel_path": "other.jpg", "content_hash": a.sha}]}, headers=h),
                 422, "not_planned")
    assert err["details"] == {"rel_paths": ["other.jpg"], "count": 1}
    assert _ok(c.post(hashes, json={"files": [{"rel_path": "a.jpg", "content_hash": a.sha}]}, headers=h))["files"][0][
        "decision"] == "unchanged"
    _error(c.post(f"/v1/libraries/{lib}/scans", json={"scope": "../x"}, headers=h), 400, "bad_request")
    _error(c.post(f"/v1/libraries/{lib}/scans", json={"moves": "sometimes"}, headers=h), 422, "invalid_request")


@pytest.mark.slow
def test_names_are_compared_in_one_unicode_form(env):
    # macOS hands out decomposed names; the clip's is stored composed (NFC).
    import unicodedata

    lib = _library(env)
    cafe = File(unicodedata.normalize("NFC", "Café/a.jpg"), "a")
    asset = _ingest(env, lib, cafe)
    run = _scan(env, lib, [cafe.at(unicodedata.normalize("NFD", cafe.rel_path))])
    assert run.counts["unchanged"] == 1 and run.counts["gone"] == 0
    assert _clips(env, lib) == {cafe.rel_path: asset}


@pytest.mark.slow
def test_an_expired_scan_is_gone(env):
    lib = _library(env)
    c, h = env.client, env.editor
    stale, waiting = _start(env, lib), _start(env, lib)
    with env.db.begin() as conn:
        conn.execute(text("UPDATE scan_sessions SET expires_at = now() - interval '1 minute'"
                          " WHERE scan_id = ANY(:s)"), {"s": [stale, waiting]})
    _error(c.post(f"/v1/scans/{stale}/found", json={"files": []}, headers=h), 410, "scan_expired")
    _error(c.post(f"/v1/scans/{stale}/found", json={"files": []}, headers=h), 404, "not_found")
    _start(env, lib)  # starting a scan clears the account's expired ones
    with env.db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM scan_sessions WHERE scan_id = :s"), {"s": waiting}).scalar() == 0


@pytest.mark.slow
def test_a_scan_is_pushed_back_a_day_by_each_call(env):
    lib = _library(env)
    s = _start(env, lib)
    with env.db.begin() as conn:
        conn.execute(text("UPDATE scan_sessions SET expires_at = now() + interval '1 minute' WHERE scan_id = :s"),
                     {"s": s})
    _ok(env.client.post(f"/v1/scans/{s}/found", json={"files": []}, headers=env.editor))
    with env.db.connect() as conn:
        left = conn.execute(text("SELECT expires_at - now() FROM scan_sessions WHERE scan_id = :s"), {"s": s}).scalar()
    assert left > timedelta(hours=23)


@pytest.mark.slow
def test_abandoning_a_scan_changes_nothing(env):
    lib = _library(env)
    asset = _ingest(env, lib, File("a.jpg", "a"))
    c, h = env.client, env.editor
    s = _start(env, lib)
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": []}, headers=h))
    assert _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))["counts"]["gone"] == 1
    _ok(c.delete(f"/v1/scans/{s}", headers=h), 204)
    _error(c.post(f"/v1/scans/{s}/finish", json={}, headers=h), 404, "not_found")
    assert _clips(env, lib) == {"a.jpg": asset}


# ---------------------------------------------------------------------------
# What a scan leaves behind: the scheduler scans each library in full every
# 15 minutes, and each scan lists every file it found
# ---------------------------------------------------------------------------


def _listed(env, scan_id: str) -> int:
    """Rows in the scan's listing."""
    with env.db.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM scan_found WHERE scan_id = :s"), {"s": scan_id}).scalar()


def _scans_kept(env, scan_id: str) -> int:
    with env.db.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM scan_sessions WHERE scan_id = :s"), {"s": scan_id}).scalar()


@pytest.mark.slow
def test_a_finished_scans_listing_goes_at_once_and_the_scan_stays_until_it_expires(env):
    """Robert, Oct 13: a full scan every 15 minutes lists every file each time
    (14,608 on his Mac); kept until each scan expired, a day after its last
    call, that would be 1.4 million rows a day. The finish deletes them; the
    scan stays, with its answer, so a finish sent again still gets it."""
    lib = _library(env)
    a = File("a.jpg", "a")
    _ingest(env, lib, a)
    _ingest(env, lib, File("gone.jpg", "gone"))
    c, h = env.client, env.editor
    s = _start(env, lib)
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": _listing([a, File("new.jpg", "new")])}, headers=h))
    _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))
    assert _listed(env, s) == 2
    finish = _ok(c.post(f"/v1/scans/{s}/finish", json={}, headers=h))
    assert finish["archived"] == 1 and _listed(env, s) == 0
    with env.db.connect() as conn:
        finished, left = conn.execute(text("SELECT finished_at, expires_at - now() FROM scan_sessions"
                                           " WHERE scan_id = :s"), {"s": s}).one()
    assert finished is not None and left > timedelta(hours=23)
    # Sent again, its answer is the scan's, never worked out again from a
    # listing that's gone (every clip would look missing).
    assert _ok(c.post(f"/v1/scans/{s}/finish", json={}, headers=h)) == finish
    assert set(_clips(env, lib)) == {"a.jpg"}
    # Expired, the scan goes too (starting one deletes the account's expired scans; so does upkeep).
    with env.db.begin() as conn:
        conn.execute(text("UPDATE scan_sessions SET expires_at = now() - interval '1 minute' WHERE scan_id = :s"),
                     {"s": s})
    _start(env, lib)
    assert _scans_kept(env, s) == 0


@pytest.mark.slow
def test_an_abandoned_scan_leaves_neither_itself_nor_its_listing(env):
    lib = _library(env)
    c, h = env.client, env.editor
    s = _start(env, lib)
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": _listing([File("a.jpg", "a"), File("b.jpg", "b")])},
               headers=h))
    _ok(c.post(f"/v1/scans/{s}/plan", json={}, headers=h))
    assert _listed(env, s) == 2
    _ok(c.delete(f"/v1/scans/{s}", headers=h), 204)
    assert (_scans_kept(env, s), _listed(env, s)) == (0, 0)


# ---------------------------------------------------------------------------
# Who may scan
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_another_accounts_scan_and_library_are_not_found(env):
    lib = _library(env)
    s = _start(env, lib)
    c = env.client
    _error(c.post(f"/v1/scans/{s}/found", json={"files": []}, headers=env.other), 404, "not_found")
    _error(c.post(f"/v1/scans/{s}/finish", json={}, headers=env.other), 404, "not_found")
    _error(c.delete(f"/v1/scans/{s}", headers=env.other), 404, "not_found")
    _error(c.post(f"/v1/libraries/{lib}/scans", json={}, headers=env.other), 404, "not_found")
    _ok(c.post(f"/v1/scans/{s}/found", json={"files": []}, headers=env.editor))  # still there for its own account


@pytest.mark.slow
def test_editors_scan_and_viewers_cant(env):
    lib = _library(env)
    c, v = env.client, env.viewer
    _error(c.post(f"/v1/libraries/{lib}/scans", json={}, headers=v), 403, "forbidden")
    s = _start(env, lib, headers=env.editor)
    for method, path, body in (("POST", "found", {"files": []}), ("POST", "plan", {}), ("GET", "plan", None),
                               ("GET", "skipped", None), ("GET", "gone", None),
                               ("POST", "hashes", {"files": []}), ("POST", "moves", {"apply": True}),
                               ("POST", "finish", {})):
        _error(c.request(method, f"/v1/scans/{s}/{path}", json=body, headers=v), 403, "forbidden")
    _error(c.delete(f"/v1/scans/{s}", headers=v), 403, "forbidden")
    assert _start(env, lib, headers=env.admin)


@pytest.mark.slow
def test_a_library_in_the_trash_isnt_scanned(env):
    lib = _library(env)
    _ok(env.client.delete(f"/v1/libraries/{lib}", headers=env.editor), 204)
    _error(env.client.post(f"/v1/libraries/{lib}/scans", json={}, headers=env.editor), 409, "library_trashed")
    _error(env.client.post("/v1/libraries/lib_nope/scans", json={}, headers=env.editor), 404, "not_found")


# ---------------------------------------------------------------------------
# Set-based: the round trips don't grow with the library
# ---------------------------------------------------------------------------


@contextmanager
def _statements(env):
    """Count the statements the API sends to the account's database."""
    seen: list[str] = []

    def count(conn, cursor, statement, *args):
        seen.append(statement)

    engine = get_engine_for_url(env.tenant_url)
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", count)


def _library_of(env, n: int) -> tuple[str, list[File]]:
    """A library of n photos, made directly (as many scans would have left
    it: their previews made as the account makes them now, so none is remade)."""
    from src.server.repository import lineage

    lib = _library(env)
    files = [File(f"k/{i:05}.jpg", f"k{i}") for i in range(n)]
    with Session(env.db) as session:
        session.add_all([Asset(asset_id=f"ast_{lib}_{i:05}", library_id=lib, rel_path=f.rel_path, content_hash=f.sha,
                               file_size=f.file_size, file_mtime=f.mtime, media_type="image", status="proxy_ready",
                               proxy_key=f"{lib}/proxy/{i:05}.webp", thumbnail_key=f"{lib}/thumbnail/{i:05}.webp",
                               created_at=T0, updated_at=T0) for i, f in enumerate(files)])
        session.commit()
        want = lineage.desired(session, "lumiverb.preview")
        session.execute(text(
            "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash, source_hash,"
            " produced_at) SELECT a.asset_id, 'lumiverb.preview', :p, :v, :h, a.content_hash, now() FROM assets a"
            " WHERE a.library_id = :lib"), {"p": want["producer"], "v": want["version"], "h": want["settings_hash"],
                                            "lib": lib})
        session.commit()
    return lib, files


def _round_trips(env, n: int) -> dict[str, int]:
    """Per call, the most statements any call of each kind sent, scanning a
    library of n photos: 70% unchanged, 10% touched, 10% moved, 10% gone, and
    5% new files."""
    lib, files = _library_of(env, n)
    tenth = n // 10
    disk = files[:7 * tenth]
    disk += [f.touched() for f in files[7 * tenth:8 * tenth]]
    disk += [f.at(f.rel_path.replace("k/", "moved/")) for f in files[8 * tenth:9 * tenth]]
    disk += [File(f"new/{i:05}.jpg", f"new {n} {i}") for i in range(n // 20)]
    most: dict[str, int] = {}

    def call(kind: str, method: str, path: str, **kwargs) -> dict:
        with _statements(env) as seen:
            r = env.client.request(method, path, headers=env.editor, **kwargs)
        assert r.status_code == 200, r.text
        most[kind] = max(most.get(kind, 0), len(seen))
        return r.json()

    s = call("start", "POST", f"/v1/libraries/{lib}/scans", json={"moves": "apply"})["scan_id"]
    listing = _listing(disk)
    for i in range(0, len(listing), 1000):
        call("found", "POST", f"/v1/scans/{s}/found", json={"files": listing[i:i + 1000]})
    plan = call("plan", "POST", f"/v1/scans/{s}/plan", params={"limit": 100}, json={})
    assert plan["counts"]["gone"] == 2 * tenth and plan["counts"]["unchanged"] == 7 * tenth
    files_, cursor = plan["files"], plan["next_cursor"]
    while cursor:
        more = call("page", "GET", f"/v1/scans/{s}/plan", params={"after": cursor, "limit": 100})
        files_, cursor = files_ + more["files"], more["next_cursor"]
    sha = {f.rel_path: f.sha for f in disk}
    to_hash = [f["rel_path"] for f in files_ if f["action"] == "hash"]
    assert len(to_hash) >= 2 * tenth
    decided: list[dict] = []
    for i in range(0, len(to_hash), 1000):
        decided += call("hashes", "POST", f"/v1/scans/{s}/hashes",
                        json={"files": [{"rel_path": p, "content_hash": sha[p]} for p in to_hash[i:i + 1000]]})["files"]
    assert sum(d["decision"] == "moved" for d in decided) == tenth
    assert sum(d["decision"] == "unchanged" for d in decided) == tenth
    # What --explain lists: a page of what's gone, or skipped, is one statement whatever the library's size.
    assert call("skipped", "GET", f"/v1/scans/{s}/skipped", params={"limit": 100})["files"] == []
    gone, cursor = [], None
    while True:
        page = call("gone", "GET", f"/v1/scans/{s}/gone", params={"limit": 100, **({"after": cursor} if cursor else {})})
        gone, cursor = gone + page["clips"], page["next_cursor"]
        if cursor is None:
            break
    assert len(gone) == tenth  # the moved ones aren't gone
    finish = call("finish", "POST", f"/v1/scans/{s}/finish", json={})
    assert finish["archived"] == tenth and finish["moved"] == tenth
    return most


@pytest.mark.slow
def test_comparing_is_set_based_not_a_round_trip_per_file(env):
    small, large = _round_trips(env, 300), _round_trips(env, 3000)
    assert "page" in large  # the large plan took several pages
    large.setdefault("page", 0)
    small.setdefault("page", large["page"])
    assert large == small, (small, large)
    assert max(large.values()) <= 25, large
