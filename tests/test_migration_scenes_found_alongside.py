"""Migration d4f7a2c9e1b3: scenes found again beside a video's scenes.

A redo's scenes are staged (video_scenes.staged) beside the ones shown, and
a run's chunks record how it finds them (video_index_chunks.run); a video's
scene numbers are unique within each set. Going back down restores the
unique constraint on (asset_id, scene_index). Runs Alembic against a
throwaway testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "c8e2f4a6b1d9", "d4f7a2c9e1b3"


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table})}


def _scene_numbers(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                                 " WHERE conname = 'uq_video_scenes_asset_scene_index'")).scalar()


@pytest.mark.migration
def test_scenes_are_numbered_per_set_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        assert _scene_numbers(engine) == "UNIQUE (asset_id, scene_index)"

        _alembic(url, "upgrade", AFTER)
        assert "staged" in _columns(engine, "video_scenes") and "run" in _columns(engine, "video_index_chunks")
        assert _scene_numbers(engine) == "UNIQUE NULLS NOT DISTINCT (asset_id, staged, scene_index)"
        with engine.connect() as conn:
            check = conn.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                                      " WHERE conname = 'ck_video_scenes_staged'")).scalar()
        assert "finding" in check and "found" in check

        _alembic(url, "downgrade", BEFORE)
        assert "staged" not in _columns(engine, "video_scenes") and "run" not in _columns(engine, "video_index_chunks")
        assert _scene_numbers(engine) == "UNIQUE (asset_id, scene_index)"
        engine.dispose()
