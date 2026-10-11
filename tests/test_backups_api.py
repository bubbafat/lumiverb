"""Settings › Backups' API (GET/POST /v1/system/backups, PUT
/v1/system/backups/folder, routers/system.py), its row on the Admin page,
and the 5-minute upkeep starting the nightly one. Admins see them and back
up now; the folder is chosen on the machine itself. A real backup of a
real install (tests/test_backups_slow.py has the rules on real databases)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text

from src.server import backups
from src.server.api.main import app
from src.server.api.routers import upkeep
from src.server.config import get_settings
from tests.test_backups_slow import PG_BIN
from tests.test_first_run_setup import HERE, LAN, control  # noqa: F401 (control: a fixture)
from tests.test_network_api import _signed_in

pytestmark = pytest.mark.slow

SETTLE_SEC = 120


@pytest.fixture
def install(control, tmp_path, monkeypatch):  # noqa: F811
    """A Mac's install: its data folder, PostgreSQL 18's tools, and the first
    admin's account with its database (made here: setup's is mocked)."""
    from src.server.api import rate_limit
    from src.server.lifecycle.migrate import migrate

    if PG_BIN is None:
        pytest.skip("needs PostgreSQL 18's pg_dump and pg_restore here")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PG_BIN", PG_BIN)
    monkeypatch.setenv("LUMIVERB_ENV_FILE", "/opt/homebrew/etc/lumiverb/env")
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    get_settings.cache_clear()
    made: list[str] = []

    def account_database(tenant_id: str) -> None:
        url = make_url(control)
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{tenant_id}"'))
        admin.dispose()
        made.append(tenant_id)
        tenant_url = url.set(database=tenant_id).render_as_string(hide_password=False)
        engine = create_engine(tenant_url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        engine.dispose()
        engine = create_engine(control)  # the fixture's template hides the password: route it for real
        with engine.begin() as conn:
            conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                         {"u": tenant_url, "t": tenant_id})
        engine.dispose()
        migrate(control, say=lambda _: None)

    yield tmp_path / "data", account_database
    admin = create_engine(make_url(control).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        for name in made:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
    get_settings.cache_clear()


def _admin(client: TestClient, account_database) -> dict[str, str]:
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    auth = _signed_in(client)
    with get_control_session() as session:
        account_database(TenantRepository(session).list_all()[0].tenant_id)
    return auth


def _settled(client: TestClient, auth: dict[str, str]) -> dict:
    """The answer once the background work is done. Waits as long as a busy
    machine needs (it's done or it isn't; how fast isn't what's tested), and
    says what it last saw if it never is."""
    deadline = time.monotonic() + SETTLE_SEC
    while True:
        body = client.get("/v1/system/backups", headers=auth).json()
        if body["running"] is None:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"not done after {SETTLE_SEC} s; last answer: {body}")
        time.sleep(0.05)

def test_an_admin_backs_up_now_and_sees_how_to_restore_it(install):
    data, account_database = install
    with TestClient(app, client=HERE) as client:
        auth = _admin(client, account_database)
        before = client.get("/v1/system/backups", headers=auth).json()
        assert before["folder"] == before["default_folder"] == str(data / "backups")
        assert before["backups"] == [] and before["changeable_here"] is True and before["last_error"] is None
        started = client.post("/v1/system/backups", headers=auth)
        assert started.status_code == 202, started.text
        body = _settled(client, auth)
    assert body["last_error"] is None
    [made] = body["backups"]
    assert made["kind"] == "now" and made["accounts"] == 1 and made["bytes"] > 0
    assert made["restore"] == [
        "brew services stop lumiverb",
        f"lumiverb-server restore --env /opt/homebrew/etc/lumiverb/env {data / 'backups' / made['name']}",
        "brew services start lumiverb",
    ]


def test_a_backup_that_fails_says_why_here_and_on_the_admin_page(install):
    data, account_database = install
    with TestClient(app, client=HERE) as client:
        auth = _signed_in(client)  # the account's database is never made: pg_dump can't back it up
        assert client.post("/v1/system/backups", headers=auth).status_code == 202
        body = _settled(client, auth)
        assert body["backups"] == [] and "pg_dump couldn't back up" in body["last_error"]
        row = {r["key"]: r for r in client.get("/v1/system/health", headers=auth).json()["rows"]}["backups"]
    assert row["state"] == "red" and "pg_dump couldn't back up" in row["reason"] and row["link"] == "/settings/backups"


def test_one_at_a_time(install):
    data, account_database = install
    with TestClient(app, client=HERE) as client:
        auth = _signed_in(client)
        with backups._one_at_a_time(data):
            refused = client.post("/v1/system/backups", headers=auth)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "backup_running"


def test_the_folder_is_chosen_on_the_machine_itself(install, tmp_path):
    data, _ = install
    drive = tmp_path / "Backup"
    drive.mkdir()
    with TestClient(app, client=HERE) as here:
        auth = _signed_in(here)
        moved = here.put("/v1/system/backups/folder", json={"folder": str(drive)}, headers=auth)
        assert moved.status_code == 200, moved.text
        assert moved.json()["folder"] == str(drive)
        bad = here.put("/v1/system/backups/folder", json={"folder": str(tmp_path / "nowhere")}, headers=auth)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "folder_not_usable"
        assert "There's no folder" in bad.json()["error"]["message"]
    with TestClient(app, client=LAN) as there:
        seen = there.get("/v1/system/backups", headers=auth).json()
        assert seen["folder"] == str(drive) and seen["changeable_here"] is False
        refused = there.put("/v1/system/backups/folder", json={"folder": None}, headers=auth)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "backups_elsewhere"
    assert backups.folder(data) == drive
    with TestClient(app, client=HERE) as here:
        back = here.put("/v1/system/backups/folder", json={"folder": None}, headers=auth)
    assert back.json()["folder"] == str(data / "backups")


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_admins(install, role):
    with TestClient(app, client=HERE) as client:
        auth = _signed_in(client, role)
        assert client.get("/v1/system/backups", headers=auth).status_code == 403
        assert client.post("/v1/system/backups", headers=auth).status_code == 403
        assert client.put("/v1/system/backups/folder", json={"folder": None}, headers=auth).status_code == 403
        assert client.get("/v1/system/backups").status_code == 401


def test_upkeep_for_every_account_starts_the_nightly_one_and_one_account_s_doesnt():
    calls = []

    def due(data_dir, url, *, pg_bin=None):
        calls.append(url)
        return "started"

    quiet = {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0,
             "transcripts_synced": 0, "transcripts_failed": 0}
    with patch.object(backups, "start_nightly_if_due", due), \
            patch.object(upkeep, "_run_sweep_all_tenants", return_value=quiet), \
            patch.object(upkeep, "_run_sweep_single_tenant", return_value=quiet), \
            patch.object(upkeep, "_propagate_faces_all_tenants", return_value={}), \
            patch.object(upkeep, "_propagate_faces_single_tenant", return_value={}), \
            patch.object(upkeep, "_purge_expired_trash_all_tenants", return_value={}), \
            patch.object(upkeep, "_purge_expired_trash_single_tenant", return_value={}), \
            patch.object(upkeep, "_cleanup_revoked_tokens", return_value=0), \
            patch.object(upkeep, "_each_account", return_value={}):
        every = upkeep.run_upkeep(upkeep.UpkeepScope(all_tenants=True))
        one = upkeep.run_upkeep(upkeep.UpkeepScope(all_tenants=False, tenant_id="ten_a"))
    assert every.backup == "started" and one.backup is None
    assert calls == [get_settings().control_plane_database_url]


def test_a_nightly_one_that_cant_start_never_fails_upkeep(caplog):
    def broken(*a, **k):
        raise PermissionError("the data folder")

    with patch.object(backups, "start_nightly_if_due", broken):
        assert upkeep._nightly_backup() == "not started"
    assert "didn't start" in caplog.text
