"""Change reports: the Mac tells the brain what changed on storage (ADR-016 phase 2).

The brain mounts the DAS over the network and can't watch it, so the Mac,
which has the disks, reports paths it sees change. The brain's worker reads
the pending reports, scans just those folders, and acknowledges them.

Uses testcontainers Postgres (control + tenant).
"""

from __future__ import annotations

import os
import unicodedata
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

ROOT = "/Volumes/media-01/Footage"


@pytest.fixture(scope="module")
def env():
    """Yield (client, headers, library_id)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-changes"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"), TestClient(app) as client:
            r = client.post("/v1/admin/tenants", json={"name": "ChangesTenant", "plan": "free"},
                            headers={"Authorization": "Bearer test-admin-changes"})
            assert r.status_code == 200, r.text
            tenant_id, api_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                headers = {"Authorization": f"Bearer {api_key}"}
                yield client, headers, _library(client, headers, "Footage", ROOT)
        _engines.clear()


def _library(client, headers, name: str, root: str) -> str:
    r = client.post("/v1/libraries", json={"name": name, "root_path": root}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


def _report(env, paths: list[str]):
    client, headers, _ = env
    return client.post("/v1/changes", json={"paths": paths}, headers=headers)


def _pending(env, library_id: str | None = None, **params) -> dict:
    client, headers, default_lib = env
    r = client.get(f"/v1/libraries/{library_id or default_lib}/changes", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _ack(env, data: dict, library_id: str | None = None):
    client, headers, default_lib = env
    return client.post(f"/v1/libraries/{library_id or default_lib}/changes/ack", json=data, headers=headers)


def _seen(pending: dict) -> dict:
    """An ack for exactly the versions the worker read."""
    return {"changes": [{"change_id": c["change_id"], "version": c["version"]} for c in pending["changes"]]}


def _clear(env, library_id: str | None = None) -> None:
    pending = _pending(env, library_id)
    if pending["changes"]:
        _ack(env, _seen(pending), library_id)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_reported_file_is_pending_for_its_library(env) -> None:
    _clear(env)
    r = _report(env, [f"{ROOT}/Day 1/A001.mov"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["unmatched"] == 0
    assert body["libraries"] == {env[2]: 1}
    assert [c["rel_path"] for c in _pending(env)["changes"]] == ["Day 1/A001.mov"]


@pytest.mark.slow
def test_the_library_root_itself_is_the_empty_path(env) -> None:
    _clear(env)
    _report(env, [ROOT, f"{ROOT}/"])
    assert [c["rel_path"] for c in _pending(env)["changes"]] == [""]


@pytest.mark.slow
def test_paths_outside_every_library_are_not_kept(env) -> None:
    _clear(env)
    body = _report(env, ["/Volumes/media-02/Other/x.mov", "/Volumes/media-01/Footage2/y.mov"]).json()
    assert body["accepted"] == 0
    assert body["unmatched"] == 2
    assert "/Volumes/media-02/Other/x.mov" in body["unmatched_sample"]
    assert _pending(env)["changes"] == []


@pytest.mark.slow
def test_the_deepest_library_root_wins(env) -> None:
    client, headers, footage = env
    outer = _library(client, headers, "Whole volume", "/Volumes/media-01")
    _clear(env)
    _clear(env, outer)
    body = _report(env, [f"{ROOT}/Day 2/B001.mov", "/Volumes/media-01/Music/song.wav"]).json()
    assert body["libraries"] == {footage: 1, outer: 1}
    assert [c["rel_path"] for c in _pending(env)["changes"]] == ["Day 2/B001.mov"]
    assert [c["rel_path"] for c in _pending(env, outer)["changes"]] == ["Music/song.wav"]
    _clear(env, outer)


@pytest.mark.slow
def test_unicode_forms_match_and_are_stored_nfc(env) -> None:
    _clear(env)
    nfc = unicodedata.normalize("NFC", "Café/clip.mov")
    _report(env, [unicodedata.normalize("NFD", f"{ROOT}/{nfc}")])
    assert [c["rel_path"] for c in _pending(env)["changes"]] == [nfc]


@pytest.mark.slow
def test_reporting_again_keeps_one_row_with_the_latest_time(env) -> None:
    _clear(env)
    _report(env, [f"{ROOT}/again.mov"])
    first = _pending(env)["changes"][0]
    _report(env, [f"{ROOT}/again.mov", f"{ROOT}/again.mov"])
    [second] = _pending(env)["changes"]
    assert second["change_id"] == first["change_id"]
    assert second["reported_at"] >= first["reported_at"]
    assert second["version"] > first["version"]


@pytest.mark.slow
def test_paths_in_a_trashed_library_are_not_kept(env) -> None:
    client, headers, _ = env
    gone = _library(client, headers, "Gone", "/Volumes/media-03/Gone")
    assert client.delete(f"/v1/libraries/{gone}", headers=headers).status_code in (200, 204)
    body = _report(env, ["/Volumes/media-03/Gone/x.mov"]).json()
    assert body["accepted"] == 0 and body["unmatched"] == 1


@pytest.mark.slow
@pytest.mark.parametrize("bad", ["relative/path.mov", f"{ROOT}/../escape.mov", ""])
def test_bad_paths_are_rejected(env, bad: str) -> None:
    r = _report(env, [bad])
    assert r.status_code == 422, r.text


@pytest.mark.slow
def test_too_many_paths_in_one_report_are_rejected(env) -> None:
    r = _report(env, [f"{ROOT}/{n}.mov" for n in range(10_001)])
    assert r.status_code == 422


@pytest.mark.slow
def test_an_empty_report_is_fine(env) -> None:
    assert _report(env, []).json()["accepted"] == 0


# ---------------------------------------------------------------------------
# Reading and acknowledging
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_ack_removes_what_was_scanned(env) -> None:
    _clear(env)
    _report(env, [f"{ROOT}/a.mov", f"{ROOT}/b.mov"])
    pending = _pending(env)
    r = _ack(env, _seen(pending))
    assert r.status_code == 200, r.text
    assert r.json()["acknowledged"] == 2
    assert _pending(env)["changes"] == []


@pytest.mark.slow
def test_a_change_reported_during_the_scan_survives_the_ack(env) -> None:
    _clear(env)
    _report(env, [f"{ROOT}/busy.mov"])
    pending = _pending(env)
    _report(env, [f"{ROOT}/busy.mov"])  # changed again while the worker scanned
    r = _ack(env, _seen(pending))
    assert r.json()["acknowledged"] == 0
    assert [c["rel_path"] for c in _pending(env)["changes"]] == ["busy.mov"]


@pytest.mark.slow
def test_ack_only_touches_its_own_library(env) -> None:
    client, headers, footage = env
    other = _library(client, headers, "Other", "/Volumes/media-04/Other")
    _clear(env)
    _report(env, ["/Volumes/media-04/Other/x.mov"])
    theirs = _pending(env, other)
    r = _ack(env, _seen(theirs), footage)
    assert r.json()["acknowledged"] == 0
    assert len(_pending(env, other)["changes"]) == 1


@pytest.mark.slow
def test_a_long_backlog_is_truncated(env) -> None:
    _clear(env)
    _report(env, [f"{ROOT}/many/{n}.mov" for n in range(5)])
    page = _pending(env, limit=3)
    assert len(page["changes"]) == 3
    assert page["truncated"] is True
    assert _pending(env)["truncated"] is False


@pytest.mark.slow
def test_summary_counts_pending_changes_per_library(env) -> None:
    client, headers, footage = env
    _clear(env)
    _report(env, [f"{ROOT}/s1.mov", f"{ROOT}/s2.mov"])
    r = client.get("/v1/changes", headers=headers)
    assert r.status_code == 200, r.text
    rows = {row["library_id"]: row for row in r.json()["libraries"]}
    assert rows[footage]["pending"] == 2
    assert rows[footage]["oldest_reported_at"]


@pytest.mark.slow
def test_unknown_library_is_404(env) -> None:
    client, headers, _ = env
    assert client.get("/v1/libraries/lib_nope/changes", headers=headers).status_code == 404
