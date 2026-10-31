"""Control migration e4a8c2f6d0b9: each day's site visits (site_visit_days, site_visit_people), a day's people going
with it; a downgrade takes both away. Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "c5e9a3f7b1d4", "e4a8c2f6d0b9"


@pytest.mark.migration
def test_a_day_is_kept_once_with_its_people_and_a_downgrade_takes_them_away() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", AFTER)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO site_visit_days (day, browsers, sources)"
                              " VALUES ('2026-10-30', '{\"try.example.org\": 3}', '{}')"))
            conn.execute(text("INSERT INTO site_visit_people (day, first_at, sites, device, why)"
                              " VALUES ('2026-10-30', '2026-10-30T10:15:00Z', '[\"try.example.org\"]',"
                              " 'iPhone Safari', '[\"tried the demo x1\"]')"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("INSERT INTO site_visit_days (day, browsers, sources) VALUES ('2026-10-30', '{}', '{}')"))
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("INSERT INTO site_visit_people (day, first_at, sites, device, why)"
                              " VALUES ('2026-10-29', now(), '[]', '', '[]')"))
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM site_visit_days"))
            assert conn.execute(text("SELECT count(*) FROM site_visit_people")).scalar() == 0, "a day's people go"

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            tables = {r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'site_visit_%'"))}
        engine.dispose()
        assert tables == set()
