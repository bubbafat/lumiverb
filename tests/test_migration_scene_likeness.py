"""Tenant migration c4e8a2d6f0b3: each scene's picture's CLIP vector (video_scenes.likeness, dimensionless) and the
model it's from (likeness_model), every scene there before without one; and a way back that drops them with
their lineage (src/producers/scene_likeness/)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

pytestmark = pytest.mark.slow

BEFORE, AFTER = "b5d1f7a3c9e2", "c4e8a2d6f0b3"


def _database(pg) -> str:
    """The test's own database, vector made in it first (as Lumiverb's install does)."""
    url = _ensure_psycopg2(pg.get_connection_url())
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    return url


def _run(url: str, sql: str, **params) -> list:
    """What the statement returns (its rows; [] for one that returns none), committed."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return result.all() if result.returns_rows else []
    finally:
        engine.dispose()


def _columns(url: str) -> dict[str, str]:
    return dict(_run(url, "SELECT column_name, udt_name FROM information_schema.columns"
                          " WHERE table_name = 'video_scenes' AND column_name LIKE 'likeness%'"))


def test_every_scene_is_without_a_vector_until_its_embedded_and_the_way_back() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _database(pg)
        _alembic(url, "upgrade", BEFORE)
        _run(url, "INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                  " VALUES ('lib_a', 'a', '/a', 'active', now(), now())")
        engine = create_engine(url)
        with Session(engine) as session:  # a video as one made before
            video = make_clip(session, "lib_a", "a.mov", media_type="video")
            session.commit()
        engine.dispose()
        _run(url, "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                  " created_at) VALUES ('scn_1', :a, 0, 0, 5000, 0, now())", a=video)

        _alembic(url, "upgrade", AFTER)
        assert _columns(url) == {"likeness": "vector", "likeness_model": "text"}
        assert _run(url, "SELECT likeness, likeness_model FROM video_scenes") == [(None, None)]
        # Dimensionless: the model it's from says how long (CLIP ViT-B-32's are 512).
        _run(url, "UPDATE video_scenes SET likeness = CAST(:v AS vector), likeness_model = 'ViT-B-32-openai'",
             v=str([0.5] * 512))
        assert _run(url, "SELECT vector_dims(likeness) FROM video_scenes") == [(512,)]
        _run(url, "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                  " produced_at, outcome, attempts) VALUES (:a, 'lumiverb.video.scene_likeness',"
                  " 'lumiverb.video.scene_likeness', '1', 'h', now(), 'ok', 0)", a=video)

        _alembic(url, "downgrade", BEFORE)
        assert _columns(url) == {}
        assert _run(url, "SELECT count(*) FROM video_scenes") == [(1,)], "the scene stays"
        assert _run(url, "SELECT count(*) FROM artifact_lineage WHERE artifact = 'lumiverb.video.scene_likeness'") \
            == [(0,)], "its vectors' lineage goes with them"
        _alembic(url, "upgrade", AFTER)
        assert _run(url, "SELECT likeness, likeness_model FROM video_scenes") == [(None, None)], "and up again"
