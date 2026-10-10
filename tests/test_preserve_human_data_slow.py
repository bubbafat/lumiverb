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
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made, made


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Control + tenant DBs, one library, temp storage. Yields (client, headers, library_id, tenant_url)."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("human_data_storage")))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_pg:
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

        with PostgresContainer(PG_IMAGE) as tenant_pg:
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


def _mix(i: int, j: int, w: float) -> list[float]:
    """normalize(_unit(i) + w * _unit(j)): a face that looks partly like j."""
    v = _unit(i)
    v[j % 512] = w
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def _vec_of(values: list[float]) -> str:
    return "[" + ",".join(str(x) for x in values) + "]"


def _seed_face_vec(tenant_url: str, asset_id: str, box: dict, emb: list[float]) -> str:
    face_id = "face_" + uuid.uuid4().hex[:20]
    with _db(tenant_url) as s:
        s.execute(
            text(
                "INSERT INTO faces (face_id, asset_id, bounding_box_json, detection_confidence,"
                " detection_model, detection_model_version, embedding_vector, created_at)"
                " VALUES (:fid, :aid, CAST(:box AS jsonb), 0.95, 'insightface', 'buffalo_l',"
                "         CAST(:v AS vector), NOW())"
            ),
            {"fid": face_id, "aid": asset_id, "box": _json(box), "v": _vec_of(emb)},
        )
        s.commit()
    return face_id


def _near(i: int) -> list[float]:
    """The same face as _unit(i), seen again (cosine distance ~0.005)."""
    v = _unit(i)
    v[(i + 100) % 512] = 0.1
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


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


def _seed_face(tenant_url: str, asset_id: str, box: dict, emb: int | None) -> str:
    face_id = "face_" + uuid.uuid4().hex[:20]
    with _db(tenant_url) as s:
        s.execute(
            text(
                "INSERT INTO faces (face_id, asset_id, bounding_box_json, detection_confidence,"
                " detection_model, detection_model_version, embedding_vector, created_at)"
                " VALUES (:fid, :aid, CAST(:box AS jsonb), 0.95, 'insightface', 'buffalo_l',"
                "         CAST(:v AS vector), NOW())"
            ),
            {"fid": face_id, "aid": asset_id, "box": _json(box), "v": _vec(emb) if emb is not None else None},
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


def _redetect(client, headers, asset_id: str, faces: list[tuple[dict, object]]) -> dict:
    """Submit detections; each embedding is a _unit index or an explicit vector."""
    r = client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "detection_model": "apple_vision",
            "detection_model_version": "2",
            "faces": [
                {
                    "bounding_box": box,
                    "detection_confidence": 0.9,
                    "embedding": _unit(emb) if isinstance(emb, int) else emb,
                }
                for box, emb in faces
            ],
            "lineage": made("faces"),
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
              "media_type": "image", "lineage": ingest_made()},
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
    """A different detector finds the same face with a shifted box: the face
    keeps its id and its confirmed person."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=1)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=1)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect(client, headers, asset_id, [(_box(0.12, 0.11, 0.2, 0.22), _near(1))])

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


def _reject(tenant_url: str, face_id: str, person_id: str) -> None:
    """A person said "not them" (un-assigning the face from them)."""
    with _db(tenant_url) as s:
        s.execute(text("INSERT INTO face_person_rejections (face_id, person_id, created_at)"
                       " VALUES (:f, :p, NOW())"), {"f": face_id, "p": person_id})
        s.commit()


@pytest.mark.slow
def test_redetect_keeps_a_face_someone_said_isnt_a_certain_person(env) -> None:
    """Stricter face settings that don't find it keep the face and its "not
    them"; looser ones that find it again don't put it back on them (Oct 9:
    face settings are two clicks now)."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    alice = _seed_person(tenant_url, emb=91)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=91)
    _reject(tenant_url, face_id, alice)

    stricter = _redetect(client, headers, asset_id, [(_box(0.60, 0.60), 92)])
    assert face_id not in stricter["face_ids"]
    assert _face_exists(tenant_url, face_id) and _rejected(tenant_url, face_id) == {alice}

    looser = _redetect(client, headers, asset_id, [(_box(0.11, 0.10), _near(91)), (_box(0.60, 0.60), 92)])
    assert face_id in looser["face_ids"]  # the same face, found again
    assert _rejected(tenant_url, face_id) == {alice}
    assert (_match(tenant_url, face_id) or (None,))[0] != alice


