"""Control migration 8d107a0b4312: public links find their account's database
from its routing.

The copies of the address in public_libraries and public_projects go and
every link stays; going down puts the copies back from tenant_db_routing.
Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "7c2d4e6f8a1b", "8d107a0b4312"
OLD, ROUTED = "postgresql://old-place/ten_a", "postgresql://where-it-is/ten_a"


def _columns(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table})}


@pytest.mark.migration
def test_public_links_lose_their_copy_of_the_address_and_get_it_back_from_routing() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                              " VALUES ('ten_a', 'a', 'free', 'active', now())"))
            conn.execute(text("INSERT INTO tenant_db_routing (tenant_id, connection_string, region, created_at)"
                              " VALUES ('ten_a', :u, 'local', now())"), {"u": ROUTED})
            conn.execute(text("INSERT INTO public_libraries (library_id, tenant_id, connection_string, created_at)"
                              " VALUES ('lib_1', 'ten_a', :u, now())"), {"u": OLD})
            conn.execute(text("INSERT INTO public_projects (project_id, tenant_id, connection_string, created_at)"
                              " VALUES ('col_1', 'ten_a', :u, now())"), {"u": OLD})

        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            for table in ("public_libraries", "public_projects"):
                assert "connection_string" not in _columns(conn, table)
            assert conn.execute(text("SELECT library_id, tenant_id FROM public_libraries")).all() == [("lib_1", "ten_a")]
            assert conn.execute(text("SELECT project_id, tenant_id FROM public_projects")).all() == [("col_1", "ten_a")]

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT connection_string FROM public_libraries")).scalars().all() == [ROUTED]
            assert conn.execute(text("SELECT connection_string FROM public_projects")).scalars().all() == [ROUTED]
        engine.dispose()
