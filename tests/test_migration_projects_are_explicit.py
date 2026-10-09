"""Migration e517f1428b9b: projects are explicit lists (Robert, Oct 9).

Smart projects (a saved query) go, with the type and saved_query columns;
going back down restores the columns (every project static). Runs Alembic
against a throwaway testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "b77f2a03eee2", "e517f1428b9b"


def _columns(engine) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'"))}


@pytest.mark.migration
def test_smart_projects_go_and_it_goes_back_down() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for pid, kind in (("prj_static", "static"), ("prj_smart", "smart")):
                conn.execute(text("INSERT INTO projects (project_id, name, visibility, sort_order, type, status,"
                                  " created_at, updated_at) VALUES (:p, :p, 'private', 'manual', :k, 'active',"
                                  " now(), now())"), {"p": pid, "k": kind})

        _alembic(url, "upgrade", AFTER)
        assert not {"type", "saved_query"} & _columns(engine)
        with engine.connect() as conn:
            assert [r[0] for r in conn.execute(text("SELECT project_id FROM projects"))] == ["prj_static"]

        _alembic(url, "downgrade", BEFORE)
        assert {"type", "saved_query"} <= _columns(engine)
        engine.dispose()
