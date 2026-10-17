"""Keys that go unused end on their own (tenant settings' key_idle_days):
off until an admin sets the days; then upkeep ends each key a person made
that nobody has used for that long (made that long ago, if never used).
Never a key nobody made (the scheduler's: the install's own), a device's, or
the account's last admin key. Setting fewer days, or turning them on, asks
first when keys would end at once (409 keys_would_end, naming them), and
each key in /v1/keys says when it ends unless used (ends_at)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.server.api import rate_limit
from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.repository.control_plane import ApiKeyRepository
from src.shared.utils import utcnow
from tests.real_account import admin_with_database, drop_databases
from tests.test_first_run_setup import HERE, LOCALHOST, control  # noqa: F401 (control: a fixture)

pytestmark = pytest.mark.slow


@pytest.fixture
def account(control, monkeypatch):  # noqa: F811
    """The first admin signed in, their account's database made."""
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    get_settings.cache_clear()
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        robert, tenant_id = admin_with_database(client, control)
        yield client, robert, tenant_id, control
    drop_databases(control)


def _key(client: TestClient, robert: dict[str, str], label: str, role: str) -> tuple[str, dict[str, str]]:
    made = client.post("/v1/keys", json={"label": label, "role": role}, headers=robert)
    assert made.status_code == 200, made.text
    return made.json()["key_id"], {"Authorization": f"Bearer {made.json()['plaintext']}"}


def _last_used(control_url: str, key_id: str, at: datetime | None, made: datetime) -> None:
    engine = create_engine(control_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET last_used_at = :u, created_at = :c WHERE key_id = :k"),
                     {"u": at, "c": made, "k": key_id})
    engine.dispose()


def _ours(tenant_id: str, label: str) -> tuple[str, dict[str, str]]:
    """A key nobody made, as the scheduler's is."""
    with get_control_session() as ctrl:
        api_key, plaintext = ApiKeyRepository(ctrl).create(tenant_id, label=label, role="admin")
        return api_key.key_id, {"Authorization": f"Bearer {plaintext}"}


def test_unused_keys_end_once_an_admin_sets_the_days(account):
    client, robert, tenant_id, control_url = account
    now = utcnow()
    old_script, old_script_auth = _key(client, robert, "old script", "editor")
    laptop, laptop_auth = _key(client, robert, "laptop", "admin")
    fresh, fresh_auth = _key(client, robert, "fresh", "viewer")
    _last_used(control_url, old_script, now - timedelta(days=200), now - timedelta(days=300))
    _last_used(control_url, laptop, None, now - timedelta(days=120))  # never used: since it was made
    _last_used(control_url, fresh, now - timedelta(days=10), now - timedelta(days=400))
    scheduler, scheduler_auth = _ours(tenant_id, "scheduler")
    _last_used(control_url, scheduler, now - timedelta(days=500), now - timedelta(days=500))
    device = client.post("/v1/devices", json={"name": "Studio Mac"}, headers=robert).json()
    _last_used(control_url, _device_key_id(tenant_id), now - timedelta(days=500), now - timedelta(days=500))

    assert client.get("/v1/tenant/settings", headers=robert).json()["key_idle_days"] is None, "off until set"
    assert {k["label"]: k["ends_at"] for k in client.get("/v1/keys", headers=robert).json()["keys"]}["fresh"] is None
    for days in (0, 3651, "90"):
        assert client.patch("/v1/tenant/settings", json={"key_idle_days": days}, headers=robert).status_code == 422

    asked = client.patch("/v1/tenant/settings", json={"key_idle_days": 90}, headers=robert)
    assert asked.status_code == 409 and asked.json()["error"]["code"] == "keys_would_end", asked.text
    details = asked.json()["error"]["details"]
    assert details["key_idle_days"] == 90
    assert [(k["label"], k["role"]) for k in details["keys"]] == [("old script", "editor"), ("laptop", "admin")]
    assert client.get("/v1/tenant/settings", headers=robert).json()["key_idle_days"] is None, "a 409 changes nothing"

    saved = client.patch("/v1/tenant/settings", json={"key_idle_days": 90, "confirm_end_keys": True}, headers=robert)
    assert saved.status_code == 200 and saved.json()["key_idle_days"] == 90, saved.text
    ends = {k["label"]: k["ends_at"] for k in client.get("/v1/keys", headers=robert).json()["keys"]}
    assert datetime.fromisoformat(ends["fresh"]) - (now - timedelta(days=10)) == timedelta(days=90)
    assert ends["scheduler"] is None, "nobody made it: the install's own"

    ran = client.post("/v1/upkeep", headers=scheduler_auth)
    assert ran.status_code == 200, ran.text
    assert ran.json()["idle_keys"] == {"ended": 2}
    assert client.get("/v1/me", headers=old_script_auth).status_code == 401
    assert client.get("/v1/me", headers=laptop_auth).status_code == 401
    assert client.get("/v1/me", headers=fresh_auth).status_code == 200
    assert client.get("/v1/me", headers=scheduler_auth).status_code == 200
    on_device = client.get("/v1/me", headers={"Authorization": f"Bearer {device['key']}"})
    assert on_device.json()["error"]["code"] == "not_for_devices", "still its key: the Devices API ends a device's"
    assert client.post("/v1/upkeep", headers=scheduler_auth).json()["idle_keys"] == {"ended": 0}


