"""Tenant migration e9a4c2f7b1d3: the analysis proxy's CRF becomes a quality
notch. An account's CRF is kept as the nearest notch; one no notch stands
for holds the remakes for an admin, as a new version does; the default
leaves nothing behind. A downgrade gives the notch's CRF back. Runs Alembic
against a throwaway testcontainers database only."""

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "d7f3b9e5c2a8", "e9a4c2f7b1d3"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def _set(engine, values: dict | None) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM system_metadata WHERE key = 'producer.analysis_proxy'"))
        conn.execute(text("DELETE FROM producer_pauses"))
        if values is not None:
            conn.execute(text("INSERT INTO system_metadata (key, value, updated_at)"
                              " VALUES ('producer.analysis_proxy', :v, now())"), {"v": json.dumps(values)})


def _now(engine) -> tuple[dict | None, list[tuple]]:
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT value FROM system_metadata WHERE key = 'producer.analysis_proxy'")).scalar()
        held = conn.execute(text("SELECT target, scope, paused_by FROM producer_pauses")).all()
    return (json.loads(raw) if raw else None), [tuple(h) for h in held]


@pytest.mark.migration
def test_a_crf_becomes_its_nearest_notch_and_one_no_notch_stands_for_holds_the_remakes() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        try:
            for crf, kept, held in [
                (23, {"max_edge": 720, "quality": "best"}, []),  # exactly best's: nothing made another way
                (24, {"max_edge": 720, "quality": "best"}, [("analysis_proxy", "redo", None)]),
                (30, {"max_edge": 720}, [("analysis_proxy", "redo", None)]),  # a tie: the better picture, better
                (40, {"max_edge": 720, "quality": "good"}, [("analysis_proxy", "redo", None)]),
            ]:
                _set(engine, {"max_edge": 720, "crf": crf})
                _alembic(url, "upgrade", AFTER)
                assert _now(engine) == (kept, held), crf
                _alembic(url, "downgrade", BEFORE)

            _set(engine, {"crf": 28})  # the default: nothing left to keep, nothing held
            _alembic(url, "upgrade", AFTER)
            assert _now(engine) == (None, [])
            _alembic(url, "downgrade", BEFORE)

            _set(engine, None)  # nothing changed: nothing to do
            _alembic(url, "upgrade", AFTER)
            assert _now(engine) == (None, [])

            # Down: the notch's CRF back.
            _set(engine, {"quality": "good", "fps_max": 24})
            _alembic(url, "downgrade", BEFORE)
            assert _now(engine) == ({"crf": 32, "fps_max": 24}, [])
        finally:
            engine.dispose()
