"""CLI config file, the saved admin key, and `user remove`.

A config file that can't be read is never taken for the defaults (the next
save would wipe its keys and library roots); it's written 0600, all at once.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.client.cli.config import CLIConfig, ConfigError, load_config, save_config
from src.client.cli.main import app

pytestmark = pytest.mark.fast

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".lumiverb" / "config.json"
    monkeypatch.setattr("src.client.cli.config._config_path", lambda: path)
    monkeypatch.delenv("LUMIVERB_ADMIN_KEY", raising=False)
    return path


# ---------------------------------------------------------------------------
# config file
# ---------------------------------------------------------------------------


def test_no_file_is_the_defaults(config_file: Path) -> None:
    assert load_config() == CLIConfig()


@pytest.mark.parametrize("text", ["{not json", '{"max_concurrency": "many"}'])
def test_a_file_that_cant_be_parsed_raises(config_file: Path, text: str) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_text(text)

    with pytest.raises(ConfigError, match=str(config_file)):
        load_config()


def test_config_set_on_a_broken_file_changes_nothing(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True)
    broken = '{"api_key": "lv_keep", "root_map": {"/srv": "/Volumes/x"},'
    config_file.write_text(broken)

    result = runner.invoke(app, ["config", "set", "--api-url", "http://elsewhere"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert config_file.read_text() == broken


def test_main_says_what_is_wrong_and_exits_1(config_file: Path, capsys) -> None:
    from src.client.cli.main import main

    config_file.parent.mkdir(parents=True)
    config_file.write_text("{")
    with patch("sys.argv", ["lumiverb", "config", "show"]), pytest.raises(SystemExit) as exit_:
        main()

    assert exit_.value.code == 1
    assert f"Can't read {config_file}" in capsys.readouterr().err


def test_save_is_owner_only(config_file: Path) -> None:
    save_config(CLIConfig(api_key="lv_secret"))

    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert json.loads(config_file.read_text())["api_key"] == "lv_secret"


def test_save_replaces_an_existing_file_owner_only(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")
    os.chmod(config_file, 0o644)

    save_config(CLIConfig(api_key="lv_new"))

    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert json.loads(config_file.read_text())["api_key"] == "lv_new"
    assert os.listdir(config_file.parent) == ["config.json"]


def test_a_save_that_fails_leaves_the_old_file(config_file: Path) -> None:
    save_config(CLIConfig(api_key="lv_old"))

    with patch("src.client.cli.config.os.replace", side_effect=OSError("disk full")), pytest.raises(OSError):
        save_config(CLIConfig(api_key="lv_new"))

    assert json.loads(config_file.read_text())["api_key"] == "lv_old"
    assert os.listdir(config_file.parent) == ["config.json"]


def test_a_symlinked_config_stays_a_symlink(config_file: Path, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "lumiverb.json"
    real.parent.mkdir()
    real.write_text("{}")
    config_file.parent.mkdir(parents=True)
    config_file.symlink_to(real)

    save_config(CLIConfig(api_key="lv_linked"))

    assert config_file.is_symlink()
    assert json.loads(real.read_text())["api_key"] == "lv_linked"


def test_load_makes_a_readable_config_owner_only(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_text('{"api_key": "lv_secret"}')
    os.chmod(config_file, 0o644)

    assert load_config().api_key == "lv_secret"
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_save_makes_the_folder_owner_only(config_file: Path) -> None:
    save_config(CLIConfig())

    assert stat.S_IMODE(config_file.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("flag", ["--api-key", "--admin-key"])
def test_config_set_reads_a_key_from_stdin(config_file: Path, flag: str) -> None:
    result = runner.invoke(app, ["config", "set", flag, "-"], input="lv_from_stdin\n")

    assert result.exit_code == 0, result.output
    field = flag.removeprefix("--").replace("-", "_")
    assert json.loads(config_file.read_text())[field] == "lv_from_stdin"


def test_config_set_still_takes_a_key_in_argv(config_file: Path) -> None:
    assert runner.invoke(app, ["config", "set", "--api-key", "lv_argv"]).exit_code == 0
    assert json.loads(config_file.read_text())["api_key"] == "lv_argv"


@pytest.mark.parametrize(
    ("args", "stdin"),
    [(["--api-key", "-"], ""), (["--api-key", "-", "--admin-key", "-"], "lv_x\n")],
)
def test_config_set_refuses_a_bad_stdin_key(config_file: Path, args: list[str], stdin: str) -> None:
    result = runner.invoke(app, ["config", "set", *args], input=stdin)

    assert result.exit_code == 1
    assert not config_file.exists()


@pytest.mark.parametrize(
    ("url", "warned"),
    [
        ("http://192.168.1.5:8000", True),
        ("http://lumiverb.example:8000", True),
        ("https://lumiverb.example", False),
        ("http://localhost:8000", False),
        ("http://127.0.0.1:8000", False),
        ("http://[::1]:8000", False),
    ],
)
def test_plain_http_off_this_machine_warns_once(monkeypatch, capsys, url: str, warned: bool) -> None:
    from src.client.cli import client as client_mod

    monkeypatch.setattr(client_mod, "_warned_plain_http", False)
    client_mod.LumiverbClient(base_url=url, token="t").close()
    client_mod.LumiverbClient(base_url=url, token="t").close()

    err = capsys.readouterr().err
    assert err.count("plain http") == (1 if warned else 0)


# ---------------------------------------------------------------------------
# admin key
# ---------------------------------------------------------------------------


def _admin_keys_list(*args: str):
    client = MagicMock()
    client.get.return_value.json.return_value = []
    with patch("src.client.cli.main.LumiverbClient", return_value=client) as ctor:
        result = runner.invoke(app, ["admin", "keys", "list", "--tenant-id", "ten_1", *args])
    return result, ctor


def test_the_saved_admin_key_is_used(config_file: Path) -> None:
    assert runner.invoke(app, ["config", "set", "--admin-key", "saved-admin"]).exit_code == 0

    result, ctor = _admin_keys_list()

    assert result.exit_code == 0, result.output
    ctor.assert_called_once_with(api_key_override="saved-admin")


def test_the_flag_and_env_come_before_the_saved_admin_key(config_file: Path, monkeypatch) -> None:
    save_config(CLIConfig(admin_key="saved-admin"))

    _, ctor = _admin_keys_list("--admin-key", "flag-admin")
    ctor.assert_called_once_with(api_key_override="flag-admin")

    monkeypatch.setenv("LUMIVERB_ADMIN_KEY", "env-admin")
    _, ctor = _admin_keys_list()
    ctor.assert_called_once_with(api_key_override="env-admin")


# ---------------------------------------------------------------------------
# user remove
# ---------------------------------------------------------------------------


def _answer(status: int, body: dict) -> MagicMock:
    r = MagicMock(status_code=status)
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def _remove(delete_answer: MagicMock, *args: str, input: str | None = None):
    client = MagicMock()
    client.get.return_value.json.return_value = [{"user_id": "usr_1", "email": "a@b.co", "role": "admin"}]
    client.raw.return_value = delete_answer
    with patch("src.client.cli.commands.users.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["user", "remove", "--email", "a@b.co", *args], input=input)
    return result, client


def test_user_remove_asks_first() -> None:
    result, client = _remove(_answer(204, {}), input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in result.output
    client.raw.assert_not_called()


def test_user_remove_yes_asks_nothing() -> None:
    result, client = _remove(_answer(204, {}), "--yes")

    assert result.exit_code == 0, result.output
    assert "User removed." in result.output
    client.raw.assert_called_once_with("DELETE", "/v1/users/usr_1")


def test_user_remove_says_it_cant_remove_the_last_admin() -> None:
    result, _ = _remove(_answer(409, {"error": {"code": "last_admin", "message": "Cannot remove the last admin"}}),
                        "--yes")

    assert result.exit_code == 1
    assert "cannot remove the last admin" in result.output


def test_user_remove_says_why_a_400_failed() -> None:
    result, _ = _remove(_answer(400, {"error": {"code": "bad_request", "message": "Cannot delete your own account", "details": {}}}), "--yes")

    assert result.exit_code == 1
    assert "Cannot delete your own account" in result.output
