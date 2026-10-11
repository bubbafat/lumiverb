"""For API tests on the first-run `control` fixture (tests/test_first_run_setup.py),
whose account database is mocked: the first admin's account given a real,
migrated database, and someone else signed in to it."""

from __future__ import annotations

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text

from tests.test_first_run_setup import PASSWORD


def make_database(control_url: str, tenant_id: str) -> None:
    """The account's database, made, routed (the fixture's template hides the
    password) and migrated. Dropped by drop_databases."""
    from src.server.lifecycle.migrate import migrate

    url = make_url(control_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{tenant_id}"'))
    admin.dispose()
    tenant_url = url.set(database=tenant_id).render_as_string(hide_password=False)
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    engine = create_engine(control_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                     {"u": tenant_url, "t": tenant_id})
    engine.dispose()
    migrate(control_url, say=lambda _: None)


def drop_databases(control_url: str) -> None:
    """Every account database the control plane routes to on its server."""
    engine = create_engine(control_url)
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(text("SELECT tenant_id FROM tenants")).all()]
    engine.dispose()
    admin = create_engine(make_url(control_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        for name in names:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def admin_with_database(client: TestClient, control_url: str) -> tuple[dict[str, str], str]:
    """The first admin signed in, their account's database made: (headers, tenant_id)."""
    from src.server.setup import create_first_admin

    admin = create_first_admin("robert@example.com", PASSWORD)
    make_database(control_url, admin.tenant_id)
    token = client.post("/v1/auth/login", json={"email": "robert@example.com", "password": PASSWORD}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}, admin.tenant_id


def someone(client: TestClient, tenant_id: str, role: str) -> dict[str, str]:
    """Another person in the account, signed in."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import UserRepository

    email = f"{role}@example.com"
    with get_control_session() as session:
        UserRepository(session).create(tenant_id, email, bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
                                       role=role)
    token = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