@pytest.mark.slow
def test_redetect_drops_machine_made_data(env) -> None:
    """Auto-assigned matches are derived data: a face that isn't re-found is
    deleted, and a re-found face's auto match is re-derived."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=5)
    gone = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=5)
    refound = _seed_face_vec(tenant_url, asset_id, _box(0.50, 0.50), _mix(5, 6, 1.6))
    _seed_match(tenant_url, gone, person_id, confirmed=False, confidence=0.9)
    _seed_match(tenant_url, refound, person_id, confirmed=False, confidence=0.9)

    # The re-found face now looks a little less like P: no longer auto-matched.
    result = _redetect(client, headers, asset_id, [(_box(0.50, 0.50), _mix(5, 6, 1.7))])

    assert result["face_ids"] == [refound]
    assert not _face_exists(tenant_url, gone)
    assert _match(tenant_url, refound) is None


@pytest.mark.slow
def test_redetect_never_gives_a_name_to_the_face_next_to_it(env) -> None:
    """Only Bob, standing next to Alice, is found this time: his box overlaps
    hers, but he must not inherit her confirmed name."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    alice = _seed_person(tenant_url, emb=20)
    alice_face = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=20)
    _seed_match(tenant_url, alice_face, alice, confirmed=True, confidence=None)

    result = _redetect(client, headers, asset_id, [(_box(0.18, 0.10), 21)])

    [bob_face] = result["face_ids"]
    assert bob_face != alice_face
    assert _match(tenant_url, bob_face) is None
    assert _match(tenant_url, alice_face) == (alice, True)


@pytest.mark.slow
def test_mirrored_photo_keeps_names_with_their_faces(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    alice, bob = _seed_person(tenant_url, emb=22), _seed_person(tenant_url, emb=23)
    alice_face = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=22)
    bob_face = _seed_face(tenant_url, asset_id, _box(0.60, 0.10), emb=23)
    _seed_match(tenant_url, alice_face, alice, confirmed=True, confidence=None)
    _seed_match(tenant_url, bob_face, bob, confirmed=True, confidence=None)

    # Flipped: Bob now on the left, Alice on the right.
    result = _redetect(
        client, headers, asset_id, [(_box(0.10, 0.10), _near(23)), (_box(0.60, 0.10), _near(22))]
    )

    assert result["face_ids"] == [bob_face, alice_face]
    assert _match(tenant_url, alice_face) == (alice, True)
    assert _match(tenant_url, bob_face) == (bob, True)


@pytest.mark.slow
def test_siblings_side_by_side_keep_their_names(env) -> None:
    """Two similar-looking people (embedding distance 0.5), both boxes
    shifted a little: each keeps their own name, not the neighbour's."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    a_emb = _unit(40)
    b_emb = [0.0] * 512
    b_emb[40], b_emb[41] = 0.5, 0.75 ** 0.5  # cosine 0.5 with a_emb
    a_person, b_person = _seed_person(tenant_url, emb=40), _seed_person(tenant_url, emb=41)
    a_face = _seed_face_vec(tenant_url, asset_id, _box(0.30, 0.10), a_emb)
    b_face = _seed_face_vec(tenant_url, asset_id, _box(0.40, 0.10), b_emb)
    _seed_match(tenant_url, a_face, a_person, confirmed=True, confidence=None)
    _seed_match(tenant_url, b_face, b_person, confirmed=True, confidence=None)

    def near(v: list[float]) -> list[float]:
        w = list(v)
        w[200] = 0.05
        n = sum(x * x for x in w) ** 0.5
        return [x / n for x in w]

    result = _redetect(
        client, headers, asset_id, [(_box(0.36, 0.10), near(a_emb)), (_box(0.46, 0.10), near(b_emb))]
    )

    assert result["face_ids"] == [a_face, b_face]
    assert _match(tenant_url, a_face) == (a_person, True)
    assert _match(tenant_url, b_face) == (b_person, True)


@pytest.mark.slow
def test_lookalike_next_to_a_lost_face_does_not_inherit_it(env) -> None:
    """Alice isn't found this time; her lookalike (distance 0.5) overlaps her
    old box. The lookalike is a new face, not Alice."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    alice = _seed_person(tenant_url, emb=42)
    alice_face = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=42)
    _seed_match(tenant_url, alice_face, alice, confirmed=True, confidence=None)
    lookalike = [0.0] * 512
    lookalike[42], lookalike[43] = 0.5, 0.75 ** 0.5

    result = _redetect(client, headers, asset_id, [(_box(0.22, 0.10), lookalike)])

    assert result["face_ids"] != [alice_face]
    assert _match(tenant_url, alice_face) == (alice, True)


