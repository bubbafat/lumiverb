"""Migration d9f3a6b1c2e8: lineage backfilled from what the artifacts already say.

Made the way today's CLI makes them: current (version 1, the v1 settings).
Made by the macOS app: an unknown producer, so stale. A person's transcript:
a person's. The CLI's most used vision model becomes the account's.
Runs Alembic against a throwaway testcontainers database only.
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "c4d8e1f2a7b6", "d9f3a6b1c2e8"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_the_frozen_v1_hashes_are_the_registry_s():
    """The migration keeps its own copy (it must never change); on Oct 8 they agreed."""
    import importlib.util

    from src.shared.producers import PRODUCERS, effective_settings, settings_hash

    spec = importlib.util.spec_from_file_location(
        "m", os.path.join(PROJECT_ROOT, "migrations/tenant/versions/d9f3a6b1c2e8_artifact_lineage.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for artifact, (producer, h) in m.V1.items():
        assert PRODUCERS[artifact].producer == producer
        assert PRODUCERS[artifact].version == "1"
        assert settings_hash(effective_settings(artifact)) == h, artifact
    assert m._vision_hash("qwen3-vl:8b") == settings_hash(effective_settings("vision", account={"model": "qwen3-vl:8b"}))
    assert m._vision_hash("qwen3-vl:8b", m._OCR_PROMPT) == settings_hash(
        effective_settings("ocr", account={"model": "qwen3-vl:8b"}))


@pytest.mark.migration
def test_lineage_is_backfilled_from_the_artifacts() -> None:
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

            def asset(aid: str, media: str, **cols) -> None:
                values = {**extra, **cols}
                names = ", ".join(values)
                conn.execute(text(
                    f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, sha256, {names})"
                    f" VALUES (:a, 'lib', :a, 1, :m, :sha, {', '.join(':' + k for k in values)})"
                ), {"a": aid, "m": media, "sha": f"sha-{aid}", **values})

            asset("img_cli", "image", proxy_key="p/1", face_count=0)
            asset("img_mac", "image", proxy_key="p/2", face_count=1)
            asset("img_old_model", "image")
            asset("vid_cli", "video", has_transcript=True, transcript_source="whisper")
            asset("vid_mac", "video", has_transcript=True, transcript_source="whisper.cpp")
            asset("vid_person", "video", has_transcript=True, transcript_source="manual")
            asset("vid_silent", "video", has_transcript=False, transcript_source="whisper")
            asset("vid_none", "video")

            def meta(aid: str, model: str, version: str, data: str, at: str) -> None:
                conn.execute(text(
                    "INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, data, generated_at)"
                    " VALUES (:i, :a, :m, :v, CAST(:d AS jsonb), CAST(:t AS timestamptz))"
                ), {"i": f"m-{aid}-{model}", "a": aid, "m": model, "v": version, "d": data, "t": at})

            meta("img_cli", "qwen3-vl:8b", "1", '{"description": "d", "has_text": false}', "2026-10-08T10:00:00Z")
            meta("img_mac", "openai-compatible", "qwen3-vl:8b", '{"description": "d"}', "2026-10-08T10:00:00Z")
            meta("img_old_model", "llava:7b", "1", '{"description": "d"}', "2026-10-01T10:00:00Z")
            meta("vid_cli", "qwen3-vl:8b", "1", '{"description": "d"}', "2026-10-08T10:00:00Z")  # outvotes llava
            for aid, model, version in (("img_cli", "clip", "ViT-B-32-openai"),
                                        ("img_mac", "apple_vision", "feature_print_v1")):
                conn.execute(text(
                    "INSERT INTO asset_embeddings (embedding_id, asset_id, model_id, model_version, embedding_vector,"
                    " created_at) VALUES (:i, :a, :m, :v, '[0.1,0.2]', now())"
                ), {"i": f"e-{aid}", "a": aid, "m": model, "v": version})
            conn.execute(text("INSERT INTO faces (face_id, asset_id, detection_model, detection_model_version,"
                              " created_at) VALUES ('f1', 'img_mac', 'apple_vision', '1', now())"))

        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            rows = {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in conn.execute(text(
                "SELECT asset_id, artifact, producer, producer_version, outcome, source_sha256 FROM artifact_lineage"))}
            seeded = conn.execute(text("SELECT count(*) FROM system_metadata WHERE key = 'vision_model'")).scalar()

        assert seeded == 0  # the model is chosen in Settings → AI, nowhere else
        assert rows[("img_cli", "proxy")] == ("proxy", "1", "ok", "sha-img_cli")
        assert rows[("img_cli", "faces")][:3] == ("insightface", "1", "empty")
        assert rows[("img_mac", "faces")][0] == "unknown"
        assert rows[("img_cli", "clip")][:2] == ("clip", "1")
        assert rows[("img_mac", "clip")][0] == "unknown"
        assert rows[("img_cli", "vision")][:2] == ("vision", "1")
        assert rows[("img_cli", "ocr")][:3] == ("ocr", "1", "empty")
        assert rows[("img_mac", "vision")][0] == "unknown"
        assert ("img_mac", "ocr") not in rows  # never had OCR
        assert rows[("img_old_model", "vision")][:2] == ("vision", "1")  # recorded with llava's hash: stale
        assert rows[("vid_cli", "transcript")][:3] == ("whisper", "1", "ok")
        assert rows[("vid_silent", "transcript")][:3] == ("whisper", "1", "empty")
        assert rows[("vid_mac", "transcript")][0] == "unknown"
        assert rows[("vid_person", "transcript")][0] == "person"
        assert not [k for k in rows if k[0] == "vid_none"]

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('artifact_lineage')")).scalar() is None
        engine.dispose()
