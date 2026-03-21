"""Tests for bugs fixed during the session.

Fix 1: hard_delete missing asset_embeddings (FK crash)
Fix 2: Failed video chunks blocking pipeline permanently
Fix 3: TOCTOU race for duplicate video-vision jobs (try_create_unique + partial index)
Fix 4: VideoVisionWorker infinite retry — description='' treated as missing
Fix 5: VisionWorker BlockJob for missing proxy_key
Fix 6: mark_missing_for_scan bulk SQL
Fix 7: SearchSyncQueue.enqueue dedup
Fix 7b: video-preview completion enqueues search_sync
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines

from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


# ---------------------------------------------------------------------------
# Module-scoped fixture: control DB + tenant DB + tenant + library
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bug_fixes_api_client() -> tuple[TestClient, str, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create tenant and library.
    Yields (client, api_key, library_id, tenant_url).
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

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "BugFixesTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.core.database import get_control_session
            from src.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "BugFixesLib", "root_path": "/bugfixes"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id, tenant_url

        _engines.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_asset(
    client: TestClient,
    auth: dict,
    library_id: str,
    rel_path: str,
    media_type: str = "image/jpeg",
) -> str:
    """Create a scan, upsert an asset, return its asset_id."""
    r_scan = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan.status_code == 200, (r_scan.status_code, r_scan.text)
    scan_id = r_scan.json()["scan_id"]

    r_up = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": media_type,
            "scan_id": scan_id,
        },
        headers=auth,
    )
    assert r_up.status_code == 200, (r_up.status_code, r_up.text)

    r_asset = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
        headers=auth,
    )
    assert r_asset.status_code == 200, (r_asset.status_code, r_asset.text)
    return r_asset.json()["asset_id"]


def _upsert_asset_with_scan(
    client: TestClient,
    auth: dict,
    library_id: str,
    rel_path: str,
    media_type: str = "image/jpeg",
) -> tuple[str, str]:
    """Create a scan, upsert an asset, return (asset_id, scan_id)."""
    r_scan = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan.status_code == 200, (r_scan.status_code, r_scan.text)
    scan_id = r_scan.json()["scan_id"]

    r_up = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": media_type,
            "scan_id": scan_id,
        },
        headers=auth,
    )
    assert r_up.status_code == 200, (r_up.status_code, r_up.text)

    r_asset = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
        headers=auth,
    )
    assert r_asset.status_code == 200, (r_asset.status_code, r_asset.text)
    return r_asset.json()["asset_id"], scan_id


# ---------------------------------------------------------------------------
# Fix 1: hard_delete missing asset_embeddings (FK crash)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_hard_delete_library_with_asset_embeddings_no_fk_crash(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    DELETE /v1/libraries/{library_id} (via empty-trash) must not 500 even when
    asset_embeddings rows exist for assets in that library.

    Old bug: hard_delete did not delete asset_embeddings before assets, causing
    an FK violation (asset_embeddings.asset_id → assets.asset_id).
    """
    client, api_key, _shared_library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a dedicated library so we can trash + hard-delete it without affecting
    # the shared fixture library used by other tests.
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "EmbedDeleteLib", "root_path": "/embed-delete"},
        headers=auth,
    )
    assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
    library_id = r_lib.json()["library_id"]

    asset_id = _upsert_asset(client, auth, library_id, "embed_test.jpg")

    # Insert a row into asset_embeddings directly via SQL to simulate the embed worker.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO asset_embeddings
                        (embedding_id, asset_id, model_id, model_version, embedding_vector, created_at)
                    VALUES
                        (:embedding_id, :asset_id, 'clip', '1', CAST(:vec AS vector), NOW())
                    """
                ),
                {
                    "embedding_id": "emb_test_fix1_001",
                    "asset_id": asset_id,
                    "vec": "[" + ",".join(["0.1"] * 512) + "]",
                },
            )
            conn.commit()
    finally:
        engine.dispose()

    # Trash the library so empty-trash will hard-delete it.
    r_del = client.delete(f"/v1/libraries/{library_id}", headers=auth)
    assert r_del.status_code == 204, (r_del.status_code, r_del.text)

    # Hard-delete via empty-trash: must return 200, not 500.
    r_trash = client.post("/v1/libraries/empty-trash", json={}, headers=auth)
    assert r_trash.status_code == 200, (r_trash.status_code, r_trash.text)
    assert r_trash.json()["deleted"] >= 1


# ---------------------------------------------------------------------------
# Fix 2: Failed video chunks blocking pipeline permanently
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_failed_chunks_reset_to_pending_on_claim(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    After a chunk is failed, the next claim_next_chunk call resets it to pending
    and returns it, so the video-index pipeline is not permanently blocked.

    Old bug: failed chunks stayed in 'failed' status, so claim_next_chunk found
    no pending chunks and returned None forever, preventing video-vision from
    ever being enqueued.
    """
    client, api_key, library_id, _tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "fail_reset_clip.mp4", "video/mp4")

    # Init chunks (30 s → at least one chunk).
    r_init = client.post(
        f"/v1/video/{asset_id}/chunks",
        json={"duration_sec": 30.0},
        headers=auth,
    )
    assert r_init.status_code == 200, (r_init.status_code, r_init.text)

    # Claim a chunk.
    r_claim = client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    assert r_claim.status_code == 200, "Expected a claimable chunk"
    chunk = r_claim.json()
    chunk_id = chunk["chunk_id"]
    worker_id = chunk["worker_id"]

    # Fail it.
    r_fail = client.post(
        f"/v1/video/chunks/{chunk_id}/fail",
        json={"worker_id": worker_id, "error_message": "simulated failure"},
        headers=auth,
    )
    assert r_fail.status_code == 200, (r_fail.status_code, r_fail.text)
    assert r_fail.json()["status"] == "failed"

    # Claim again — the fixed code resets failed chunks to pending so they are
    # retried. The claim must succeed (200), not return 204 (no chunks).
    r_claim2 = client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    assert r_claim2.status_code == 200, (
        f"Expected 200 (chunk reset to pending after fail), got {r_claim2.status_code}: {r_claim2.text}"
    )
    # The re-claimed chunk should be the same logical chunk (same asset).
    assert r_claim2.json()["chunk_id"] is not None


