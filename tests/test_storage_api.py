"""Settings › Storage's API (GET /v1/system/storage, PUT
/v1/system/storage/previews, POST /v1/system/storage/move, routers/system.py):
where the previews are and moving them. An admin, on the machine itself,
says yes to what moves (a 409 with the facts first; with nothing to move,
it isn't asked) and to making a folder that isn't there; the move runs in
the background and is followed here; the upkeep goes on with one that
stopped.
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
    data, _, control_url = mac
    with TestClient(app, client=HERE) as client:
        auth, _ = real_account.admin_with_database(client, control_url)
        no_drive = client.put("/v1/system/storage/previews", json={"folder": "/lumiverb-no-such-drive/previews",
                                                                  "make_folder": True}, headers=auth)
        assert no_drive.status_code == 400 and no_drive.json()["error"]["code"] == "folder_not_usable"
        assert no_drive.json()["error"]["message"] == "There's no drive at /lumiverb-no-such-drive. Is it plugged in?"
        inside = data / "ten_x" / "new" / "deeper"
        refused = client.put("/v1/system/storage/previews", json={"folder": str(inside), "make_folder": True},
                             headers=auth)
        assert refused.status_code == 400 and "inside the previews" in refused.json()["error"]["message"]
    assert not (data / "ten_x" / "new").exists(), "made for a request that didn't go through: taken away"
    assert previews_location.read(data).previews == data


def test_a_folder_not_there_is_made_when_the_user_says_so(mac):
    data, drive, control_url = mac
    wanted = drive / "Lumiverb" / "previews"
    with TestClient(app, client=HERE) as client:
        auth, _ = real_account.admin_with_database(client, control_url)
        asked = client.put("/v1/system/storage/previews", json={"folder": str(wanted)}, headers=auth)
        assert asked.status_code == 409, asked.text
        err = asked.json()["error"]
        assert err["code"] == "make_folder" and err["message"].startswith(f"There's no folder at {wanted}. Make it?")
        assert err["details"]["folder"] == str(wanted) and err["details"]["same_drive_as_data"] is True
        assert not (drive / "Lumiverb").exists()
        # Made, then what moves is asked: nothing stays made until the user says yes to that too.
        made = client.put("/v1/system/storage/previews", json={"folder": str(wanted), "make_folder": True},
                          headers=auth)
        assert made.status_code == 409 and made.json()["error"]["code"] == "move_previews"
        assert made.json()["error"]["details"]["to"] == str(wanted)
        assert not (drive / "Lumiverb").exists()
        moved = client.put("/v1/system/storage/previews", json={"folder": str(wanted), "make_folder": True,
                                                               "move": True}, headers=auth)
        assert moved.status_code == 202, moved.text
        assert _settled(client, auth)["previews"] == str(wanted)
    assert (wanted / KEY).read_bytes() == b"proxy"


def test_with_nothing_to_move_it_doesnt_ask(mac):
    data, drive, control_url = mac
    (data / KEY).unlink()
    wanted = drive / "previews"
    with TestClient(app, client=HERE) as client:
        auth, _ = real_account.admin_with_database(client, control_url)
        there = client.put("/v1/system/storage/previews", json={"folder": str(wanted), "make_folder": True},
                           headers=auth)
        assert there.status_code == 202, there.text
        assert _settled(client, auth)["previews"] == str(wanted)
        back = client.put("/v1/system/storage/previews", json={"folder": None}, headers=auth)
        assert back.status_code == 202, back.text
        assert _settled(client, auth)["previews"] == str(data)
    assert previews_location.read(data) == previews_location.Location(data)


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
