"""Migration 0c663a6f08e1: edits keep only the latest (Robert, Oct 9).

A person's tag edit becomes one fixed list (asset_corrections.tags), and
the machine transcripts kept under a person's go. Going back down restores
the old columns and table (empty). Runs Alembic against a throwaway
testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "5fe827012184", "0c663a6f08e1"


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table})}


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}


@pytest.mark.migration
def test_a_persons_tags_are_one_list_and_no_machine_transcript_is_kept_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        assert {"tags_added", "tags_removed"} <= _columns(engine, "asset_corrections")
        assert "machine_transcripts" in _tables(engine)

        _alembic(url, "upgrade", AFTER)
        cols = _columns(engine, "asset_corrections")
        assert "tags" in cols and not {"tags_added", "tags_removed"} & cols
        assert "machine_transcripts" not in _tables(engine)

        _alembic(url, "downgrade", BEFORE)
        assert {"tags_added", "tags_removed"} <= _columns(engine, "asset_corrections")
        assert "tags" not in _columns(engine, "asset_corrections")
        assert "machine_transcripts" in _tables(engine)
        engine.dispose()
