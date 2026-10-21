"""A clip's last modified time (assets.changed_at, tenant migration d9a3f5c1e7b4): the most recent of every time
related to it, kept by the database itself. Its file changing, anything Lumiverb made for it, what a person
changed (a description, a note, a location, a face named) move it on; a rating (one person's) or a try that
failed doesn't. The grid sorts by it (GET /v1/query sort=changed_at), a page at a time. The migration fills it
in from what's there."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.derived import saved
from tests.faces import plant_faces
from tests.machine_lineage import made
from tests.test_migration_lineage import _alembic
from tests.test_scans_api import env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow
BEFORE = "c7e1a9d3f5b2"


@pytest.fixture
def library(env):  # noqa: F811
    name = f"Changes {uuid.uuid4().hex[:8]}"
    r = env.client.post("/v1/libraries", json={"name": name, "root_path": f"/Volumes/{name}"}, headers=env.admin)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


def _listed(env, library_id: str, **params) -> dict:  # noqa: F811
    r = env.client.get("/v1/query", params={"f": f"library:{library_id}", "sort": "changed_at", **params},
                       headers=env.admin)
    assert r.status_code == 200, r.text
    return r.json()


def _changed(env, library_id: str, clip: str) -> datetime:  # noqa: F811
    [item] = [i for i in _listed(env, library_id)["items"] if i["asset_id"] == clip]
    return datetime.fromisoformat(item["changed_at"])


def test_every_related_change_moves_it_on_and_nothing_else_does(env, library):  # noqa: F811
    photo = make_clip(env.tenant_url, library, "beach.jpg")
    times = [_changed(env, library, photo)]

    def moved(why: str) -> None:
        now = _changed(env, library, photo)
        assert now > times[-1], why
        times.append(now)

    def stayed(why: str) -> None:
        assert _changed(env, library, photo) == times[-1], why

    r = saved(env.client, "vision", photo, headers=env.admin, json={
        "model_id": "m", "description": "Waves on a beach.", "tags": ["beach"],
        "lineage": made("lumiverb.image.description")})
    assert r.status_code == 200, r.text
    moved("a description made")
    r = env.client.patch(f"/v1/assets/{photo}/corrections", json={"description": "Our beach."}, headers=env.admin)
    assert r.status_code == 200, r.text
    moved("a person's description")
    r = env.client.put(f"/v1/assets/{photo}/note", json={"text": "Sue's favourite"}, headers=env.admin)
    assert r.status_code == 200, r.text
    moved("a note")
    r = env.client.put("/v1/assets/locations", json={"asset_ids": [photo], "lat": 33.69, "lon": -78.88},
                       headers=env.admin)
    assert r.status_code == 200, r.text
    moved("a location set")
    [face] = plant_faces(env.client, photo, [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                                              "detection_confidence": 0.95, "embedding": [0.1] * 512}],
                         headers=env.admin)
    moved("faces found")
    r = env.client.post(f"/v1/faces/{face}/assign", json={"new_person_name": "Sue"}, headers=env.admin)
    assert r.status_code == 200, r.text
    moved("a face named")

    r = env.client.put(f"/v1/assets/{photo}/rating", json={"favorite": True, "stars": 4}, headers=env.admin)
    assert r.status_code == 200, r.text
    stayed("a rating is one person's")
    r = env.client.post("/v1/producers/failures", headers=env.admin, json={
        "items": [{"asset_id": photo, "artifact": "lumiverb.image.text", "error": "the machine said no"}]})
    assert r.status_code == 200, r.text
    stayed("a try that failed")

    # Its file changed on disk (a scan writes the new time): that time, when it's the latest.
    later = times[-1] + timedelta(hours=3)
    engine = create_engine(env.tenant_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE assets SET file_mtime = :t WHERE asset_id = :a"), {"t": later, "a": photo})
    engine.dispose()
    assert _changed(env, library, photo) == later


def test_the_grid_sorts_by_it_a_page_at_a_time(env, library):  # noqa: F811
    first, second, third = (make_clip(env.tenant_url, library, f"{n}.jpg") for n in ("a", "b", "c"))
    r = env.client.put(f"/v1/assets/{first}/note", json={"text": "touched last"}, headers=env.admin)
    assert r.status_code == 200, r.text
    newest = [i["asset_id"] for i in _listed(env, library)["items"]]
    assert newest[0] == first and set(newest) == {first, second, third}

    seen, after = [], None
    while True:
        page = _listed(env, library, limit=1, **({"after": after} if after else {}))
        seen += [i["asset_id"] for i in page["items"]]
        after = page.get("next_cursor")
        if not after:
            break
    assert seen == newest, "a page at a time, the same order"
    oldest = [i["asset_id"] for i in _listed(env, library, dir="asc")["items"]]
    assert oldest == list(reversed(newest))


def test_the_migration_fills_it_in_from_what_is_there():
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', :t, :t)"), {"t": t0})
            for clip in ("ast_plain", "ast_described", "ast_corrected"):
                conn.execute(text(
                    "INSERT INTO assets (asset_id, library_id, rel_path, file_size, file_mtime, media_type, status,"
                    " availability, video_indexed, created_at, updated_at)"
                    " VALUES (:a, 'lib_a', :p, 1, :mtime, 'image', 'pending', 'online', false, :t, :t)"),
                    {"a": clip, "p": f"{clip}.jpg", "mtime": t0 - timedelta(days=30), "t": t0})
            conn.execute(text(
                "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                " produced_at, outcome) VALUES ('ast_described', 'lumiverb.image.description',"
                " 'lumiverb.image.description', '1', 'h', :t, 'ok'),"
                " ('ast_plain', 'lumiverb.image.text', '', '', '', :late, 'failed')"),
                {"t": t0 + timedelta(days=2), "late": t0 + timedelta(days=9)})
            conn.execute(text("INSERT INTO asset_corrections (asset_id, description, updated_at)"
                              " VALUES ('ast_corrected', 'mine', :t)"), {"t": t0 + timedelta(days=5)})
        _alembic(url, "upgrade", "head")
        with engine.connect() as conn:
            filled = dict(conn.execute(text("SELECT asset_id, changed_at FROM assets")).all())
        assert filled == {"ast_plain": t0, "ast_described": t0 + timedelta(days=2),
                          "ast_corrected": t0 + timedelta(days=5)}, "added, made (not a failure), a person's"
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            assert "changed_at" not in {c for (c,) in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'assets'")).all()}
        engine.dispose()