@pytest.mark.slow
def test_tight_box_inside_loose_box_is_the_same_face(env) -> None:
    """Apple Vision's tight box inside InsightFace's loose one overlaps
    little (IoU ~0.16) but is the same face."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=24)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10, 0.30, 0.30), emb=24)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect(client, headers, asset_id, [(_box(0.19, 0.19, 0.12, 0.12), _near(24))])

    assert result["face_ids"] == [face_id]
    assert result["face_count"] == 1


@pytest.mark.slow
def test_faces_without_embeddings_pair_by_solid_overlap(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=30)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=None)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    r = client.post(
        f"/v1/assets/{asset_id}/faces",
        json={"detection_model": "apple_vision", "detection_model_version": "2",
              "faces": [{"bounding_box": _box(0.12, 0.12), "detection_confidence": 0.9}],
              "lineage": made("faces")},
        headers=headers,
    )

    assert r.status_code == 201, r.text
    assert r.json()["face_ids"] == [face_id]
    assert _match(tenant_url, face_id) == (person_id, True)


@pytest.mark.slow
def test_redetect_clears_person_when_machine_match_is_dropped(env) -> None:
    """The re-found face no longer looks like P: its auto match goes, and so
    must faces.person_id and P's pointer to it as representative."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    other_asset = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=25)
    face_id = _seed_face_vec(tenant_url, asset_id, _box(0.10, 0.10), _mix(25, 26, 1.6))
    backup = _seed_face(tenant_url, other_asset, _box(0.10, 0.10), emb=25)
    _seed_match(tenant_url, face_id, person_id, confirmed=False, confidence=0.9)
    _seed_match(tenant_url, backup, person_id, confirmed=False, confidence=0.9)
    with _db(tenant_url) as s:
        s.execute(
            text("UPDATE people SET representative_face_id = :f WHERE person_id = :p"),
            {"f": face_id, "p": person_id},
        )
        s.commit()

    # Same face, same box; the new embedding is a little further from P.
    result = _redetect(client, headers, asset_id, [(_box(0.10, 0.10), _mix(25, 26, 1.7))])

    assert result["face_ids"] == [face_id]
    assert _match(tenant_url, face_id) is None
    with _db(tenant_url) as s:
        assert s.execute(
            text("SELECT person_id FROM faces WHERE face_id = :f"), {"f": face_id}
        ).scalar() is None
        assert s.execute(
            text("SELECT representative_face_id FROM people WHERE person_id = :p"), {"p": person_id}
        ).scalar() == backup


@pytest.mark.slow
def test_rejection_survives_redetect_with_a_different_box(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=27)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10, 0.30, 0.30), emb=27)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{face_id}/assign", headers=headers).status_code == 204

    result = _redetect(client, headers, asset_id, [(_box(0.19, 0.19, 0.12, 0.12), _near(27))])

    assert result["face_ids"] == [face_id]
    assert _match(tenant_url, face_id) is None


