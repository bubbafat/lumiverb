"""Settings › Storage's API (GET /v1/system/storage, PUT
/v1/system/storage/previews, POST /v1/system/storage/move, routers/system.py):
where the previews are and moving them. An admin, on the machine itself,
says yes to what moves (a 409 with the facts first); the move runs in the
background and is followed here; the upkeep goes on with one that stopped.
The rules on real folders: tests/test_previews_location.py."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.config import get_settings
from src.server.storage import move
from src.shared import previews_location
from tests import real_account
from tests.test_first_run_setup import HERE, LAN, control  # noqa: F401 (control: a fixture)

pytestmark = pytest.mark.slow

SETTLE_SEC = 120

KEY = "ten_x/lib_1/proxies/00/ast_1_a.webp"


@pytest.fixture
def mac(control, tmp_path, monkeypatch):  # noqa: F811
    from src.server.api import rate_limit
    from src.server.storage import local

    data = tmp_path / "data"
    (data / KEY).parent.mkdir(parents=True)
    (data / KEY).write_bytes(b"proxy")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    get_settings.cache_clear()
    local.get_storage.cache_clear()
    drive = tmp_path / "Media"
    drive.mkdir()
    yield data, drive, control
    real_account.drop_databases(control)
    local.get_storage.cache_clear()
    get_settings.cache_clear()


def _settled(client: TestClient, auth: dict[str, str]) -> dict:
    """The answer once the background work is done. Waits as long as a busy
    machine needs (it's done or it isn't; how fast isn't what's tested), and
    says what it last saw if it never is."""
    deadline = time.monotonic() + SETTLE_SEC
    while True:
        body = client.get("/v1/system/storage", headers=auth).json()
        if body["moving"] is None:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"not done after {SETTLE_SEC} s; last answer: {body}")
        time.sleep(0.05)

def test_an_admin_on_the_mac_sees_what_moves_says_yes_and_it_moves(mac):
    data, drive, control_url = mac
    with TestClient(app, client=HERE) as client:
        auth, _ = real_account.admin_with_database(client, control_url)
        before = client.get("/v1/system/storage", headers=auth).json()
        assert before["previews"] == before["data"] == str(data)
        assert before["same_disk"] is True and before["moving"] is None and before["changeable_here"] is True
        asked = client.put("/v1/system/storage/previews", json={"folder": str(drive)}, headers=auth)
        assert asked.status_code == 409, asked.text
        err = asked.json()["error"]
        assert err["code"] == "move_previews" and "Moves 1 previews (5 bytes)" in err["message"]
        assert err["details"] | {"free": 0} == {"from": str(data), "to": str(drive), "files": 1, "bytes": 5,
                                                "free": 0}
        assert not (drive / KEY).exists(), "nothing moves until the user says so"
        moved = client.put("/v1/system/storage/previews", json={"folder": str(drive), "move": True}, headers=auth)
        assert moved.status_code == 202, moved.text
        after = _settled(client, auth)
        assert after["previews"] == str(drive) and after["previews_available"] is True
    assert (drive / KEY).read_bytes() == b"proxy" and not (data / KEY).exists()
    assert previews_location.read(data) == previews_location.Location(drive)


def test_from_another_device_nothing_moves(mac):
    data, drive, control_url = mac
    with TestClient(app, client=HERE) as here:
        auth, _ = real_account.admin_with_database(here, control_url)
    with TestClient(app, client=LAN) as there:
        seen = there.get("/v1/system/storage", headers=auth).json()
        assert seen["changeable_here"] is False
        refused = there.put("/v1/system/storage/previews", json={"folder": str(drive), "move": True}, headers=auth)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "storage_elsewhere"
    assert previews_location.read(data).previews == data


def test_a_folder_it_cant_use_says_why(mac, tmp_path):
    _, _, control_url = mac
    with TestClient(app, client=HERE) as client:
        auth, _ = real_account.admin_with_database(client, control_url)
        bad = client.put("/v1/system/storage/previews", json={"folder": str(tmp_path / "nowhere")}, headers=auth)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "folder_not_usable"
    assert "There's no folder" in bad.json()["error"]["message"]


def test_one_move_at_a_time_and_try_again(mac):
    data, drive, control_url = mac
    (drive / previews_location.MARKER).write_text("")
    previews_location.write(data, drive, data)  # a move that stopped
    move._update_state(data, error="The new drive is full.", failed_at="2026-10-11T00:00:00+00:00")
    with TestClient(app, client=HERE) as client:
        auth, _ = real_account.admin_with_database(client, control_url)
        body = client.get("/v1/system/storage", headers=auth).json()
        assert body["moving"]["error"] == "The new drive is full." and body["moving"]["from_folder"] == str(data)
        again = client.put("/v1/system/storage/previews", json={"folder": None, "move": True}, headers=auth)
        assert again.status_code == 409 and again.json()["error"]["code"] == "move_running"
        assert client.post("/v1/system/storage/move", headers=auth).status_code == 202
        assert _settled(client, auth)["previews"] == str(drive)
        assert client.post("/v1/system/storage/move", headers=auth).status_code == 409  # not moving
    assert (drive / KEY).is_file()


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_admins(mac, role):
    _, drive, control_url = mac
    with TestClient(app, client=HERE) as client:
        _, tenant_id = real_account.admin_with_database(client, control_url)
        auth = real_account.someone(client, tenant_id, role)
        assert client.get("/v1/system/storage", headers=auth).status_code == 403
        assert client.put("/v1/system/storage/previews", json={"folder": str(drive)}, headers=auth).status_code == 403
        assert client.post("/v1/system/storage/move", headers=auth).status_code == 403


def test_upkeep_goes_on_with_a_move_that_stopped(mac, monkeypatch):
    from src.server.api.routers import upkeep

    data, drive, control_url = mac
    called = []
    monkeypatch.setattr(move, "go_on", lambda d, **k: called.append((Path(d), k)) or "started")
    assert upkeep._previews_move() == "started"
    assert called == [(data, {"waits": True})]
