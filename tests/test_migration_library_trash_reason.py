"""Migration v7w8x9y0z1a2: clips trashed with their library get the reason
'library', so projects stop promising they come back when the file does.

Runs Alembic against a throwaway testcontainers database only.
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "u6v7w8x9y0z1", "v7w8x9y0z1a2"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def _reasons(conn) -> dict[str, str | None]:
    rows = conn.execute(text("SELECT asset_id, deleted_reason FROM assets")).all()
    return {r[0]: r[1] for r in rows}


@pytest.mark.migration
def test_clips_of_trashed_libraries_are_marked_and_unmarked() -> None:
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)

        with engine.begin() as conn:
            for lib, status in (("lib_gone", "trashed"), ("lib_live", "active")):
                conn.execute(text(
                    "INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                    " VALUES (:id, :id, '/' || :id, :status, now(), now())"
                ), {"id": lib, "status": status})
            # Every other required column without a default, filled by type.
            filler = {"text": "x", "character varying": "x", "integer": 0, "bigint": 0,
                      "boolean": False, "double precision": 0.0,
                      "timestamp with time zone": "2026-01-01T00:00:00Z"}
            extra = {
                name: filler[dtype]
                for name, dtype in conn.execute(text(
                    "SELECT column_name, data_type FROM information_schema.columns"
                    " WHERE table_name = 'assets' AND is_nullable = 'NO' AND column_default IS NULL"
                    "   AND column_name NOT IN ('asset_id', 'library_id', 'rel_path', 'file_size', 'media_type')"
                )).all()
            }
            for asset_id, lib, deleted, reason in (
                ("ast_with_library", "lib_gone", True, None),   # went with its library
                ("ast_user_first", "lib_gone", True, "user"),   # trashed before the library
                ("ast_missing_live", "lib_live", True, None),   # legacy "missing", library live
                ("ast_active", "lib_live", False, None),
            ):
                cols = ", ".join(extra)
                vals = ", ".join(f":{c}" for c in extra)
                conn.execute(text(
                    "INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type,"
                    f" deleted_at, deleted_reason{', ' + cols if cols else ''})"
                    " VALUES (:a, :lib, :a, 1, 'video',"
                    f" CASE WHEN :deleted THEN now() END, :reason{', ' + vals if vals else ''})"
                ), {"a": asset_id, "lib": lib, "deleted": deleted, "reason": reason, **extra})

        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert _reasons(conn) == {
                "ast_with_library": "library",
                "ast_user_first": "user",
                "ast_missing_live": None,
                "ast_active": None,
            }

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            assert _reasons(conn)["ast_with_library"] is None
            assert _reasons(conn)["ast_user_first"] == "user"
        engine.dispose()
