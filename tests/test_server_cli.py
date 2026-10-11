"""lumiverb-server's install is always named, never assumed
(src/server/lifecycle/cli.py): on the brain /etc/lumiverb/env is prod's, so a
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
