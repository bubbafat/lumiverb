"""Migration f4c2a9e7b1d3: content hashes name their scheme, and the columns say what they hold.

Every bare SHA-256 becomes "sha256:" and its digits (lowercased); an fp1
stays as it was; a value that is neither, which no hasher of ours made, is
no content hash and goes (NULL). Columns sha256 / legacy_sha256 /
source_sha256 become content_hash / previous_hash / source_hash, a redo's run
says source_hash, active_assets shows the new name, and a check holds every
value to the grammar. Going down takes each "sha256:" off again. Runs
Alembic against a throwaway testcontainers database only.
"""

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "e2b7d4f9a1c6", "f4c2a9e7b1d3"

HEX = "0123456789abcdef" * 4
FP1 = "fp1:" + "fedcba9876543210" * 4

_FILLER = {"text": "x", "character varying": "x", "integer": 0, "bigint": 0, "smallint": 0, "boolean": False,
           "double precision": 0.0, "timestamp with time zone": "2026-01-01T00:00:00Z", "jsonb": "{}"}


def _insert(conn, table: str, **cols) -> None:
    """A row with the given columns and a filler in every other one that must have a value."""
    extra = {name: _FILLER[dtype] for name, dtype in conn.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_name = :t AND is_nullable = 'NO' AND column_default IS NULL AND is_identity = 'NO'"),
        {"t": table}).all()
        if name not in cols}
    values = {**extra, **cols}
    conn.execute(text(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join(':' + k for k in values)})"),
                 values)


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                            {"t": table}).scalars())


@pytest.mark.migration
def test_hashes_are_labelled_and_renamed_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("SET session_replication_role = replica"))  # no foreign keys to satisfy
            for aid, value in (("bare", HEX), ("upper", HEX.upper()), ("fp1", FP1), ("junk", "sha-junk"),
                               ("none", None)):
                _insert(conn, "assets", asset_id=aid, library_id="lib", rel_path=aid, file_size=1,
                        media_type="image", sha256=value, deleted_at=None)
                _insert(conn, "artifact_lineage", asset_id=aid, artifact="ocr", producer="p", producer_version="1",
                        settings_hash="h", source_sha256=value)
            # One file deleted for good, remembered twice at its path: once labelled they're one.
            for value in (HEX, HEX.upper(), FP1, "junk", None):  # ignored_id 1 to 5
                _insert(conn, "ignored_files", library_id="lib", rel_path="gone.jpg", sha256=value)
            _insert(conn, "scan_found", scan_id="scan_1", rel_path="a.mov", file_size=1, media_type="video",
                    sha256=FP1, legacy_sha256=HEX)
            _insert(conn, "video_index_chunks", chunk_id="chk_1", asset_id="bare", chunk_index=0, start_ms=0,
                    end_ms=1, status="pending", run=json.dumps({"producer": "scenes", "source_sha256": HEX}))
            _insert(conn, "video_index_chunks", chunk_id="chk_2", asset_id="bare", chunk_index=1, start_ms=1,
                    end_ms=2, status="pending", run=None)

        _alembic(url, "upgrade", AFTER)
        with engine.begin() as conn:
            assert {"content_hash"} <= _columns(conn, "assets") and "sha256" not in _columns(conn, "assets")
            assert "content_hash" in _columns(conn, "active_assets") and "sha256" not in _columns(conn, "active_assets")
            assert {"content_hash", "previous_hash"} <= _columns(conn, "scan_found")
            assert "source_hash" in _columns(conn, "artifact_lineage")
            labelled = {"bare": "sha256:" + HEX, "upper": "sha256:" + HEX, "fp1": FP1, "junk": None, "none": None}
            assert dict(conn.execute(text("SELECT asset_id, content_hash FROM assets")).all()) == labelled
            assert dict(conn.execute(text("SELECT asset_id, source_hash FROM artifact_lineage")).all()) == labelled
            assert sorted(conn.execute(text("SELECT ignored_id, content_hash FROM ignored_files")).all()) == [
                (1, "sha256:" + HEX), (3, FP1), (4, None)]  # the upper-case copy and the second unknown go
            assert conn.execute(text("SELECT content_hash, previous_hash FROM scan_found")).one() == (FP1, "sha256:" + HEX)
            assert conn.execute(text("SELECT run FROM video_index_chunks WHERE chunk_id = 'chk_1'")).scalar_one() == {
                "producer": "scenes", "source_hash": "sha256:" + HEX}
            assert conn.execute(text("SELECT run FROM video_index_chunks WHERE chunk_id = 'chk_2'")).scalar_one() is None

        for table, column in (("assets", "content_hash"), ("ignored_files", "content_hash"),
                              ("artifact_lineage", "source_hash"), ("scan_found", "previous_hash")):
            with engine.begin() as conn, pytest.raises(IntegrityError, match=f"ck_{table}_{column}"):
                conn.execute(text(f"UPDATE {table} SET {column} = :v"), {"v": HEX})  # a bare hash names no scheme

        _alembic(url, "downgrade", BEFORE)
        with engine.begin() as conn:
            assert "sha256" in _columns(conn, "active_assets") and "content_hash" not in _columns(conn, "assets")
            assert dict(conn.execute(text("SELECT asset_id, sha256 FROM assets")).all()) == {
                "bare": HEX, "upper": HEX, "fp1": FP1, "junk": None, "none": None}
            assert dict(conn.execute(text("SELECT asset_id, source_sha256 FROM artifact_lineage")).all())["bare"] == HEX
            assert conn.execute(text("SELECT sha256, legacy_sha256 FROM scan_found")).one() == (FP1, HEX)
            assert conn.execute(text("SELECT run FROM video_index_chunks WHERE chunk_id = 'chk_1'")).scalar_one() == {
                "producer": "scenes", "source_sha256": HEX}
            conn.execute(text("UPDATE assets SET sha256 = 'anything' WHERE asset_id = 'bare'"))  # no check down here
        engine.dispose()
