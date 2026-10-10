"""Migration e7b2c9d4a1f3: guesses from fixes already out of sight are
checked again (ADR-017 phase 3).

A guess made from a fix that's trashed, archived, missing or deleted for
good is due as basis_gone; one from fixes in sight, one already marked, one
a stronger reason waits on and a person's location aren't touched; running
it again changes nothing; 'outside_window' becomes a recheck reason. Runs
Alembic against a throwaway testcontainers database only.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "c4e8a1d7b3f9", "e7b2c9d4a1f3"


def _mark_gone_sql() -> str:
    path = Path(__file__).parent.parent / "migrations/tenant/versions/e7b2c9d4a1f3_location_basis_gone.py"
    spec = importlib.util.spec_from_file_location("e7b2_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.MARK_GONE


@pytest.mark.migration
def test_guesses_from_fixes_out_of_sight_are_checked_again() -> None:
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
            clips = {"inside": None, "trashed": "user", "archived": "archived", "missing": "missing"}
            clips |= {g: None for g in ("g_in", "g_trashed", "g_archived", "g_missing", "g_purged", "g_marked",
                                        "g_changed", "g_new", "placed")}
            for aid, reason in clips.items():
                conn.execute(text(
                    f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, deleted_at,"
                    f" deleted_reason, {names}) VALUES (:a, 'lib', :a, 1, 'image',"
                    f" CASE WHEN CAST(:r AS text) IS NULL THEN NULL ELSE now() END, :r,"
                    f" {', '.join(':' + k for k in extra)})"), {"a": aid, "r": reason, **extra})

            def guess(asset_id: str, fixes: list[str], *, recheck: str | None = None, source: str = "time",
                      **flags) -> None:
                basis = {"fixes": [{"asset_id": f, "gap_min": 10.0} for f in fixes], **flags}
                conn.execute(text(
                    "INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_at, recheck)"
                    " VALUES (:a, 1, 2, 500, :s, 'applied', CAST(:b AS jsonb), now(), :r)"),
                    {"a": asset_id, "s": source, "b": json.dumps(basis), "r": recheck})

            guess("g_in", ["inside"])
            guess("g_trashed", ["inside", "trashed"])
            guess("g_archived", ["archived"])
            guess("g_missing", ["missing"])
            guess("g_purged", ["deleted-for-good"])
            guess("g_marked", ["trashed"], basis_gone=True)
            guess("g_changed", ["trashed"], recheck="basis_changed")
            guess("g_new", ["trashed"], recheck="new_fix")
            guess("placed", ["trashed"], source="person")

        _alembic(url, "upgrade", AFTER)
        want = {"g_in": None, "g_trashed": "basis_gone", "g_archived": "basis_gone", "g_missing": "basis_gone",
                "g_purged": "basis_gone", "g_marked": None, "g_changed": "basis_changed", "g_new": "basis_gone",
                "placed": None}

        def rechecks() -> dict:
            with engine.connect() as conn:
                return {r[0]: r[1] for r in conn.execute(text("SELECT asset_id, recheck FROM asset_location"))}

        assert rechecks() == want
        with engine.begin() as conn:
            conn.execute(text(_mark_gone_sql()))  # again: nothing changes
            conn.execute(text("UPDATE asset_location SET recheck = 'outside_window' WHERE asset_id = 'g_in'"))
        assert rechecks() == {**want, "g_in": "outside_window"}

        _alembic(url, "downgrade", BEFORE)
        assert rechecks() == {**want, "g_in": "basis_changed"}
        with engine.begin() as conn, pytest.raises(Exception, match="ck_asset_location_recheck"), conn.begin_nested():
            conn.execute(text("UPDATE asset_location SET recheck = 'outside_window' WHERE asset_id = 'g_in'"))
        engine.dispose()
