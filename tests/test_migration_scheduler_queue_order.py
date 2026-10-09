"""Migration 5a0272639723: the index the scheduler's queue reads clips oldest first by.
Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "fbd7f3be1cb0", "5a0272639723"


def _indexes(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'assets'"))}


@pytest.mark.migration
def test_the_queue_order_index_comes_and_goes() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", AFTER)
        assert "ix_assets_queue_order" in _indexes(engine)
        _alembic(url, "downgrade", BEFORE)
        assert "ix_assets_queue_order" not in _indexes(engine)
        engine.dispose()
