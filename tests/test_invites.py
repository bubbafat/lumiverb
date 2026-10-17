"""Invites (src/server/invites.py; routers/users.py, routers/auth.py join): an
admin adds someone by email and role and gets a link to send them. The link
works once, for 7 days, at the address other people open Lumiverb at; there
the person chooses their password and is signed in. Until then nobody knows
their password. A new link, only for someone who hasn't signed in yet, voids
the old one."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from src.server import invites
from src.server.api import rate_limit
from src.server.api.main import app
from src.server.config import get_settings
from src.server.lifecycle import network
from src.server.lifecycle.init import nginx_conf, nginx_site
from src.server.setup import create_first_admin
from src.shared.utils import utcnow
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LOCALHOST,
    PASSWORD,
    control,
)

pytestmark = pytest.mark.slow

THEIRS = "a password of their own"


@pytest.fixture
def admin(control, monkeypatch, tmp_path):  # noqa: F811
    """Signed in as the install's first admin, on a server whose APP_HOST is its public URL."""
    for limiter in (rate_limit.login_limiter, rate_limit.reset_password_limiter):
        monkeypatch.setattr(limiter, "_hits", {})
    monkeypatch.setenv("APP_HOST", "https://lumiverb.example.com/")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    create_first_admin("robert@example.com", PASSWORD)
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        token = _login(client, "robert@example.com", PASSWORD).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client
    get_settings.cache_clear()


def _login(client: TestClient, email: str, password: str):
    return client.post("/v1/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""})


def _join(client: TestClient, url: str, password: str = THEIRS):
    token = url.split("token=", 1)[1]
    return client.post("/v1/auth/join", json={"token": token, "password": password}, headers={"Authorization": ""})


def _invite(client: TestClient, email: str = "ana@example.com", role: str = "editor") -> dict:
    made = client.post("/v1/users", json={"email": email, "role": role})
    assert made.status_code == 201, made.text
    return made.json()


def _user_id(client: TestClient, email: str) -> str:
    return next(u["user_id"] for u in client.get("/v1/users").json() if u["email"] == email)


def test_an_invite_is_a_link_where_they_choose_their_password_and_are_signed_in(admin):
    made = _invite(admin)
    assert (made["email"], made["role"], made["last_login_at"]) == ("ana@example.com", "editor", None)
    url = urlsplit(made["invite_urls"][0])
    assert (url.scheme, url.netloc, url.path) == ("https", "lumiverb.example.com", "/join"), "APP_HOST: a server's"
    left = datetime.fromisoformat(made["invite_expires_at"]) - utcnow()
    assert timedelta(days=7) - timedelta(minutes=1) < left <= timedelta(days=7)
    assert _login(admin, "ana@example.com", "").status_code == 401, "nobody knows their password"

    joined = _join(admin, made["invite_urls"][0])
    assert joined.status_code == 200, joined.text
    me = admin.get("/v1/me", headers={"Authorization": f"Bearer {joined.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "ana@example.com"
    assert _login(admin, "ana@example.com", THEIRS).status_code == 200
    listed = {u["email"]: u for u in admin.get("/v1/users").json()}
    assert listed["ana@example.com"]["last_login_at"] is not None
    assert listed["ana@example.com"]["invite_urls"] is None, "a link is shown once, when it's made"

    again = _join(admin, made["invite_urls"][0], THEIRS + "!")
    assert again.status_code == 400 and again.json()["error"]["code"] == "invite_not_valid", "a link works once"
    assert _login(admin, "ana@example.com", THEIRS).status_code == 200, "unchanged"


def test_on_a_mac_open_to_other_devices_the_link_is_its_network_address(admin, tmp_path, monkeypatch):
    conf = tmp_path / "nginx.conf"
    conf.write_text(nginx_conf(nginx_site(listen=(network.include_line(tmp_path),), server_name="_",
                                          root=tmp_path / "web", upstream="http://127.0.0.1:6465")))
    monkeypatch.setenv("APP_HOST", "http://localhost:6464")
    monkeypatch.setenv("NGINX_CONFIG", str(conf))
    monkeypatch.setenv("WEB_PORT", "6464")
    monkeypatch.setattr(network, "addresses", lambda port, scheme="https": [f"{scheme}://macstudio.local:{port}",
                                                                          f"{scheme}://192.168.86.55:{port}"])
    get_settings.cache_clear()

    assert _invite(admin, "mac-only@example.com")["invite_urls"][0].startswith(
        "http://localhost:6464/join?token="), "this Mac only: its own address, until Settings › Network says"
    network.set_other_devices(tmp_path, True)
    first, by_address = _invite(admin)["invite_urls"]
    assert first.startswith("https://macstudio.local:6443/join?token="), "its name first (Apple devices), over HTTPS"
    assert by_address == first.replace("macstudio.local", "192.168.86.55"), "the same link by address: any device"


def test_a_new_link_voids_the_old_one(admin):
    first = _invite(admin)["invite_urls"][0]
    renewed = admin.post(f"/v1/users/{_user_id(admin, 'ana@example.com')}/invite")
    assert renewed.status_code == 200, renewed.text
    second = renewed.json()["invite_urls"][0]
    assert second != first and renewed.json()["invite_expires_at"]
    assert _join(admin, first).json()["error"]["code"] == "invite_not_valid"
    assert _join(admin, second).status_code == 200


def test_no_new_link_for_someone_who_has_signed_in(admin):
    _join(admin, _invite(admin)["invite_urls"][0])
    refused = admin.post(f"/v1/users/{_user_id(admin, 'ana@example.com')}/invite")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "already_joined"
    assert "Forgot password" in refused.json()["error"]["message"]


def test_an_expired_link_doesn_t_work(admin, monkeypatch):
    monkeypatch.setattr(invites, "INVITE_FOR", timedelta(seconds=-1))
    joined = _join(admin, _invite(admin)["invite_urls"][0])
    assert joined.status_code == 400 and joined.json()["error"]["code"] == "invite_not_valid"
    assert "Ask whoever invited you for a new one" in joined.json()["error"]["message"]


def test_a_password_too_short_leaves_the_link_working(admin):
    url = _invite(admin)["invite_urls"][0]
    short = _join(admin, url, "short")
    assert short.status_code == 400 and short.json()["error"]["code"] == "password_length"
    assert _join(admin, url).status_code == 200


def test_only_admins_make_new_links(admin):
    _invite(admin, "not-yet@example.com", "viewer")
    _join(admin, _invite(admin, "editor@example.com", "editor")["invite_urls"][0])
    editor = _login(admin, "editor@example.com", THEIRS).json()["access_token"]
    refused = admin.post(f"/v1/users/{_user_id(admin, 'not-yet@example.com')}/invite",
                         headers={"Authorization": f"Bearer {editor}"})
    assert refused.status_code == 403


def test_a_password_given_is_ready_to_sign_in_with_no_link(admin):
    """For a script that sets one."""
    made = admin.post("/v1/users", json={"email": "bot@example.com", "role": "viewer", "password": THEIRS})
    assert made.status_code == 201 and made.json()["invite_urls"] is None
    assert _login(admin, "bot@example.com", THEIRS).status_code == 200
