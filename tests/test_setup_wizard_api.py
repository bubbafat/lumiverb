"""The first-run wizard's progress (GET/PUT /v1/tenant/setup,
routers/tenant.py): which steps an account's admins went through, kept in
its database so the wizard can be left and picked up again, and whether
it's put away. Admins only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.server.api.main import app
from src.server.config import get_settings
from tests import real_account
from tests.test_first_run_setup import HERE, LOCALHOST, control  # noqa: F401 (control: a fixture)

pytestmark = pytest.mark.slow

STEPS = ["storage", "network", "find", "ai", "people", "library"]


@pytest.fixture
def account(control, tmp_path, monkeypatch):  # noqa: F811
    from src.server.api import rate_limit

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    get_settings.cache_clear()
    yield control
    real_account.drop_databases(control)
    get_settings.cache_clear()


def test_steps_are_kept_in_order_and_it_can_be_put_away(account):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, _ = real_account.admin_with_database(client, account)
        assert client.get("/v1/tenant/setup", headers=auth).json() == {
            "steps": STEPS, "done": [], "dismissed": False, "finished": False}
        r = client.put("/v1/tenant/setup", json={"done": ["network", "storage"]}, headers=auth)
        assert r.status_code == 200 and r.json()["done"] == ["storage", "network"], "in step order"
        r = client.put("/v1/tenant/setup", json={"dismissed": True}, headers=auth)
        assert r.json()["done"] == ["storage", "network"] and r.json()["dismissed"] is True, "the rest stays"
        r = client.put("/v1/tenant/setup", json={"done": STEPS}, headers=auth)
        assert r.json()["finished"] is True
        assert client.get("/v1/tenant/setup", headers=auth).json()["finished"] is True


def test_a_step_there_isn_t_is_refused(account):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        auth, _ = real_account.admin_with_database(client, account)
        r = client.put("/v1/tenant/setup", json={"done": ["storage", "backups"]}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_step"
    assert r.json()["error"]["details"]["steps"] == STEPS


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_admins(account, role):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        _, tenant_id = real_account.admin_with_database(client, account)
        auth = real_account.someone(client, tenant_id, role)
        assert client.get("/v1/tenant/setup", headers=auth).status_code == 403
        assert client.put("/v1/tenant/setup", json={"dismissed": True}, headers=auth).status_code == 403
