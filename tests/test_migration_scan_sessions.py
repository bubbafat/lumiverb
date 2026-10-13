"""Migration e7b9d1f3a5c2: a scan's session on the server (ADR-018 decision 12).

scan_sessions holds a scan while it runs and scan_found what it found; both
go with their library, a scan's listing with its scan, and a scan's options
are checked. Going back down drops both. Runs Alembic against a throwaway
testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "d4f7a2c9e1b3", "e7b9d1f3a5c2"


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name IN ('scan_sessions', 'scan_found')"))}


def _indexes(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('scan_sessions', 'scan_found')"))}


def _scan(conn, scan_id: str, library_id: str, **values) -> None:
    conn.execute(text(
        "INSERT INTO scan_sessions (scan_id, library_id, started_at, expires_at, media_type, moves)"
        " VALUES (:s, :l, now(), now() + interval '1 day', :media_type, :moves)"
    ), {"s": scan_id, "l": library_id, "media_type": "all", "moves": "ask", **values})


@pytest.mark.migration
def test_scans_and_their_listings_are_kept_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        assert _tables(engine) == set()

        _alembic(url, "upgrade", AFTER)
        assert _tables(engine) == {"scan_sessions", "scan_found"}
        assert {"scan_sessions_pkey", "scan_found_pkey", "ix_scan_sessions_library_id", "ix_scan_sessions_expires_at",
                "ix_scan_found_moving"} <= _indexes(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at,"
                              " is_public, revision) VALUES ('lib_1', 'L', '/l', 'active', now(), now(), false, 0)"))
            _scan(conn, "scan_1", "lib_1")
            conn.execute(text("INSERT INTO scan_found (scan_id, rel_path, file_size, media_type)"
                              " VALUES ('scan_1', 'a.jpg', 10, 'image')"))
            row = conn.execute(text("SELECT scope, thorough, force, dry_run, settling FROM scan_sessions"
                                    " JOIN scan_found USING (scan_id)")).one()
        assert tuple(row) == ("", False, False, False, False)
        for bad in ({"media_type": "audio"}, {"moves": "sometimes"}):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                _scan(conn, "scan_2", "lib_1", **bad)
        with engine.begin() as conn:  # a scan's listing goes with it, and a scan with its library
            conn.execute(text("DELETE FROM libraries WHERE library_id = 'lib_1'"))
            assert conn.execute(text("SELECT (SELECT count(*) FROM scan_sessions) + (SELECT count(*) FROM scan_found)"
                                     )).scalar() == 0

        _alembic(url, "downgrade", BEFORE)
        assert _tables(engine) == set()
        engine.dispose()
