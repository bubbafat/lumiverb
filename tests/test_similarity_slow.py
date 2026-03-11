from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines, get_control_session
from src.repository.control_plane import TenantDbRoutingRepository
from src.repository.tenant import AssetEmbeddingRepository, AssetRepository, LibraryRepository, ScanRepository
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


def _asset_vectors(dim: int = 512) -> tuple[list[float], list[float], list[float]]:
    base = [1.0, 0.0] + [0.0] * (dim - 2)
    close = [0.9, 0.1] + [0.0] * (dim - 2)
    far = [0.0, 1.0] + [0.0] * (dim - 2)
    return base, close, far


@pytest.fixture(scope="module")
def similarity_client() -> Tuple[_AuthClient, str, str]:
    """
    Two Postgres containers; create tenant; point routing at tenant DB.
    Returns (_AuthClient, library_id, tenant_url) for authenticated requests.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        # Create tenant and API key via admin router
        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SimilarityApiTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            # Point routing at tenant DB URL
            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            # Create a library for similarity tests
            with TestClient(app) as client:
                auth_client = _AuthClient(client, api_key)
                lib_name = "SimilarityLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/similarity"},
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


@pytest.mark.slow
def test_similar_no_embedding(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar for asset with no embedding returns embedding_available=False and empty hits."""
    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        scan_repo = ScanRepository(session)
        scan = scan_repo.create(library_id=library_id)
        asset_repo = AssetRepository(session)
        asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="photos/a.jpg",
            file_size=123,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )

    resp = auth_client.get(
        f"/v1/similar?asset_id={asset.asset_id}&library_id={library_id}",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["embedding_available"] is False
    assert data["hits"] == []
    assert data["total"] == 0


@pytest.mark.slow
def test_similar_with_embeddings(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """Similarity results are ordered by cosine distance, closest first."""
    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        scan_repo = ScanRepository(session)
        scan = scan_repo.create(library_id=library_id)
        asset_repo = AssetRepository(session)
        # Base, close, far vectors
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="photos/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        close_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="photos/close.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        far_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="photos/far.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )

        emb_repo = AssetEmbeddingRepository(session)
        from src.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

        emb_repo.upsert(base_asset.asset_id, "clip", CLIP_VERSION, base_vec)
        emb_repo.upsert(close_asset.asset_id, "clip", CLIP_VERSION, close_vec)
        emb_repo.upsert(far_asset.asset_id, "clip", CLIP_VERSION, far_vec)

        base_id = base_asset.asset_id
        close_id = close_asset.asset_id
        far_id = far_asset.asset_id

    resp = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["embedding_available"] is True
    assert data["total"] >= 2
    ids = [hit["asset_id"] for hit in data["hits"]]
    assert close_id in ids and far_id in ids
    # Closest first: close should appear before far
    assert ids.index(close_id) < ids.index(far_id)


@pytest.mark.slow
def test_similar_asset_not_found(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar with unknown asset_id returns 404."""
    auth_client, library_id, _tenant_url = similarity_client

    resp = auth_client.get(
        f"/v1/similar?asset_id=ast_unknown&library_id={library_id}",
    )
    assert resp.status_code == 404


@pytest.mark.slow
def test_similar_date_range_filter(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar with from_ts/to_ts returns only assets with taken_at in range."""
    auth_client, library_id, tenant_url = similarity_client

    # October 2025 range (Unix seconds, inclusive)
    oct_start = int(datetime(2025, 10, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    oct_end = int(datetime(2025, 10, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    nov_1 = datetime(2025, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        scan_repo = ScanRepository(session)
        scan = scan_repo.create(library_id=library_id)
        asset_repo = AssetRepository(session)
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="date_filter/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        close_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="date_filter/close_oct.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        far_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="date_filter/far_nov.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        base_asset.taken_at = datetime(2025, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        close_asset.taken_at = datetime(2025, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        far_asset.taken_at = nov_1
        session.add(base_asset)
        session.add(close_asset)
        session.add(far_asset)
        session.commit()

        emb_repo = AssetEmbeddingRepository(session)
        from src.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

        emb_repo.upsert(base_asset.asset_id, "clip", CLIP_VERSION, base_vec)
        emb_repo.upsert(close_asset.asset_id, "clip", CLIP_VERSION, close_vec)
        emb_repo.upsert(far_asset.asset_id, "clip", CLIP_VERSION, far_vec)

        base_id = base_asset.asset_id
        close_id = close_asset.asset_id
        far_id = far_asset.asset_id

    # No date filter: both close and far appear (may be more hits from other tests in same library)
    resp = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["embedding_available"] is True
    ids_no_filter = [hit["asset_id"] for hit in data["hits"]]
    assert close_id in ids_no_filter and far_id in ids_no_filter

    # October only: only close (far is November)
    resp_oct = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}&from_ts={oct_start}&to_ts={oct_end}",
    )
    assert resp_oct.status_code == 200, resp_oct.text
    data_oct = resp_oct.json()
    assert data_oct["embedding_available"] is True
    assert data_oct["total"] == 1
    assert data_oct["hits"][0]["asset_id"] == close_id


@pytest.mark.slow
def test_similar_from_ts_gt_to_ts_returns_422(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar with from_ts > to_ts returns 422."""
    auth_client, library_id, _tenant_url = similarity_client

    resp = auth_client.get(
        f"/v1/similar?asset_id=ast_any&library_id={library_id}&from_ts=1000&to_ts=500",
    )
    assert resp.status_code == 422
    assert "from_ts" in resp.text or "to_ts" in resp.text.lower()


@pytest.mark.slow
def test_similar_asset_types_filter(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar with asset_types=image returns only image assets; asset_types=video only video."""
    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        scan_repo = ScanRepository(session)
        scan = scan_repo.create(library_id=library_id)
        asset_repo = AssetRepository(session)
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="type_filter/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        close_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="type_filter/close_image.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image/jpeg",
            scan_id=scan.scan_id,
        )
        far_asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path="type_filter/far_video.mp4",
            file_size=100,
            file_mtime=None,
            media_type="video",
            scan_id=scan.scan_id,
        )

        emb_repo = AssetEmbeddingRepository(session)
        from src.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

        emb_repo.upsert(base_asset.asset_id, "clip", CLIP_VERSION, base_vec)
        emb_repo.upsert(close_asset.asset_id, "clip", CLIP_VERSION, close_vec)
        emb_repo.upsert(far_asset.asset_id, "clip", CLIP_VERSION, far_vec)

        base_id = base_asset.asset_id
        close_id = close_asset.asset_id
        far_id = far_asset.asset_id

    # No asset_types filter: both close (image) and far (video) can appear
    resp = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["embedding_available"] is True
    ids_no_filter = [hit["asset_id"] for hit in data["hits"]]
    assert close_id in ids_no_filter and far_id in ids_no_filter

    # asset_types=image: only close (image) in results
    resp_image = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}&asset_types=image",
    )
    assert resp_image.status_code == 200, resp_image.text
    data_image = resp_image.json()
    assert data_image["embedding_available"] is True
    ids_image = [hit["asset_id"] for hit in data_image["hits"]]
    assert close_id in ids_image
    assert far_id not in ids_image

    # asset_types=video: only far (video) in results
    resp_video = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}&asset_types=video",
    )
    assert resp_video.status_code == 200, resp_video.text
    data_video = resp_video.json()
    assert data_video["embedding_available"] is True
    ids_video = [hit["asset_id"] for hit in data_video["hits"]]
    assert far_id in ids_video
    assert close_id not in ids_video



