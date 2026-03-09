"""Scanner tests: full scan flow against API + testcontainers Postgres."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.cli.scanner import scan_library
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def scanner_client() -> tuple[TestClient, str]:
    """Two testcontainers Postgres; create tenant; yield (TestClient, api_key)."""
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
                    json={"name": "ScannerTestTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

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
                yield client, api_key
        _engines.clear()


def _create_library(client: TestClient, api_key: str, root_path: str, name: str) -> dict:
    r = client.post(
        "/v1/libraries",
        json={"name": name, "root_path": root_path},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


@pytest.mark.slow
def test_scan_discovers_images(
    scanner_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Create 5 JPEG files, run scan, assert files_discovered=5, files_added=5, files_missing=0."""
    client, api_key = scanner_client
    for i in range(5):
        (tmp_path / f"img_{i}.jpg").write_bytes(b"\xff")
    library = _create_library(
        client, api_key, str(tmp_path), "DiscoversImages_" + __import__("secrets").token_urlsafe(4)
    )
    auth_client = _AuthClient(client, api_key)
    result = scan_library(auth_client, library, force=True)
    assert result.status == "complete"
    assert result.files_discovered == 5
    assert result.files_added == 5
    assert result.files_missing == 0


@pytest.mark.slow
def test_scan_skips_known_files(
    scanner_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Scan same directory twice (no --force); second scan all skipped."""
    client, api_key = scanner_client
    for i in range(5):
        (tmp_path / f"skip_{i}.jpg").write_bytes(b"\x00")
    library = _create_library(
        client, api_key, str(tmp_path), "SkipsKnown_" + __import__("secrets").token_urlsafe(4)
    )
    auth_client = _AuthClient(client, api_key)
    scan_library(auth_client, library, force=True)
    result = scan_library(auth_client, library, force=False)
    assert result.status == "complete"
    assert result.files_added == 0
    assert result.files_skipped == 5
    assert result.files_discovered == 5


@pytest.mark.slow
def test_scan_updated_file(
    scanner_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Scan, change file size, scan again (no --force); assert files_updated=1, files_skipped=1."""
    client, api_key = scanner_client
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "b.jpg").write_bytes(b"b")
    library = _create_library(
        client, api_key, str(tmp_path), "Updated_" + __import__("secrets").token_urlsafe(4)
    )
    auth_client = _AuthClient(client, api_key)
    scan_library(auth_client, library, force=True)
    (tmp_path / "a.jpg").write_bytes(b"longer content")
    result = scan_library(auth_client, library, force=False)
    assert result.status == "complete"
    assert result.files_updated == 1
    assert result.files_skipped == 1


@pytest.mark.slow
def test_scan_detects_missing_files(
    scanner_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Scan, delete one file, scan again, assert files_missing=1."""
    client, api_key = scanner_client
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "b.jpg").write_bytes(b"b")
    (tmp_path / "c.jpg").write_bytes(b"c")
    library = _create_library(
        client, api_key, str(tmp_path), "Missing_" + __import__("secrets").token_urlsafe(4)
    )
    auth_client = _AuthClient(client, api_key)
    scan_library(auth_client, library, force=True)
    (tmp_path / "b.jpg").unlink()
    result = scan_library(auth_client, library, force=True)
    assert result.status == "complete"
    assert result.files_missing == 1


@pytest.mark.slow
def test_scan_unreachable_root(scanner_client: tuple[TestClient, str]) -> None:
    """Pass non-existent root_path in library dict; assert status='aborted'."""
    client, api_key = scanner_client
    library = _create_library(
        client, api_key, "/nonexistent/path/12345", "Unreachable_" + __import__("secrets").token_urlsafe(4)
    )
    library["root_path"] = "/nonexistent/path/12345"
    auth_client = _AuthClient(client, api_key)
    result = scan_library(auth_client, library, force=True)
    assert result.status == "aborted"


@pytest.mark.slow
def test_scan_mixed_reconciliation(
    scanner_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Mixed scan: 2 skip, 1 add, 1 update, 1 missing; assert all counts precisely."""
    client, api_key = scanner_client
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "b.jpg").write_bytes(b"b")
    (tmp_path / "c.jpg").write_bytes(b"c")
    (tmp_path / "d.jpg").write_bytes(b"d")
    library = _create_library(
        client, api_key, str(tmp_path), "Mixed_" + __import__("secrets").token_urlsafe(4)
    )
    auth_client = _AuthClient(client, api_key)
    scan_library(auth_client, library, force=True)
    (tmp_path / "c.jpg").write_bytes(b"updated content")
    (tmp_path / "d.jpg").unlink()
    (tmp_path / "e.jpg").write_bytes(b"new")
    result = scan_library(auth_client, library, force=False)
    assert result.status == "complete"
    assert result.files_added == 1
    assert result.files_updated == 1
    assert result.files_skipped == 2
    assert result.files_missing == 1
    assert result.files_discovered == 4  # add + update + skip (missing not "discovered")


@pytest.mark.slow
def test_scan_path_override(
    scanner_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Create files in subdir/ and other/; scan with path_override='subdir'; assert only subdir files discovered."""
    client, api_key = scanner_client
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (subdir / "one.jpg").write_bytes(b"1")
    (subdir / "two.jpg").write_bytes(b"2")
    (other / "three.jpg").write_bytes(b"3")
    library = _create_library(
        client, api_key, str(tmp_path), "Override_" + __import__("secrets").token_urlsafe(4)
    )
    auth_client = _AuthClient(client, api_key)
    result = scan_library(auth_client, library, path_override="subdir", force=True)
    assert result.status == "complete"
    assert result.files_discovered == 2
    assert result.files_added == 2
