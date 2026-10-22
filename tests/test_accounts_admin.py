"""The server admin's accounts (routers/admin.py), as the web app's Admin ›
Accounts uses them: an account made with its first admin's invite, renamed,
and deleted once what it holds is said and its name typed. A deleted
account is never reached again: nobody signs in to it, its tokens and keys
answer nothing, and upkeep doesn't work on it. A server admin can't delete
their own account, nor leave no server admin."""

from __future__ import annotations

import pytest

from src.server.repository.control_plane import TenantRepository
from tests.test_first_run_setup import control  # noqa: F401 (a fixture)
from tests.test_server_admins import INSTALL_KEY, install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow
PASSWORD = "a password of ana's own"


def _join(client, url: str) -> dict:
    r = client.post("/v1/auth/join", json={"token": url.split("token=", 1)[1], "password": PASSWORD},
                    headers={"Authorization": ""})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _account(client, robert, name: str = "Ana's", email: str = "ana@example.com") -> tuple[str, dict]:
    r = client.post("/v1/admin/tenants", json={"name": name, "email": email}, headers=robert)
    assert r.status_code == 200, r.text
    return r.json()["tenant_id"], _join(client, r.json()["invite_urls"][0])


def _listed(client, robert) -> dict[str, dict]:
    r = client.get("/v1/admin/tenants", headers=robert)
    assert r.status_code == 200, r.text
    return {t["tenant_id"]: t for t in r.json()}


def test_an_account_made_with_its_first_admins_email_invites_them(install):  # noqa: F811
    client, robert, _ = install
    r = client.post("/v1/admin/tenants", json={"name": "  Ana's  ", "email": "ana@example.com"}, headers=robert)
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["invite_urls"] and made["invite_expires_at"] and made["api_key"].startswith("lv_")
    listed = _listed(client, robert)[made["tenant_id"]]
    assert (listed["name"], listed["status"], listed["users"]) == ("Ana's", "active", 1) and listed["created_at"]
    ana = _join(client, made["invite_urls"][0])
    me = client.get("/v1/me", headers=ana).json()
    assert (me["email"], me["role"], me["server_admin"]) == ("ana@example.com", "admin", False)

    again = client.post("/v1/admin/tenants", json={"name": "Twice", "email": "ANA@example.com"}, headers=robert)
    assert again.status_code == 409 and again.json()["error"]["code"] == "email_conflict", again.text
    for name in ("", "  ", "a\nb"):
        assert client.post("/v1/admin/tenants", json={"name": name}, headers=robert).status_code == 422, name


def test_an_account_is_renamed(install):  # noqa: F811
    client, robert, _ = install
    tenant_id, _ana = _account(client, robert)
    r = client.patch(f"/v1/admin/tenants/{tenant_id}", json={"name": "Ana and Sam"}, headers=robert)
    assert r.status_code == 200 and r.json()["name"] == "Ana and Sam", r.text
    assert _listed(client, robert)[tenant_id]["name"] == "Ana and Sam"
    assert client.patch(f"/v1/admin/tenants/{tenant_id}", json={"name": "a\tb"}, headers=robert).status_code == 422


