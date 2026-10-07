"""Human data survives re-runs (ADR-016 phase 0).

Re-detecting faces, the upkeep sweep, rescans and re-transcription must
never undo what a person decided: confirmed face assignments, "not this
person", dismissals, trash (including emptied trash) and manual
transcripts.
"""

from __future__ import annotations

import io
import os
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from src.server.repository.tenant import FaceRepository, PersonRepository
from src.server.storage.local import LocalStorage
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Control + tenant DBs, one library, temp storage. Yields (client, headers, library_id, tenant_url)."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("human_data_storage")))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(
            make_url(control_url).set(database="{tenant_id}")
        )
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as bootstrap:
                r = bootstrap.post(
                    "/v1/admin/tenants",
                    json={"name": "HumanDataTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with (
                patch("src.server.api.routers.ingest.get_storage", return_value=storage),
                patch("src.server.api.routers.trash.get_storage", return_value=storage),
                TestClient(app) as client,
            ):
                headers = {"Authorization": f"Bearer {api_key}"}
                r = client.post(
                    "/v1/libraries",
                    json={"name": "HumanDataLib", "root_path": "/media"},
                    headers=headers,
                )
                assert r.status_code == 200, r.text
                yield client, headers, r.json()["library_id"], tenant_url

    _engines.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _db(tenant_url: str):
    engine = create_engine(tenant_url, future=True)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _unit(i: int) -> list[float]:
    v = [0.0] * 512
    v[i % 512] = 1.0
    return v


def _vec(i: int) -> str:
    return "[" + ",".join(str(x) for x in _unit(i)) + "]"


def _box(x: float, y: float, w: float = 0.2, h: float = 0.2) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def _seed_asset(tenant_url: str, library_id: str, media_type: str = "image") -> str:
    asset_id = "ast_" + uuid.uuid4().hex[:20]
    with _db(tenant_url) as s:
        s.execute(
            text(
                "INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type,"
                " availability, status, created_at, updated_at)"
                " VALUES (:id, :lib, :rp, 1000, :mt, 'online', 'discovered', NOW(), NOW())"
            ),
            {"id": asset_id, "lib": library_id, "rp": f"seed/{asset_id}", "mt": media_type},
        )
        s.commit()
    return asset_id


def _seed_face(tenant_url: str, asset_id: str, box: dict, emb: int) -> str:
    face_id = "face_" + uuid.uuid4().hex[:20]
    with _db(tenant_url) as s:
        s.execute(
            text(
                "INSERT INTO faces (face_id, asset_id, bounding_box_json, detection_confidence,"
                " detection_model, detection_model_version, embedding_vector, created_at)"
                " VALUES (:fid, :aid, CAST(:box AS jsonb), 0.95, 'insightface', 'buffalo_l',"
                "         CAST(:v AS vector), NOW())"
            ),
            {"fid": face_id, "aid": asset_id, "box": _json(box), "v": _vec(emb)},
        )
        s.commit()
    return face_id


def _json(value: dict) -> str:
    import json

    return json.dumps(value)


def _seed_person(tenant_url: str, emb: int, *, dismissed: bool = False) -> str:
    person_id = "person_" + uuid.uuid4().hex[:16]
    with _db(tenant_url) as s:
        s.execute(
            text(
                "INSERT INTO people (person_id, display_name, created_by_user, dismissed,"
                " centroid_vector, confirmation_count, created_at)"
                " VALUES (:pid, :name, true, :dismissed, CAST(:v AS vector), 0, NOW())"
            ),
            {"pid": person_id, "name": person_id, "dismissed": dismissed, "v": _vec(emb)},
        )
        s.commit()
    return person_id


def _seed_match(
    tenant_url: str, face_id: str, person_id: str, *, confirmed: bool, confidence: float | None
) -> None:
    with _db(tenant_url) as s:
        s.execute(
            text(
                "INSERT INTO face_person_matches (match_id, face_id, person_id, confidence,"
                " confirmed, created_at) VALUES (:mid, :fid, :pid, :conf, :confirmed, NOW())"
            ),
            {
                "mid": "fpm_" + uuid.uuid4().hex[:16],
                "fid": face_id,
                "pid": person_id,
                "conf": confidence,
                "confirmed": confirmed,
            },
        )
        s.execute(
            text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
            {"pid": person_id, "fid": face_id},
        )
        s.commit()


def _redetect(client, headers, asset_id: str, faces: list[tuple[dict, int]]) -> dict:
    r = client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "detection_model": "apple_vision",
            "detection_model_version": "2",
            "faces": [
                {"bounding_box": box, "detection_confidence": 0.9, "embedding": _unit(emb)}
                for box, emb in faces
            ],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _match(tenant_url: str, face_id: str) -> tuple[str, bool] | None:
    with _db(tenant_url) as s:
        row = s.execute(
            text("SELECT person_id, confirmed FROM face_person_matches WHERE face_id = :fid"),
            {"fid": face_id},
        ).first()
        return (row[0], row[1]) if row else None


def _face_exists(tenant_url: str, face_id: str) -> bool:
    with _db(tenant_url) as s:
        return s.execute(
            text("SELECT 1 FROM faces WHERE face_id = :fid"), {"fid": face_id}
        ).first() is not None


def _image() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), color=(90, 120, 150)).save(buf, format="JPEG")
    return buf.getvalue()


