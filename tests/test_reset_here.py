"""A forgotten password on a Mac, which has no email set up (auth.py
forgot_password; `lumiverb-server reset-password`): from the machine
Lumiverb runs on, the reset link the email would carry comes back at once;
from anywhere else, the email (when set up) or where to go instead. Only
where the install says the person at it owns it (a Mac's,
LUMIVERB_RESET_ON_THIS_MACHINE): on a server, anything on it could reset any
account. The command line does the same, on any install. A new password
signs the person out everywhere."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.server.api import rate_limit
from src.server.api.main import app
from src.server.api.routers import auth
from src.server.config import get_settings
from src.server.lifecycle import cli
from src.server.setup import create_first_admin
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LAN,
    LOCALHOST,
    PASSWORD,
    _env_file,
    control,
)

pytestmark = pytest.mark.slow

NEW = "an entirely new password"


@pytest.fixture
def admin(control, monkeypatch):  # noqa: F811
    for limiter in (rate_limit.login_limiter, rate_limit.forgot_password_limiter, rate_limit.reset_password_limiter):
        monkeypatch.setattr(limiter, "_hits", {})
    return create_first_admin("robert@example.com", PASSWORD).email


@pytest.fixture
def mac(monkeypatch):
    """The install's env file says so (init --reset-on-this-machine)."""
    monkeypatch.setenv("LUMIVERB_RESET_ON_THIS_MACHINE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def email_set_up(monkeypatch):
    for key, value in {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "apikey", "SMTP_PASSWORD": "secret",
                       "SMTP_FROM": "noreply@example.com", "APP_HOST": "https://lumiverb.example.com"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    sent: list[str] = []
    monkeypatch.setattr(auth, "_send_reset_email", sent.append)
    yield sent
    get_settings.cache_clear()


def _login(client: TestClient, password: str) -> int:
    return client.post("/v1/auth/login", json={"email": "robert@example.com", "password": password}).status_code


def test_on_the_mac_forgot_password_leads_straight_to_a_new_one(admin, mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        asked = client.post("/v1/auth/forgot-password", json={"email": f" {admin} "})
        assert asked.status_code == 200, asked.text
        reset_url = asked.json()["reset_url"]
        assert reset_url.startswith("/reset-password?token=")
        token = reset_url.split("token=", 1)[1]
        assert client.post("/v1/auth/reset-password", json={"token": token, "password": NEW}).status_code == 200
        assert _login(client, NEW) == 200 and _login(client, PASSWORD) == 401
        again = client.post("/v1/auth/reset-password", json={"token": token, "password": NEW + "!"})
        assert again.status_code == 400, "a link works once"


@pytest.mark.parametrize(("host", "origin"), [
    ("rebind.example:6464", None),
    ("rebind.example:6464", "http://rebind.example:6464"),
    ("localhost:6464", "https://elsewhere.example"),
])
def test_on_the_mac_a_page_from_anywhere_else_gets_no_link(admin, mac, host, origin):
    # Its requests can reach nginx from 127.0.0.1, but carry the page's own
    # name or origin: not this machine, so no link, and nothing to read.
    headers = {"Host": host, **({"Origin": origin} if origin else {})}
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        asked = client.post("/v1/auth/forgot-password", json={"email": admin}, headers=headers)
        assert asked.status_code == 501 and "reset_url" not in asked.text, asked.text
        assert _login(client, PASSWORD) == 200, "unchanged"


def test_on_the_mac_an_unknown_email_is_said(admin, mac):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        asked = client.post("/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert asked.status_code == 404 and asked.json()["error"]["code"] == "no_such_account"


def test_from_another_device_without_email_it_says_where_to_go(admin, mac):
    with TestClient(app, client=LAN) as client:
        asked = client.post("/v1/auth/forgot-password", json={"email": admin})
    assert asked.status_code == 501 and asked.json()["error"]["code"] == "reset_elsewhere"
    assert asked.json()["error"]["message"].startswith("Password reset isn't set up here. On the computer Lumiverb")
    assert "reset_url" not in asked.text


def test_on_a_server_its_own_machine_gets_no_link(admin):
    """Anything on the server could ask from loopback: the operator has the command line."""
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        asked = client.post("/v1/auth/forgot-password", json={"email": admin})
    assert asked.status_code == 501 and asked.json()["error"] == {
        "code": "reset_not_set_up", "message": "Password reset isn't set up here. Ask whoever looks after Lumiverb.",
        "details": {}}
    assert "reset_url" not in asked.text


def test_on_a_server_with_email_its_own_machine_is_emailed_like_anyone(admin, email_set_up):
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        asked = client.post("/v1/auth/forgot-password", json={"email": admin})
        unknown = client.post("/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert (asked.status_code, asked.text) == (204, "") and unknown.status_code == 204, "no enumeration"
    assert email_set_up == [admin, "nobody@example.com"]


def test_the_command_line_sets_a_new_one(admin, control, tmp_path, monkeypatch, capsys):  # noqa: F811
    typed = iter([NEW, NEW])
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(typed))
    assert cli.main(["reset-password", "--env", str(_env_file(tmp_path, control)), "--email", admin]) == 0
    assert capsys.readouterr().out.strip() == "✓ A new password for robert@example.com; they're signed out everywhere else"
    with TestClient(app, client=LAN) as client:
        assert _login(client, NEW) == 200


@pytest.mark.parametrize(("email", "typed", "says"), [
    ("nobody@example.com", [], "✗ There's no account with that email here"),
    ("robert@example.com", [NEW, NEW + "!"] * 3, "✗ The passwords don't match"),  # asked again, three tries
    ("robert@example.com", ["short"] * 3, "✗ Use 12 to 72 characters"),
])
def test_the_command_line_refuses_what_won_t_do(admin, control, tmp_path, monkeypatch, capsys, email, typed, says):  # noqa: F811
    answers = iter(typed)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(answers))
    assert cli.main(["reset-password", "--env", str(_env_file(tmp_path, control)), "--email", email]) == 1
    said = capsys.readouterr().err.strip().splitlines()
    assert said[-1] == says and said.count(says) == (3 if typed else 1), said
    with TestClient(app, client=LAN) as client:
        assert _login(client, PASSWORD) == 200, "unchanged"
