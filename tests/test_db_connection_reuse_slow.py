"""The suite reuses its Postgres connections, as the server does.

Every connection a test opens to the run's Postgres goes through a port
Docker maps on this machine, and closing it leaves the port in TIME_WAIT
for a minute. With a new connection per session (NullPool) a full parallel
run opened ~26k of them and used up the ephemeral port range, so Docker
couldn't map a port for a container started mid-run (the real-Quickwit and
migration tests). Sessions on one engine take the same connection back.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.database import get_engine_for_url


@pytest.mark.slow
def test_sessions_on_one_engine_share_a_connection() -> None:
    with PostgresContainer() as pg:
        engine = get_engine_for_url(pg.get_connection_url())
        try:
            pids = set()
            for _ in range(3):
                with Session(engine) as session:
                    pids.add(session.execute(text("SELECT pg_backend_pid()")).scalar())
            assert len(pids) == 1
        finally:
            engine.dispose()
