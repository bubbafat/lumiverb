"""A library's previews folder (GET/PUT /v1/libraries/{id}/previews,
routers/libraries.py) and how Settings › Storage lists it: an admin, on the
machine itself, gives a library a folder of its own (a big video library on
a drive of its own) or sends it back to the server's, after a 409 that says
what moves and whether it's the drive the originals are on. The rules on
real folders: tests/test_previews_location.py."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.config import get_settings
from src.shared import previews_location
from tests import real_account
from tests.test_first_run_setup import HERE, LAN, control  # noqa: F401 (control: a fixture)

pytestmark = pytest.mark.slow


@pytest.fixture
def mac(control, tmp_path, monkeypatch):  # noqa: F811
    """A Mac's install; its originals in a folder of their own."""
    from src.server.api import rate_limit
    from src.server.storage import local

    data = tmp_path / "data"
    originals = tmp_path / "Media" / "Trips"
    originals.mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", '{"/": "/"}')
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    get_settings.cache_clear()
    local.get_storage.cache_clear()
    yield data, originals, tmp_path, control
    real_account.drop_databases(control)
    local.get_storage.cache_clear()
    get_settings.cache_clear()


def _library(client: TestClient, control_url: str, originals) -> tuple[dict, str, str]:
    auth, tenant_id = real_account.admin_with_database(client, control_url)
    made = client.post("/v1/libraries", json={"name": "Trips", "root_path": str(originals)}, headers=auth)
    assert made.status_code == 200, made.text
    return auth, made.json()["library_id"], tenant_id


def _settled(client: TestClient, auth: dict, library_id: str) -> dict:
    for _ in range(200):
        body = client.get(f"/v1/libraries/{library_id}/previews", headers=auth).json()
        if body["moving"] is None:
            return body
        time.sleep(0.05)
    raise AssertionError("the move didn't finish")


def test_a_library_gets_a_folder_of_its_own_after_saying_yes_and_back(mac):
    data, originals, tmp, control_url = mac
    drive = tmp / "Video"
    drive.mkdir()
    with TestClient(app, client=HERE) as client:
        auth, library_id, tenant_id = _library(client, control_url, originals)
        key = f"{tenant_id}/{library_id}/analysis/00/ast_1_a.mp4"
        (data / key).parent.mkdir(parents=True)
        (data / key).write_bytes(b"clip")
        before = client.get(f"/v1/libraries/{library_id}/previews", headers=auth).json()
        assert before["folder"] == before["server_folder"] == str(data) and before["own"] is False
        asked = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(drive)}, headers=auth)
        assert asked.status_code == 409, asked.text
        err = asked.json()["error"]
        assert err["code"] == "move_previews" and "Moves Trips's 1 previews (4 bytes)" in err["message"]
        # One disk here: the originals' too, so it says what unplugging it would do.
        assert err["details"]["same_drive_as_originals"] is True and "can't be browsed" in err["message"]
        assert not (drive / key).exists()
        assert client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(drive), "move": True},
                          headers=auth).status_code == 202
        after = _settled(client, auth, library_id)
        assert after["folder"] == str(drive) and after["own"] is True and after["available"] is True
        storage = client.get("/v1/system/storage", headers=auth).json()
        assert storage["libraries"] == [{"library_id": library_id, "name": "Trips", "folder": str(drive),
                                         "available": True}]
        assert storage["previews"] == str(data), "the server's folder stays"
        assert (drive / key).read_bytes() == b"clip" and not (data / key).exists()
        back = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": None, "move": True}, headers=auth)
        assert back.status_code == 202, back.text
        assert _settled(client, auth, library_id)["own"] is False
    assert (data / key).read_bytes() == b"clip"
    assert previews_location.read(data).libraries == {}


def test_from_another_device_and_for_others_nothing_moves(mac):
    data, originals, tmp, control_url = mac
    with TestClient(app, client=HERE) as here:
        auth, library_id, tenant_id = _library(here, control_url, originals)
    with TestClient(app, client=LAN) as there:
        seen = there.get(f"/v1/libraries/{library_id}/previews", headers=auth).json()
        assert seen["changeable_here"] is False
        refused = there.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(tmp), "move": True},
                            headers=auth)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "storage_elsewhere"
        assert there.get("/v1/libraries/lib_nope/previews", headers=auth).status_code == 404
    with TestClient(app, client=HERE) as here:
        viewer = real_account.someone(here, tenant_id, "viewer")
        assert here.get(f"/v1/libraries/{library_id}/previews", headers=viewer).status_code == 403
        assert here.put(f"/v1/libraries/{library_id}/previews", json={"folder": None},
                        headers=viewer).status_code == 403
    assert previews_location.read(data).libraries == {}


def test_a_folder_it_cant_use_says_why(mac):
    data, originals, tmp, control_url = mac
    with TestClient(app, client=HERE) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        bad = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(tmp / "nowhere")}, headers=auth)
        assert bad.status_code == 400 and "There's no folder" in bad.json()["error"]["message"]
        where = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": None}, headers=auth)
        assert where.status_code == 400 and "already" in where.json()["error"]["message"]