def _ingest(client, headers, library_id: str, rel_path: str):
    return client.post(
        "/v1/ingest",
        headers=headers,
        files={"proxy": ("p.jpg", io.BytesIO(_image()), "image/jpeg")},
        data={"library_id": library_id, "rel_path": rel_path, "file_size": "5000",
              "media_type": "image"},
    )


def _ignored(client, headers, library_id: str) -> dict[str, str]:
    r = client.get(f"/v1/libraries/{library_id}/ignored-paths", headers=headers)
    assert r.status_code == 200, r.text
    return {i["rel_path"]: i["reason"] for i in r.json()["items"]}


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_redetect_keeps_confirmed_assignment_on_refound_face(env) -> None:
    """A different detector finds the same face with a shifted box and an
    embedding far from the person's centroid: the face keeps its id and its
    confirmed person."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=1)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=1)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect(client, headers, asset_id, [(_box(0.12, 0.11, 0.2, 0.22), 2)])

    assert result["face_ids"] == [face_id]
    assert result["face_count"] == 1
    assert _match(tenant_url, face_id) == (person_id, True)


@pytest.mark.slow
def test_redetect_keeps_confirmed_face_it_does_not_refind(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=3)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=3)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect(client, headers, asset_id, [(_box(0.60, 0.60), 4)])

    assert face_id not in result["face_ids"]
    assert result["face_count"] == 2
    assert _face_exists(tenant_url, face_id)
    assert _match(tenant_url, face_id) == (person_id, True)


@pytest.mark.slow
def test_redetect_drops_machine_made_data(env) -> None:
    """Auto-assigned matches are derived data: a face that isn't re-found is
    deleted, and a re-found face's auto match is re-derived."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=5)
    gone = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=5)
    refound = _seed_face(tenant_url, asset_id, _box(0.50, 0.50), emb=5)
    _seed_match(tenant_url, gone, person_id, confirmed=False, confidence=0.9)
    _seed_match(tenant_url, refound, person_id, confirmed=False, confidence=0.9)

    # The re-found face now looks like nobody we know.
    result = _redetect(client, headers, asset_id, [(_box(0.50, 0.50), 6)])

    assert result["face_ids"] == [refound]
    assert not _face_exists(tenant_url, gone)
    assert _match(tenant_url, refound) is None


@pytest.mark.slow
def test_unassign_is_remembered_by_upkeep_and_redetect(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=7)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=7)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    r = client.delete(f"/v1/faces/{face_id}/assign", headers=headers)
    assert r.status_code == 204, r.text

    # The upkeep sweep would match it straight back (distance 0).
    with _db(tenant_url) as s:
        FaceRepository(s).propagate_assignments()
    assert _match(tenant_url, face_id) is None

    # So would auto-assign after re-detection.
    _redetect(client, headers, asset_id, [(_box(0.10, 0.10), 7)])
    assert _match(tenant_url, face_id) is None

    # An explicit assignment overrides the rejection.
    r = client.post(f"/v1/faces/{face_id}/assign", json={"person_id": person_id}, headers=headers)
    assert r.status_code == 200, r.text
    assert _match(tenant_url, face_id) == (person_id, True)
    with _db(tenant_url) as s:
        assert s.execute(
            text("SELECT COUNT(*) FROM face_person_rejections WHERE face_id = :fid"),
            {"fid": face_id},
        ).scalar() == 0


