"""Migration b8d4f0a2c6e1: Pause all is a row of its own.

An account whose every switch of that time is paused (Pause all, before it
was stored) gets the "all" row, so a producer added since (capture,
location) is paused too. One partly paused gets nothing. Down puts each
switch's row back. Runs Alembic against a throwaway database only.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "4e3bcb0d9be8", "b8d4f0a2c6e1"
OLD_SWITCHES = ("scans", "upkeep", "probe", "analysis_proxy", "scenes", "scene_vision", "vision", "ocr", "clip",
                "faces", "transcript")


def _rows(engine) -> dict:
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text(
            "SELECT target, paused_by FROM producer_pauses WHERE scope = 'work'"))}


def _pause(engine, targets, by="usr_1") -> None:
    with engine.begin() as conn:
        for t in targets:
            conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                              " VALUES (:t, 'work', :by, now())"), {"t": t, "by": by})


@pytest.mark.migration
@pytest.mark.parametrize("paused,stays_paused", [(OLD_SWITCHES, True), (OLD_SWITCHES[:-1], False)])
def test_an_account_paused_before_new_producers_stays_paused(paused, stays_paused) -> None:
    from src.server.repository import lineage
    from src.shared.producers import pause_targets

    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        _pause(engine, paused)
        _alembic(url, "upgrade", AFTER)
        assert ("all" in _rows(engine)) is stays_paused
        with Session(engine) as session:
            held = lineage.pauses(session).work
            assert ("capture" in held) is stays_paused
            assert ("all" in held) is False  # read as every switch, never as one called "all"
            if stays_paused:
                assert set(pause_targets()) <= set(held) and held["capture"]["paused_by"] == "usr_1"
                assert lineage.upkeep_paused(session)
        _alembic(url, "downgrade", BEFORE)
        rows = _rows(engine)
        assert "all" not in rows and set(rows) == set(paused)
        engine.dispose()
