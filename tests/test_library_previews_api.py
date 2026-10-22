"""A library's previews folder (GET/PUT /v1/libraries/{id}/previews,
routers/libraries.py) and how Settings › Storage lists it: an admin, on the
machine itself, gives a library a folder of its own (a big video library on
a drive of its own) or sends it back to the server's, after a 409 that says
what moves and whether it's the drive the originals are on. The rules on
real folders: tests/test_previews_location.py."""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.config import get_settings
from src.shared import previews_location
from tests import real_account
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LAN,
    LOCALHOST,
    control,
)

pytestmark = pytest.mark.slow

SETTLE_SEC = 120


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
    """The answer once the move is done. Waits as long as a busy machine needs
    (it's done or it isn't; how fast isn't what's tested), and says what it
    last saw if it never is."""
    deadline = time.monotonic() + SETTLE_SEC
    while True:
        body = client.get(f"/v1/libraries/{library_id}/previews", headers=auth).json()
        if body["moving"] is None:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"not done after {SETTLE_SEC} s; last answer: {body}")
        time.sleep(0.05)

def test_a_library_gets_a_folder_of_its_own_after_saying_yes_and_back(mac):
    data, originals, tmp, control_url = mac
    drive = tmp / "Video"
    drive.mkdir()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
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
                                         "available": True, "write_error": None}]
        assert storage["previews"] == str(data), "the server's folder stays"
        assert (drive / key).read_bytes() == b"clip" and not (data / key).exists()
        back = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": None, "move": True}, headers=auth)
        assert back.status_code == 202, back.text
        assert _settled(client, auth, library_id)["own"] is False
    assert (data / key).read_bytes() == b"clip"
    assert previews_location.read(data).libraries == {}


def test_from_another_device_and_for_others_nothing_moves(mac):
    data, originals, tmp, control_url = mac
    with TestClient(app, client=HERE, base_url=LOCALHOST) as here:
        auth, library_id, tenant_id = _library(here, control_url, originals)
    with TestClient(app, client=LAN) as there:
        seen = there.get(f"/v1/libraries/{library_id}/previews", headers=auth).json()
        assert seen["changeable_here"] is False
        refused = there.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(tmp), "move": True},
                            headers=auth)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "storage_elsewhere"
        assert there.get("/v1/libraries/lib_nope/previews", headers=auth).status_code == 404
    with TestClient(app, client=HERE, base_url=LOCALHOST) as here:
        viewer = real_account.someone(here, tenant_id, "viewer")
        assert here.get(f"/v1/libraries/{library_id}/previews", headers=viewer).status_code == 403
        assert here.put(f"/v1/libraries/{library_id}/previews", json={"folder": None},
                        headers=viewer).status_code == 403
    assert previews_location.read(data).libraries == {}


def test_a_folder_it_cant_use_says_why(mac):
    data, originals, tmp, control_url = mac
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        bad = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": "/lumiverb-no-such-drive/x"},
                         headers=auth)
        assert bad.status_code == 400 and "There's no drive" in bad.json()["error"]["message"]
        where = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": None}, headers=auth)
        assert where.status_code == 400 and "already" in where.json()["error"]["message"]


def test_never_a_folder_with_a_library_s_files_or_lumiverb_s_own(mac):
    """A library's previews and the server's alike: not a library's folder this server reads, inside one or
    holding one (409 originals_folder), and not in Lumiverb's own data (409 data_folder); nothing moves."""
    data, originals, tmp, control_url = mac
    (data / "models").mkdir(parents=True)
    (data / "models" / "weights.bin").write_bytes(b"w")
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        for put in (lambda folder: client.put(f"/v1/libraries/{library_id}/previews", json=folder, headers=auth),
                    lambda folder: client.put("/v1/system/storage/previews", json=folder, headers=auth)):
            for folder in (originals / "Lumiverb previews", originals.parent):
                refused = put({"folder": str(folder), "make_folder": True, "move": True})
                assert refused.status_code == 409, refused.text
                assert refused.json()["error"]["code"] == "originals_folder"
                assert f"Trips's folder ({originals})" in refused.json()["error"]["message"]
            refused = put({"folder": str(data / "models"), "move": True})
            assert (refused.status_code, refused.json()["error"]["code"]) == (409, "data_folder"), refused.text
        assert not (originals / "Lumiverb previews").exists(), "made for a request that didn't go through: gone"
    assert previews_location.read(data) == previews_location.Location(data)


