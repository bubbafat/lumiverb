"""API tests for the asset submission surface (vision/ocr/transcript/note/
embedding/face/move endpoints), including their batch counterparts.

These exercise the largest uncovered branches in
``src/server/api/routers/assets.py``: every single-asset submission endpoint
has a batch sibling, and the batch siblings + transcript/note flows had no
coverage at all before this file.

The tests share one tenant DB across the module to keep startup cost down,
following the same pattern as ``test_assets_api.py``.
"""

from __future__ import annotations

import os
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines

from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


# ---- fixture --------------------------------------------------------------


@pytest.fixture(scope="module")
def submission_client() -> Iterator[tuple[TestClient, dict[str, str], str, list[str], str]]:
    """Yield (client, auth_headers, library_id, image_asset_ids, video_asset_id).

    Three image assets are created so the batch endpoints have something to
    iterate over, plus one video asset for transcript tests.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SubmissionTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                api_key = r.json()["api_key"]
                tenant_id = r.json()["tenant_id"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "SubmissionLib", "root_path": "/photos"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                image_asset_ids: list[str] = []
                for i, rel_path in enumerate(["sub_a.jpg", "sub_b.jpg", "sub_c.jpg"]):
                    r_up = client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": rel_path,
                            "file_size": 2000 + i,
                            "file_mtime": "2025-02-01T12:00:00Z",
                            "media_type": "image",
                        },
                        headers=auth,
                    )
                    assert r_up.status_code == 200
                    r_by = client.get(
                        "/v1/assets/by-path",
                        params={"library_id": library_id, "rel_path": rel_path},
                        headers=auth,
                    )
                    image_asset_ids.append(r_by.json()["asset_id"])

                r_vid = client.post(
                    "/v1/assets/upsert",
                    json={
                        "library_id": library_id,
                        "rel_path": "sub_video.mp4",
                        "file_size": 50_000,
                        "file_mtime": "2025-02-01T12:00:00Z",
                        "media_type": "video",
                    },
                    headers=auth,
                )
                assert r_vid.status_code == 200
                r_by = client.get(
                    "/v1/assets/by-path",
                    params={"library_id": library_id, "rel_path": "sub_video.mp4"},
                    headers=auth,
                )
                video_asset_id = r_by.json()["asset_id"]

                yield client, auth, library_id, image_asset_ids, video_asset_id

        _engines.clear()


# ---- vision (single) ------------------------------------------------------


@pytest.mark.slow
def test_submit_vision_happy_path(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client
    asset_id = image_asset_ids[0]

    r = client.post(
        f"/v1/assets/{asset_id}/vision",
        json={
            "model_id": "moondream2",
            "model_version": "2024-08-26",
            "description": "A red barn at sunset",
            "tags": ["barn", "rural", "sunset"],
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "described"


@pytest.mark.slow
def test_submit_vision_404(submission_client) -> None:
    client, auth, _, _, _ = submission_client
    r = client.post(
        "/v1/assets/ast_doesnotexist0000000000000/vision",
        json={"model_id": "moondream2", "description": "x"},
        headers=auth,
    )
    assert r.status_code == 404


# ---- ocr (single) ---------------------------------------------------------


@pytest.mark.slow
def test_submit_ocr_requires_vision_metadata_first(submission_client) -> None:
    """OCR submission for an asset with no prior vision metadata returns 400."""
    client, auth, _, image_asset_ids, _ = submission_client
    # sub_b has not had vision submitted
    asset_id = image_asset_ids[1]

    r = client.post(
        f"/v1/assets/{asset_id}/ocr",
        json={"ocr_text": "hello"},
        headers=auth,
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_submit_ocr_after_vision(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client
    asset_id = image_asset_ids[0]  # already has vision metadata from above

    r = client.post(
        f"/v1/assets/{asset_id}/ocr",
        json={"ocr_text": "STOP SIGN"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["ocr_text"] == "STOP SIGN"


@pytest.mark.slow
def test_submit_ocr_404(submission_client) -> None:
    client, auth, _, _, _ = submission_client
    r = client.post(
        "/v1/assets/ast_doesnotexist0000000000000/ocr",
        json={"ocr_text": "x"},
        headers=auth,
    )
    assert r.status_code == 404


# ---- batch ocr ------------------------------------------------------------


@pytest.mark.slow
def test_batch_ocr_mixed_present_and_missing(submission_client) -> None:
    """Batch OCR updates assets that already have vision metadata, skips others."""
    client, auth, _, image_asset_ids, _ = submission_client

    # Make sure sub_c has vision metadata so we can update it
    r = client.post(
        f"/v1/assets/{image_asset_ids[2]}/vision",
        json={"model_id": "moondream2", "description": "a stop sign"},
        headers=auth,
    )
    assert r.status_code == 200

    payload = {
        "items": [
            {"asset_id": image_asset_ids[0], "ocr_text": "BATCH ONE"},
            {"asset_id": image_asset_ids[2], "ocr_text": "BATCH THREE"},
            # asset without vision metadata → skipped
            {"asset_id": image_asset_ids[1], "ocr_text": "BATCH TWO SKIPPED"},
            # nonexistent → skipped
            {"asset_id": "ast_nonex0000000000000000000", "ocr_text": "skip"},
        ]
    }
    r = client.post("/v1/assets/batch-ocr", json=payload, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 2
    assert body["skipped"] == 2


# ---- batch vision ---------------------------------------------------------


@pytest.mark.slow
def test_batch_vision_updates_and_skips(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client

    payload = {
        "items": [
            {
                "asset_id": image_asset_ids[1],
                "model_id": "moondream2",
                "model_version": "1",
                "description": "second image",
                "tags": ["t1"],
            },
            {
                "asset_id": "ast_doesnotexist0000000000000",
                "model_id": "moondream2",
                "model_version": "1",
                "description": "skipped",
                "tags": [],
            },
        ]
    }
    r = client.post("/v1/assets/batch-vision", json=payload, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped"] == 1


# ---- batch embeddings -----------------------------------------------------


@pytest.mark.slow
def test_batch_embeddings_updates_and_skips(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client

    payload = {
        "items": [
            {
                "asset_id": image_asset_ids[0],
                "model_id": "clip",
                "model_version": "1",
                "vector": [0.1] * 512,
            },
            {
                "asset_id": "ast_doesnotexist0000000000000",
                "model_id": "clip",
                "model_version": "1",
                "vector": [0.2] * 512,
            },
        ]
    }
    r = client.post("/v1/assets/batch-embeddings", json=payload, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped"] == 1


@pytest.mark.slow
def test_submit_embedding_single_404(submission_client) -> None:
    client, auth, _, _, _ = submission_client
    r = client.post(
        "/v1/assets/ast_doesnotexist0000000000000/embeddings",
        json={"model_id": "clip", "model_version": "1", "vector": [0.1] * 512},
        headers=auth,
    )
    assert r.status_code == 404


# ---- batch moves ----------------------------------------------------------


@pytest.mark.slow
def test_batch_moves_updates_rel_path(submission_client) -> None:
    client, auth, library_id, image_asset_ids, _ = submission_client

    new_path = "moved/sub_b_renamed.jpg"
    payload = {
        "items": [
            {"asset_id": image_asset_ids[1], "rel_path": new_path},
            {"asset_id": "ast_doesnotexist0000000000000", "rel_path": "x.jpg"},
        ]
    }
    r = client.post("/v1/assets/batch-moves", json=payload, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped"] == 1

    # Confirm the move actually happened
    r2 = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": new_path},
        headers=auth,
    )
    assert r2.status_code == 200
    assert r2.json()["asset_id"] == image_asset_ids[1]


# ---- batch faces ----------------------------------------------------------


@pytest.mark.slow
def test_batch_faces_processes_and_normalizes_corners(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client

    # One asset uses {x,y,w,h}, the other uses {x1,y1,x2,y2} corner format —
    # exercises _normalize_bounding_box on the batch path.
    payload = {
        "items": [
            {
                "asset_id": image_asset_ids[0],
                "detection_model": "insightface",
                "detection_model_version": "buffalo_l",
                "faces": [
                    {
                        "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                        "detection_confidence": 0.99,
                        "embedding": [0.1] * 512,
                    }
                ],
            },
            {
                "asset_id": image_asset_ids[1],
                "detection_model": "apple_vision",
                "detection_model_version": "1",
                "faces": [
                    {
                        "bounding_box": {"x1": 0.2, "y1": 0.2, "x2": 0.4, "y2": 0.5},
                        "detection_confidence": 0.95,
                        "embedding": [0.2] * 512,
                    }
                ],
            },
            {
                "asset_id": "ast_doesnotexist0000000000000",
                "faces": [
                    {
                        "bounding_box": {"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1},
                        "detection_confidence": 0.5,
                    }
                ],
            },
        ]
    }
    r = client.post("/v1/assets/batch-faces", json=payload, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 2
    assert body["skipped"] == 1

    # Verify the corner-format bounding box was normalized
    r2 = client.get(f"/v1/assets/{image_asset_ids[1]}/faces", headers=auth)
    assert r2.status_code == 200
    faces = r2.json()["faces"]
    assert len(faces) == 1
    bb = faces[0]["bounding_box"]
    assert {"x", "y", "w", "h"} <= set(bb.keys())
    assert abs(bb["w"] - 0.2) < 1e-6
    assert abs(bb["h"] - 0.3) < 1e-6


# ---- transcript -----------------------------------------------------------


_VALID_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:02,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:02,500 --> 00:00:04,000\n"
    "Second line\n"
)


@pytest.mark.slow
def test_submit_transcript_rejects_image(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client
    r = client.post(
        f"/v1/assets/{image_asset_ids[0]}/transcript",
        json={"srt": _VALID_SRT, "language": "en"},
        headers=auth,
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_submit_transcript_404(submission_client) -> None:
    client, auth, _, _, _ = submission_client
    r = client.post(
        "/v1/assets/ast_doesnotexist0000000000000/transcript",
        json={"srt": _VALID_SRT},
        headers=auth,
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_submit_transcript_invalid_srt(submission_client) -> None:
    client, auth, _, _, video_asset_id = submission_client
    r = client.post(
        f"/v1/assets/{video_asset_id}/transcript",
        json={"srt": "not a real srt at all", "language": "en"},
        headers=auth,
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_submit_empty_transcript_marks_no_speech(submission_client) -> None:
    client, auth, _, _, video_asset_id = submission_client
    r = client.post(
        f"/v1/assets/{video_asset_id}/transcript",
        json={"srt": "   ", "language": "en"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "no_speech"


@pytest.mark.slow
def test_submit_then_delete_transcript(submission_client) -> None:
    client, auth, _, _, video_asset_id = submission_client

    # Best-effort Quickwit indexing should swallow connection errors so the
    # endpoint still returns 200 even without a quickwit sidecar running.
    r = client.post(
        f"/v1/assets/{video_asset_id}/transcript",
        json={"srt": _VALID_SRT, "language": "en"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "transcribed"

    # Now delete the transcript
    r = client.delete(f"/v1/assets/{video_asset_id}/transcript", headers=auth)
    assert r.status_code == 204

    # Deleting a transcript that does not exist on a real asset succeeds (idempotent)
    r = client.delete(f"/v1/assets/{video_asset_id}/transcript", headers=auth)
    assert r.status_code == 204

    # And 404 on unknown asset
    r = client.delete(
        "/v1/assets/ast_doesnotexist0000000000000/transcript",
        headers=auth,
    )
    assert r.status_code == 404


# ---- notes ----------------------------------------------------------------


@pytest.mark.slow
def test_note_set_clear_and_delete(submission_client) -> None:
    client, auth, _, image_asset_ids, _ = submission_client
    asset_id = image_asset_ids[2]

    # Set a note
    r = client.put(
        f"/v1/assets/{asset_id}/note",
        json={"text": "  this is a note  "},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["note"] == "this is a note"
    assert body["note_author"] is not None
    assert body["note_updated_at"] is not None

    # Clear via empty text
    r = client.put(
        f"/v1/assets/{asset_id}/note",
        json={"text": "   "},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["note"] is None

    # DELETE on already-cleared note still works
    r = client.delete(f"/v1/assets/{asset_id}/note", headers=auth)
    assert r.status_code == 204

    # 404 on unknown asset
    r = client.put(
        "/v1/assets/ast_doesnotexist0000000000000/note",
        json={"text": "x"},
        headers=auth,
    )
    assert r.status_code == 404
    r = client.delete("/v1/assets/ast_doesnotexist0000000000000/note", headers=auth)
    assert r.status_code == 404
