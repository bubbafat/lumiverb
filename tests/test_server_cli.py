"""lumiverb-server's install is always named, never assumed
(src/server/lifecycle/cli.py): on Linux /etc/lumiverb/env is the live install's, so a
dev checkout run with no --env must not migrate prod. And a file it can't
use is one clear line, not a traceback."""

from __future__ import annotations

import os

import pytest

from src.server.lifecycle import cli

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def no_install_in_the_environment(monkeypatch):
    monkeypatch.delenv("LUMIVERB_ENV_FILE", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)


def _err(capsys) -> str:
    return capsys.readouterr().err.strip()


def test_no_install_named_is_refused_not_guessed(capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "migrate", lambda url: called.append(url) or 0)
    assert cli.main(["migrate"]) == 1
    assert _err(capsys).startswith("✗ Say which install: --env FILE or $LUMIVERB_ENV_FILE")
    assert called == [], "nothing was migrated"


def test_run_with_no_install_named_is_refused_and_starts_nothing(capsys, monkeypatch):
    monkeypatch.setattr(cli, "Supervisor", lambda *a, **k: pytest.fail("nothing may start"))
    monkeypatch.setattr(cli, "migrate_when_ready", lambda *a, **k: pytest.fail("nothing may migrate"))
    assert cli.main(["run"]) == 1
    assert _err(capsys).startswith("✗ Say which install:")


def test_run_with_a_data_dir_that_isnt_a_full_path_is_refused_and_starts_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "Supervisor", lambda *a, **k: pytest.fail("nothing may start"))
    monkeypatch.setattr(cli, "migrate_when_ready", lambda *a, **k: pytest.fail("nothing may migrate"))
    env_file = tmp_path / "env"
    env_file.write_text("CONTROL_PLANE_DATABASE_URL=postgresql://x\nDATA_DIR=data\n")
    assert cli.main(["run", "--env", str(env_file)]) == 1
    assert _err(capsys) == "✗ DATA_DIR must be a full path, not data"


def test_the_environment_can_name_it(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "migrate", lambda url: called.append(url) or 0)
    env_file = tmp_path / "env"
    env_file.write_text("CONTROL_PLANE_DATABASE_URL=postgresql://from-the-file\n")
    monkeypatch.setenv("LUMIVERB_ENV_FILE", str(env_file))
    assert cli.main(["migrate"]) == 0
    monkeypatch.delenv("LUMIVERB_ENV_FILE")
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", "postgresql://from-the-environment")
    assert cli.main(["migrate"]) == 0
    assert called == ["postgresql://from-the-file", "postgresql://from-the-environment"]


@pytest.mark.parametrize("argv", [["--env", "{file}", "migrate"], ["migrate", "--env", "{file}"]],
                         ids=["before the command", "after it"])
def test_env_goes_before_or_after_the_command(argv, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "migrate", lambda url: called.append(url) or 0)
    env_file = tmp_path / "env"
    env_file.write_text("CONTROL_PLANE_DATABASE_URL=postgresql://from-the-file\n")
    assert cli.main([part.format(file=env_file) for part in argv]) == 0
    assert called == ["postgresql://from-the-file"]


def test_a_named_file_that_isnt_there_is_refused(tmp_path, capsys):
    assert cli.main(["--env", str(tmp_path / "nowhere"), "migrate"]) == 1
    assert _err(capsys) == f"✗ No env file at {tmp_path / 'nowhere'}"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_it_cant_read_is_one_line_not_a_traceback(tmp_path, capsys):
    env_file = tmp_path / "env"
    env_file.write_text("CONTROL_PLANE_DATABASE_URL=x\n")
    env_file.chmod(0)
    try:
        assert cli.main(["--env", str(env_file), "migrate"]) == 1
    finally:
        env_file.chmod(0o600)
    err = _err(capsys)
    assert err == f"✗ Can't read {env_file}: Permission denied", err


def test_a_file_without_the_control_plane_says_so(tmp_path, capsys):
    env_file = tmp_path / "env"
    env_file.write_text("API_PORT=8100\n")
    assert cli.main(["--env", str(env_file), "migrate"]) == 1
    assert _err(capsys) == f"✗ No CONTROL_PLANE_DATABASE_URL in {env_file}"


def test_a_line_it_cant_read_is_one_line_too(tmp_path, capsys):
    env_file = tmp_path / "env"
    env_file.write_text("not a setting\n")
    assert cli.main(["--env", str(env_file), "migrate"]) == 1
    assert _err(capsys).startswith(f"✗ {env_file}, line 1: not KEY=VALUE")


def test_setup_against_a_database_it_cant_reach_is_one_line(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "getpass", lambda _prompt: pytest.fail("nothing may be asked"))
    env_file = tmp_path / "env"
    env_file.write_text("CONTROL_PLANE_DATABASE_URL=postgresql+psycopg2://nobody:x@127.0.0.1:1/nothing\n"
                        "TENANT_DATABASE_URL_TEMPLATE=postgresql+psycopg2://nobody:x@127.0.0.1:1/{tenant_id}\n")
    # Restored afterwards: setup puts the file's settings in the environment.
    monkeypatch.delenv("TENANT_DATABASE_URL_TEMPLATE", raising=False)
    try:
        assert cli.main(["setup", "--env", str(env_file), "--email", "a@example.com"]) == 1
    finally:
        from src.server.config import get_settings
        from src.server.database import _engines
        get_settings.cache_clear()
        _engines.clear()
    assert _err(capsys).startswith("✗ Can't reach the control database: ")


@pytest.mark.parametrize("command", ["run", "setup"])
def test_an_env_file_missing_a_server_setting_says_which_before_anything_starts(command, tmp_path, capsys,
                                                                               monkeypatch):
    # The API and the scheduler would otherwise fail at their first use, over and over.
    monkeypatch.setattr(cli, "Supervisor", lambda *a, **k: pytest.fail("nothing may start"))
    monkeypatch.delenv("TENANT_DATABASE_URL_TEMPLATE", raising=False)
    env_file = tmp_path / "env"
    env_file.write_text(f"CONTROL_PLANE_DATABASE_URL=postgresql://x\nDATA_DIR={tmp_path}\n")
    assert cli.main([command, "--env", str(env_file)]) == 1
    assert _err(capsys) == f"✗ {env_file} needs TENANT_DATABASE_URL_TEMPLATE"


def test_migrate_needs_only_the_control_database(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "migrate", lambda url: called.append(url) or 0)
    monkeypatch.delenv("TENANT_DATABASE_URL_TEMPLATE", raising=False)
    env_file = tmp_path / "env"
    env_file.write_text("CONTROL_PLANE_DATABASE_URL=postgresql://x\n")
    assert cli.main(["migrate", "--env", str(env_file)]) == 0 and called == ["postgresql://x"]