@pytest.mark.slow
def test_merge_keeps_target_confirmation_over_source_rejection(env) -> None:
    """F was removed from S, then confirmed as T. Merging S into T must not
    turn "not S" into "not T" and drop T's confirmed face."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    source = _seed_person(tenant_url, emb=44)
    target = _seed_person(tenant_url, emb=45)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=45)
    _seed_match(tenant_url, face_id, source, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{face_id}/assign", headers=headers).status_code == 204
    r = client.post(f"/v1/faces/{face_id}/assign", json={"person_id": target}, headers=headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/v1/people/{target}/merge", json={"source_person_id": source}, headers=headers)
    assert r.status_code == 200, r.text

    assert _match(tenant_url, face_id) == (target, True)
    with _db(tenant_url) as s:
        assert s.execute(
            text("SELECT COUNT(*) FROM face_person_rejections WHERE face_id = :f"), {"f": face_id}
        ).scalar() == 0


def _rejected(tenant_url: str, face_id: str) -> set[str]:
    with _db(tenant_url) as s:
        return {
            row[0] for row in s.execute(
                text("SELECT person_id FROM face_person_rejections WHERE face_id = :f"),
                {"f": face_id},
            )
        }


def _person_of(tenant_url: str, face_id: str) -> str | None:
    with _db(tenant_url) as s:
        return s.execute(
            text("SELECT person_id FROM faces WHERE face_id = :f"), {"f": face_id}
        ).scalar()


@pytest.mark.slow
def test_merge_keeps_rejection_over_machine_match(env) -> None:
    """F was removed from S ("not S"); auto-assign later put F on T. S and T
    are the same person, so merging must not lose the user's "not S"."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    source = _seed_person(tenant_url, emb=46)
    target = _seed_person(tenant_url, emb=47)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=46)
    _seed_match(tenant_url, face_id, source, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{face_id}/assign", headers=headers).status_code == 204
    _seed_match(tenant_url, face_id, target, confirmed=False, confidence=0.7)

    r = client.post(f"/v1/people/{target}/merge", json={"source_person_id": source}, headers=headers)
    assert r.status_code == 200, r.text

    assert _match(tenant_url, face_id) is None
    assert _person_of(tenant_url, face_id) is None
    assert _rejected(tenant_url, face_id) == {target}


@pytest.mark.slow
@pytest.mark.parametrize("into", ["first", "second"])
def test_merge_latest_decision_wins_either_way(env, into) -> None:
    """F was removed from P1, then (later) confirmed as P2. Whichever way
    the two are merged, the later decision stands: F is the merged person."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    p1, p2 = _seed_person(tenant_url, emb=48), _seed_person(tenant_url, emb=49)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=48)
    _seed_match(tenant_url, face_id, p1, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{face_id}/assign", headers=headers).status_code == 204
    r = client.post(f"/v1/faces/{face_id}/assign", json={"person_id": p2}, headers=headers)
    assert r.status_code == 200, r.text
    target, source = (p1, p2) if into == "first" else (p2, p1)

    r = client.post(f"/v1/people/{target}/merge", json={"source_person_id": source}, headers=headers)
    assert r.status_code == 200, r.text

    assert _match(tenant_url, face_id) == (target, True)
    assert _person_of(tenant_url, face_id) == target
    assert _rejected(tenant_url, face_id) == set()


@pytest.mark.slow
@pytest.mark.parametrize("into", ["first", "second"])
def test_merge_later_rejection_wins_either_way(env, into) -> None:
    """F was confirmed as P1, then (later) removed from P2: after merging,
    the later "not this person" stands and F is unassigned."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    p1, p2 = _seed_person(tenant_url, emb=50), _seed_person(tenant_url, emb=51)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=50)
    other = _seed_face(tenant_url, asset_id, _box(0.60, 0.10), emb=50)
    # F on P2 first, removed (rejection "not P2", recorded now)...
    _seed_match(tenant_url, face_id, p2, confirmed=True, confidence=None)
    assert client.delete(f"/v1/faces/{face_id}/assign", headers=headers).status_code == 204
    # ...but that rejection must be LATER than F's confirmation as P1:
    with _db(tenant_url) as s:
        s.execute(
            text("INSERT INTO face_person_matches (match_id, face_id, person_id, confirmed,"
                 " confirmed_at, created_at) VALUES (:m, :f, :p, true,"
                 " now() - interval '1 day', now() - interval '1 day')"),
            {"m": "fpm_" + uuid.uuid4().hex[:16], "f": face_id, "p": p1},
        )
        s.execute(text("UPDATE faces SET person_id = :p WHERE face_id = :f"), {"p": p1, "f": face_id})
        s.commit()
    _seed_match(tenant_url, other, p1, confirmed=True, confidence=None)
    target, source = (p1, p2) if into == "first" else (p2, p1)

    r = client.post(f"/v1/people/{target}/merge", json={"source_person_id": source}, headers=headers)
    assert r.status_code == 200, r.text

    assert _match(tenant_url, face_id) is None
    assert _person_of(tenant_url, face_id) is None
    assert _rejected(tenant_url, face_id) == {target}
    assert _match(tenant_url, other) == (target, True)


def _representative(tenant_url: str, person_id: str) -> str | None:
    with _db(tenant_url) as s:
        return s.execute(
            text("SELECT representative_face_id FROM people WHERE person_id = :p"), {"p": person_id}
        ).scalar()


@pytest.mark.slow
def test_unassigning_the_representative_picks_another_or_none(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=52)
    first = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=52)
    second = _seed_face(tenant_url, asset_id, _box(0.60, 0.10), emb=52)
    _seed_match(tenant_url, first, person_id, confirmed=True, confidence=None)
    _seed_match(tenant_url, second, person_id, confirmed=True, confidence=None)
    with _db(tenant_url) as s:
        s.execute(text("UPDATE people SET representative_face_id = :f WHERE person_id = :p"),
                  {"f": first, "p": person_id})
        s.commit()

    assert client.delete(f"/v1/faces/{first}/assign", headers=headers).status_code == 204
    assert _representative(tenant_url, person_id) == second

    assert client.delete(f"/v1/faces/{second}/assign", headers=headers).status_code == 204
    assert _representative(tenant_url, person_id) is None


