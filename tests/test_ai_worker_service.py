"""The AI worker as a Mac's service (ADR-022 phase 4): its own launchd agent
(Homebrew's one service is the whole server), written and started by
`lumiverb-server ai-worker --install`, which prints what to add in the other
Lumiverb (its https:// address and certificate's fingerprint, or, for a
worker from before HTTPS, loudly, its http:// one and how to switch), taken
away by --uninstall, refused off a Mac; and the worker restarting into the
new install after `brew upgrade`, as the server does. launchctl is never
really run here."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.server.ai_worker import fingerprint, key_file, service
from src.server.lifecycle import cli

pytestmark = pytest.mark.fast
LUMIVERB = Path("/opt/homebrew/opt/lumiverb/bin/lumiverb-server")


class Launchctl:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.code if argv[1] == "bootstrap" else 0, "", "nope")


def test_the_agent_runs_the_worker_through_the_opt_link_at_login_and_again(tmp_path) -> None:
    got = service.agent(LUMIVERB, Path("/opt/homebrew/etc/lumiverb/env"), tmp_path, host="0.0.0.0", port=6366,
                        at_once=2)
    assert got["Label"] == "io.lumiverb.ai-worker"
    assert got["ProgramArguments"] == [str(LUMIVERB), "ai-worker", "--env", "/opt/homebrew/etc/lumiverb/env",
                                       "--host", "0.0.0.0", "--port", "6366", "--at-once", "2"]
    assert got["RunAtLoad"] and got["KeepAlive"] and got["ThrottleInterval"] >= 10
    assert got["StandardOutPath"] == got["StandardErrorPath"] == str(tmp_path / "ai-worker" / "worker.log")


def test_install_writes_and_starts_it_replacing_the_one_before_and_uninstall_takes_it_away(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    home, data = tmp_path / "home", tmp_path / "data"
    run = Launchctl()
    path = service.install(LUMIVERB, tmp_path / "env", data, host="0.0.0.0", port=6366, at_once=2, home=home, run=run)
    assert path == home / "Library" / "LaunchAgents" / "io.lumiverb.ai-worker.plist"
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][:2] == [str(LUMIVERB), "ai-worker"]
    assert [c[:2] for c in run.calls] == [["launchctl", "bootstrap"]]
    # Again: the one there is stopped first.
    run = Launchctl()
    service.install(LUMIVERB, tmp_path / "env", data, host="0.0.0.0", port=6399, at_once=1, home=home, run=run)
    assert [c[:2] for c in run.calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    assert "6399" in plistlib.loads(path.read_bytes())["ProgramArguments"]
    run = Launchctl()
    assert service.uninstall(home=home, run=run) is True and not path.exists()
    assert [c[:2] for c in run.calls] == [["launchctl", "bootout"]]
    assert service.uninstall(home=home, run=Launchctl()) is False


def test_install_says_when_launchctl_wont_and_is_a_macs_only(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="launchctl wouldn't start it"):
        service.install(LUMIVERB, tmp_path / "env", tmp_path, host="0.0.0.0", port=6366, at_once=2,
                        home=tmp_path, run=Launchctl(code=5))
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="systemd unit"):
        service.install(LUMIVERB, tmp_path / "env", tmp_path, host="0.0.0.0", port=6366, at_once=2,
                        home=tmp_path, run=Launchctl())


def test_the_command_installs_with_the_env_file_it_was_given(tmp_path, monkeypatch, capsys):
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    monkeypatch.delenv("DATA_DIR")  # the test process's own (tests/conftest.py) would win over the file's
    seen = {}

    def install(command, env_file, data, **kw):
        seen.update(command=command, env_file=env_file, data=data, **kw)
        return tmp_path / "agent.plist"

    with patch("src.server.ai_worker.service.install", install), patch.object(cli, "_launched_as", lambda: LUMIVERB):
        assert cli.main(["ai-worker", "--env", str(env), "--install", "--port", "6399"]) == 0
    assert seen == {"command": LUMIVERB, "env_file": env, "data": tmp_path, "host": "0.0.0.0", "port": 6399,
                    "at_once": 2, "https": False, "plain_http": False}
    out = capsys.readouterr().out
    # What to add in the other Lumiverb: its https:// address, its certificate's fingerprint, where its key is.
    assert "runs as a service" in out and "over HTTPS" in out and "--show-key" in out
    assert "https://" in out and ":6399/v1" in out and "http://" not in out
    assert fingerprint(tmp_path) is not None and f"certificate fingerprint {fingerprint(tmp_path)}" in out
    with patch("src.server.ai_worker.service.uninstall", lambda: True):
        assert cli.main(["ai-worker", "--env", str(env), "--uninstall"]) == 0
    assert "stopped and gone" in capsys.readouterr().out


def test_the_agent_keeps_the_way_it_was_installed_to_serve(tmp_path) -> None:
    """--https (a worker from before switching) and --plain-http go into the agent, so every start serves the same."""
    env = Path("/opt/homebrew/etc/lumiverb/env")
    args = service.agent(LUMIVERB, env, tmp_path, host="0.0.0.0", port=6366, at_once=2)["ProgramArguments"]
    assert "--https" not in args and "--plain-http" not in args
    assert service.agent(LUMIVERB, env, tmp_path, host="0.0.0.0", port=6366, at_once=2,
                         https=True)["ProgramArguments"][-1] == "--https"
    assert service.agent(LUMIVERB, env, tmp_path, host="0.0.0.0", port=6366, at_once=2,
                         plain_http=True)["ProgramArguments"][-1] == "--plain-http"


def test_installing_a_worker_from_before_says_loudly_it_serves_plain_http_and_how_to_switch(tmp_path, capsys, monkeypatch):
    """A worker set up before it served HTTPS (a key, no certificate) keeps plain HTTP, as the Lumiverb using it
    expects, until it's installed with --https: then it's what to add, with its fingerprint."""
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    monkeypatch.delenv("DATA_DIR")  # the test process's own (tests/conftest.py) would win over the file's
    key_file(tmp_path).parent.mkdir(parents=True)
    key_file(tmp_path).write_text("a-key-from-before\n")
    seen: list[dict] = []

    def install(command, env_file, data, **kw):
        seen.append(kw)
        return tmp_path / "agent.plist"

    with patch("src.server.ai_worker.service.install", install), patch.object(cli, "_launched_as", lambda: LUMIVERB):
        assert cli.main(["ai-worker", "--env", str(env), "--install"]) == 0
        out = capsys.readouterr().out
        assert "⚠ PLAIN HTTP" in out and "set up before the AI worker served HTTPS" in out and "--install --https" in out
        assert "a machine at http://" in out and "with the certificate fingerprint" not in out
        assert fingerprint(tmp_path) is None, "no certificate made: it isn't served"
        assert cli.main(["ai-worker", "--env", str(env), "--install", "--https"]) == 0
        out = capsys.readouterr().out
    assert [(s["https"], s["plain_http"]) for s in seen] == [(False, False), (True, False)]
    assert "over HTTPS" in out and f"certificate fingerprint {fingerprint(tmp_path)}" in out and "PLAIN" not in out
    assert key_file(tmp_path).read_text() == "a-key-from-before\n", "its key stays: only its address changes"


def test_the_worker_restarts_into_an_upgraded_install(tmp_path, monkeypatch):
    """As run does: brew upgrade moves the opt link, the worker stops serving and execs the new install."""
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    execs: list = []

    def upgrade_now(command, stop, upgraded, every=0):
        upgraded.set()
        stop.set()

    class Server:
        def __init__(self, config) -> None:
            self.should_exit = False

        def run(self) -> None:
            import time

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:  # until the stop reaches it
                if self.should_exit:
                    return
                time.sleep(0.01)
            raise AssertionError("the worker never stopped serving")

    monkeypatch.setattr(sys, "argv", ["lumiverb-server", "ai-worker", "--env", str(env)])
    # The worker puts its install's settings in the environment (DATA_DIR for the models folder):
    # these, restored after, so nothing else run here sees them.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", "postgresql://x@127.0.0.1/none")
    with patch.object(cli, "watch_for_upgrade", upgrade_now), patch.object(cli, "_launched_as", lambda: LUMIVERB), \
            patch("uvicorn.Server", Server), patch("uvicorn.Config", lambda app, **kw: None), \
            patch("src.shared.logging_config.configure_api_logging", lambda: None), \
            patch("os.execv", lambda path, argv: execs.append((path, argv))):
        assert cli.main(["ai-worker", "--env", str(env)]) == 0
    assert execs == [(LUMIVERB, [str(LUMIVERB), "ai-worker", "--env", str(env)])]
