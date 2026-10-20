"""Every Lumiverb process's database connections give up on a database that
doesn't answer (src/server/database.py): a connection is made within
CONNECT_TIMEOUT_SEC or given up on, and one made is kept checked (libpq's TCP
keepalives), so a database host gone away holds up a request, or the
scheduler's work for an account, for a minute at most, never until a restart.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from testcontainers.postgres import PostgresContainer

from src.server import database
from tests.conftest import PG_IMAGE, _ensure_psycopg2

pytestmark = pytest.mark.slow


def test_every_engines_connections_have_a_time_limit_and_are_kept_checked():
    assert database.CONNECT_TIMEOUT_SEC == 10, "every process's, unless it sets less (the plug-in runner)"
    with PostgresContainer(PG_IMAGE) as pg:
        engine = database._create_engine(_ensure_psycopg2(pg.get_connection_url()))
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar_one() == 1
                said = conn.connection.dbapi_connection.get_dsn_parameters()
        finally:
            engine.dispose()
    # As libpq has them for the connection made.
    assert (said["connect_timeout"], said["keepalives"], said["keepalives_idle"], said["keepalives_interval"],
            said["keepalives_count"]) == ("10", "1", "30", "10", "3")


@pytest.fixture
def silent():
    """A database server that takes a connection and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def take() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    taker = threading.Thread(target=take, daemon=True)
    taker.start()
    yield server.getsockname()[1]
    stop.set()
    taker.join(5)
    for s in held:
        s.close()
    server.close()


def test_a_database_that_never_answers_is_given_up_on(silent, monkeypatch):
    monkeypatch.setattr(database, "CONNECT_TIMEOUT_SEC", 2)  # libpq's least, so the test is quick
    engine = database._create_engine(f"postgresql://nobody:x@127.0.0.1:{silent}/acct")
    said: list[BaseException] = []

    def connect() -> None:
        try:
            with engine.connect():
                pass
        except BaseException as e:  # noqa: BLE001 — what the caller would get
            said.append(e)

    began = time.monotonic()
    trying = threading.Thread(target=connect, daemon=True)
    trying.start()
    trying.join(60)
    engine.dispose()
    assert not trying.is_alive(), f"still waiting after {time.monotonic() - began:.0f} s"
    assert len(said) == 1 and isinstance(said[0], OperationalError), said
    assert "timeout" in str(said[0]).lower(), said[0]