@pytest.mark.slow
def test_merge_that_leaves_no_faces_clears_the_representative(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    source = _seed_person(tenant_url, emb=53)
    target = _seed_person(tenant_url, emb=54)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=54)
    _seed_match(tenant_url, face_id, target, confirmed=False, confidence=0.8)
    with _db(tenant_url) as s:
        s.execute(text("UPDATE people SET representative_face_id = :f WHERE person_id = :p"),
                  {"f": face_id, "p": target})
        s.execute(text("INSERT INTO face_person_rejections (face_id, person_id, created_at)"
                       " VALUES (:f, :p, now())"), {"f": face_id, "p": source})
        s.commit()

    r = client.post(f"/v1/people/{target}/merge", json={"source_person_id": source}, headers=headers)
    assert r.status_code == 200, r.text

    assert _match(tenant_url, face_id) is None
    assert _representative(tenant_url, target) is None


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
def test_upkeep_deletes_empty_dismissed_people_and_nothing_a_person_named(env) -> None:
    """A faces redo no longer runs the cleanup: upkeep does, for the account,
    and only dismissed people nothing is assigned to and nobody named."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=12)
    with _db(tenant_url) as s:
        repo = PersonRepository(s)
        empty = repo.create_dismissed(face_ids=[]).person_id
        named = repo.create_dismissed(face_ids=[]).person_id
        holding = repo.create_dismissed(face_ids=[face_id]).person_id
    with _db(tenant_url) as s:
        s.execute(text("UPDATE people SET display_name = 'Uncle Bob' WHERE person_id = :pid"), {"pid": named})
        s.commit()
    nobody = _seed_person(tenant_url, emb=13)  # not dismissed, no faces

    r = client.post("/v1/upkeep", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["dismissed_people"]["deleted"] == 1
    with _db(tenant_url) as s:
        left = {row[0] for row in s.execute(text("SELECT person_id FROM people")).all()}
    assert empty not in left
    assert {named, holding, nobody} <= left
    assert _match(tenant_url, face_id) == (holding, True)


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
def test_missing_file_is_restored_when_it_reappears(env) -> None:
    client, headers, library_id, _ = env
    rel_path = "trash/missing-missing.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]

    body: dict = {"asset_ids": [asset_id], "reason": "missing"}
    r = client.request("DELETE", "/v1/assets", json=body, headers=headers)
    assert r.json()["trashed"] == [asset_id]
    assert rel_path not in _ignored(client, headers, library_id)

    r = _ingest(client, headers, library_id, rel_path)
    assert r.status_code == 200, r.text
    assert r.json()["asset_id"] == asset_id
    assert client.get(f"/v1/assets/{asset_id}", headers=headers).status_code == 200


@pytest.mark.slow
def test_user_can_trash_a_missing_asset(env) -> None:
    """A file the scanner marked missing can still be trashed by the user,
    and then it stays trashed when the drive comes back."""
    client, headers, library_id, _ = env
    rel_path = "trash/missing-then-trashed.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]
    client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"},
                   headers=headers)

    assert client.delete(f"/v1/assets/{asset_id}", headers=headers).status_code == 204

    assert _ignored(client, headers, library_id).get(rel_path) == "trashed"
    assert _ingest(client, headers, library_id, rel_path).status_code == 409


@pytest.mark.slow
def test_batch_user_trash_upgrades_missing(env) -> None:
    client, headers, library_id, _ = env
    rel_path = "trash/batch-upgrade.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]
    client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"},
                   headers=headers)

    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "user"},
                       headers=headers)

    assert r.json()["trashed"] == [asset_id]
    assert _ignored(client, headers, library_id).get(rel_path) == "trashed"


@pytest.mark.slow
def test_empty_trash_without_ids_spares_missing_files(env) -> None:
    """Emptying the trash purges what the user trashed, never files that are
    only missing (an unplugged drive): those keep their human data."""
    client, headers, library_id, _ = env
    missing = _ingest(client, headers, library_id, "trash/spare-missing.jpg").json()["asset_id"]
    trashed = _ingest(client, headers, library_id, "trash/spare-trashed.jpg").json()["asset_id"]
    client.request("DELETE", "/v1/assets", json={"asset_ids": [missing], "reason": "missing"},
                   headers=headers)
    client.delete(f"/v1/assets/{trashed}", headers=headers)

    r = client.request("DELETE", "/v1/trash/empty", json={"all": True}, headers=headers)

    assert r.status_code == 200, r.text
    assert _ignored(client, headers, library_id).get("trash/spare-trashed.jpg") == "emptied"
    back = _ingest(client, headers, library_id, "trash/spare-missing.jpg")
    assert back.status_code == 200
    assert back.json()["asset_id"] == missing  # the same asset, restored


@pytest.mark.slow
def test_trash_matching_filter_is_user_trash(env) -> None:
    """Adding an exclude filter with "trash matching" is the user trashing
    those assets: they stay trashed when the filter is removed."""
    client, headers, library_id, _ = env
    rel_path = "filtered/junk.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]

    r = client.post(
        f"/v1/libraries/{library_id}/filters",
        json={"type": "exclude", "pattern": "filtered/**", "trash_matching": True},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    filter_id = r.json()["filter_id"]
    assert client.get(f"/v1/assets/{asset_id}", headers=headers).status_code == 404
    client.delete(f"/v1/libraries/{library_id}/filters/{filter_id}", headers=headers)

    assert _ignored(client, headers, library_id).get(rel_path) == "trashed"
    assert _ingest(client, headers, library_id, rel_path).status_code == 409


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
def test_a_missing_clip_is_deleted_for_good_only_through_the_trash(env) -> None:
    """Archived because its file went missing: emptying the trash leaves it
    alone. Moved to the trash first, it goes, and is remembered like any
    trash a person emptied (Robert's model, Oct 8)."""
    client, headers, library_id, _ = env
    rel_path = "trash/emptied-missing.jpg"
    asset_id = _ingest(client, headers, library_id, rel_path).json()["asset_id"]
    client.request(
        "DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"},
        headers=headers,
    )
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=headers)
    assert r.json()["deleted"] == 0

    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "user"}, headers=headers)
    assert r.json()["trashed"] == [asset_id]
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=headers)
    assert r.json()["deleted"] == 1
    assert rel_path in _ignored(client, headers, library_id)


