"""Each part gets only the secrets it needs (src/shared/secrets_env.py).

The install's env file holds its keys and passwords. run gives each part
only its own (test_server_run.py); the scheduler, once its settings are
read, drops its own from its environment, so the tools it starts (ffmpeg,
exiftool, its face and transcript processes) have none; and the API's
playback tools get an environment without them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from src.shared import secrets_env

pytestmark = pytest.mark.fast

DB = "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/control_plane"
SECRETS = {"CONTROL_PLANE_DATABASE_URL": DB, "TENANT_DATABASE_URL_TEMPLATE": DB.replace("control_plane", "{tenant_id}"),
           "ADMIN_KEY": "a", "API_SECRET_KEY": "b", "JWT_SECRET": "c", "SMTP_PASSWORD": "d", "PGPASSWORD": "e",
           "SOME_SERVICE_TOKEN": "f", "ANOTHER_DB": "mysql://u:p@h/db"}
NOT_SECRETS = {"PATH": "/usr/bin", "HOME": "/Users/x", "DATA_DIR": "/data", "QUICKWIT_URL": "http://127.0.0.1:6466",
               "TOKENIZERS_PARALLELISM": "false", "LUMIVERB_ROOT_MAP": '{"/": "/"}', "KEYCHAIN_PATH": "/k",
               "APP_HOST": "http://localhost:6464", "PG_BIN": "/opt/homebrew/opt/postgresql@18/bin"}


@pytest.fixture
def environment():
    """The process's environment, put back whatever the test did to it."""
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


def test_secrets_are_told_by_name_or_by_a_password_in_a_url():
    assert [n for n, v in {**SECRETS, **NOT_SECRETS}.items() if secrets_env.is_secret(n, v)] == list(SECRETS)


def test_without_secrets_keeps_only_what_it_is_told_to():
    env = {**SECRETS, **NOT_SECRETS}
    assert secrets_env.without_secrets(env) == NOT_SECRETS
    assert secrets_env.without_secrets(env, keep=["CONTROL_PLANE_DATABASE_URL"]) == {
        **NOT_SECRETS, "CONTROL_PLANE_DATABASE_URL": DB}


def test_what_a_process_starts_after_forgetting_has_no_secrets(environment):
    environment.update(SECRETS)
    environment.update(NOT_SECRETS)
    dropped = secrets_env.forget()
    assert set(SECRETS) <= set(dropped)
    seen = json.loads(subprocess.run([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
                                     capture_output=True, text=True, check=True).stdout)
    assert not set(SECRETS) & set(seen)
    assert seen["DATA_DIR"] == "/data" and seen["TOKENIZERS_PARALLELISM"] == "false"


def test_the_scheduler_forgets_its_secrets_once_its_settings_are_read(environment, monkeypatch, tmp_path):
    from src.server.config import get_settings
    from src.server.scheduler import service

    class NotFurther:  # the next step: stops it there
        def acquire(self) -> bool:
            return False

    environment.update(SECRETS)
    monkeypatch.chdir(tmp_path)  # no .env of this checkout's
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(service, "configure_logging", lambda: None)
    monkeypatch.setattr("src.server.database.APPLICATION_NAME", "test")
    monkeypatch.setattr("src.server.scheduler.scans.ServiceLock", NotFurther)
    get_settings.cache_clear()
    try:
        assert service.main() == 1
        assert not set(SECRETS) & set(os.environ), "what it starts would have them"
        assert get_settings().control_plane_database_url == DB, "it read them first"
    finally:
        get_settings.cache_clear()


def test_the_apis_playback_tools_have_no_secrets(environment, monkeypatch, tmp_path):
    from src.server.api.routers import playback

    environment.update(SECRETS)
    given = []

    def run(cmd, **kwargs):
        given.append(kwargs.get("env"))
        raise subprocess.SubprocessError("not run")

    monkeypatch.setattr(playback.subprocess, "run", run)
    assert playback._duration(tmp_path / "x.mp4") is None
    assert given and given[0] is not None and not set(SECRETS) & set(given[0])
