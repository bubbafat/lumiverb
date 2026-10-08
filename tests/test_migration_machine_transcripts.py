"""Migration 96a9e82eebd7: the machine transcripts clips show are kept, so a
person's can go on top. Transcripts from before transcript_source existed
have none: they're the machine's too. Runs Alembic against a throwaway
testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "b799bec833a5", "96a9e82eebd7"
SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"


@pytest.mark.migration
def test_shown_machine_transcripts_are_kept_including_ones_without_a_source() -> None:
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

            def video(aid: str, **cols) -> None:
                values = {**extra, **cols}
                names = ", ".join(values)
                conn.execute(text(
                    f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, {names})"
                    f" VALUES (:a, 'lib', :a, 1, 'video', {', '.join(':' + k for k in values)})"
                ), {"a": aid, **values})

            video("whisper", transcript_srt=SRT, transcript_text="hello", has_transcript=True,
                  transcript_source="whisper", transcribed_at="2026-10-01T00:00:00Z")
            video("no_source", transcript_srt=SRT, transcript_text="hello", has_transcript=True,
                  transcribed_at="2026-09-01T00:00:00Z")
            video("no_source_no_speech", has_transcript=False, transcribed_at="2026-09-01T00:00:00Z")
            video("person", transcript_srt=SRT, has_transcript=True, transcript_source="manual",
                  transcribed_at="2026-10-01T00:00:00Z")
            video("never")
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            kept = dict(conn.execute(text("SELECT asset_id, source FROM machine_transcripts")).all())
            srt = conn.execute(text("SELECT srt FROM machine_transcripts WHERE asset_id = 'no_source'")).scalar()
    assert kept == {"whisper": "whisper", "no_source": "unknown", "no_source_no_speech": "unknown"}
    assert srt == SRT
