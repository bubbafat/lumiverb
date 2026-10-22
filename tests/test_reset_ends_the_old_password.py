"""A password reset ends everything the old password could have made
(repository/control_plane.py): every session, the API keys the person made
(and keys made with those), and their links not yet used, reset and invite
links alike. The same from a reset link (auth.py reset-password) and from
the operator's `lumiverb-server reset-password`. Never the install's own
processing: the scheduler's key and the operator's are nobody's."""

from __future__ import annotations

import threading

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from src.server.api import rate_limit
from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import get_control_engine
from src.server.lifecycle import cli
from src.server.repository.control_plane import PasswordResetTokenRepository
from src.server.scheduler.service import scheduler_key
from src.server.setup import create_first_admin
from tests.test_first_run_setup import (  # noqa: F401 (control: a fixture)
    HERE,
    LOCALHOST,
    PASSWORD,
    _env_file,
    control,
)

pytestmark = pytest.mark.slow

ROBERT = "robert@example.com"
ANA = "ana@example.com"
NEW = "an entirely new password"
ADMIN_KEY = "the-operators-admin-key"


@pytest.fixture
def install(control, monkeypatch, tmp_path):  # noqa: F811
    """A Mac's install (Forgot password there gives the link at once) with
    its first admin, Robert, and Ana, an editor; signed in as Robert."""
    for limiter in (rate_limit.login_limiter, rate_limit.forgot_password_limiter, rate_limit.reset_password_limiter,
                    rate_limit.forgot_password_here, rate_limit.forgot_password_anywhere):
        monkeypatch.setattr(limiter, "_hits", {})
        monkeypatch.setattr(limiter, "max_requests", 100)  # these tests sign in more often than a person may
    monkeypatch.setenv("LUMIVERB_RESET_ON_THIS_MACHINE", "true")
    monkeypatch.setenv("ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("APP_HOST", "http://localhost:6464")  # the env file's, put back after (cli reads it in)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    tenant_id = create_first_admin(ROBERT, PASSWORD).tenant_id
    with TestClient(app, client=HERE, base_url=LOCALHOST) as client:
        robert = _login(client, ROBERT, PASSWORD)
        made = client.post("/v1/users", json={"email": ANA, "role": "editor", "password": PASSWORD},
                           headers=_bearer(robert))
        assert made.status_code == 201, made.text
        yield client, tenant_id, robert
    get_settings.cache_clear()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, email: str, password: str) -> str:
    signed_in = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert signed_in.status_code == 200, signed_in.text
    return signed_in.json()["access_token"]


def _new_key(client: TestClient, token: str) -> str:
    """A key made in Settings › API Keys (or by the CLI with its own key)."""
    made = client.post("/v1/keys", json={"label": "cli"}, headers=_bearer(token))
    assert made.status_code == 200, made.text
    return made.json()["plaintext"]


def _works(client: TestClient, key: str) -> int:
    return client.get("/v1/keys", headers=_bearer(key)).status_code


def _reset_link(client: TestClient, email: str) -> str:
    asked = client.post("/v1/auth/forgot-password", json={"email": email})
    assert asked.status_code == 200, asked.text
    return asked.json()["reset_url"].split("token=", 1)[1]


def _reset(client: TestClient, link: str, password: str = NEW):
    return client.post("/v1/auth/reset-password", json={"token": link, "password": password})


def _invite(client: TestClient, robert: str, email: str = "carol@example.com") -> str:
    made = client.post("/v1/users", json={"email": email, "role": "viewer"}, headers=_bearer(robert))
    assert made.status_code == 201, made.text
    return made.json()["invite_urls"][0].split("token=", 1)[1]


def _join(client: TestClient, link: str):
    return client.post("/v1/auth/join", json={"token": link, "password": NEW})


def _operator_key(client: TestClient, tenant_id: str) -> str:
    """The account's key the operator's admin API makes (deploy-api.sh's, for the CLI)."""
    made = client.post(f"/v1/admin/tenants/{tenant_id}/keys", json={"name": "default"}, headers=_bearer(ADMIN_KEY))
    assert made.status_code == 200, made.text
    return made.json()["api_key"]


def _operator_resets(tmp_path, control, monkeypatch, email: str, typed=(NEW, NEW)) -> int:  # noqa: F811
    typing = iter(typed)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(typing))
    return cli.main(["reset-password", "--env", str(_env_file(tmp_path, control)), "--email", email])


def test_a_password_reset_revokes_the_keys_that_user_made(install):
    client, tenant_id, robert = install
    cli_key = _new_key(client, robert)
    made_with_it = _new_key(client, cli_key)
    anas = _new_key(client, _login(client, ANA, PASSWORD))
    scheduler, operator = scheduler_key(tenant_id), _operator_key(client, tenant_id)

    reset = _reset(client, _reset_link(client, ROBERT))
    assert (reset.status_code, reset.json()) == (200, {"api_keys_revoked": 2}), reset.text
    assert _works(client, cli_key) == 401 and _works(client, made_with_it) == 401
    assert _works(client, robert) == 401, "and every session, as before"
    assert _works(client, anas) == 200, "another user's keys"
    assert _works(client, scheduler) == 200, "the install's processing: the scheduler's key is nobody's"
    assert _works(client, operator) == 200, "nor the operator's"
    assert _works(client, _new_key(client, _login(client, ROBERT, NEW))) == 200, "signed in again, they make new ones"


