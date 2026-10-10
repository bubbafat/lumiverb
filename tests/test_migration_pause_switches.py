"""Migration c4e1d7a9b2f3: pause switches (Robert, Oct 9, after #50).

Nothing stores "pause all" any more: each switch (Scans, Upkeep, each
producer the scheduler makes) is paused on its own. An account paused with
the old 'all' row comes out with every switch paused, each keeping who
paused it and when; a switch already paused keeps its own. Runs Alembic
against a throwaway testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "a1839b2c9c58", "c4e1d7a9b2f3"


def _rows(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text("SELECT target, paused_by FROM processing_paused"))}


@pytest.mark.migration
def test_an_old_pause_all_becomes_every_switch_paused() -> None:
    from src.shared.producers import pause_targets

    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO processing_paused (target, paused_by, paused_at)"
                              " VALUES ('all', 'usr_1', now()), ('vision', 'usr_2', now())"))
        _alembic(url, "upgrade", AFTER)
        rows = _rows(engine)
        # no 'all' left; producers added since (capture, location) have no row of that time
        assert set(rows) == set(pause_targets()) - {"capture", "location"}
        assert rows["vision"] == "usr_2" and rows["scans"] == "usr_1" and rows["upkeep"] == "usr_1"
        _alembic(url, "downgrade", BEFORE)  # down: the rows stay, as switches
        assert "all" not in _rows(engine)
        engine.dispose()


@pytest.mark.migration
def test_an_account_not_paused_stays_running() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO processing_paused (target, paused_by, paused_at) VALUES ('ocr', NULL, now())"))
        _alembic(url, "upgrade", AFTER)
        assert _rows(engine) == {"ocr": None}
        engine.dispose()
