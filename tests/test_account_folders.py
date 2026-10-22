# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Each account's folders on this server (src/server/api/account_folders.py),
the server admin's to give: PUT /v1/admin/tenants/{id}/folders.

Against the real API and databases (tests/test_scans_api.py's two
accounts). Until a server admin gives an account folders (null, every
account's after the migration), it chooses any folder the server reads, as
before: nothing is asked. Given some, a library this server reads is made,
moved and handed to it only inside them (403 outside_account_folders), a
library a device reads is the device's business, and this server's reader
hears them with each library; folders overlapping another live account's,
or leaving one of its libraries outside, are refused. A library's folder
can be a link: the scheduler reads it only where it leads inside its
account's folders. A change of folders and a library's folder being saved
wait for each other."""

from __future__ import annotations

import json
import threading
import time
import uuid
from unittest.mock import patch

import pytest

from src.server.database import get_control_session
from src.server.repository.control_plane import DELETED, TenantRepository
from src.server.repository.libraries import LibraryRepository
from tests.test_device_keys import _check_in, _register
from tests.test_scans_api import _error, _ok, env  # noqa: F401 — the shared fixture
from tests.test_server_admins import _waiting_on_locks

pytestmark = pytest.mark.slow

# The install's own key (the fixture's ADMIN_KEY): a server admin's on /v1/admin.
INSTALL = {"Authorization": "Bearer test-admin-scans"}
ANA = ["/Volumes/Ana", "/srv/ana"]


def _accounts(env) -> dict[str, dict]:
    return {t["name"]: t for t in _ok(env.client.get("/v1/admin/tenants", headers=INSTALL))}


def _ids(env) -> tuple[str, str]:
    accounts = _accounts(env)
    return accounts["ScansTenant"]["tenant_id"], accounts["OtherScansTenant"]["tenant_id"]


def _give(env, tenant_id: str, folders, headers: dict = INSTALL):
    return env.client.put(f"/v1/admin/tenants/{tenant_id}/folders", json={"folders": folders}, headers=headers)


def _make(env, root_path: str, reader: str = "server", headers: dict | None = None):
    return env.client.post("/v1/libraries", headers=headers or env.admin, json={
        "name": f"lib-{uuid.uuid4().hex[:8]}", "root_path": root_path, "reader": reader})


def _reader(env, library_id: str, reader: str, **body):
    return env.client.put(f"/v1/libraries/{library_id}/reader", json={"reader": reader, **body}, headers=env.admin)


def _root(env, library_id: str) -> tuple[str, str]:
    lib = _ok(env.client.get(f"/v1/libraries/{library_id}", headers=env.admin))
    return lib["reader"], lib["root_path"]


@pytest.fixture(autouse=True)
def _every_folder_again(env, monkeypatch):
    """No root map but what a test sets (never this checkout's .env); and after each test, every account's
    folders lifted and its libraries gone, so the next starts as a server with nothing given."""
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", "{}")
    yield
    for tenant_id in _ids(env):
        _ok(_give(env, tenant_id, None))
    for headers in (env.admin, env.other):
        for lib in _ok(env.client.get("/v1/libraries", headers=headers)):
            _ok(env.client.delete(f"/v1/libraries/{lib['library_id']}", headers=headers), 204)
        _ok(env.client.post("/v1/libraries/empty-trash", json={"all": True}, headers=headers))


def test_with_no_folders_of_its_own_an_account_chooses_any_folder_as_before(env):
    assert [a["folders"] for a in _accounts(env).values()] == [None, None]
    assert _ok(env.client.get("/v1/tenant/settings", headers=env.viewer))["folders"] is None
    library_id = _ok(_make(env, "/Volumes/Anything/Trips"))["library_id"]
    _ok(env.client.patch(f"/v1/libraries/{library_id}", json={"root_path": "/srv/elsewhere"}, headers=env.admin))
    device_id, _ = _register(env, "Anywhere Mac")
    _ok(_reader(env, library_id, device_id, root_path="/Users/robert/Trips"))
    _ok(_reader(env, library_id, "server"))
    assert _root(env, library_id) == ("server", "/Users/robert/Trips"), "handed back at its folder as it was"
    heard = _ok(_check_in(env, env.admin, library_id, reader="server"))
    assert [(lib["library_id"], lib["folders"]) for lib in heard["libraries"]] == [(library_id, None)]


def test_given_folders_a_library_this_server_reads_is_made_and_moved_only_inside_them(env):
    mine, _ = _ids(env)
    assert _ok(_give(env, mine, ANA)) == {"tenant_id": mine, "folders": ANA}
    assert _accounts(env)["ScansTenant"]["folders"] == ANA
    assert _ok(env.client.get("/v1/tenant/settings", headers=env.admin))["folders"] == ANA

    library_id = _ok(_make(env, "/Volumes/Ana/Trips"))["library_id"]
    _ok(_make(env, "/srv/ana"))  # the folder itself
    # Another folder, a sibling whose name starts the same, a folder holding one of them.
    for root in ("/Volumes/Bob", "/Volumes/Ana2", "/Volumes/Anarchy/Trips", "/srv", "/Volumes"):
        refused = _error(_make(env, root), 403, "outside_account_folders")
        assert refused["details"] == {"folders": ANA}
        assert refused["message"] == ("This account's folders are /Volumes/Ana, /srv/ana: choose one inside them, "
                                      "or ask a server admin.")

    _error(env.client.patch(f"/v1/libraries/{library_id}", json={"root_path": "/Volumes/Bob/Trips"},
                            headers=env.admin), 403, "outside_account_folders")
    assert _root(env, library_id) == ("server", "/Volumes/Ana/Trips"), "nothing changed"
    _ok(env.client.patch(f"/v1/libraries/{library_id}", json={"root_path": "/srv/ana/Trips"}, headers=env.admin))
    # Everything but its folder, as before.
    _ok(env.client.patch(f"/v1/libraries/{library_id}", json={"name": f"Renamed {library_id[-6:]}"},
                         headers=env.admin))


def test_a_device_s_library_is_its_own_until_handed_to_this_server_inside_the_folders(env):
    mine, _ = _ids(env)
    _ok(_give(env, mine, ["/Volumes/Ana"]))
    device_id, _ = _register(env, "Ana's Mac")
    on_the_mac = _ok(_make(env, "/Users/ana/Pictures", reader=device_id))["library_id"]
    _ok(env.client.patch(f"/v1/libraries/{on_the_mac}", json={"root_path": "/Users/ana/Movies"}, headers=env.admin))
    _ok(_reader(env, on_the_mac, device_id, root_path="/Users/ana/Desktop"))

    # Handed to this server: its folder as it is, or the one given, is in the account's folders.
    _error(_reader(env, on_the_mac, "server"), 403, "outside_account_folders")
    _error(_reader(env, on_the_mac, "server", root_path="/Volumes/Anabel"), 403, "outside_account_folders")
    assert _root(env, on_the_mac) == (device_id, "/Users/ana/Desktop"), "still the device's"
    _ok(_reader(env, on_the_mac, "server", root_path="/Volumes/Ana/Pictures"))
    assert _root(env, on_the_mac) == ("server", "/Volumes/Ana/Pictures")

    already = _ok(_make(env, "/Volumes/Ana/Movies", reader=device_id))["library_id"]
    _ok(_reader(env, already, "server"))
    assert _root(env, already) == ("server", "/Volumes/Ana/Movies"), "its folder as it was, inside them"


def test_this_server_s_reader_hears_the_folders_with_each_library_a_device_doesn_t(env):
    mine, _ = _ids(env)
    _ok(_give(env, mine, ["/Volumes/Ana"]))
    library_id = _ok(_make(env, "/Volumes/Ana/Trips"))["library_id"]
    device_id, device = _register(env, "Hearing Mac")
    its = _ok(_make(env, "/Users/ana/Pictures", reader=device_id))["library_id"]
    heard = _ok(_check_in(env, env.admin, library_id, reader="server"))
    assert [(lib["library_id"], lib["folders"]) for lib in heard["libraries"]] == [(library_id, ["/Volumes/Ana"])]
    assert [(lib["library_id"], lib["folders"]) for lib in _ok(_check_in(env, device, its))["libraries"]] == [
        (its, None)]


def test_folders_overlapping_another_live_account_s_are_refused_and_a_deleted_one_s_are_nobody_s(env):
    mine, other = _ids(env)
    _ok(_give(env, mine, ["/Volumes/Shared/ana"]))
    for theirs in (["/Volumes/Shared/ana"], ["/Volumes/Shared"], ["/srv/bob", "/Volumes/Shared/ana/2024"]):
        refused = _error(_give(env, other, theirs), 409, "folders_overlap")
        assert (refused["details"]["account"], refused["details"]["tenant_id"]) == ("ScansTenant", mine)
        assert refused["details"]["theirs"] == "/Volumes/Shared/ana"
    refused = _error(_give(env, other, ["/Volumes/Shared"]), 409, "folders_overlap")
    assert refused["message"] == ("/Volumes/Shared overlaps /Volumes/Shared/ana, a folder of ScansTenant: each "
                                  "account's folders are its own.")
    assert _accounts(env)["OtherScansTenant"]["folders"] is None, "nothing changed"
    assert _ok(_give(env, other, ["/Volumes/Shared/anna"]))["folders"] == ["/Volumes/Shared/anna"], \
        "a sibling whose name starts the same is another folder"

    with patch("src.server.accounts.provision_tenant_database"):
        gone = _ok(env.client.post("/v1/admin/tenants", json={"name": f"Gone {uuid.uuid4().hex[:6]}"},
                                   headers=INSTALL))["tenant_id"]
    with get_control_session() as ctrl:
        repo = TenantRepository(ctrl)
        repo.set_folders(repo.get_by_id(gone), ["/Volumes/Old"])
        repo.update_status(gone, DELETED)
    _ok(_give(env, other, ["/Volumes/Old/bob"]))
    assert _give(env, gone, None).status_code == 404, "a deleted account is given nothing"
    assert _give(env, "ten_nobody", None).status_code == 404


def test_folders_leaving_a_library_this_server_reads_outside_are_refused_naming_it(env):
    mine, _ = _ids(env)
    footage = _ok(_make(env, "/Volumes/Footage"))
    old = _ok(_make(env, "/Volumes/Old"))
    _ok(env.client.delete(f"/v1/libraries/{old['library_id']}", headers=env.admin), 204)
    device_id, _ = _register(env, "Leaving Mac")
    _ok(_make(env, "/Users/ana/Pictures", reader=device_id))  # the device's: not in the way
    _ok(_make(env, "/Volumes/Ana/Trips"))

    refused = _error(_give(env, mine, ["/Volumes/Ana"]), 409, "libraries_outside")
    assert sorted(refused["details"]["libraries"], key=lambda lib: lib["root_path"]) == [
        {"library_id": footage["library_id"], "name": footage["name"], "root_path": "/Volumes/Footage",
         "trashed": False},
        {"library_id": old["library_id"], "name": old["name"], "root_path": "/Volumes/Old", "trashed": True}]
    assert f"{footage['name']} (/Volumes/Footage)" in refused["message"]
    assert f"{old['name']} (/Volumes/Old, in the trash)" in refused["message"]
    assert refused["message"].endswith("would be outside these folders: move them first (Library settings › Who "
                                       "reads it), or include their folders.")
    assert _accounts(env)["ScansTenant"]["folders"] is None, "nothing changed"

    one = _error(_give(env, mine, ["/Volumes/Ana", "/Volumes/Old"]), 409, "libraries_outside")
    assert one["message"] == (f"{footage['name']} (/Volumes/Footage) would be outside these folders: move it first "
                              "(Library settings › Who reads it), or include its folder.")
    assert _ok(_give(env, mine, ["/Volumes"]))["folders"] == ["/Volumes"]


def test_null_lifts_them_and_none_leaves_only_the_account_s_own_computers(env):
    mine, _ = _ids(env)
    _ok(_give(env, mine, []))
    refused = _error(_make(env, "/Volumes/Anything"), 403, "outside_account_folders")
    assert refused["details"] == {"folders": []}
    assert refused["message"] == ("This account has no folders on this server: a computer of its own can read its "
                                  "libraries (Settings › Devices), or ask a server admin.")
    device_id, _ = _register(env, "Own Mac")
    _ok(_make(env, "/Users/ana/Pictures", reader=device_id))
    assert _ok(env.client.get("/v1/tenant/settings", headers=env.admin))["folders"] == []

    assert _ok(_give(env, mine, None)) == {"tenant_id": mine, "folders": None}
    _ok(_make(env, "/Volumes/Anything"))
    assert _accounts(env)["ScansTenant"]["folders"] is None
    assert _ok(env.client.get("/v1/tenant/settings", headers=env.admin))["folders"] is None


def test_a_folder_is_given_as_library_roots_are_kept_and_where_the_server_reads(env, monkeypatch):
    mine, _ = _ids(env)
    for bad in ("Volumes/Ana", "/Volumes/Ana/", "/", "", "/Volumes/../etc", "/Volumes/./Ana", "/Volumes//Ana",
                "/Volumes\\Ana", "/Volumes/Ana\n", "/Volumes/\tAna", "/" + "a" * 4096):
        refused = _error(_give(env, mine, ["/srv/fine", bad]), 422, "invalid_folder")
        assert refused["details"] == {"folder": bad}
    assert _give(env, mine, "/Volumes/Ana").status_code == 422, "a list, or null"
    assert env.client.put(f"/v1/admin/tenants/{mine}/folders", json={}, headers=INSTALL).status_code == 422, \
        "the choice is said"
    assert _give(env, mine, [f"/{i}" for i in range(101)]).status_code == 422
    assert _accounts(env)["ScansTenant"]["folders"] is None, "nothing changed"
    assert _ok(_give(env, mine, ["/b", "/a", "/b"]))["folders"] == ["/b", "/a"], "each once, as given"

    # Where the root map bounds what the server reads (a Linux install), a folder outside it is refused.
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", '{"/Volumes/media-01": "/mnt/media-01"}')
    _ok(_give(env, mine, ["/Volumes/media-01/ana"]))
    refused = _error(_give(env, mine, ["/Volumes/media-01/ana", "/Volumes/media-010"]), 422, "folder_not_read")
    assert refused["details"] == {"folder": "/Volumes/media-010", "root_map": ["/Volumes/media-01"]}
    assert refused["message"] == ("This server doesn't read /Volumes/media-010: its root map (LUMIVERB_ROOT_MAP) "
                                  "reads only /Volumes/media-01.")
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", '{"/": "/"}')  # a Mac: every folder it can read
    _ok(_give(env, mine, ["/srv/ana"]))


def test_only_a_server_admin_gives_folders(env):
    mine, _ = _ids(env)
    _, device = _register(env, "Giving Mac")
    for headers in (env.admin, env.editor, env.viewer, env.other):
        _error(_give(env, mine, ["/Volumes/Ana"], headers=headers), 403, "server_admin_required")
    _error(_give(env, mine, ["/Volumes/Ana"], headers=device), 403, "not_for_devices")
    assert _accounts(env)["ScansTenant"]["folders"] is None


def test_a_library_whose_folder_links_into_another_account_s_is_never_read(env, tmp_path, monkeypatch, caplog):
    """A library's folder can be a link: written inside its account's folders, the API takes it, and the
    scheduler, reaching it, resolves it and the account's folders, and reads nothing of another account's."""
    from src.processing import machine, roots
    from src.processing.originals import reach
    from src.server.scheduler.settings import SchedulerSettings

    disk = tmp_path.resolve()
    a, b = disk / "a", disk / "b"
    (a / "Trips").mkdir(parents=True)
    (a / "Trips" / "ana.jpg").write_bytes(b"ana's")
    (b / "Own").mkdir(parents=True)
    (b / "Own" / "bob.jpg").write_bytes(b"bob's")
    (b / "x").symlink_to(a)
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", json.dumps({str(disk): str(disk)}))
    mine, other = _ids(env)
    _ok(_give(env, mine, [str(a)]))
    _ok(_give(env, other, [str(b)]))
    linked = _ok(_make(env, str(b / "x" / "Trips"), headers=env.other))  # in B's folder, as written
    own = _ok(_make(env, str(b / "Own"), headers=env.other))
    _error(_make(env, str(a / "Trips"), headers=env.other), 403, "outside_account_folders")

    def heard() -> dict[str, dict]:
        return {lib["library_id"]: lib for lib in _ok(_check_in(env, env.other, reader="server"))["libraries"]}

    machine.use(SchedulerSettings().machine())
    monkeypatch.setattr(roots, "_said", set())
    libraries = heard()
    found = reach(libraries[own["library_id"]], require_entries=True)
    assert found is not None and [f["rel_path"] for f in found.walk()] == ["bob.jpg"]
    assert reach(libraries[linked["library_id"]], require_entries=True) is None
    assert f"{linked['name']} ({linked['library_id']}) refused: root leaves its account's folders" in caplog.text

    # With no folders of its own, as before: read where the link leads.
    _ok(_give(env, other, None))
    found = reach(heard()[linked["library_id"]], require_entries=True)
    assert found is not None and [f["rel_path"] for f in found.walk()] == ["ana.jpg"]


def _in_the_background(send) -> tuple[threading.Thread, list]:
    answer: list = []
    thread = threading.Thread(target=lambda: answer.append(send()))
    thread.start()
    return thread, answer


def _until_one_waits_on_a_lock() -> None:
    deadline = time.monotonic() + 30
    while _waiting_on_locks() < 1:
        assert time.monotonic() < deadline, "nothing waited"
        time.sleep(0.05)


def test_folders_given_while_a_library_s_folder_is_saved_wait_for_it_then_find_it(env, monkeypatch):
    mine, _ = _ids(env)
    saving, go_on = threading.Event(), threading.Event()
    create = LibraryRepository.create

    def slowly(self, **kw):
        saving.set()
        assert go_on.wait(30)
        return create(self, **kw)

    monkeypatch.setattr(LibraryRepository, "create", slowly)
    making, made = _in_the_background(lambda: _make(env, "/Volumes/Bob"))
    assert saving.wait(30)
    giving, given = _in_the_background(lambda: _give(env, mine, ["/Volumes/Ana"]))
    _until_one_waits_on_a_lock()
    go_on.set()
    making.join(30)
    giving.join(30)
    _ok(made[0])
    refused = _error(given[0], 409, "libraries_outside")
    assert [lib["root_path"] for lib in refused["details"]["libraries"]] == ["/Volumes/Bob"]


def test_a_library_s_folder_chosen_while_folders_are_given_waits_and_is_held_to_them(env):
    mine, _ = _ids(env)
    with get_control_session() as ctrl:
        repo = TenantRepository(ctrl)
        tenant = repo.to_change_folders(mine)
        making, made = _in_the_background(lambda: _make(env, "/Volumes/Bob"))
        _until_one_waits_on_a_lock()
        repo.set_folders(tenant, ["/Volumes/Ana"])
    making.join(30)
    _error(made[0], 403, "outside_account_folders")
