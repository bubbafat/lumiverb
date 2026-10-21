"""Tenant migration b5d1f7a3c9e2: Live Photos' identifier (assets.live_photo_id), its indexes, and a way back
that brings every motion into sight first (src/server/repository/live_photos.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

pytestmark = pytest.mark.slow

BEFORE, AFTER = "d9a3f5c1e7b4", "b5d1f7a3c9e2"


def _database(pg) -> str:
    """The test's own database, vector made in it first (as Lumiverb's install does). No role of its own: a role is
    the whole server's, which every test shares."""
    url = _ensure_psycopg2(pg.get_connection_url())
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    return url


def _one(url: str, sql: str, **params):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


def test_the_identifier_its_indexes_and_the_way_back() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _database(pg)
        _alembic(url, "upgrade", BEFORE)
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
        with Session(engine) as session:  # a clip as one made before: it never names the column
            photo = make_clip(session, "lib_a", "IMG_0001.HEIC")
            motion = make_clip(session, "lib_a", "IMG_0001.MOV", media_type="video")
            session.commit()
        engine.dispose()

        _alembic(url, "upgrade", AFTER)
        assert _one(url, "SELECT count(*) FROM active_assets WHERE live_photo_id IS NULL") == 2  # the view has it
        assert _one(url, "SELECT count(*) FROM pg_indexes WHERE indexname IN ('ix_assets_live_photo', 'ix_assets_stem')") == 2
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("UPDATE assets SET live_photo_id = 'ID-1' WHERE asset_id IN (:p, :m)"),
                         {"p": photo, "m": motion})
            conn.execute(text("UPDATE assets SET deleted_at = now(), deleted_reason = 'live_photo' WHERE asset_id = :m"),
                         {"m": motion})
        engine.dispose()

        _alembic(url, "downgrade", BEFORE)
        assert _one(url, "SELECT count(*) FROM active_assets") == 2, "its motion in sight again: nothing says what it's part of"
        assert _one(url, "SELECT count(*) FROM information_schema.columns"
                         " WHERE table_name = 'assets' AND column_name = 'live_photo_id'") == 0
        _alembic(url, "upgrade", AFTER)
        assert _one(url, "SELECT count(*) FROM active_assets WHERE live_photo_id IS NULL") == 2, "and up again"
