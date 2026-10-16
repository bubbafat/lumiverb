"""Migration c8e2f4a6b1d9: face detection by level instead of gates one by one.

Photos' faces found with the gates the account had are current at Normal
(as the server's lineage rules count them: nothing is found again for the
change), ones found otherwise stay stale, the gates an account changed go
(its grouping settings stay), and videos' levels move up one for Off. Runs
Alembic against a throwaway testcontainers database only.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "b5d1e8a3c7f2", "c8e2f4a6b1d9"


def _migration():
    path = Path(__file__).parent.parent / "migrations/tenant/versions/c8e2f4a6b1d9_face_levels.py"
    spec = importlib.util.spec_from_file_location("c8e2_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.fast
def test_its_hashes_are_the_code_s():
    """Its copies of the settings hash: before levels as main had them (Oct 11), and after as the registry has them."""
    from src.shared.producers import effective_settings, settings_hash

    m = _migration()
    assert m._hash(m.OLD_DEFAULTS) == "8a9e204f61973fa2"
    assert m._hash(m.NEW_SETTINGS) == settings_hash(effective_settings("faces"))


@pytest.mark.migration
def test_faces_found_as_the_account_had_them_are_current_at_normal() -> None:
    from src.server.repository import lineage

    m = _migration()
    customized = {**m.OLD_DEFAULTS, "min_confidence": 0.7}
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
            for aid, media in [("made_then", "image"), ("made_defaults", "image"), ("named", "image"),
                               ("video", "video"), ("video_unknown", "video")]:
                conn.execute(text(
                    f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, face_count, {names})"
                    f" VALUES (:a, 'lib', :a, 1, :m, 1, {', '.join(':' + k for k in extra)})"),
                    {"a": aid, "m": media, **extra})

            def made(asset_id: str, artifact: str, producer: str, settings_hash: str, level=None) -> None:
                conn.execute(text(
                    "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                    " produced_at, outcome, attempts, level) VALUES (:a, :art, :p, '1', :h, now(), 'ok', 0, :l)"),
                    {"a": asset_id, "art": artifact, "p": producer, "h": settings_hash, "l": level})

            made("made_then", "faces", "insightface", m._hash(customized))  # with the account's gates
            made("made_defaults", "faces", "insightface", m._hash(m.OLD_DEFAULTS))  # before it changed them
            made("named", "faces", "person", "")
            made("video", "video_faces", "insightface-video", "d76cf8414b216951", level=0)
            made("video_unknown", "video_faces", "unknown", "", level=None)
            conn.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('producer.faces', :v, now())"),
                         {"v": json.dumps({"min_confidence": 0.7, "merge_distance": 0.3})})

        _alembic(url, "upgrade", AFTER)

        def rows() -> dict:
            with engine.connect() as conn:
                return {r[0]: (r[1], r[2]) for r in conn.execute(text(
                    "SELECT asset_id || ':' || artifact, settings_hash, level FROM artifact_lineage"))}

        new = m._hash(m.NEW_SETTINGS)
        assert rows() == {"made_then:faces": (new, 2), "made_defaults:faces": (m._hash(m.OLD_DEFAULTS), None),
                          "named:faces": ("", None), "video:video_faces": ("d76cf8414b216951", 1),
                          "video_unknown:video_faces": ("", None)}
        with engine.connect() as conn:
            kept = conn.execute(text("SELECT value FROM system_metadata WHERE key = 'producer.faces'")).scalar()
        assert json.loads(kept) == {"merge_distance": 0.3}  # the gate goes, the grouping stays
        # As the server counts them: the one found with the account's gates is current, at Normal
        # (today's server reads today's schema: migrated on to the latest first).
        _alembic(url, "upgrade", "head")
        with Session(engine) as session:
            counts = lineage.counts(session, "faces", lineage.desired(session, "faces"))
        assert (counts["current"], counts["stale"], counts["below"]) == (2, 1, 0)  # made_then, and the person's

        _alembic(url, "downgrade", BEFORE)
        assert rows()["made_then:faces"] == (m._hash(m.OLD_DEFAULTS), None)
        assert rows()["video:video_faces"] == ("d76cf8414b216951", 0)

        # An account that never changed a gate: everything found with the defaults is current.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM system_metadata WHERE key = 'producer.faces'"))
        _alembic(url, "upgrade", AFTER)
        assert rows()["made_then:faces"] == (new, 2) and rows()["made_defaults:faces"] == (new, 2)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM system_metadata WHERE key = 'producer.faces'")).scalar() == 0
        engine.dispose()