def test_a_password_reset_with_no_keys_says_none_were_revoked(install):
    client, _, _ = install
    reset = _reset(client, _reset_link(client, ANA))
    assert (reset.status_code, reset.json()) == (200, {"api_keys_revoked": 0}), reset.text


def test_using_one_reset_link_voids_the_others(install):
    client, _, _ = install
    first, second, anas = _reset_link(client, ROBERT), _reset_link(client, ROBERT), _reset_link(client, ANA)
    assert _reset(client, second).status_code == 200
    assert _reset(client, first, NEW + "!").status_code == 400, "their other link"
    assert _login(client, ROBERT, NEW)
    assert _reset(client, anas).status_code == 200, "someone else's link still works"


def test_a_password_reset_voids_an_unused_invite(install):
    client, _, robert = install
    invite = _invite(client, robert)
    assert _reset(client, _reset_link(client, "carol@example.com")).status_code == 200
    joined = _join(client, invite)
    assert joined.status_code == 400 and joined.json()["error"]["code"] == "invite_not_valid", joined.text
    assert _login(client, "carol@example.com", NEW), "the password from the reset"


def test_two_links_used_at_once_set_one_password(install):
    """One wins; the other finds itself void, and is refused (not an error)."""
    client, _, _ = install
    links = [_reset_link(client, ROBERT), _reset_link(client, ROBERT)]
    hashes = [bcrypt.hashpw(f"{NEW} {i}".encode(), bcrypt.gensalt(rounds=4)).decode() for i in range(2)]
    start = threading.Barrier(2)
    outcomes: list[object] = []

    def use(link: str, password_hash: str) -> None:
        with Session(get_control_engine()) as session:
            start.wait()
            try:
                consumed = PasswordResetTokenRepository(session).consume_valid_and_update_password(link, password_hash)
                outcomes.append("refused" if consumed is None else "set")
            except Exception as exc:  # a deadlock would be one
                outcomes.append(exc)

    threads = [threading.Thread(target=use, args=pair) for pair in zip(links, hashes, strict=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(map(str, outcomes)) == ["refused", "set"], outcomes


def test_the_command_line_revokes_their_keys_and_voids_their_links_too(install, control, tmp_path, monkeypatch, capsys):  # noqa: F811
    client, tenant_id, robert = install
    cli_key = _new_key(client, robert)
    anas = _new_key(client, _login(client, ANA, PASSWORD))
    scheduler = scheduler_key(tenant_id)
    unused = _reset_link(client, ROBERT)

    assert _operator_resets(tmp_path, control, monkeypatch, ROBERT) == 0
    assert capsys.readouterr().out.strip() == (
        "✓ A new password for robert@example.com; they're signed out everywhere else, "
        "and the API key they made is revoked")
    assert _works(client, cli_key) == 401 and _works(client, robert) == 401
    assert _works(client, anas) == 200 and _works(client, scheduler) == 200
    assert _reset(client, unused, NEW + "!").status_code == 400, "their unused link"
    assert _login(client, ROBERT, NEW)


def test_the_command_line_says_how_many_keys(install, control, tmp_path, monkeypatch, capsys):  # noqa: F811
    client, _, robert = install
    _new_key(client, robert), _new_key(client, robert)
    assert _operator_resets(tmp_path, control, monkeypatch, ROBERT) == 0
    assert capsys.readouterr().out.strip().endswith(", and the 2 API keys they made are revoked")


def test_the_command_line_voids_an_unused_invite(install, control, tmp_path, monkeypatch, capsys):  # noqa: F811
    client, _, robert = install
    invite = _invite(client, robert)
    assert _operator_resets(tmp_path, control, monkeypatch, "carol@example.com") == 0
    assert capsys.readouterr().out.strip() == (
        "✓ A new password for carol@example.com; they're signed out everywhere else"), "no keys: nothing more said"
    assert _join(client, invite).status_code == 400
    assert _login(client, "carol@example.com", NEW)


def test_the_command_line_asks_again_for_a_password_that_will_not_do(install, control, tmp_path, monkeypatch, capsys):  # noqa: F811
    client, _, robert = install
    typed = ["too short", NEW, NEW + "!", NEW, NEW]
    assert _operator_resets(tmp_path, control, monkeypatch, ROBERT, typed) == 0
    said = capsys.readouterr().err.splitlines()
    assert said == ["At least 12 characters, at most 72.", "✗ Use 12 to 72 characters", "✗ The passwords don't match"]
    assert _login(client, ROBERT, NEW)

    # Three tries, then nothing changed: their sessions and old password stand.
    robert = _login(client, ROBERT, NEW)
    assert _operator_resets(tmp_path, control, monkeypatch, ROBERT, ["short"] * 3) == 1
    assert capsys.readouterr().err.splitlines()[1:] == ["✗ Use 12 to 72 characters"] * 3
    assert _works(client, robert) == 200 and _login(client, ROBERT, NEW)