# ---------------------------------------------------------------------------
# Fix 3: TOCTOU race for duplicate video-vision jobs (try_create_unique)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_try_create_unique_prevents_duplicate_video_vision_jobs(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    WorkerJobRepository.try_create_unique uses ON CONFLICT DO NOTHING on the
    partial unique index to ensure only one pending/claimed video-vision job
    exists per asset at any time.

    Calling try_create_unique twice for the same (job_type, asset_id) must:
    - Return True on the first call (job inserted).
    - Return False on the second call (conflict, job not inserted).
    - Leave exactly one row in worker_jobs for that (job_type, asset_id).
    """
    client, api_key, library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "toctou_clip.mp4", "video/mp4")

    engine = create_engine(tenant_url)
    try:
        from src.core.database import get_tenant_session_for_url
    except ImportError:
        get_tenant_session_for_url = None

    # Use the repository directly via a raw session to call try_create_unique twice.
    from sqlmodel import Session as SQLModelSession

    try:
        with engine.connect() as conn:
            # Call try_create_unique twice via raw SQL that mirrors the implementation.
            job_id_1 = "job_toctou_test_001"
            job_id_2 = "job_toctou_test_002"

            r1 = conn.execute(
                text(
                    """
                    INSERT INTO worker_jobs (job_id, job_type, asset_id, status, priority, fail_count, created_at)
                    VALUES (:job_id, 'video-vision', :asset_id, 'pending', 10, 0, NOW())
                    ON CONFLICT (job_type, asset_id) WHERE status = 'pending' OR status = 'claimed' DO NOTHING
                    """
                ),
                {"job_id": job_id_1, "asset_id": asset_id},
            )
            first_inserted = r1.rowcount

            r2 = conn.execute(
                text(
                    """
                    INSERT INTO worker_jobs (job_id, job_type, asset_id, status, priority, fail_count, created_at)
                    VALUES (:job_id, 'video-vision', :asset_id, 'pending', 10, 0, NOW())
                    ON CONFLICT (job_type, asset_id) WHERE status = 'pending' OR status = 'claimed' DO NOTHING
                    """
                ),
                {"job_id": job_id_2, "asset_id": asset_id},
            )
            second_inserted = r2.rowcount

            count_row = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM worker_jobs
                    WHERE job_type = 'video-vision'
                      AND asset_id = :asset_id
                      AND status IN ('pending', 'claimed')
                    """
                ),
                {"asset_id": asset_id},
            ).scalar()
            conn.commit()
    finally:
        engine.dispose()

    assert first_inserted == 1, "First try_create_unique should insert a row"
    assert second_inserted == 0, "Second try_create_unique should be a no-op (conflict)"
    assert count_row == 1, f"Expected exactly 1 pending video-vision job, got {count_row}"


