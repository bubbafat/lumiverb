"""Migration fbd7f3be1cb0: upgrades, correction history, and how each scene's
description was made (ADR-016 phase 3, piece 5).

A scene already described takes the lineage its video recorded; one not
described yet has none. A video half-described before the migration with
older settings can't be told apart: every scene gets the video's last record.
Runs Alembic against a throwaway testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "fab90a220ba5", "fbd7f3be1cb0"


@pytest.mark.migration
def test_scenes_take_their_videos_lineage_and_it_goes_back_down() -> None:
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
            conn.execute(text(
                f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, {names})"
                f" VALUES ('vid', 'lib', 'v.mov', 1, 'video', {', '.join(':' + k for k in extra)})"), extra)
            for i, description in enumerate(("described", None)):
                conn.execute(text(
                    "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                    " description, created_at) VALUES (:s, 'vid', :i, 0, 1, 0, :d, now())"),
                    {"s": f"s{i}", "i": i, "d": description})
            conn.execute(text(
                "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                " source_sha256, produced_at, outcome, attempts) VALUES ('vid', 'scene_vision', 'scene-vision',"
                " '1', 'h1', 'sha', now(), 'ok', 0)"))

        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            scenes = dict(conn.execute(text("SELECT scene_id, lineage FROM video_scenes ORDER BY scene_id")).all())
            tables = {r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}
        assert scenes == {"s0": {"producer": "scene-vision", "version": "1", "settings_hash": "h1"}, "s1": None}
        assert {"producer_upgrades", "producer_upgrade_items", "correction_history"} <= tables  # until be66e81c7e5d

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'video_scenes'"))}
            tables = {r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}
        assert "lineage" not in cols
        assert not {"producer_upgrades", "producer_upgrade_items", "correction_history"} & tables
        engine.dispose()