def _device_key_id(tenant_id: str) -> str:
    with get_control_session() as ctrl:
        [key] = ApiKeyRepository(ctrl).device_keys(tenant_id).values()
        return key.key_id


def test_more_days_or_none_ask_nothing_and_fewer_ask_again(account):
    client, robert, _, control_url = account
    now = utcnow()
    script, _ = _key(client, robert, "script", "editor")
    _last_used(control_url, script, now - timedelta(days=50), now - timedelta(days=60))
    assert client.patch("/v1/tenant/settings", json={"key_idle_days": 90}, headers=robert).status_code == 200
    assert client.patch("/v1/tenant/settings", json={"key_idle_days": 365}, headers=robert).status_code == 200
    fewer = client.patch("/v1/tenant/settings", json={"key_idle_days": 30}, headers=robert)
    assert fewer.status_code == 409 and [k["label"] for k in fewer.json()["error"]["details"]["keys"]] == ["script"]
    off = client.patch("/v1/tenant/settings", json={"key_idle_days": None}, headers=robert)
    assert off.status_code == 200 and off.json()["key_idle_days"] is None
    assert client.patch("/v1/tenant/settings", json={"key_idle_days": 60}, headers=robert).status_code == 200, (
        "none unused for longer: nothing to ask")


def test_only_an_admin_sets_them(account):
    client, robert, _, _ = account
    _, editor = _key(client, robert, "an editor's", "editor")
    refused = client.patch("/v1/tenant/settings", json={"key_idle_days": 90}, headers=editor)
    assert refused.status_code == 403
    assert client.get("/v1/tenant/settings", headers=editor).json()["key_idle_days"] is None


def test_the_account_s_last_admin_key_is_kept(account):
    client, robert, tenant_id, control_url = account
    now = utcnow()
    older, _ = _key(client, robert, "older", "admin")
    newer, _ = _key(client, robert, "newer", "admin")
    _last_used(control_url, older, now - timedelta(days=40), now - timedelta(days=40))
    _last_used(control_url, newer, now - timedelta(days=35), now - timedelta(days=35))
    with get_control_session() as ctrl:
        repo = ApiKeyRepository(ctrl)
        assert [k.label for k in repo.unused_since(tenant_id, now - timedelta(days=30))] == ["older"]
        assert repo.end_unused(tenant_id, now - timedelta(days=30)) == 1
        assert [k.label for k in repo.list_by_tenant(tenant_id) if k.role == "admin"] == ["newer"]
        assert repo.end_unused(tenant_id, now - timedelta(days=30)) == 0
