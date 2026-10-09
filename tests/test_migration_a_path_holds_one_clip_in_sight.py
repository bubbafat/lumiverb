"""Migration b77f2a03eee2: a path holds one clip in sight (Robert, Oct 9).

A changed file is a new clip and the old one is archived where it was, so
(library_id, rel_path) is unique only among clips in sight. Going back down
restores the full unique constraint. An emptied-trash record names a file,
so a path can have several. Runs Alembic against a throwaway
testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "0c663a6f08e1", "b77f2a03eee2"


def _ignored_files_keys(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'ignored_files' AND indexdef LIKE 'CREATE UNIQUE%'"))}


def _unique_on_path(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'assets' AND indexdef LIKE 'CREATE UNIQUE%'"
            " AND indexdef LIKE '%(library_id, rel_path)%'"))}


@pytest.mark.migration
def test_a_path_is_unique_only_among_clips_in_sight_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        assert _unique_on_path(engine) == {"uq_assets_library_rel_path"}

        _alembic(url, "upgrade", AFTER)
        assert _unique_on_path(engine) == {"uq_assets_library_rel_path_in_sight"}
        with engine.connect() as conn:
            where = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_assets_library_rel_path_in_sight'")).scalar()
        assert "WHERE (deleted_at IS NULL)" in where
        keys = " ".join(_ignored_files_keys(engine))
        assert "COALESCE(sha256" in keys and "(ignored_id)" in keys

        _alembic(url, "downgrade", BEFORE)
        assert _unique_on_path(engine) == {"uq_assets_library_rel_path"}
        assert [k for k in _ignored_files_keys(engine) if "ignored_files_pkey" in k][0].endswith(
            "(library_id, rel_path)")
        engine.dispose()
