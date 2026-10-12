"""Admin › Logs' API (GET /v1/system/logs, routers/system.py): an account
admin reads the service's log (src/server/service_log.py). A server with more
than one account shows it only on the machine itself: the log names every
account's files."""

from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from src.server.api import rate_limit
from src.server.api.main import app
from src.server.database import get_control_session
from src.server.repository.control_plane import TenantRepository, UserRepository
from src.server.setup import create_first_admin
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LAN,
    PASSWORD,
    control,
)

pytestmark = pytest.mark.slow

LINES = [
    "lumiverb-server: api started (pid 3)",
    "api | 2026-10-11 21:53:23,440 INFO src.server.api.routers.system: Backup started (an admin's Back up now)",
    "scheduler | 2026-10-11 21:54:00,000 ERROR src.server.scheduler.runners: faces failed for IMG_1.jpg",
    "scheduler | Traceback (most recent call last):",
    "scheduler | ValueError: boom",
]


@pytest.fixture
def mac_log(control, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "lumiverb.log"
    path.write_text("".join(line + "\n" for line in LINES))
    monkeypatch.setenv("LUMIVERB_LOG_FILE", str(path))
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    return path


def _signed_in(client: TestClient, role: str = "admin") -> dict[str, str]:
    admin = create_first_admin("robert@example.com", PASSWORD)
    email = "robert@example.com"
    if role != "admin":
        email = f"{role}@example.com"
        with get_control_session() as session:
            UserRepository(session).create(admin.tenant_id, email,
                                           bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), role=role)
    token = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_an_admin_reads_the_log_newest_first(mac_log):
    with TestClient(app, client=HERE) as client:
        auth = _signed_in(client)
        got = client.get("/v1/system/logs", headers=auth)
        assert got.status_code == 200, got.text
        body = got.json()
        assert (body["source"], body["where"], body["unavailable"], body["next_cursor"]) == (
            "file", str(mac_log), None, None)
        assert [(e["part"], e["level"]) for e in body["entries"]] == [
            ("scheduler", "error"), ("api", "info"), ("lumiverb-server", "info")]
        assert body["entries"][0]["text"].endswith("ValueError: boom")
        problems = client.get("/v1/system/logs", params={"problems": True, "q": "boom"}, headers=auth).json()
        assert [e["part"] for e in problems["entries"]] == ["scheduler"]
        paged = client.get("/v1/system/logs", params={"limit": 1}, headers=auth).json()
        rest = client.get("/v1/system/logs", params={"after": paged["next_cursor"]}, headers=auth).json()
        assert [e["id"] for e in paged["entries"] + rest["entries"]] == [e["id"] for e in body["entries"]]


def test_one_account_reads_it_from_another_device_too(mac_log):
    with TestClient(app, client=HERE) as here:
        auth = _signed_in(here)
    with TestClient(app, client=LAN) as client:
        assert client.get("/v1/system/logs", headers=auth).status_code == 200


def test_with_more_than_one_account_only_on_the_machine_itself(mac_log):
    with TestClient(app, client=HERE) as here:
        auth = _signed_in(here)
        with get_control_session() as session:
            TenantRepository(session).create("Another account")
        assert here.get("/v1/system/logs", headers=auth).status_code == 200
    with TestClient(app, client=LAN) as client:
        refused = client.get("/v1/system/logs", headers=auth)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "logs_elsewhere"


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_admins(mac_log, role):
    with TestClient(app, client=HERE) as client:
        assert client.get("/v1/system/logs", headers=_signed_in(client, role)).status_code == 403


def test_a_cursor_not_of_this_log_is_a_400(mac_log):
    with TestClient(app, client=HERE) as client:
        got = client.get("/v1/system/logs", params={"after": "nonsense"}, headers=_signed_in(client))
    assert got.status_code == 400 and got.json()["error"]["code"] == "bad_cursor"


def test_no_log_here_says_why(control, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LUMIVERB_LOG_FILE", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(rate_limit.login_limiter, "_hits", {})
    with TestClient(app, client=HERE) as client:
        body = client.get("/v1/system/logs", headers=_signed_in(client)).json()
    assert body["source"] is None and body["entries"] == [] and "terminal" in body["unavailable"]
