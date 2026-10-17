"""Where an account's database is: its routing row, for every caller.

Requests read it (src/server/api/middleware.py), public links' too; work
outside a request (a response's background task, upkeep for every account,
the scheduler) reads it as well (src/server/database.py tenant_database_url). The env file's
template names only a new account's database: here it names somewhere the
account isn't, as after a restore or with the tests' own databases."""

from __future__ import annotations

import inspect
import os
from contextlib import contextmanager
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
from tests.clips import make_clip
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
def test_clips_put_in_the_trash_leave_search_at_once(account):
    """Search is in the account's own database, the one its routing names
    (ADR-019): a clip put in the trash is out of what a search finds as the
    trash commits, with nothing left to drop after the response."""
    client, headers, _, tenant_url = account
    r = client.post("/v1/libraries", json={"name": "Trash", "root_path": "/trash"}, headers=headers)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    clip = make_clip(tenant_url, library_id, "a.jpg", file_size=1)
    r = client.put(f"/v1/assets/{clip}/note", json={"text": "the lighthouse keeper"}, headers=headers)
    assert r.status_code == 200, r.text

    def found() -> list[str]:
        r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", "query:lighthouse")],
                       headers=headers)
        assert r.status_code == 200, r.text
        return [item["asset_id"] for item in r.json()["items"]]

    assert found() == [clip]
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "user"}, headers=headers)
    assert r.status_code == 200, r.text
    assert found() == []


@pytest.mark.slow
def test_upkeep_for_every_account_reaches_each_account(account):
    """The operator's upkeep (tenants=all) opens each account's database as
    its routing says: here, an empty dismissed person is cleaned up."""
    from sqlmodel import Session

    from src.server.repository.people import PersonRepository

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


@contextmanager
def _moved(account):
    """The account's database moved, as a restore onto another server does:
    a copy elsewhere, and its routing row rewritten to it (backups.py restore).
    Put back after."""
    *_, tenant_id, tenant_url = account
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    control = create_engine(get_settings().control_plane_database_url)
    with PostgresContainer(PG_IMAGE) as elsewhere:
        moved_url = _ensure_psycopg2(elsewhere.get_connection_url())
        _provision_tenant_db(moved_url, project_root)
        with control.begin() as conn:
            conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                         {"u": moved_url, "t": tenant_id})
        try:
            yield moved_url
        finally:
            with control.begin() as conn:
                conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                             {"u": tenant_url, "t": tenant_id})
            control.dispose()


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["library", "project"])
def test_a_public_link_reaches_the_account_where_its_routing_says(account, kind):
    """A link made public before the account's database moved opens the
    database it moved to: the link keeps no address of its own."""
    from sqlmodel import Session

    from src.server.models.tenant import Project
    from src.server.repository.libraries import LibraryRepository

    client, headers, *_ = account
    r = client.post("/v1/libraries", json={"name": f"Shared {kind}", "root_path": f"/shared-{kind}",
                                           "is_public": kind == "library"}, headers=headers)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    if kind == "project":
        r = client.post("/v1/projects", json={"name": "Shared", "visibility": "public"}, headers=headers)
        assert r.status_code in (200, 201), r.text
        link_id = r.json()["project_id"]
        url = f"/v1/public/projects/{link_id}"
    else:
        url = f"/v1/libraries/{library_id}"
    assert client.get(url).status_code == 200

    with _moved(account) as moved_url:
        engine = create_engine(moved_url)
        with Session(engine) as session:
            moved = LibraryRepository(session).create(name="After the move", root_path=f"/shared-{kind}")
            session.execute(text("UPDATE libraries SET library_id = :id, is_public = :p WHERE library_id = :old"),
                            {"id": library_id, "p": kind == "library", "old": moved.library_id})
            if kind == "project":
                session.add(Project(project_id=link_id, name="After the move", visibility="public"))
            session.commit()
        engine.dispose()

        r = client.get(url)

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "After the move"


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


@pytest.mark.fast
def test_public_links_keep_no_address_of_their_own():
    """A public link names its account; the account's routing row says where
    its database is (src/server/api/middleware.py _routed)."""
    from src.server.models.control_plane import PublicLibrary, PublicProject

    for model in (PublicLibrary, PublicProject):
        assert "connection_string" not in model.model_fields, model.__name__
