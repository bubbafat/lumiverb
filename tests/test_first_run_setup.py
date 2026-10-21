"""First-run setup (src/server/setup.py; routers/setup.py; `lumiverb-server
setup`): an install's first admin, while nobody can log in yet. The web
app's setup page answers only the machine Lumiverb runs on, and only as
nginx saw the request; the command line reads the install's env file. Both
make the admin in the installer's account (Linux) or a new one (a Mac), and
then setup is closed for good, even for two at once."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session, select
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from src.server.lifecycle import cli
from src.server.models.control_plane import Tenant, TenantDbRouting, User
from src.server.repository.control_plane import TenantRepository
from src.server.setup import SetupError, create_first_admin
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _run_control_migrations

pytestmark = pytest.mark.slow

HERE = ("127.0.0.1", 50000)
LOCALHOST = "http://localhost:6464"  # how a browser on the machine itself addresses it
LAN = ("192.168.86.20", 50000)
PASSWORD = "correct horse battery"


@pytest.fixture
def control(monkeypatch):
    """A fresh install's control plane: migrated, nobody in it. Making an
    account's own database is mocked (as the admin API's tests do)."""
    with PostgresContainer(PG_IMAGE) as postgres:
        url = _ensure_psycopg2(postgres.get_connection_url())
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(url)
        monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", url)
        monkeypatch.setenv("TENANT_DATABASE_URL_TEMPLATE", str(make_url(url).set(database="{tenant_id}")))
        monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-of-at-least-32-bytes")
        get_settings.cache_clear()
        _engines.clear()
        with patch("src.server.accounts.provision_tenant_database"):
            yield url
        _engines.clear()
        get_settings.cache_clear()


def _rows(url: str, model) -> list:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            return list(session.exec(select(model)).all())
    finally:
        engine.dispose()


def _client(address=HERE) -> TestClient:
    return TestClient(app, client=address, base_url=LOCALHOST)


def test_a_page_from_anywhere_else_can_t_set_it_up(control):  # noqa: F811
    # A page whose name now points at this machine reaches it from 127.0.0.1,
    # with its own name.
    with _client() as client:
        headers = {"Host": "rebind.example:6464", "Origin": "http://rebind.example:6464"}
        assert client.get("/v1/setup", headers=headers).json() == {"needed": True, "on_this_machine": False}
        refused = client.post("/v1/setup", json={"email": "x@example.com", "password": PASSWORD}, headers=headers)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "setup_elsewhere"
        assert client.get("/v1/setup").json() == {"needed": True, "on_this_machine": True}, "still open here"


def test_a_new_install_makes_an_account_and_its_admin_then_setup_is_closed(control):
    with _client() as client:
        assert client.get("/v1/setup").json() == {"needed": True, "on_this_machine": True}

        made = client.post("/v1/setup", json={"email": " robert@example.com ", "password": PASSWORD})
        assert made.status_code == 201, made.text
        me = client.get("/v1/me", headers={"Authorization": f"Bearer {made.json()['access_token']}"})
        assert me.status_code == 200, "logged in as the new admin"

        assert client.get("/v1/setup").json()["needed"] is False
        again = client.post("/v1/setup", json={"email": "someone@example.com", "password": PASSWORD})
        assert again.status_code == 409 and again.json()["error"]["code"] == "already_set_up"

    [user] = _rows(control, User)
    [account] = _rows(control, Tenant)
    assert (user.email, user.role, user.tenant_id) == ("robert@example.com", "admin", account.tenant_id)
    assert account.name == "Lumiverb"
    assert [r.tenant_id for r in _rows(control, TenantDbRouting)] == [account.tenant_id]


def test_from_another_device_it_says_where_and_makes_nothing(control):
    with _client(LAN) as client:
        assert client.get("/v1/setup").json() == {"needed": True, "on_this_machine": False}
        refused = client.post("/v1/setup", json={"email": "a@example.com", "password": PASSWORD})
    assert refused.status_code == 403
    assert refused.json()["error"] == {
        "code": "setup_elsewhere",
        "message": "Open Lumiverb on the computer it's installed on to finish setting up.",
        "details": {},
    }
    assert _rows(control, User) == [] and _rows(control, Tenant) == []


@pytest.mark.parametrize(("peer", "forwarded", "allowed"), [
    (HERE, "127.0.0.1", True),  # this machine, through nginx
    (HERE, "::1", True),
    (HERE, "127.0.0.1, 192.168.86.20", False),  # nginx saw the LAN; the left is what the client sent
    (LAN, "127.0.0.1", False),  # straight to the API, claiming to be here
], ids=["here through nginx", "here over IPv6", "lan with a forged header", "lan claiming here"])
def test_only_the_address_nginx_saw_counts(control, peer, forwarded, allowed):
    with _client(peer) as client:
        answer = client.post("/v1/setup", json={"email": "a@example.com", "password": PASSWORD},
                             headers={"X-Forwarded-For": forwarded})
    assert answer.status_code == (201 if allowed else 403), answer.text


def test_the_installers_account_gets_the_admin(control):
    # Linux: deploy-api.sh makes the account, but nobody to log in.
    engine = create_engine(control)
    with Session(engine) as session:
        installers = TenantRepository(session).create(name="Media").tenant_id
    engine.dispose()

    with _client() as client:
        assert client.post("/v1/setup", json={"email": "a@example.com", "password": PASSWORD,
                                              "account_name": "ignored"}).status_code == 201
    [user] = _rows(control, User)
    assert user.tenant_id == installers
    assert [t.name for t in _rows(control, Tenant)] == ["Media"], "no second account"


def test_several_accounts_and_nobody_to_log_in_points_to_the_command_line(control):
    engine = create_engine(control)
    with Session(engine) as session:
        TenantRepository(session).create(name="One")
        TenantRepository(session).create(name="Two")
    engine.dispose()
    with _client() as client:
        refused = client.post("/v1/setup", json={"email": "a@example.com", "password": PASSWORD})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "several_accounts"
    assert "lumiverb user create" in refused.json()["error"]["message"]
    assert _rows(control, User) == []


@pytest.mark.parametrize(("email", "password", "code"), [
    ("robert", PASSWORD, "invalid_email"),
    ("robert@example.com", "short", "password_too_short"),
    ("robert@example.com", "x" * 73, "password_too_long"),
])
def test_details_that_wont_do_are_said_and_nothing_is_made(control, email, password, code):
    with _client() as client:
        refused = client.post("/v1/setup", json={"email": email, "password": password})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == code
    assert _rows(control, User) == [] and _rows(control, Tenant) == []


def test_two_at_once_make_one_admin_in_one_account(control):
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def set_up(email: str) -> None:
        start.wait()
        try:
            outcomes.append(create_first_admin(email, PASSWORD).email)
        except SetupError as e:
            outcomes.append(e.code)

    threads = [threading.Thread(target=set_up, args=(f"{who}@example.com",)) for who in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes, key=lambda o: o == "already_set_up")[1] == "already_set_up", outcomes
    assert len(_rows(control, User)) == 1 and len(_rows(control, Tenant)) == 1


def _env_file(tmp_path, url: str):
    env_file = tmp_path / "env"
    env_file.write_text(
        f"CONTROL_PLANE_DATABASE_URL={url}\n"
        f"TENANT_DATABASE_URL_TEMPLATE={make_url(url).set(database='{tenant_id}')}\n"
        "JWT_SECRET=test-jwt-secret-of-at-least-32-bytes\nAPP_HOST=http://localhost:6464\n"
    )
    return env_file


def test_the_command_line_makes_the_same_admin(control, tmp_path, monkeypatch, capsys):
    typed = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(typed))
    monkeypatch.setattr("builtins.input", lambda _prompt: "robert@example.com")

    assert cli.main(["setup", "--env", str(_env_file(tmp_path, control))]) == 0
    assert capsys.readouterr().out.strip() == (
        "✓ robert@example.com is this Lumiverb's admin. Log in at http://localhost:6464")
    [user] = _rows(control, User)
    assert user.role == "admin"

    # Closed: said before asking for anything.
    monkeypatch.setattr(cli, "getpass", lambda _prompt: pytest.fail("nothing may be asked"))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("nothing may be asked"))
    assert cli.main(["setup", "--env", str(_env_file(tmp_path, control))]) == 1
    assert capsys.readouterr().err.strip() == "✗ This Lumiverb is set up already. Log in instead."


def test_the_command_line_says_the_rule_first_and_asks_again(control, tmp_path, monkeypatch, capsys):
    typed = iter(["too short", PASSWORD, PASSWORD + "!", PASSWORD, PASSWORD])
    asked = []

    def getpass(prompt: str) -> str:
        asked.append((prompt, capsys.readouterr().err.splitlines()))  # what was said before each question
        return next(typed)

    monkeypatch.setattr(cli, "getpass", getpass)
    assert cli.main(["setup", "--env", str(_env_file(tmp_path, control)), "--email", "a@example.com"]) == 0
    assert asked == [
        ("Password: ", ["At least 12 characters, at most 72."]),
        ("Password: ", ["✗ Use 12 to 72 characters"]),  # checked as it's typed, before it's asked for again
        ("Password again: ", []),
        ("Password: ", ["✗ The passwords don't match"]),
        ("Password again: ", []),
    ]
    assert [user.email for user in _rows(control, User)] == ["a@example.com"]


def test_the_command_line_makes_nothing_after_three_tries(control, tmp_path, monkeypatch, capsys):
    typed = iter([PASSWORD, PASSWORD + "!"] * 3)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(typed))
    assert cli.main(["setup", "--env", str(_env_file(tmp_path, control)), "--email", "a@example.com"]) == 1
    assert capsys.readouterr().err.splitlines()[1:] == ["✗ The passwords don't match"] * 3
    assert _rows(control, User) == []


def test_the_command_line_checks_the_email_before_asking_for_a_password(control, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "getpass", lambda _prompt: pytest.fail("nothing more may be asked"))
    assert cli.main(["setup", "--env", str(_env_file(tmp_path, control)), "--email", "robert"]) == 1
    assert capsys.readouterr().err.strip() == "✗ Enter your email address."
    assert _rows(control, User) == []
