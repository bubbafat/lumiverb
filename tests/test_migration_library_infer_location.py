"""Migration b5d9e3f7a1c4: Infer location is each library's (ADR-017).

Where the account's setting was on, every library is on, and the account's
setting goes (the Inference window stays); where it was off or never set,
every library is off. Every guess's lineage gets the hash the location
producer has now, so nothing is redone. Down puts the account's setting
back on if a library's was, and the old hash. Runs Alembic against a
throwaway testcontainers database only.
"""

import json

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from src.shared.producers import effective_settings, settings_hash
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "a3c7e1f5b9d2", "b5d9e3f7a1c4"
OLD_HASH = settings_hash({"infer_location": True})


def _account(url: str, overrides: dict | None) -> None:
    """Two libraries, a guess's lineage and a person's, and the location producer's settings."""
    engine = create_engine(url)
    with engine.begin() as conn:
        for lib in ("lib_a", "lib_b"):
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES (:l, :l, '/x', 'active', now(), now())"), {"l": lib})
        filler = {"text": "x", "character varying": "x", "integer": 0, "bigint": 0, "boolean": False,
                  "double precision": 0.0, "timestamp with time zone": "2026-01-01T00:00:00Z"}
        extra = {name: filler[dtype] for name, dtype in conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_name = 'assets' AND is_nullable = 'NO' AND column_default IS NULL"
            "   AND column_name NOT IN ('asset_id', 'library_id', 'rel_path', 'file_size', 'media_type')"
        )).all()}
        names = ", ".join(extra)
        for aid in ("guessed", "placed"):
            conn.execute(text(
                f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, {names})"
                f" VALUES (:a, 'lib_a', :a, 1, 'image', {', '.join(':' + k for k in extra)})"), {"a": aid, **extra})
        for aid, producer, h in (("guessed", "location", OLD_HASH), ("placed", "person", "")):
            conn.execute(text(
                "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                " produced_at, outcome, attempts) VALUES (:a, 'location', :p, '1', :h, now(), 'ok', 0)"),
                {"a": aid, "p": producer, "h": h})
        if overrides is not None:
            conn.execute(text("INSERT INTO system_metadata (key, value, updated_at)"
                              " VALUES ('producer.location', :v, now())"), {"v": json.dumps(overrides)})
    engine.dispose()


def _state(url: str) -> dict:
    engine = create_engine(url)
    with engine.connect() as conn:
        out = {
            "libraries": dict(conn.execute(text("SELECT library_id, infer_location FROM libraries")).all())
            if conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_name = 'libraries'"
                                 " AND column_name = 'infer_location'")).first() else None,
            "hashes": dict(conn.execute(text("SELECT asset_id, settings_hash FROM artifact_lineage")).all()),
            "overrides": conn.execute(text(
                "SELECT value FROM system_metadata WHERE key = 'producer.location'")).scalar(),
        }
    engine.dispose()
    if out["overrides"] is not None:
        out["overrides"] = json.loads(out["overrides"])
    return out


@pytest.mark.migration
@pytest.mark.parametrize(("overrides", "on", "left"), [
    ({"infer_location": True, "inference_minutes": 120}, True, {"inference_minutes": 120}),
    ({"infer_location": True}, True, None),
    ({"inference_minutes": 90}, False, {"inference_minutes": 90}),
    (None, False, None),
])
def test_infer_location_moves_to_every_library_and_nothing_is_redone(overrides, on, left) -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        engine.dispose()
        _alembic(url, "upgrade", BEFORE)
        _account(url, overrides)

        _alembic(url, "upgrade", AFTER)
        now = settings_hash(effective_settings("lumiverb.location.guess"))  # the producer's hash as the code has it
        assert _state(url) == {"libraries": {"lib_a": on, "lib_b": on},
                               "hashes": {"guessed": now, "placed": ""}, "overrides": left}

        _alembic(url, "downgrade", BEFORE)
        back = {**(left or {}), "infer_location": True} if on else left
        assert _state(url) == {"libraries": None, "hashes": {"guessed": OLD_HASH, "placed": ""}, "overrides": back}
