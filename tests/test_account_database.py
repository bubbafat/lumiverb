"""Where an account's database is: its routing row, for every caller.

Requests read it (src/server/api/middleware.py); work outside a request
(a response's background task, upkeep for every account, the scheduler)
reads it too (src/server/database.py tenant_database_url). The env file's
template names only a new account's database: here it names somewhere the
account isn't, as after a restore or with the tests' own databases."""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

ADMIN_KEY = "test-admin-secret"
NOT_HERE = "postgresql://lumiverb:pw@the-template-is-not-where-this-account-is.invalid:5432/{tenant_id}"


@pytest.fixture(scope="module")
def account():
    """(client, headers, tenant_id, tenant_url): an account routed to its own
    database, whose template names somewhere else."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = NOT_HERE
        os.environ["ADMIN_KEY"] = ADMIN_KEY
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.accounts.provision_tenant_database"), TestClient(app) as client:
            r = client.post("/v1/admin/tenants", json={"name": "Routed", "plan": "free"},
                            headers={"Authorization": f"Bearer {ADMIN_KEY}"})
            assert r.status_code == 200, r.text
            tenant_id, api_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            engine = create_engine(control_url)
            with engine.begin() as conn:
                conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                             {"u": tenant_url, "t": tenant_id})
            engine.dispose()
            with TestClient(app) as client:
                yield client, {"Authorization": f"Bearer {api_key}"}, tenant_id, tenant_url
        _engines.clear()


@pytest.mark.slow
def test_work_outside_a_request_opens_the_database_the_routing_names(account):
    from src.server.database import get_tenant_session, tenant_database_url

    *_, tenant_id, tenant_url = account
    assert tenant_database_url(tenant_id) == tenant_url
    with get_tenant_session(tenant_id) as session:
        name = session.execute(text("SELECT current_database()")).scalar()
    assert name == make_url(tenant_url).database


@pytest.mark.slow
def test_an_account_with_no_routing_has_no_database(account):
    from src.server.database import get_tenant_session

    with pytest.raises(LookupError), get_tenant_session("ten_NOBODY"):
        pass


@pytest.mark.slow
def test_clips_put_in_the_trash_leave_search_after_the_response(account, caplog):
    """DELETE /v1/assets drops the trashed clips' search documents once it has
    answered (assets.py _drop_from_search), reading which are still out of
    sight from the account's own database."""
    client, headers, tenant_id, _ = account
    r = client.post("/v1/libraries", json={"name": "Trash", "root_path": "/trash"}, headers=headers)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    r = client.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": "a.jpg", "file_size": 1,
                                               "file_mtime": "2025-01-01T12:00:00Z", "media_type": "image"},
                    headers=headers)
    assert r.status_code == 200, r.text
    clip = client.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": "a.jpg"},
                      headers=headers).json()["asset_id"]

    dropped: list[tuple[str, list[str]]] = []
    with (patch("src.server.search.quickwit_client.QuickwitClient.delete_tenant_documents_by_asset_ids",
                lambda self, t, ids: dropped.append((t, list(ids)))),
          caplog.at_level(logging.WARNING, logger="src.server.api.routers.assets")):
        r = client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "user"}, headers=headers)

    assert r.status_code == 200, r.text
    assert dropped == [(tenant_id, [clip])], caplog.text


@pytest.mark.slow
def test_upkeep_for_every_account_reaches_each_account(account):
    """The operator's upkeep (tenants=all) opens each account's database as
    its routing says: here, an empty dismissed person is cleaned up."""
    from sqlmodel import Session

    from src.server.repository.tenant import PersonRepository

    client, _, _, tenant_url = account
    engine = create_engine(tenant_url)
    with Session(engine) as session:
        PersonRepository(session).create_dismissed(face_ids=[])
        session.commit()
    engine.dispose()

    r = client.post("/v1/upkeep/cleanup-dismissed", params={"tenants": "all"},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})

    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1


@pytest.mark.fast
def test_only_making_an_account_reads_the_template():
    """The template names a new account's database (accounts.py, and
    database.py provisioning it); every other caller asks the routing.
    A restore is told it by the install's env file (lifecycle/cli.py)."""
    repo = Path(__file__).resolve().parent.parent
    readers = sorted(str(p.relative_to(repo)) for p in (repo / "src").rglob("*.py")
                     if "tenant_database_url_template" in p.read_text())
    assert readers == ["src/server/accounts.py", "src/server/config.py", "src/server/database.py"], readers

    from src.server import database

    assert "tenant_database_url_template" not in inspect.getsource(database.get_tenant_engine)
    assert "tenant_database_url_template" not in inspect.getsource(database.tenant_database_url)