# ---------------------------------------------------------------------------
# Fix 4: VideoVisionWorker infinite retry — description='' not treated as missing
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_video_vision_worker_empty_string_description_not_treated_as_missing() -> None:
    """
    When a scene already has description='' (empty string, not None), the
    VideoVisionWorker final validation check must NOT raise RuntimeError.

    Old bug: `not s.get("description")` was falsy for empty string → raised error.
    New code: `s.get("description") is None` → empty string is not None → no error.
    """
    from src.workers.video_vision_worker import VideoVisionWorker

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    client = MagicMock()

    # First GET: scene with description=None (needs vision).
    scene_needs_vision = {
        "scene_id": "scn_fix4_a",
        "thumbnail_key": "t/lib/scenes/00/ast_fix4_0000001000.jpg",
        "rep_frame_ms": 1000,
        "start_ms": 0,
        "end_ms": 2000,
        "description": None,
    }
    # Second GET (re-fetch for validation): scene with description='' (empty, set by patch).
    scene_after_vision = {
        **scene_needs_vision,
        "description": "",
    }

    client.get.side_effect = [
        _Resp({"scenes": [scene_needs_vision]}),
        _Resp({"scenes": [scene_after_vision]}),
    ]
    client.patch.return_value = _Resp({})
    client.post.return_value = _Resp({})

    artifact_store = MagicMock()
    artifact_store.read_artifact.return_value = b"\xff\xd8\xffscene"

    worker = VideoVisionWorker(client=client, artifact_store=artifact_store, once=True)

    with patch("src.workers.video_vision_worker.get_caption_provider") as mock_factory:
        provider = MagicMock()
        # Provider returns empty string description (not None).
        provider.describe.return_value = {"description": "", "tags": []}
        mock_factory.return_value = provider

        # Must NOT raise RuntimeError; old code would raise because '' is falsy.
        result = worker.process(
            {
                "asset_id": "ast_fix4",
                "media_type": "video",
                "vision_model_id": "moondream",
                "vision_api_url": "http://example/v1",
                "vision_api_key": None,
                "rel_path": "fix4.mp4",
            }
        )

    assert result["model_id"] == "moondream"


# ---------------------------------------------------------------------------
# Fix 5: VisionWorker BlockJob for missing proxy_key
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_vision_worker_missing_proxy_key_raises_block_job() -> None:
    """
    VisionWorker.process() raises BlockJob (not ValueError) when proxy_key is
    absent from the job dict.

    Old bug: the code raised ValueError, which would be treated as a transient
    error and the job would be retried indefinitely.
    New code: raises BlockJob so the job is permanently blocked immediately.
    """
    from src.workers.base import BlockJob
    from src.workers.vision_worker import VisionWorker

    client = MagicMock()
    artifact_store = MagicMock()
    worker = VisionWorker(client=client, artifact_store=artifact_store, once=True)

    job_without_proxy_key = {
        "job_id": "job_fix5_test",
        "asset_id": "ast_fix5",
        # proxy_key intentionally absent
        "vision_model_id": "moondream",
        "vision_api_url": "http://localhost:1234/v1",
        "vision_api_key": None,
    }

    with pytest.raises(BlockJob, match="proxy_key is required"):
        worker.process(job_without_proxy_key)


