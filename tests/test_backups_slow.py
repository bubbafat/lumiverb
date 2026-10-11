"""Backups on real PostgreSQL 18 (src/server/backups.py): every database
backed up, then put back as it was; on a new install too (the accounts
routed to it); as Linux's database role, which may only create databases;
a restore that fails changes nothing; one while Lumiverb runs is refused;
an upgrade's migrations are backed up first; the commands; the API.

Needs PostgreSQL 18's client tools on this machine (pg_dump, pg_restore):
LUMIVERB_TEST_PG_BIN, PG_BIN, Homebrew's postgresql@18 or Debian's
/usr/lib/postgresql/18/bin."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlmodel import Session

from src.server import backups
from src.server.backups import BackupError
from src.server.lifecycle import cli
from src.server.lifecycle.migrate import APP_DIR, migrate, pending
from tests.conftest import PG_IMAGE, _RealPostgresContainer, _the_server

pytestmark = pytest.mark.slow


def _pg_bin() -> str | None:
    for candidate in (os.environ.get("LUMIVERB_TEST_PG_BIN"), os.environ.get("PG_BIN"),
                      "/opt/homebrew/opt/postgresql@18/bin", "/usr/lib/postgresql/18/bin"):
        if candidate and (Path(candidate) / "pg_dump").is_file():
            return candidate
    found = shutil.which("pg_dump")
    return str(Path(found).parent) if found else None


PG_BIN = _pg_bin()


def _major(pg_bin: str) -> int:
    said = subprocess.run([f"{pg_bin}/pg_dump", "--version"], capture_output=True, text=True).stdout
    return backups._major(said) or 0


if PG_BIN is None or _major(PG_BIN) < 18:
    pytestmark = [pytest.mark.slow, pytest.mark.skip(reason="needs PostgreSQL 18's pg_dump and pg_restore here")]


def _run(url: str, *statements: str, autocommit: bool = False) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT") if autocommit else create_engine(url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def _value(url: str, key: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT value FROM system_metadata WHERE key = :k"), {"k": key}).scalar()
    finally:
        engine.dispose()


def _exists(admin_url: str, name: str) -> bool:
    engine = create_engine(make_url(admin_url).set(database="postgres"))
    try:
        with engine.connect() as conn:
            return bool(conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar())
    finally:
        engine.dispose()


def _routing(control_url: str) -> dict[str, str]:
    engine = create_engine(control_url)
    try:
        with engine.connect() as conn:
            return dict(conn.execute(text("SELECT tenant_id, connection_string FROM tenant_db_routing")).all())
    finally:
        engine.dispose()


def _install(base: str, control: str, tenant_db: str, tenant_id: str = "ten_a", *, create: bool = True) -> tuple[str, str]:
    """A migrated install on `base` (a server URL): its control plane and one account."""
    from src.server.models.control_plane import Tenant, TenantDbRouting

    admin = make_url(base).set(database="postgres").render_as_string(hide_password=False)
    control_url = make_url(base).set(database=control).render_as_string(hide_password=False)
    tenant_url = make_url(base).set(database=tenant_db).render_as_string(hide_password=False)
    if create:
        for name in (control, tenant_db):
            _run(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'CREATE DATABASE "{name}"', autocommit=True)
            _run(make_url(base).set(database=name).render_as_string(hide_password=False),
                 "CREATE EXTENSION IF NOT EXISTS vector")
    migrate(control_url, say=lambda _: None)
    engine = create_engine(control_url)
    with Session(engine) as session:
        session.add(Tenant(tenant_id=tenant_id, name="A"))
        session.flush()
        session.add(TenantDbRouting(tenant_id=tenant_id, connection_string=tenant_url))
        session.commit()
    engine.dispose()
    migrate(control_url, say=lambda _: None)
    return control_url, tenant_url


@pytest.fixture
def server(request):
    """The run's Postgres (a superuser), with databases this test names, dropped after."""
    srv = _the_server()
    made: list[str] = []
    base = srv.url("postgres")

    def name(n: str) -> str:
        made.append(n)
        return n

    yield base, name
    for n in made + [m + backups.RESTORING for m in made] + [m + backups.REPLACED for m in made]:
        srv.run(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')


def _worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def test_every_database_is_backed_up_and_put_back_as_it_was(server, tmp_path):
    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk_control_{w}"), name(f"bk_ten_{w}"))
    _run(tenant_url, "INSERT INTO system_metadata (key, value, updated_at) VALUES ('note', 'before', now())")

    made = backups.back_up(tmp_path, control_url, "now", pg_bin=PG_BIN)
    assert made.path.parent == tmp_path / "backups"
    assert sorted(d["file"] for d in made.databases) == ["control_plane.dump", "ten_a.dump"]
    assert stat.S_IMODE(made.path.stat().st_mode) == 0o700
    assert all(stat.S_IMODE((made.path / d["file"]).stat().st_mode) == 0o600 for d in made.databases)
    assert backups.read_state(tmp_path)["last_error"] is None

    _run(tenant_url, "UPDATE system_metadata SET value = 'after' WHERE key = 'note'",
         "INSERT INTO system_metadata (key, value, updated_at) VALUES ('later', 'x', now())")
    _run(control_url, "UPDATE tenants SET name = 'Renamed' WHERE tenant_id = 'ten_a'")

    said: list[str] = []
    left = backups.restore(backups.read(made.path), control_url, tenant_url_template=None, pg_bin=PG_BIN,
                           say=said.append)
    assert left == []
    assert _value(tenant_url, "note") == "before" and _value(tenant_url, "later") is None
    engine = create_engine(control_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM tenants WHERE tenant_id = 'ten_a'")).scalar() == "A"
    engine.dispose()
    assert _value(tenant_url, "search.recreate") == "needed", "its search index is remade from it"
    assert not _exists(base, f"bk_ten_{w}{backups.RESTORING}") and not _exists(base, f"bk_ten_{w}{backups.REPLACED}")
    assert said == [f"  ✓ bk_ten_{w}", f"  ✓ bk_control_{w}"]


def test_on_a_new_install_the_accounts_are_routed_to_it(server, tmp_path):
    """Its accounts' databases go where this install's template says."""
    base, name = server
    w = _worker()
    control_url, _ = _install(base, name(f"bk2_control_{w}"), name(f"bk2_old_{w}"), tenant_id=f"ten_new_{w}")
    name(f"ten_new_{w}")
    made = backups.back_up(tmp_path, control_url, "now", pg_bin=PG_BIN)
    template = make_url(base).set(database="{tenant_id}").render_as_string(hide_password=False)
    backups.restore(made, control_url, tenant_url_template=template, pg_bin=PG_BIN, say=lambda _: None)
    assert _routing(control_url) == {f"ten_new_{w}": template.format(tenant_id=f"ten_new_{w}")}
    assert _exists(base, f"ten_new_{w}")


def test_as_linux_s_database_role_which_may_only_create_databases(tmp_path):
    """deploy-api.sh's `app`: CREATEDB, not a superuser; pgvector in template1."""
    with _RealPostgresContainer(PG_IMAGE) as pg:
        root = make_url(pg.get_connection_url()).set(drivername="postgresql+psycopg2")
        admin = root.set(database="postgres").render_as_string(hide_password=False)
        _run(root.set(database="template1").render_as_string(hide_password=False), "CREATE EXTENSION IF NOT EXISTS vector")
        _run(admin, "CREATE ROLE app LOGIN PASSWORD 'apppw' CREATEDB",
             "CREATE DATABASE control_plane OWNER app", "CREATE DATABASE ten_a OWNER app", autocommit=True)
        base = root.set(username="app", password="apppw", database="postgres").render_as_string(hide_password=False)
        control_url, tenant_url = _install(base, "control_plane", "ten_a", create=False)
        _run(tenant_url, "INSERT INTO system_metadata (key, value, updated_at) VALUES ('note', 'before', now())")
        made = backups.back_up(tmp_path, control_url, "now", pg_bin=PG_BIN)
        _run(tenant_url, "UPDATE system_metadata SET value = 'after' WHERE key = 'note'")
        template = make_url(base).set(database="{tenant_id}").render_as_string(hide_password=False)
        backups.restore(made, control_url, tenant_url_template=template, pg_bin=PG_BIN, say=lambda _: None)
        assert _value(tenant_url, "note") == "before"


def test_a_restore_that_fails_changes_nothing(server, tmp_path):
    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk3_control_{w}"), name(f"bk3_ten_{w}"))
    made = backups.back_up(tmp_path, control_url, "now", pg_bin=PG_BIN)
    _run(tenant_url, "INSERT INTO system_metadata (key, value, updated_at) VALUES ('note', 'now', now())")
    # The account's archive is broken, but listed as it is (so it gets past verify).
    broken = made.path / "ten_a.dump"
    broken.write_bytes(broken.read_bytes()[: broken.stat().st_size // 2])
    manifest = json.loads((made.path / backups.MANIFEST).read_text())
    for d in manifest["databases"]:
        if d["file"] == "ten_a.dump":
            d["sha256"] = backups._sha256(broken)
    (made.path / backups.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match=f"pg_restore couldn't put back bk3_ten_{w}"):
        backups.restore(backups.read(made.path), control_url, tenant_url_template=None, pg_bin=PG_BIN,
                        say=lambda _: None)
    assert _value(tenant_url, "note") == "now", "the live database is untouched"
    assert not _exists(base, f"bk3_ten_{w}{backups.RESTORING}")


def test_while_lumiverb_is_connected_it_is_refused(server, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "CLOSING_GRACE_SEC", 0.5)
    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk4_control_{w}"), name(f"bk4_ten_{w}"))
    made = backups.back_up(tmp_path, control_url, "now", pg_bin=PG_BIN)
    engine = create_engine(tenant_url, connect_args={"application_name": "lumiverb-scheduler"})
    try:
        with engine.connect(), pytest.raises(
                BackupError, match=r"still running \(connected: bk4_ten_\w+ \(lumiverb-scheduler\)\)"):
            backups.restore(made, control_url, tenant_url_template=None, pg_bin=PG_BIN, say=lambda _: None)
    finally:
        engine.dispose()


def test_a_pg_dump_older_than_the_server_is_refused(server, tmp_path):
    base, name = server
    w = _worker()
    control_url, _ = _install(base, name(f"bk5_control_{w}"), name(f"bk5_ten_{w}"))
    old = tmp_path / "pg14"
    old.mkdir()
    (old / "pg_dump").write_text("#!/bin/sh\necho 'pg_dump (PostgreSQL) 14.11'\n")
    (old / "pg_dump").chmod(0o755)
    with pytest.raises(BackupError, match="PostgreSQL 14's; the database is 18"):
        backups.back_up(tmp_path, control_url, "now", pg_bin=str(old))
    assert "PostgreSQL 14" in backups.read_state(tmp_path)["last_error"]


def test_an_upgrade_s_migrations_are_backed_up_first(server, tmp_path):
    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk6_control_{w}"), name(f"bk6_ten_{w}"))
    env = {"DATA_DIR": str(tmp_path), "PG_BIN": PG_BIN}
    assert not pending(control_url)
    assert cli.backed_up_before_upgrade(env, control_url) is None, "nothing to migrate, nothing made"
    subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "downgrade", "-1"],
                   cwd=APP_DIR, env={**os.environ, "ALEMBIC_TENANT_URL": tenant_url}, check=True, capture_output=True)
    assert pending(control_url)
    made = cli.backed_up_before_upgrade(env, control_url)
    assert made is not None and made.kind == "before-upgrade" and made.path.parent == tmp_path / "backups"
    migrate(control_url, say=lambda _: None)
    assert not pending(control_url)


def test_an_upgrade_backs_up_to_the_data_folder_when_the_chosen_one_is_gone(server, tmp_path):
    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk7_control_{w}"), name(f"bk7_ten_{w}"))
    data, volumes = tmp_path / "data", tmp_path / "Volumes"
    unplugged = volumes / "Backup"
    unplugged.mkdir(parents=True)
    backups.set_folder(data, unplugged)
    unplugged.rmdir()
    volumes.chmod(0o555)  # unplugged: nothing can be made where it was
    subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "downgrade", "-1"],
                   cwd=APP_DIR, env={**os.environ, "ALEMBIC_TENANT_URL": tenant_url}, check=True, capture_output=True)
    try:
        made = cli.backed_up_before_upgrade({"DATA_DIR": str(data), "PG_BIN": PG_BIN}, control_url)
    finally:
        volumes.chmod(0o755)
    assert made is not None and made.path.parent == data / "backups"