def test_a_name_lumiverb_keeps_in_its_data_folder_is_refused_before_it_is_offered_to_be_made(mac):
    """models, backups, plugins...: not there yet, a 409 data_folder at once, never a 409 make_folder first, and
    nothing made, asked to make it or not."""
    data, originals, tmp, control_url = mac
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        for put in (lambda folder: client.put(f"/v1/libraries/{library_id}/previews", json=folder, headers=auth),
                    lambda folder: client.put("/v1/system/storage/previews", json=folder, headers=auth)):
            for name in ("models", "plugins", "Backups"):
                for make in (False, True):
                    refused = put({"folder": str(data / name), "make_folder": make, "move": True})
                    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "data_folder"), refused.text
                    assert f"the name {name} is its own" in refused.json()["error"]["message"]
                    assert not (data / name).exists()
    assert previews_location.read(data) == previews_location.Location(data)


def test_a_folder_not_there_is_asked_about_then_made(mac):
    data, originals, tmp, control_url = mac
    wanted = tmp / "Video" / "Lumiverb"
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        asked = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(wanted)}, headers=auth)
        assert asked.status_code == 409 and asked.json()["error"]["code"] == "make_folder"
        assert not (tmp / "Video").exists()
        # Nothing to move, but it's the originals' drive: that's still said first.
        said = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(wanted), "make_folder": True},
                          headers=auth)
        assert said.status_code == 409, said.text
        err = said.json()["error"]
        assert err["code"] == "move_previews" and err["details"]["files"] == 0
        assert err["message"].startswith(f"Trips's previews go to {wanted} from now on.") and "can't be browsed" in err["message"]
        assert not (tmp / "Video").exists()
        moved = client.put(f"/v1/libraries/{library_id}/previews",
                           json={"folder": str(wanted), "make_folder": True, "move": True}, headers=auth)
        assert moved.status_code == 202, moved.text
        assert _settled(client, auth, library_id)["folder"] == str(wanted)
    assert previews_location.read(data).libraries == {library_id: wanted}


def test_with_nothing_to_move_or_say_it_doesnt_ask(mac, monkeypatch):
    from src.server.api.routers import libraries

    data, originals, tmp, control_url = mac
    monkeypatch.setattr(libraries, "_same_device", lambda a, b: False)  # the originals on a drive of their own
    drive = tmp / "Video"
    drive.mkdir()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        there = client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(drive)}, headers=auth)
        assert there.status_code == 202, there.text
        assert _settled(client, auth, library_id)["folder"] == str(drive)
    assert previews_location.read(data).libraries == {library_id: drive}


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_its_own_folder_it_cant_write_any_more_is_flagged(mac):
    data, originals, tmp, control_url = mac
    drive = tmp / "Video"
    drive.mkdir()
    (drive / previews_location.MARKER).write_text("")
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, _ = _library(client, control_url, originals)
        previews_location.write(data, data, None, libraries={library_id: drive})
        drive.chmod(0o500)
        try:
            own = client.get(f"/v1/libraries/{library_id}/previews", headers=auth).json()
            listed = client.get("/v1/system/storage", headers=auth).json()["libraries"]
        finally:
            drive.chmod(0o700)
    said = f"Lumiverb can't write to {drive}: no permission."
    assert own["available"] is True and own["write_error"] == said
    assert listed == [{"library_id": library_id, "name": "Trips", "folder": str(drive), "available": True,
                       "write_error": said}]


def test_a_library_deleted_for_good_takes_its_previews_and_its_folder_choice(mac):
    # ADR-020 phase 0: its rows go, then (once committed) its previews from
    # every folder they may be in, and storage.json forgets its own folder.
    data, originals, tmp, control_url = mac
    drive = tmp / "Video"
    drive.mkdir()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, library_id, tenant_id = _library(client, control_url, originals)
        other = client.post("/v1/libraries", json={"name": "Kept", "root_path": str(originals)}, headers=auth)
        assert other.status_code == 200, other.text
        kept = data / tenant_id / other.json()["library_id"] / "proxies/00/ast_2_b.webp"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"another library's")
        assert client.put(f"/v1/libraries/{library_id}/previews", json={"folder": str(drive), "move": True},
                          headers=auth).status_code == 202
        assert _settled(client, auth, library_id)["own"] is True
        own = drive / tenant_id / library_id / "face_crops/07/face_1.webp"
        left = data / tenant_id / library_id / "scenes/00/ast_1_0000000000.jpg"  # one a move left behind
        for path in (own, left):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        assert client.delete(f"/v1/libraries/{library_id}", headers=auth).status_code == 204
        r = client.post("/v1/libraries/empty-trash", json={"library_ids": [library_id], "remove_from_projects": True},
                        headers=auth)
        assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    assert not (drive / tenant_id / library_id).exists() and not (data / tenant_id / library_id).exists()
    assert previews_location.read(data).libraries == {}
    assert kept.read_bytes() == b"another library's" and (drive / previews_location.MARKER).is_file()