def test_deleting_says_what_it_holds_asks_its_name_and_then_nobody_reaches_it(install):  # noqa: F811
    client, robert, _ = install
    tenant_id, ana = _account(client, robert)
    key = client.post("/v1/keys", json={"label": "a script", "role": "admin"}, headers=ana)
    assert key.status_code == 200, key.text
    ana_key = {"Authorization": f"Bearer {key.json()['plaintext']}"}
    assert client.get("/v1/me", headers=ana).status_code == 200

    r = client.delete(f"/v1/admin/tenants/{tenant_id}", headers=robert)
    assert r.status_code == 409 and r.json()["error"]["code"] == "confirm_delete_account", r.text
    # This fixture's new accounts' databases can't be reached (its template's password hidden): said as not known.
    assert r.json()["error"]["details"] == {"name": "Ana's", "users": 1, "libraries": None, "clips": None}
    r = client.delete(f"/v1/admin/tenants/{tenant_id}", params={"confirm": "ana's"}, headers=robert)
    assert r.status_code == 409, "its name as it is"
    assert client.get("/v1/me", headers=ana).status_code == 200, "nothing done"
    assert client.delete(f"/v1/admin/tenants/{tenant_id}", params={"confirm": "Ana's"}, headers=robert).status_code == 204

    assert _listed(client, robert)[tenant_id]["status"] == "deleted"
    gone = client.get("/v1/me", headers=ana)
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "account_deleted", gone.text
    assert client.get("/v1/libraries", headers=ana_key).status_code == 401
    login = client.post("/v1/auth/login", json={"email": "ana@example.com", "password": PASSWORD},
                        headers={"Authorization": ""})
    assert login.status_code == 403, login.text
    wrong = client.post("/v1/auth/login", json={"email": "ana@example.com", "password": "not hers at all"},
                        headers={"Authorization": ""})
    assert wrong.status_code == 401, "said only to someone who knows the password"
    from src.server.database import get_control_session

    with get_control_session() as session:
        assert tenant_id not in {t.tenant_id for t in TenantRepository(session).list_live()}, "not worked on"
    assert client.delete(f"/v1/admin/tenants/{tenant_id}", params={"confirm": "Ana's"},
                         headers=robert).status_code == 404


def test_what_an_account_holds_is_counted_from_its_database(install):  # noqa: F811
    from src.server.api.routers.admin import _holds

    client, robert, own = install
    r = client.post("/v1/libraries", json={"name": "Footage", "root_path": "/Volumes/footage"}, headers=robert)
    assert r.status_code == 200, r.text
    assert _holds(own, 1) == {"users": 1, "libraries": 1, "clips": 0}


def test_a_server_admin_cant_delete_their_own_account_nor_leave_no_server_admin(install):  # noqa: F811
    client, robert, own = install
    r = client.delete(f"/v1/admin/tenants/{own}", headers=robert)
    assert r.status_code == 409 and r.json()["error"]["code"] == "own_account", r.text
    install_key = {"Authorization": f"Bearer {INSTALL_KEY}"}
    r = client.delete(f"/v1/admin/tenants/{own}", params={"confirm": "x"}, headers=install_key)
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_server_admin", r.text
    assert client.get("/v1/me", headers=robert).status_code == 200


def _ana_a_server_admin(client, robert) -> tuple[str, dict]:
    tenant_id, ana = _account(client, robert)
    named = client.post("/v1/admin/server-admins", json={"email": "ana@example.com"}, headers=robert)
    assert named.status_code == 200, named.text
    return tenant_id, ana


def test_a_server_admin_in_a_deleted_account_isnt_one_left(install):  # noqa: F811
    """One rule for taking the last server admin away (UserRepository.keeps_a_server_admin): one in a deleted
    account can't sign in, so removing the last other one is refused as deleting their account is."""
    client, robert, own = install
    ana_s, _ana = _ana_a_server_admin(client, robert)
    assert client.delete(f"/v1/admin/tenants/{ana_s}", params={"confirm": "Ana's"}, headers=robert).status_code == 204
    me = client.get("/v1/me", headers=robert).json()["user_id"]
    last = client.delete(f"/v1/admin/server-admins/{me}", headers=robert)
    assert (last.status_code, last.json()["error"]["code"]) == (409, "last_server_admin"), last.text
    assert client.get("/v1/admin/tenants", headers=robert).status_code == 200


def test_two_server_admins_deleting_each_other_s_accounts_at_once_leave_one(install):  # noqa: F811
    from tests.test_server_admins import _at_once

    client, robert, own = install
    ana_s, ana = _ana_a_server_admin(client, robert)
    statuses = _at_once(client, [("DELETE", f"/v1/admin/tenants/{ana_s}", robert, {"confirm": "Ana's"}),
                                 ("DELETE", f"/v1/admin/tenants/{own}", ana, {"confirm": "Lumiverb"})])
    assert sorted(statuses) == [(204, None), (409, "last_server_admin")], statuses
    from src.server.database import get_control_session

    with get_control_session() as session:
        assert len({t.tenant_id for t in TenantRepository(session).list_live()} & {own, ana_s}) == 1