@pytest.mark.slow
def test_emptying_the_trash_keeps_archived_files_even_when_named(env) -> None:
    """Only what a person trashed is deleted for good, unless missing files are asked for by name
    and include_missing says so (Robert's call, Oct 8): an archived file may still come back."""
    client, headers, library_id, _ = env
    asset_id = _ingest(client, headers, library_id, "trash/archived-named.jpg").json()["asset_id"]
    client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"}, headers=headers)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 0
    # Still archived: it comes back when the file does.
    assert _ingest(client, headers, library_id, "trash/archived-named.jpg").json()["asset_id"] == asset_id


@pytest.mark.slow
def test_server_stores_nfc_paths(env) -> None:
    """Whatever form a client sends, one file has one rel_path (NFC)."""
    import unicodedata

    client, headers, library_id, _ = env
    nfc = unicodedata.normalize("NFC", "nfc/Café.jpg")
    nfd = unicodedata.normalize("NFD", nfc)

    created = _ingest(client, headers, library_id, nfd)
    again = _ingest(client, headers, library_id, nfc)

    assert created.status_code == again.status_code == 200
    assert again.json()["asset_id"] == created.json()["asset_id"]
    r = client.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": nfd},
                   headers=headers)
    assert r.status_code == 200
    assert r.json()["rel_path"] == nfc


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
            json={"srt": srt, "language": "en", "source": source,
                  **({} if source == "manual" else {"lineage": made("transcript")})},
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
    assert client.delete(f"/v1/assets/{asset_id}/transcript", params={"which": "manual"}, headers=headers).status_code == 204
    assert post(_SRT_WHISPER, "whisper") == "transcribed"


# ---------------------------------------------------------------------------
# A face-model switch (ADR-016 phase 3, piece 4): embeddings from different
# models aren't comparable, so names are re-anchored by box overlap alone.
# ---------------------------------------------------------------------------


