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

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from src.server.repository.tenant import AssetEmbeddingRepository, AssetRepository, LibraryRepository
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
        with patch("src.server.api.routers.admin.provision_tenant_database"):
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
    base_id: str
    close_id: str
    far_id: str
    with Session(engine) as session:
        asset_repo = AssetRepository(session)
        asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="photos/a.jpg",
            file_size=123,
            file_mtime=None,
            media_type="image",
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
        asset_repo = AssetRepository(session)
        # Base, close, far vectors
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="photos/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        close_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="photos/close.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        far_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="photos/far.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )

        emb_repo = AssetEmbeddingRepository(session)
        from src.client.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

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
def test_similar_auto_detects_model(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar without model_id auto-detects from source asset's embedding.

    This catches the bug where the server defaulted to 'clip' model when no
    model_id was passed, causing Apple Vision (or any non-CLIP) embeddings
    to appear as 'no embedding available'.
    """
    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        asset_repo = AssetRepository(session)
        # Use 768-dim vectors (Apple Vision dimension)
        base_vec, close_vec, far_vec = _asset_vectors(dim=768)

        base_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="auto_detect/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        close_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="auto_detect/close.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        far_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="auto_detect/far.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )

        emb_repo = AssetEmbeddingRepository(session)
        # Use apple_vision model (NOT clip)
        emb_repo.upsert(base_asset.asset_id, "apple_vision", "feature_print_v1", base_vec)
        emb_repo.upsert(close_asset.asset_id, "apple_vision", "feature_print_v1", close_vec)
        emb_repo.upsert(far_asset.asset_id, "apple_vision", "feature_print_v1", far_vec)

        base_id = base_asset.asset_id
        close_id = close_asset.asset_id
        far_id = far_asset.asset_id

    # No model_id param — server should auto-detect apple_vision
    resp = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Must find the embedding (not return embedding_available=False)
    assert data["embedding_available"] is True, (
        "Server did not auto-detect apple_vision embedding; returned embedding_available=False"
    )
    assert data["total"] >= 2
    ids = [hit["asset_id"] for hit in data["hits"]]
    assert close_id in ids and far_id in ids
    assert ids.index(close_id) < ids.index(far_id)


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
        asset_repo = AssetRepository(session)
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="date_filter/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        close_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="date_filter/close_oct.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        far_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="date_filter/far_nov.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        base_asset.taken_at = datetime(2025, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        close_asset.taken_at = datetime(2025, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        far_asset.taken_at = nov_1
        session.add(base_asset)
        session.add(close_asset)
        session.add(far_asset)
        session.commit()

        emb_repo = AssetEmbeddingRepository(session)
        from src.client.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

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
def test_search_by_image_basic(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/similar/search-by-image returns ranked hits using CLIP embeddings."""
    import base64
    import io
    from unittest.mock import patch

    from PIL import Image as PILImage

    from src.client.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        asset_repo = AssetRepository(session)
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="image_search/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        close_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="image_search/close.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        far_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="image_search/far.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )

        emb_repo = AssetEmbeddingRepository(session)
        emb_repo.upsert(base_asset.asset_id, "clip", CLIP_VERSION, base_vec)
        emb_repo.upsert(close_asset.asset_id, "clip", CLIP_VERSION, close_vec)
        emb_repo.upsert(far_asset.asset_id, "clip", CLIP_VERSION, far_vec)

        base_id = base_asset.asset_id
        close_id = close_asset.asset_id
        far_id = far_asset.asset_id

    # Create a small in-memory JPEG and base64-encode it
    img = PILImage.new("RGB", (256, 256), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    image_b64 = base64.b64encode(buf.getvalue()).decode()

    class _DummyProvider:
        def embed_image(self, _pil_image):
            # Use the base vector so similarity is defined vs stored embeddings
            return base_vec

    with patch("src.client.workers.embeddings.clip_provider.CLIPEmbeddingProvider", return_value=_DummyProvider()):
        resp = auth_client.post(
            "/v1/similar/search-by-image",
            json={
                "library_id": library_id,
                "image_b64": image_b64,
                "limit": 10,
                "offset": 0,
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    hits = data.get("hits", [])
    assert data["total"] >= 2
    ids = [h["asset_id"] for h in hits]
    assert close_id in ids and far_id in ids
    # Closest first: close should appear before far
    assert ids.index(close_id) < ids.index(far_id)


@pytest.mark.slow
def test_similar_asset_types_filter(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar with asset_types=image returns only image assets; asset_types=video only video."""
    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        asset_repo = AssetRepository(session)
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="type_filter/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        close_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="type_filter/close_image.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        far_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="type_filter/far_video.mp4",
            file_size=100,
            file_mtime=None,
            media_type="video",
        )

        emb_repo = AssetEmbeddingRepository(session)
        from src.client.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

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


@pytest.mark.slow
def test_similar_camera_filter(similarity_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/similar with camera_make/camera_model returns only matching camera(s); repeated params OR across pairs."""
    auth_client, library_id, tenant_url = similarity_client

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        asset_repo = AssetRepository(session)
        base_vec, close_vec, far_vec = _asset_vectors()

        base_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="camera_filter/base.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        close_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="camera_filter/close_canon.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        far_asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path="camera_filter/far_nikon.jpg",
            file_size=100,
            file_mtime=None,
            media_type="image",
        )
        base_asset.camera_make = "Canon"
        base_asset.camera_model = "EOS R5"
        close_asset.camera_make = "Canon"
        close_asset.camera_model = "EOS R5"
        far_asset.camera_make = "Nikon"
        far_asset.camera_model = "Z9"
        session.add(base_asset)
        session.add(close_asset)
        session.add(far_asset)
        session.commit()

        emb_repo = AssetEmbeddingRepository(session)
        from src.client.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

        emb_repo.upsert(base_asset.asset_id, "clip", CLIP_VERSION, base_vec)
        emb_repo.upsert(close_asset.asset_id, "clip", CLIP_VERSION, close_vec)
        emb_repo.upsert(far_asset.asset_id, "clip", CLIP_VERSION, far_vec)

        base_id = base_asset.asset_id
        close_id = close_asset.asset_id
        far_id = far_asset.asset_id

    # No camera filter: both close and far can appear
    resp = auth_client.get(
        f"/v1/similar?asset_id={base_id}&library_id={library_id}",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["embedding_available"] is True
    ids_no_filter = [hit["asset_id"] for hit in data["hits"]]
    assert close_id in ids_no_filter and far_id in ids_no_filter

    # Single pair: Canon EOS R5 -> only close
    resp_canon = auth_client.get(
        "/v1/similar",
        params={"asset_id": base_id, "library_id": library_id, "camera_make": "Canon", "camera_model": "EOS R5"},
    )
    assert resp_canon.status_code == 200, resp_canon.text
    data_canon = resp_canon.json()
    assert data_canon["embedding_available"] is True
    ids_canon = [hit["asset_id"] for hit in data_canon["hits"]]
    assert close_id in ids_canon
    assert far_id not in ids_canon

    # Two pairs (OR): Canon EOS R5 or Nikon Z9 -> both close and far
    resp_both = auth_client.get(
        "/v1/similar",
        params={
            "asset_id": base_id,
            "library_id": library_id,
            "camera_make": ["Canon", "Nikon"],
            "camera_model": ["EOS R5", "Z9"],
        },
    )
    assert resp_both.status_code == 200, resp_both.text
    data_both = resp_both.json()
    assert data_both["embedding_available"] is True
    ids_both = [hit["asset_id"] for hit in data_both["hits"]]
    assert close_id in ids_both and far_id in ids_both




