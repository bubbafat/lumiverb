"""Tenant migration aec154348364: each library names its reader and keeps
its own Scans pause (ADR-018 phase 1).

The account's Scans switch (producer_pauses target "scans") becomes every
library's pause, by whoever paused it and when, then its row goes; Pause
all stays the account's. Going down, a library paused pauses the switch
again. Runs Alembic against a throwaway testcontainers database only."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_library_trash_reason import _alembic

BEFORE, AFTER = "b3d5f7a9c1e4", "aec154348364"
WHEN = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _libraries(conn) -> dict[str, tuple]:
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(text(
        "SELECT library_id, reader, scans_paused_by, scans_paused_at FROM libraries"))}


@pytest.mark.migration
@pytest.mark.parametrize("scans_paused", [True, False])
def test_the_scans_switch_becomes_each_librarys_and_comes_back(scans_paused: bool) -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for i in ("lib_a", "lib_b"):
                conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                                  " VALUES (:i, :i, '/x', 'active', now(), now())"), {"i": i})
            conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                              " VALUES ('all', 'work', 'usr_2', now()), ('vision', 'work', 'usr_3', now())"))
            if scans_paused:
                conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                                  " VALUES ('scans', 'work', 'usr_1', :at)"), {"at": WHEN})

        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            libraries = _libraries(conn)
            targets = {r[0] for r in conn.execute(text("SELECT target FROM producer_pauses"))}
        expected = ("usr_1", WHEN) if scans_paused else (None, None)
        assert libraries == {i: ("server", *expected) for i in ("lib_a", "lib_b")}
        assert targets == {"all", "vision"}  # the switch's row went; Pause all and the rest stay

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            back = {r[0]: (r[1], r[2]) for r in conn.execute(text(
                "SELECT target, paused_by, paused_at FROM producer_pauses"))}
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'libraries'"))}
        assert ("scans" in back) is scans_paused
        if scans_paused:
            assert back["scans"] == ("usr_1", WHEN)
        assert not columns & {"reader", "scans_paused_at", "scans_paused_by"}
        engine.dispose()
