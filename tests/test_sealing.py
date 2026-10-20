"""Secrets Lumiverb keeps for other services are sealed with the install's
own key (src/server/sealing.py): a dump or backup alone reads none of them,
one sealed by another install's key is known as such (asked again, never
failing), and a value from before sealing is read until upkeep seals it.
Every install gets the key: init writes it, and a Mac's run adds it to an
env file from before (lifecycle/cli.py with_secrets_key); the scheduler and
the plug-in runner are handed it, nothing else is but the API."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.server import sealing
from src.server.sealing import SecretsKeyMissingError, is_sealed, opened, seal

pytestmark = pytest.mark.fast


def test_a_secret_sealed_is_opened_with_the_installs_key_and_holds_nothing_readable():
    sealed = seal("sk-live-1234")
    assert re.fullmatch(r"lv1:[0-9a-f]{8}:[A-Za-z0-9_=-]+", sealed), sealed
    assert "sk-live-1234" not in sealed and is_sealed(sealed)
    assert opened(sealed) == "sk-live-1234"
    assert seal("sk-live-1234") != sealed, "a new one each time (its IV)"
    assert seal(sealed) == sealed, "sealed once"
    assert (seal(""), opened(""), opened(None)) == ("", "", "")


def test_one_stored_before_sealing_is_read_as_it_is():
    assert not is_sealed("sk-plain") and opened("sk-plain") == "sk-plain"


def test_one_sealed_by_another_installs_key_or_changed_is_known_and_never_read(monkeypatch):
    sealed = seal("sk-1")
    monkeypatch.setattr(sealing, "_key", lambda: Fernet.generate_key().decode())
    assert opened(sealed) is None, "a backup restored elsewhere: asked again"
    monkeypatch.undo()
    prefix, kid, token = sealed.split(":", 2)
    assert opened(f"{prefix}:{kid}:{token[:-4]}AAAA") is None, "changed: not read"


def test_without_a_key_nothing_is_sealed_and_nothing_sealed_is_read(monkeypatch):
    sealed = seal("sk-1")
    monkeypatch.setattr(sealing, "_key", lambda: "")
    with pytest.raises(SecretsKeyMissingError, match="LUMIVERB_SECRETS_KEY"):
        seal("sk-2")
    assert opened(sealed) is None and seal("") == ""


def test_an_env_file_from_before_gets_a_key_once(tmp_path):
    from src.server.lifecycle.cli import with_secrets_key
    from src.server.lifecycle.envfile import read_env_file

    env_file = tmp_path / "env"
    env_file.write_text("DATA_DIR=/var/lumiverb\n")
    env, said = with_secrets_key(env_file, {"DATA_DIR": "/var/lumiverb"})
    key = read_env_file(env_file)["LUMIVERB_SECRETS_KEY"]
    assert env["LUMIVERB_SECRETS_KEY"] == key and Fernet(key.encode())
    assert said and "keep a copy of it with your backups" in said
    assert with_secrets_key(env_file, read_env_file(env_file)) == (read_env_file(env_file), None)
    assert env_file.read_text().count("LUMIVERB_SECRETS_KEY=") == 1


def test_a_new_install_has_one_and_only_the_api_scheduler_and_runner_are_handed_it(tmp_path):
    from src.server.lifecycle.cli import server_children
    from src.server.lifecycle.envfile import read_env_file
    from src.server.lifecycle.init import Choices, env_text
    from datetime import UTC, datetime

    text = env_text(Choices(data_dir=tmp_path / "var"), now=datetime.now(UTC), secret=lambda: "s")
    (tmp_path / "env").write_text(text)
    env = read_env_file(tmp_path / "env")
    assert Fernet(env["LUMIVERB_SECRETS_KEY"].encode())
    env["LUMIVERB_REFUSED_ROOTS"] = "[]"
    handed = {c.name: "LUMIVERB_SECRETS_KEY" in c.env for c in server_children(env, config_dir=Path("/etc"))}
    assert handed == {"api": True, "scheduler": True, "plugins": True}
