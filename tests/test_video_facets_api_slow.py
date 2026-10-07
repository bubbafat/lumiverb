"""Video facets: probe results stored per asset (ADR-016 phase 1).

Scan sends the probe with ingest; enrich backfills older assets with
PUT /v1/assets/{id}/video-facet. The asset detail returns it, and the
duration it carries becomes the asset's duration.
"""

from __future__ import annotations

import io
import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from src.server.storage.local import LocalStorage
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

FACET = {
    "duration_sec": 7.07,
    "container": "mov",
    "video_codec": "h264",
    "width": 1080,
    "height": 1920,
    "rotation": 90,
    "frame_rate_num": 30000,
    "frame_rate_den": 1001,
    "start_timecode": "01:00:00;00",
    "drop_frame": True,
    "audio_codec": "aac",
    "audio_channels": 2,
    "audio_sample_rate": 48000,
}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Yields (client, headers, library_id)."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("facet_storage")))
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
                    json={"name": "FacetTenant", "plan": "free"},
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
                    json={"name": "FacetLib", "root_path": "/media"},
                    headers=headers,
                )
                assert r.status_code == 200, r.text
                yield client, headers, r.json()["library_id"]

    _engines.clear()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), color=(20, 40, 60)).save(buf, format="JPEG")
    return buf.getvalue()


def _ingest(client, headers, library_id: str, rel_path: str, media_type: str, **extra):
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "5000",
            "media_type": media_type, **extra}
    r = client.post(
        "/v1/ingest",
        headers=headers,
        files={"proxy": ("p.jpg", io.BytesIO(_jpeg()), "image/jpeg")},
        data=data,
    )
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _missing_probe(client, headers, library_id: str) -> set[str]:
    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "missing_probe": "true", "limit": 500},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {i["asset_id"] for i in r.json()["items"]}


@pytest.mark.slow
def test_put_facet_is_returned_and_sets_duration(env) -> None:
    client, headers, library_id = env
    asset_id = _ingest(client, headers, library_id, "v/put.mov", "video")
    assert asset_id in _missing_probe(client, headers, library_id)

    r = client.put(f"/v1/assets/{asset_id}/video-facet", json=FACET, headers=headers)
    assert r.status_code == 200, r.text

    detail = client.get(f"/v1/assets/{asset_id}", headers=headers).json()
    assert detail["video_facet"] == FACET
    assert detail["duration_sec"] == pytest.approx(7.07)
    assert asset_id not in _missing_probe(client, headers, library_id)


@pytest.mark.slow
def test_put_facet_replaces_previous(env) -> None:
    client, headers, library_id = env
    asset_id = _ingest(client, headers, library_id, "v/replace.mov", "video")
    client.put(f"/v1/assets/{asset_id}/video-facet", json=FACET, headers=headers)

    updated = dict(FACET, frame_rate_num=25, frame_rate_den=1, start_timecode=None, drop_frame=None)
    r = client.put(f"/v1/assets/{asset_id}/video-facet", json=updated, headers=headers)
    assert r.status_code == 200, r.text

    assert client.get(f"/v1/assets/{asset_id}", headers=headers).json()["video_facet"] == updated


@pytest.mark.slow
def test_ingest_stores_facet(env) -> None:
    client, headers, library_id = env
    asset_id = _ingest(
        client, headers, library_id, "v/ingested.mov", "video", video_facet=json.dumps(FACET)
    )

    detail = client.get(f"/v1/assets/{asset_id}", headers=headers).json()
    assert detail["video_facet"] == FACET
    assert asset_id not in _missing_probe(client, headers, library_id)


@pytest.mark.slow
def test_facet_rejected_for_images(env) -> None:
    client, headers, library_id = env
    asset_id = _ingest(client, headers, library_id, "i/photo.jpg", "image")

    r = client.put(f"/v1/assets/{asset_id}/video-facet", json=FACET, headers=headers)

    assert r.status_code == 400
    assert asset_id not in _missing_probe(client, headers, library_id)
    assert client.get(f"/v1/assets/{asset_id}", headers=headers).json()["video_facet"] is None


@pytest.mark.slow
def test_facet_for_unknown_asset_is_404(env) -> None:
    client, headers, _ = env

    r = client.put("/v1/assets/ast_nope/video-facet", json=FACET, headers=headers)

    assert r.status_code == 404


@pytest.mark.slow
def test_emptying_trash_removes_facet(env) -> None:
    client, headers, library_id = env
    asset_id = _ingest(
        client, headers, library_id, "v/gone.mov", "video", video_facet=json.dumps(FACET)
    )
    client.delete(f"/v1/assets/{asset_id}", headers=headers)

    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id]}, headers=headers)

    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1
