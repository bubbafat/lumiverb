"""Control migration a1f5d9c3e7b2: the site-visit tables e4a8c2f6d0b9 made are taken away, and a downgrade makes them
again. Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

MADE, GONE = "e4a8c2f6d0b9", "a1f5d9c3e7b2"


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'site_visit_%'"))}


@pytest.mark.migration
def test_the_site_visit_tables_go_and_a_downgrade_makes_them_again() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", MADE)
        assert _tables(engine) == {"site_visit_days", "site_visit_people"}

        _alembic(url, "upgrade", GONE)
        assert _tables(engine) == set()

        _alembic(url, "downgrade", MADE)
        assert _tables(engine) == {"site_visit_days", "site_visit_people"}
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO site_visit_days (day, browsers, sources) VALUES ('2026-10-30', '{}', '{}')"))
            conn.execute(text("INSERT INTO site_visit_people (day, first_at, sites, device, why)"
                              " VALUES ('2026-10-30', now(), '[]', '', '[]')"))
        engine.dispose()