def _redetect_with(client, headers, asset_id: str, faces: list[tuple[dict, object]], embedding_model: str) -> dict:
    r = client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "detection_model": "insightface",
            "detection_model_version": embedding_model,
            "embedding_model": embedding_model,
            "faces": [{"bounding_box": box, "detection_confidence": 0.9,
                       "embedding": _unit(emb) if isinstance(emb, int) else emb} for box, emb in faces],
            "lineage": made("faces"),
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _embedding_model(tenant_url: str, face_id: str) -> str | None:
    with _db(tenant_url) as s:
        return s.execute(text("SELECT embedding_model FROM faces WHERE face_id = :f"), {"f": face_id}).scalar()


@pytest.mark.slow
def test_a_named_face_keeps_its_name_through_a_face_model_switch(env) -> None:
    """The new model's embedding of the same face looks nothing like the old
    one's (another space); the box is where it was. The name stays."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=1)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=1)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)
    assert _embedding_model(tenant_url, face_id) == "buffalo_l"  # every face so far

    result = _redetect_with(client, headers, asset_id, [(_box(0.11, 0.10), 300)], "antelopev2")

    assert result["face_ids"] == [face_id] and result["face_count"] == 1
    assert _match(tenant_url, face_id) == (person_id, True)
    assert _embedding_model(tenant_url, face_id) == "antelopev2"


@pytest.mark.slow
def test_across_a_switch_only_a_solid_overlap_carries_a_name(env) -> None:
    """Without comparable embeddings, of two faces the new model finds, the
    one beside the named face (a little overlap) doesn't inherit the name;
    the one where it was does."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=2)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=2)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.25, 0.10), 2), (_box(0.11, 0.11), 9)],
                            "antelopev2")

    assert result["face_ids"][1] == face_id and result["face_ids"][0] != face_id
    assert _match(tenant_url, face_id) == (person_id, True)


@pytest.mark.slow
def test_across_a_switch_a_named_face_not_found_again_is_kept(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=6)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=6)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.25, 0.10), 6), (_box(0.70, 0.70), 7)],
                            "antelopev2")

    assert face_id not in result["face_ids"]
    assert _face_exists(tenant_url, face_id)  # a confirmed face that isn't found again is kept
    assert _match(tenant_url, face_id) == (person_id, True)


@pytest.mark.slow
def test_across_a_switch_a_name_carries_only_on_a_substantial_overlap(env) -> None:
    """Robert, Oct 9: stricter than boxes touching. The new model's box over
    most of the named one (IoU 0.64) is the same face; one that only
    overlaps a little (IoU 0.16, a tight box in a corner) is another face,
    and the named one is kept beside it."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=3)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=3)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.15, 0.15, 0.08, 0.08), 301)], "antelopev2")
    assert face_id not in result["face_ids"]
    assert _match(tenant_url, face_id) == (person_id, True)  # kept, beside the new face

    other = _seed_asset(tenant_url, library_id)
    named = _seed_face(tenant_url, other, _box(0.10, 0.10), emb=3)
    _seed_match(tenant_url, named, person_id, confirmed=True, confidence=None)
    result = _redetect_with(client, headers, other, [(_box(0.12, 0.12, 0.16, 0.16), 301)], "antelopev2")
    assert result["face_ids"] == [named]
    assert _match(tenant_url, named) == (person_id, True)


@pytest.mark.slow
def test_one_and_one_in_different_places_are_different_faces(env) -> None:
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=4)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=4)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.60, 0.60), 302)], "antelopev2")

    assert face_id not in result["face_ids"]
    assert _match(tenant_url, face_id) == (person_id, True)  # kept, beside the new face


@pytest.mark.slow
def test_one_and_one_with_the_same_model_still_listens_to_the_embeddings(env) -> None:
    """With comparable embeddings that disagree, it isn't the same face."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=5)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=5)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.15, 0.15, 0.08, 0.08), 303)], "buffalo_l")

    assert face_id not in result["face_ids"]