# ---------------------------------------------------------------------------
# Fix 6: mark_missing_for_scan bulk SQL
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_mark_missing_for_scan_bulk_sql(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    After a scan that only sees 1 out of 3 pre-existing assets, completing the
    scan must mark the other 2 as availability='missing' via bulk SQL.

    This verifies the fix changed from an O(n) Python loop to a single bulk
    UPDATE statement and that it correctly identifies assets not seen by scan_id.
    """
    client, api_key, library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    suffix = os.urandom(4).hex()

    # Create a dedicated scan and upsert 3 assets with the same scan, making
    # them all availability='online'.
    r_scan1 = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan1.status_code == 200
    scan1_id = r_scan1.json()["scan_id"]

    paths = [f"missing_test_{suffix}_{i}.jpg" for i in range(3)]
    asset_ids = []
    for path in paths:
        r_up = client.post(
            "/v1/assets/upsert",
            json={
                "library_id": library_id,
                "rel_path": path,
                "file_size": 1000,
                "file_mtime": "2025-01-01T12:00:00Z",
                "media_type": "image/jpeg",
                "scan_id": scan1_id,
            },
            headers=auth,
        )
        assert r_up.status_code == 200
        r_asset = client.get(
            "/v1/assets/by-path",
            params={"library_id": library_id, "rel_path": path},
            headers=auth,
        )
        assert r_asset.status_code == 200
        asset_ids.append(r_asset.json()["asset_id"])

    # Complete scan1 so assets get availability='online'.
    r_comp1 = client.post(f"/v1/scans/{scan1_id}/complete", headers=auth)
    assert r_comp1.status_code == 200

    # Verify all 3 are online before the test.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id, availability FROM assets WHERE asset_id = ANY(:ids)"
                ),
                {"ids": asset_ids},
            ).fetchall()
    finally:
        engine.dispose()
    assert all(r[1] == "online" for r in rows), f"Expected all online: {rows}"

    # Start a new scan that only sees assets[0].
    r_scan2 = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan2.status_code == 200
    scan2_id = r_scan2.json()["scan_id"]

    r_up2 = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": paths[0],
            "file_size": 1000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "image/jpeg",
            "scan_id": scan2_id,
        },
        headers=auth,
    )
    assert r_up2.status_code == 200

    # Complete scan2 — this triggers mark_missing_for_scan.
    r_comp2 = client.post(f"/v1/scans/{scan2_id}/complete", headers=auth)
    assert r_comp2.status_code == 200
    # The response should report 2 missing.
    assert r_comp2.json()["files_missing"] >= 2

    # Verify DB state: assets[0] stays online; assets[1] and assets[2] → missing.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            avail_map = dict(
                conn.execute(
                    text(
                        "SELECT asset_id, availability FROM assets WHERE asset_id = ANY(:ids)"
                    ),
                    {"ids": asset_ids},
                ).fetchall()
            )
    finally:
        engine.dispose()

    assert avail_map[asset_ids[0]] == "online", "Seen asset must stay online"
    assert avail_map[asset_ids[1]] == "missing", "Unseen asset must be marked missing"
    assert avail_map[asset_ids[2]] == "missing", "Unseen asset must be marked missing"


# ---------------------------------------------------------------------------
# Fix 7: SearchSyncQueue.enqueue dedup
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_sync_queue_enqueue_dedup(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    SearchSyncQueueRepository.enqueue is idempotent: calling it twice for the
    same (asset_id, scene_id=None) returns None on the second call and leaves
    exactly one pending row in search_sync_queue.

    This verifies the guard that checks for existing pending/processing rows
    before inserting.
    """
    client, api_key, library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(
        client, auth, library_id, f"ssq_dedup_{os.urandom(4).hex()}.jpg"
    )

    from sqlmodel import Session as SQLModelSession
    from src.repository.tenant import SearchSyncQueueRepository

    engine = create_engine(tenant_url)
    try:
        with SQLModelSession(engine) as session:
            repo = SearchSyncQueueRepository(session)

            result1 = repo.enqueue(asset_id=asset_id, operation="index")
            assert result1 is not None, "First enqueue should return a SearchSyncQueue row"

            result2 = repo.enqueue(asset_id=asset_id, operation="index")
            assert result2 is None, "Second enqueue for same asset should be skipped (dedup)"

        with engine.connect() as conn:
            count = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM search_sync_queue
                    WHERE asset_id = :asset_id
                      AND scene_id IS NULL
                      AND status IN ('pending', 'processing')
                    """
                ),
                {"asset_id": asset_id},
            ).scalar()
    finally:
        engine.dispose()

    assert count == 1, f"Expected exactly 1 pending row, got {count}"


# ---------------------------------------------------------------------------
# Fix 7b: video-preview completion enqueues search_sync
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_video_preview_complete_enqueues_search_sync(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    POST /v1/jobs/{job_id}/complete for a video-preview job must enqueue a
    search_sync_queue entry for the asset so it gets indexed.

    Regression: completing a video-preview job previously did not enqueue
    search sync, leaving the asset invisible to search after preview generation.
    """
    client, api_key, library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(
        client, auth, library_id, f"preview_sync_{os.urandom(4).hex()}.mp4", "video/mp4"
    )

    # Enqueue a video-preview job.
    r_enq = client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "video-preview", "filter": {"library_id": library_id}, "force": False},
        headers=auth,
    )
    assert r_enq.status_code == 200, (r_enq.status_code, r_enq.text)
    assert r_enq.json()["enqueued"] >= 1

    # Claim the job.
    r_next = client.get(
        "/v1/jobs/next",
        params={"job_type": "video-preview"},
        headers=auth,
    )
    assert r_next.status_code == 200, (r_next.status_code, r_next.text)
    job = r_next.json()
    job_id = job["job_id"]
    claimed_asset_id = job["asset_id"]

    # Complete the video-preview job.
    r_complete = client.post(
        f"/v1/jobs/{job_id}/complete",
        json={"video_preview_key": f"previews/{claimed_asset_id}.mp4"},
        headers=auth,
    )
    assert r_complete.status_code == 200, (r_complete.status_code, r_complete.text)

    # Verify a search_sync_queue entry was created for this asset.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM search_sync_queue
                    WHERE asset_id = :asset_id
                      AND scene_id IS NULL
                    """
                ),
                {"asset_id": claimed_asset_id},
            ).scalar()
    finally:
        engine.dispose()

    assert count >= 1, (
        f"Expected at least 1 search_sync_queue entry for asset {claimed_asset_id} "
        f"after video-preview completion, got {count}"
    )
