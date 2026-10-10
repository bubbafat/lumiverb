"""Migration f3c4d5e6a7b8: scene numbers once per clip; crashes counted per clip.

A clip whose scene detection resumed had its scenes numbered from 0 again:
they're numbered again in time order, then (asset_id, scene_index) is
unique. producer_crashes keeps each clip's crashes in a row, and goes with
its clip. Going back down drops both. Runs Alembic against a throwaway
testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "e8a1b2c3d4f5", "f3c4d5e6a7b8"


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}


@pytest.mark.migration
def test_scenes_are_numbered_again_once_each_and_crashes_are_kept_per_clip() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib', 'lib', '/lib', 'active', now(), now())"))
            filler = {"text": "x", "character varying": "x", "integer": 0, "bigint": 0, "boolean": False,
                      "double precision": 0.0, "timestamp with time zone": "2026-01-01T00:00:00Z"}
            extra = {name: filler[dtype] for name, dtype in conn.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_name = 'assets' AND is_nullable = 'NO' AND column_default IS NULL"
                "   AND column_name NOT IN ('asset_id', 'library_id', 'rel_path', 'file_size', 'media_type')"
            )).all()}
            names = ", ".join(extra)
            for aid in ("vid", "other"):
                conn.execute(text(
                    f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, {names})"
                    f" VALUES (:a, 'lib', :a, 1, 'video', {', '.join(':' + k for k in extra)})"), {"a": aid, **extra})
            # A resumed clip: two runs each numbered from 0.
            for sid, aid, i, start in (("s1", "vid", 0, 0), ("s2", "vid", 1, 1000), ("s3", "vid", 0, 30000),
                                       ("s4", "vid", 1, 31000), ("s5", "other", 0, 0)):
                conn.execute(text(
                    "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                    " created_at) VALUES (:s, :a, :i, :t, :t, :t, now())"), {"s": sid, "a": aid, "i": i, "t": start})

        _alembic(url, "upgrade", AFTER)
        with engine.begin() as conn:
            numbered = dict(conn.execute(text("SELECT scene_id, scene_index FROM video_scenes")).all())
            assert numbered == {"s1": 0, "s2": 1, "s3": 2, "s4": 3, "s5": 0}
            with pytest.raises(Exception, match="uq_video_scenes_asset_scene_index"):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms,"
                                      " end_ms, rep_frame_ms, created_at) VALUES ('s6', 'vid', 3, 0, 0, 0, now())"))
            conn.execute(text("INSERT INTO producer_crashes (asset_id, artifact, crashes, error, updated_at)"
                              " VALUES ('other', 'scenes', 2, 'boom', now())"))
            conn.execute(text("DELETE FROM video_scenes WHERE asset_id = 'other'"))
            conn.execute(text("DELETE FROM assets WHERE asset_id = 'other'"))
            assert conn.execute(text("SELECT count(*) FROM producer_crashes")).scalar() == 0  # gone with its clip

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            assert "producer_crashes" not in _tables(conn)
            conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms,"
                              " rep_frame_ms, created_at) VALUES ('s7', 'vid', 0, 0, 0, 0, now())"))
        engine.dispose()
