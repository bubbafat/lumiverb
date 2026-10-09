"""Migration be66e81c7e5d: redo on change (ADR-016 phase 4, Robert Oct 9).

The upgrade step's tables go (approvals, their clips, the history of edits
"Replace my edits" took away); producer_redo_paused records which
producers' redo an admin stopped. Going back down restores the old tables
(empty) and drops the new one. Runs Alembic against a throwaway
testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "fbd7f3be1cb0", "be66e81c7e5d"
GONE = {"producer_upgrades", "producer_upgrade_items", "correction_history"}


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}


@pytest.mark.migration
def test_the_upgrade_tables_go_and_stopped_redos_are_kept_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        assert GONE <= _tables(engine)

        _alembic(url, "upgrade", AFTER)
        assert not GONE & _tables(engine)
        assert "producer_redo_paused" in _tables(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO producer_redo_paused (artifact, paused_by, paused_at)"
                              " VALUES ('vision', 'usr_1', now())"))
            with pytest.raises(Exception):  # one row per producer
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO producer_redo_paused (artifact, paused_at) VALUES ('vision', now())"))

        _alembic(url, "downgrade", BEFORE)
        assert GONE <= _tables(engine)
        assert "producer_redo_paused" not in _tables(engine)
        engine.dispose()
