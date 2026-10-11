"""lumiverb-server run restarts into a new install after `brew upgrade`
(src/server/lifecycle/cli.py): Homebrew moves the opt link launchd starts it
through to the new keg, and deletes the old one right after. run sees the
link resolve elsewhere, stops everything, and execs the same command, which
is now the new install."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from src.server.lifecycle import cli

pytestmark = pytest.mark.fast


def _keg(cellar: Path, version: str) -> Path:
    command = cellar / version / "bin" / "lumiverb-server"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    return command


def _point(opt: Path, keg_bin: Path) -> None:
    """What `brew link` does: the opt link swapped to another keg at once."""
    swap = opt.with_name(opt.name + ".new")
    swap.symlink_to(keg_bin.parent.parent)
    os.replace(swap, opt)


@pytest.fixture
def homebrew(tmp_path: Path) -> dict[str, Path]:
    cellar, opt = tmp_path / "Cellar" / "lumiverb", tmp_path / "opt" / "lumiverb"
    opt.parent.mkdir(parents=True)
    old, new = _keg(cellar, "0.1.0"), _keg(cellar, "0.1.1")
    _point(opt, old)
    return {"command": opt / "bin" / "lumiverb-server", "old": old, "new": new, "opt": opt}


def test_an_install_is_where_the_opt_link_points(homebrew):
    before = cli.install_identity(homebrew["command"])
    assert before is not None and before[0] == str(homebrew["old"].resolve())
    _point(homebrew["opt"], homebrew["new"])
    assert cli.install_identity(homebrew["command"])[0] == str(homebrew["new"].resolve())


def test_a_reinstall_of_the_same_version_is_another_install(homebrew):
    before = cli.install_identity(homebrew["command"])
    homebrew["old"].unlink()
    homebrew["old"].write_text("#!/bin/sh\n")  # the same path, a new file
    assert cli.install_identity(homebrew["command"]) != before


def test_mid_upgrade_with_the_link_gone_it_waits(homebrew):
    homebrew["opt"].unlink()
    assert cli.install_identity(homebrew["command"]) is None
    stop, upgraded = threading.Event(), threading.Event()
    watcher = threading.Thread(target=cli.watch_for_upgrade, args=(homebrew["command"], stop, upgraded),
                               kwargs={"every": 0.01})
    watcher.start()
    watcher.join(1)
    assert not watcher.is_alive() and not upgraded.is_set(), "nothing to compare with: it doesn't watch"


def test_an_upgrade_stops_it_once_the_link_points_elsewhere(homebrew):
    stop, upgraded = threading.Event(), threading.Event()
    watcher = threading.Thread(target=cli.watch_for_upgrade, args=(homebrew["command"], stop, upgraded),
                               kwargs={"every": 0.01})
    watcher.start()
    time.sleep(0.05)
    homebrew["opt"].unlink()  # mid-upgrade: not yet
    time.sleep(0.05)
    assert not upgraded.is_set()
    _point(homebrew["opt"], homebrew["new"])
    watcher.join(2)
    assert upgraded.is_set() and stop.is_set()


def test_run_stops_everything_then_starts_the_new_install(homebrew, tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text(f"CONTROL_PLANE_DATABASE_URL=postgresql://x\nDATA_DIR={tmp_path / 'data'}\n")
    stopped: list[bool] = []

    class Supervising:
        def __init__(self, children):
            pass

        def run(self, stop):
            stop.wait(10)
            stopped.append(stop.is_set())

    execs: list[tuple] = []
    monkeypatch.setattr(cli, "Supervisor", Supervising)
    monkeypatch.setattr(cli, "_then_migrated", lambda *a: None)
    monkeypatch.setattr(cli, "UPGRADE_CHECK_SEC", 0.01)
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: execs.append((path, argv)))
    monkeypatch.setattr(sys, "argv", [str(homebrew["command"]), "run", "--env", str(env_file)])
    threading.Timer(0.1, _point, args=(homebrew["opt"], homebrew["new"])).start()

    assert cli.main(["run", "--env", str(env_file)]) == 0
    assert stopped == [True], "everything stopped first"
    assert execs == [(homebrew["command"], [str(homebrew["command"]), "run", "--env", str(env_file)])]
