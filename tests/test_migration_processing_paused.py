"""Migration a1839b2c9c58: pausing processing (Robert, Oct 9).

processing_paused records what an admin paused: all of the account's
processing ('all') or one producer's (its artifact), one row each. Going
back down drops it. Runs Alembic against a throwaway testcontainers
database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "e517f1428b9b", "a1839b2c9c58"


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}


@pytest.mark.migration
def test_what_was_paused_is_kept_one_row_each_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        assert "processing_paused" not in _tables(engine)

        _alembic(url, "upgrade", AFTER)
        assert "processing_paused" in _tables(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO processing_paused (target, paused_by, paused_at)"
                              " VALUES ('all', 'usr_1', now()), ('vision', NULL, now())"))
            with pytest.raises(Exception):  # one row each
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO processing_paused (target, paused_at) VALUES ('all', now())"))

        _alembic(url, "downgrade", BEFORE)
        assert "processing_paused" not in _tables(engine)
        engine.dispose()