@pytest.mark.slow
def test_bulk_assign_skips_rejected_faces(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=8)
    rejected = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=8)
    other = _seed_face(tenant_url, asset_id, _box(0.50, 0.50), emb=8)
    _seed_match(tenant_url, rejected, person_id, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{rejected}/assign", headers=headers).status_code == 204

    with _db(tenant_url) as s:
        repo = PersonRepository(s)
        repo._assign_faces(person_id, [rejected, other])
        s.commit()

    assert _match(tenant_url, rejected) is None
    assert _match(tenant_url, other) == (person_id, True)


@pytest.mark.slow
def test_dismissal_survives_redetect_and_cleanup(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=9)
    with _db(tenant_url) as s:
        dismissed_id = PersonRepository(s).create_dismissed(face_ids=[face_id]).person_id

    _redetect(client, headers, asset_id, [(_box(0.11, 0.10), 9)])
    with _db(tenant_url) as s:
        assert PersonRepository(s).cleanup_empty_dismissed() == 0

    assert _match(tenant_url, face_id) == (dismissed_id, True)


@pytest.mark.slow
def test_merge_carries_rejections_to_target(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    source = _seed_person(tenant_url, emb=10)
    target = _seed_person(tenant_url, emb=11)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=10)
    _seed_match(tenant_url, face_id, source, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{face_id}/assign", headers=headers).status_code == 204

    r = client.post(
        f"/v1/people/{target}/merge", json={"source_person_id": source}, headers=headers
    )
    assert r.status_code == 200, r.text

    with _db(tenant_url) as s:
        people = {
            row[0]
            for row in s.execute(
                text("SELECT person_id FROM face_person_rejections WHERE face_id = :fid"),
                {"fid": face_id},
            )
        }
    assert people == {target}


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_user_trash_survives_rescan(env) -> None:
    client, headers, library_id, _ = env
    r = _ingest(client, headers, library_id, "trash/user.jpg")
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]

    assert client.delete(f"/v1/assets/{asset_id}", headers=headers).status_code == 204
    assert _ignored(client, headers, library_id).get("trash/user.jpg") == "trashed"

    r = _ingest(client, headers, library_id, "trash/user.jpg")
    assert r.status_code == 409, r.text
    assert client.get(f"/v1/assets/{asset_id}", headers=headers).status_code == 404


@pytest.mark.slow
@pytest.mark.parametrize("reason", ["missing", None])
def test_missing_file_is_restored_when_it_reappears(env, reason) -> None:
    client, headers, library_id, _ = env
    rel_path = f"trash/missing-{reason}.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]

    body: dict = {"asset_ids": [asset_id]}
    if reason:
        body["reason"] = reason
    r = client.request("DELETE", "/v1/assets", json=body, headers=headers)
    assert r.json()["trashed"] == [asset_id]
    assert rel_path not in _ignored(client, headers, library_id)

    r = _ingest(client, headers, library_id, rel_path)
    assert r.status_code == 200, r.text
    assert r.json()["asset_id"] == asset_id
    assert client.get(f"/v1/assets/{asset_id}", headers=headers).status_code == 200


@pytest.mark.slow
def test_emptied_trash_is_remembered_until_unignored(env) -> None:
    client, headers, library_id, _ = env
    rel_path = "trash/emptied.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]
    assert client.delete(f"/v1/assets/{asset_id}", headers=headers).status_code == 204

    r = client.request(
        "DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1
    assert _ignored(client, headers, library_id).get(rel_path) == "emptied"
    assert _ingest(client, headers, library_id, rel_path).status_code == 409

    r = client.request(
        "DELETE",
        f"/v1/libraries/{library_id}/ignored-paths",
        json={"rel_paths": [rel_path]},
        headers=headers,
    )
    assert r.json() == {"removed": 1}
    r = _ingest(client, headers, library_id, rel_path)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True


@pytest.mark.slow
def test_emptied_missing_file_is_not_remembered(env) -> None:
    client, headers, library_id, _ = env
    rel_path = "trash/emptied-missing.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]
    client.request(
        "DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"},
        headers=headers,
    )
    client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=headers)

    assert rel_path not in _ignored(client, headers, library_id)
    assert _ingest(client, headers, library_id, rel_path).status_code == 200


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

_SRT_MANUAL = "1\n00:00:00,000 --> 00:00:02,000\nTyped by a person\n"
_SRT_WHISPER = "1\n00:00:00,000 --> 00:00:02,000\nHeard by a model\n"


def _transcript(tenant_url: str, asset_id: str) -> tuple[str | None, str | None]:
    with _db(tenant_url) as s:
        row = s.execute(
            text("SELECT transcript_srt, transcript_source FROM assets WHERE asset_id = :aid"),
            {"aid": asset_id},
        ).first()
        return row[0], row[1]


@pytest.mark.slow
def test_machine_transcript_never_replaces_manual(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id, media_type="video")

    def post(srt: str, source: str):
        r = client.post(
            f"/v1/assets/{asset_id}/transcript",
            json={"srt": srt, "language": "en", "source": source},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        return r.json()["status"]

    assert post(_SRT_WHISPER, "whisper") == "transcribed"
    assert _transcript(tenant_url, asset_id) == (_SRT_WHISPER, "whisper")

    assert post(_SRT_MANUAL, "manual") == "transcribed"
    assert post(_SRT_WHISPER, "whisper") == "kept_manual"
    assert post("", "whisper") == "kept_manual"
    assert _transcript(tenant_url, asset_id) == (_SRT_MANUAL, "manual")

    # Deleting the transcript is the person's call too; afterwards a machine
    # transcript may be written again.
    assert client.delete(f"/v1/assets/{asset_id}/transcript", headers=headers).status_code == 204
    assert post(_SRT_WHISPER, "whisper") == "transcribed"