def test_the_commands(server, tmp_path, monkeypatch, capsys):
    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk8_control_{w}"), name(f"bk8_ten_{w}"))
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", control_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PG_BIN", PG_BIN)
    monkeypatch.delenv("TENANT_DATABASE_URL_TEMPLATE", raising=False)
    monkeypatch.delenv("LUMIVERB_ENV_FILE", raising=False)
    _run(tenant_url, "INSERT INTO system_metadata (key, value, updated_at) VALUES ('note', 'before', now())")
    assert cli.main(["backup", "--before-upgrade"]) == 0  # a Linux update's, before migrating
    assert backups.list_backups(tmp_path / "backups")[0].kind == "before-upgrade"
    assert cli.main(["backup"]) == 0
    out = capsys.readouterr().out
    assert "✓ Backed up 2 database(s)" in out
    made = backups.list_backups(tmp_path / "backups")[0]
    assert made.kind == "now"

    _run(tenant_url, "UPDATE system_metadata SET value = 'after' WHERE key = 'note'")
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(["restore", made.name]) == 1
    assert _value(tenant_url, "note") == "after"

    assert cli.main(["restore", made.name, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "✓ What was here:" in out and "✓ Restored the backup from" in out
    assert _value(tenant_url, "note") == "before"
    kinds = sorted(b.kind for b in backups.list_backups(tmp_path / "backups"))
    assert kinds == ["before-restore", "before-upgrade", "now"]
    # Undone: the backup made first puts back what was there.
    undo = next(b for b in backups.list_backups(tmp_path / "backups") if b.kind == "before-restore")
    assert cli.main(["restore", str(undo.path), "--yes"]) == 0
    assert _value(tenant_url, "note") == "after"


def test_a_connection_that_s_closing_doesn_t_hold_a_restore_up(server, tmp_path):
    """Lumiverb just stopped, or the restore's own look before: a client that
    lets go within the grace isn't taken for Lumiverb still running (a full
    run under load once failed on that: the backend outlives its client a
    moment)."""
    import threading

    base, name = server
    w = _worker()
    control_url, tenant_url = _install(base, name(f"bk9_control_{w}"), name(f"bk9_ten_{w}"))
    engine = create_engine(tenant_url, connect_args={"application_name": "lumiverb-api"})
    conn = engine.connect()
    threading.Timer(0.4, lambda: (conn.close(), engine.dispose())).start()
    started = time.monotonic()
    assert backups.connected_elsewhere([control_url, tenant_url], grace=60) == []
    assert time.monotonic() - started < 30, "said once it was gone, not at the end of the grace"


def test_the_api_s_and_scheduler_s_connections_say_who_they_are(server, monkeypatch):
    from src.server import database

    base, name = server
    w = _worker()
    control_url, _ = _install(base, name(f"bk10_control_{w}"), name(f"bk10_ten_{w}"))
    monkeypatch.setattr(database, "APPLICATION_NAME", database.APPLICATION_NAME)
    database.set_application_name("lumiverb-scheduler")
    engine = database._create_engine(control_url)
    try:
        with engine.connect():
            assert backups.connected_elsewhere([control_url], grace=0) == [f"bk10_control_{w} (lumiverb-scheduler)"]
    finally:
        engine.dispose()