@pytest.mark.slow
def test_one_and_one_without_embeddings_and_no_switch_needs_a_solid_overlap(env) -> None:
    """The loose one-and-one rule is for a face-model switch only. With the
    same model and no embeddings, a small overlap isn't the same face."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=6)
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=None)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.15, 0.15, 0.08, 0.08), None)], "buffalo_l")

    assert face_id not in result["face_ids"]
    assert _match(tenant_url, face_id) == (person_id, True)  # kept, beside the new face


@pytest.mark.slow
def test_a_name_carried_across_a_switch_teaches_the_new_model_that_person(env) -> None:
    """Robert's idea, end to end. The account switches its face model. A
    picture whose one face was named is found again by the new model: the
    face keeps the name and the person's centroid is rebuilt from the new
    model's embedding of it. Then the new model finds that person in a
    picture nobody has named, and suggests them."""
    client, headers, library_id, tenant_url = env
    with _db(tenant_url) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at)"
                       " VALUES ('producer.faces', '{\"model\": \"antelopev2\"}', now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"))
        s.commit()
    try:
        named = _seed_asset(tenant_url, library_id)
        person_id = _seed_person(tenant_url, emb=10)
        face_id = _seed_face(tenant_url, named, _box(0.10, 0.10), emb=10)
        _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

        # The new model sees the same face as _unit(400): another space.
        _redetect_with(client, headers, named, [(_box(0.12, 0.11), 400)], "antelopev2")
        with _db(tenant_url) as s:
            model = s.execute(text("SELECT centroid_model FROM people WHERE person_id = :p"),
                              {"p": person_id}).scalar()
        assert model == "antelopev2"

        # A picture nobody named: the new model finds a face close to that one.
        other = _seed_asset(tenant_url, library_id)
        result = _redetect_with(client, headers, other, [(_box(0.40, 0.40), _near(400))], "antelopev2")
        assert _match(tenant_url, result["face_ids"][0]) == (person_id, False)  # suggested, not confirmed
    finally:
        with _db(tenant_url) as s:
            s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.faces'"))
            s.commit()


@pytest.mark.slow
def test_faces_of_another_model_are_never_matched_to_this_ones_people(env) -> None:
    """Before the account switches, a face from another model isn't
    compared with people's centroids (another space)."""
    client, headers, library_id, tenant_url = env
    person_id = _seed_person(tenant_url, emb=11)
    other = _seed_asset(tenant_url, library_id)
    result = _redetect_with(client, headers, other, [(_box(0.40, 0.40), _near(11))], "antelopev2")
    assert _match(tenant_url, result["face_ids"][0]) is None
    assert person_id


@pytest.mark.slow
def test_a_named_face_given_its_first_embedding_teaches_its_person(env) -> None:
    """A Mac without the face model named a face it couldn't embed; the CLI
    finds it again with an embedding (same model): the person's centroid is
    built from it, so they're suggested elsewhere."""
    client, headers, library_id, tenant_url = env
    asset_id = _seed_asset(tenant_url, library_id)
    person_id = _seed_person(tenant_url, emb=13)
    with _db(tenant_url) as s:
        s.execute(text("UPDATE people SET centroid_vector = NULL WHERE person_id = :p"), {"p": person_id})
        s.commit()
    face_id = _seed_face(tenant_url, asset_id, _box(0.10, 0.10), emb=None)
    _seed_match(tenant_url, face_id, person_id, confirmed=True, confidence=None)

    result = _redetect_with(client, headers, asset_id, [(_box(0.10, 0.10), 13)], "buffalo_l")

    assert result["face_ids"] == [face_id]
    with _db(tenant_url) as s:
        has_centroid = s.execute(text("SELECT centroid_vector IS NOT NULL FROM people WHERE person_id = :p"),
                                 {"p": person_id}).scalar()
    assert has_centroid


@pytest.mark.slow
def test_mid_switch_a_face_the_new_model_hasnt_seen_gets_no_ranked_suggestions(env) -> None:
    """After the account switches, a face the new model hasn't found again
    is in another space than the people's centroids: ranking people by
    distance to it would be noise. The popover falls back to its plain list."""
    client, headers, library_id, tenant_url = env
    with _db(tenant_url) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at)"
                       " VALUES ('producer.faces', '{\"model\": \"antelopev2\"}', now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"))
        s.commit()
    try:
        person_id = _seed_person(tenant_url, emb=12)
        with _db(tenant_url) as s:
            s.execute(text("UPDATE people SET centroid_model = 'antelopev2' WHERE person_id = :p"), {"p": person_id})
            s.commit()
        face_id = _seed_face(tenant_url, _seed_asset(tenant_url, library_id), _box(0.10, 0.10), emb=12)

        r = client.get(f"/v1/faces/{face_id}/nearest-people", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == []
    finally:
        with _db(tenant_url) as s:
            s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.faces'"))
            s.commit()
