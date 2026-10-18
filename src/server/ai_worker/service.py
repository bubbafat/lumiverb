"""The AI worker as a Mac's service (ADR-022 phase 4): a launchd agent of
its own, since Homebrew's one service (`brew services`) is the whole
server, which a machine lending only its GPU doesn't run.

`lumiverb-server ai-worker --install` writes the agent and starts it: at
login, and again whenever it stops (an upgrade: the worker restarts into the
new install on its own, cli.py); `--uninstall` stops it and takes it away.
Its output goes to the install's data folder (ai-worker/worker.log). A
Linux machine runs it as a systemd unit instead (the README).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

LABEL = "io.lumiverb.ai-worker"


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def agent(command: Path, env_file: Path, data: Path, *, host: str, port: int, at_once: int) -> dict:
    """The launchd agent: the worker as `command` runs it (the opt link's, so an upgrade's is the one run)."""
    log = str(data / "ai-worker" / "worker.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(command), "ai-worker", "--env", str(env_file.absolute()), "--host", host,
                             "--port", str(port), "--at-once", str(at_once)],
        "RunAtLoad": True,
        "KeepAlive": True,
        # A worker that can't start (no key folder, a port taken) isn't started again at once, every second.
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


Run = Callable[..., subprocess.CompletedProcess]


def install(command: Path, env_file: Path, data: Path, *, host: str, port: int, at_once: int,
            home: Path | None = None, run: Run = subprocess.run) -> Path:
    """Write the agent (replacing one there) and start it. Returns its file. Raises RuntimeError off a Mac,
    or when launchctl refuses it."""
    if sys.platform != "darwin":
        raise RuntimeError("--install is a Mac's (a launchd agent); on Linux, run it as a systemd unit (the README)")
    path = agent_path(home)
    (data / "ai-worker").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if path.exists():
        run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, text=True)  # the one before
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(agent(command, env_file, data, host=host, port=port, at_once=at_once)))
    started = run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if started.returncode != 0:
        raise RuntimeError(f"launchctl wouldn't start it: {(started.stderr or started.stdout).strip()}")
    return path


def uninstall(*, home: Path | None = None, run: Run = subprocess.run) -> bool:
    """Stop the agent and take it away. True when there was one."""
    path = agent_path(home)
    if not path.exists():
        return False
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    path.unlink()
    return True
