"""The plug-in runner (ADR-023 phase 1b): every plug-in the server admin
installed, each a process of its own, kept from taking the server down
(Robert, 2026-10-18: "We don't need enterprise security. We're just doing
a good enough job. I don't want them taking down the server").

`python -m src.server.plugin_runner`: on a Mac a child of `lumiverb-server
run`, beside the API and the scheduler; on Linux its own unit,
lumiverb-plugins.service. For each installed plug-in it:

- starts its manifest's command from its installed folder
  (DATA_DIR/plugins/<id>/program/), through plugin_launch.py (a lower
  priority, no file bigger than its disk_mb, no core files), with a clean
  environment: its own folders and its socket (LUMIVERB_PLUGIN_SOCKET,
  LUMIVERB_PLUGIN_DATA), the PATH, nothing of Lumiverb's settings, keys or
  database addresses;
- asks its health on its socket (GET /v1/health: ready, preparing with how
  far, or failing with why), giving each answer a time limit, and restarts
  one that stops answering;
- watches its processes' memory (more than its memory_mb: ended and started
  again) and its data folder's size (more than its disk_mb: ended and left
  stopped);
- starts one that exits again, waiting longer each time it stops soon after
  starting, and leaves it stopped, with why, when it keeps doing so;
- says each plug-in's output in the service's log a line at a time, named
  ("plugin hello | …"), long lines cut and floods dropped;
- writes what each one is doing (DATA_DIR/plugins/runner.json, every few
  seconds) for the API to show; a newer install of one is started afresh,
  an uninstalled one stopped.

Nothing here waits on a plug-in longer than a request's time limit.
"""

from __future__ import annotations

import collections
import contextlib
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from src.plugins.manifest import Manifest
from src.server.plugin_client import PluginAnswerError, ask, socket_path, sockets_dir
from src.server.plugin_install import install_mark, plugin_dir, program_dir
from src.shared.utils import utcnow

logger = logging.getLogger(__name__)

LAUNCH = Path(__file__).with_name("plugin_launch.py")
STATE_FILE = "runner.json"

STARTING, READY, PREPARING, FAILING, RESTARTING, STOPPED = (
    "starting", "ready", "preparing", "failing", "restarting", "stopped")
STATES = (STARTING, READY, PREPARING, FAILING, RESTARTING, STOPPED)

START_WITHIN_SEC = 60.0  # answers its health within this of starting, or it's ended
HEALTH_EVERY_SEC = 15.0
HEALTH_STARTING_EVERY_SEC = 0.5
HEALTH_TIMEOUT_SEC = 5.0
UNANSWERED = 3  # health asks missed in a row before it's restarted
MEMORY_EVERY_SEC = 5.0
DISK_EVERY_SEC = 60.0
STOP_WITHIN_SEC = 10.0  # after SIGTERM, before what's left is killed
STAYED_UP_SEC = 180.0  # up this long, a plug-in that exits had been working
RESTART_FIRST_SEC = 2.0
RESTART_MAX_SEC = 600.0
GIVE_UP_AFTER = 6  # stops soon after starting, in a row, before it's left stopped
LIST_EVERY_SEC = 10.0  # what's installed, read again
HEARTBEAT_SEC = 10.0  # the state file is written at least this often
LINE_MAX = 2000
LINES_PER_MINUTE = 600


@dataclass
class Plugin:
    """One installed plug-in as the runner keeps it."""

    plugin_id: str
    manifest: Manifest
    program: tuple[str, str]  # its version and which install it is: a new install changes it
    since: datetime
    state: str = STARTING
    why: str | None = None
    detail: str | None = None
    done: int | None = None
    total: int | None = None
    process: subprocess.Popen | None = None
    started_at: float = 0.0
    restart_at: float | None = None
    delay: float = 0.0
    soon: int = 0  # stops soon after starting, in a row
    restarts: int = 0
    misses: int = 0
    next_health: float = 0.0
    next_memory: float = 0.0
    next_disk: float = 0.0
    memory_mb: int | None = None
    ending: str | None = None  # why the runner is ending it, said once it's gone
    stay_stopped: bool = False  # ending it leaves it stopped
    kill_at: float | None = None
    output: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))

    def said(self) -> dict[str, Any]:
        return {"state": self.state, "version": self.manifest.version, "why": self.why, "detail": self.detail,
                "done": self.done, "total": self.total, "since": self.since.isoformat(),
                "memory_mb": self.memory_mb, "restarts": self.restarts}


def state_file(data: Path) -> Path:
    return data / "plugins" / STATE_FILE


def group_memory_mb() -> dict[int, int]:
    """Each process group's memory (resident), in MB: `ps`, on a Mac and on Linux alike."""
    out = subprocess.run(["ps", "-A", "-o", "pgid=,rss="], capture_output=True, text=True, timeout=10,
                         check=False).stdout
    kb: dict[int, int] = collections.defaultdict(int)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kb[int(parts[0])] += int(parts[1])
    return {group: total // 1024 for group, total in kb.items()}


def folder_mb(path: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total // (1024 * 1024)


def ask_health(sock: Path, timeout: float | None = None) -> dict[str, Any]:
    """Its answer to GET /v1/health. Raises PluginAnswerError."""
    return ask(sock, "GET", "/v1/health", timeout=HEALTH_TIMEOUT_SEC if timeout is None else timeout)


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


def _text(value: Any, most: int = 500) -> str | None:
    return value[:most] if isinstance(value, str) and value else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class Runner:
    def __init__(self, data: Path, *, installed: Callable[[], dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 memory: Callable[[], dict[int, int]] = group_memory_mb) -> None:
        """installed: what's installed now, {plugin_id: plugin_catalog.Installed} (the control database's)."""
        if installed is None:
            from src.server.plugin_catalog import installed
        self.data = data
        self.plugins: dict[str, Plugin] = {}
        self._installed = installed
        self._clock = clock
        self._memory = memory
        self._next_list = 0.0
        self._said: dict[str, Any] | None = None
        self._said_at = 0.0

    # --- one turn ---------------------------------------------------------------

    def turn(self) -> None:
        now = self._clock()
        if now >= self._next_list:
            self._next_list = now + LIST_EVERY_SEC
            self._sync(now)
        groups: dict[int, int] | None = None
        for p in list(self.plugins.values()):
            if p.process is not None and p.ending is None and now >= p.next_memory and p.process.poll() is None:
                if groups is None:
                    try:
                        groups = self._memory()
                    except (OSError, subprocess.SubprocessError) as exc:
                        logger.warning("plug-ins: can't see their memory (%s)", exc)
                        groups = {}
                self._check_memory(p, now, groups)
            self._watch(p, now)
        self._write(now)

    def run(self, stop: threading.Event, poll_sec: float = 0.5) -> None:
        try:
            while True:
                try:
                    self.turn()
                except Exception:  # noqa: BLE001 — the runner goes on; what went wrong is said
                    logger.exception("plug-ins: a turn failed")
                if stop.wait(poll_sec):
                    break
        finally:
            self.stop_all()

    # --- what's installed -------------------------------------------------------

    def _sync(self, now: float) -> None:
        try:
            installed = self._installed()
        except Exception as exc:  # noqa: BLE001 — the database away: what runs goes on
            logger.warning("plug-ins: can't read what's installed (%s); those running go on", exc)
            return
        for pid in list(self.plugins):
            if pid not in installed:
                logger.info("plug-in %s: uninstalled, stopping it", pid)
                self._stop_now(self.plugins.pop(pid))
        for pid, entry in installed.items():
            program = self._program(pid, entry.manifest)
            if program is None:
                continue
            p = self.plugins.get(pid)
            if p is not None and p.program == program:
                continue
            if p is not None:
                logger.info("plug-in %s: version %s installed, starting it afresh", pid, entry.manifest.version)
                self._stop_now(p)
            self.plugins[pid] = p = Plugin(pid, entry.manifest, program, utcnow())
            self._start(p, now)

    def _program(self, plugin_id: str, manifest: Manifest) -> tuple[str, str] | None:
        """Its version and which install it is; None while its folder isn't there (mid-install or
        mid-uninstall: seen at the next look)."""
        program = program_dir(self.data, plugin_id)
        if not program.is_dir():
            return None
        return manifest.version, install_mark(program) or ""

    # --- starting and stopping ----------------------------------------------------

    def _environment(self, p: Plugin) -> dict[str, str]:
        home = plugin_dir(self.data, p.plugin_id)
        env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
               "HOME": str(home / "data"), "TMPDIR": str(home / "tmp"), "PYTHONUNBUFFERED": "1",
               "LUMIVERB_PLUGIN_ID": p.plugin_id, "LUMIVERB_PLUGIN_PROTOCOL": str(p.manifest.protocol),
               "LUMIVERB_PLUGIN_SOCKET": str(socket_path(self.data, p.plugin_id)),
               "LUMIVERB_PLUGIN_DATA": str(home / "data")}
        for name in ("LANG", "LC_ALL", "TZ"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        return env

    def _start(self, p: Plugin, now: float) -> None:
        home, program = plugin_dir(self.data, p.plugin_id), program_dir(self.data, p.plugin_id)
        p.restart_at = p.ending = p.kill_at = p.memory_mb = None
        p.stay_stopped = False
        p.misses = 0
        try:
            sockets_dir(self.data).mkdir(parents=True, exist_ok=True)
            os.chmod(sockets_dir(self.data), 0o700)
            sock = socket_path(self.data, p.plugin_id)
        except OSError as exc:
            self._left_stopped(p, f"Its socket can't be made: {exc.strerror or exc}")
            return
        env = self._environment(p)
        command = p.manifest.run[0]
        found = str(program / command) if "/" in command else shutil.which(command, path=env["PATH"])
        if found is None:
            self._left_stopped(p, f"Its program, {command}, isn't on this machine")
            return
        try:
            (home / "data").mkdir(parents=True, exist_ok=True)
            os.chmod(home / "data", 0o700)
            if (home / "tmp").exists():
                shutil.rmtree(home / "tmp", ignore_errors=True)
            (home / "tmp").mkdir(mode=0o700)
            sock.unlink(missing_ok=True)
            largest = p.manifest.disk_mb * 1024 * 1024
            p.process = subprocess.Popen(
                [sys.executable, "-I", str(LAUNCH), str(largest), "--", found, *p.manifest.run[1:]],
                cwd=program, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            self._left_stopped(p, f"It couldn't be started: {exc.strerror or exc}")
            return
        threading.Thread(target=self._relay, args=(p, p.process.stdout), name=f"plugin-{p.plugin_id}",
                         daemon=True).start()
        p.state, p.why, p.detail, p.done, p.total = STARTING, None, None, None, None
        p.started_at, p.since = now, utcnow()
        p.next_health = now + HEALTH_STARTING_EVERY_SEC
        p.next_memory, p.next_disk = now + MEMORY_EVERY_SEC, now + DISK_EVERY_SEC
        logger.info("plug-in %s %s started (pid %d)", p.plugin_id, p.manifest.version, p.process.pid)

    def _left_stopped(self, p: Plugin, why: str) -> None:
        p.state, p.why, p.since, p.process = STOPPED, why, utcnow(), None
        p.restart_at = None
        logger.warning("plug-in %s: left stopped: %s", p.plugin_id, why)

    def _end(self, p: Plugin, why: str, now: float, *, stay_stopped: bool = False) -> None:
        """Ask it to stop (SIGTERM to its processes), killed once STOP_WITHIN_SEC is up: what then becomes of
        it is said when it's gone (_gone)."""
        if p.process is None or p.ending is not None:
            return
        p.ending, p.stay_stopped, p.kill_at = why, stay_stopped, now + STOP_WITHIN_SEC
        logger.warning("plug-in %s: ending it: %s", p.plugin_id, why)
        _signal_group(p.process, signal.SIGTERM)

    def _stop_now(self, p: Plugin) -> None:
        if p.process is None:
            return
        _signal_group(p.process, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.process.wait(timeout=STOP_WITHIN_SEC)
        _signal_group(p.process, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.process.wait(timeout=5)
        p.process = None

    def stop_all(self) -> None:
        """Every plug-in at once: SIGTERM, then what's left is killed once STOP_WITHIN_SEC is up."""
        running = [p for p in self.plugins.values() if p.process is not None]
        for p in running:
            _signal_group(p.process, signal.SIGTERM)
        until = time.monotonic() + STOP_WITHIN_SEC
        for p in running:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.process.wait(timeout=max(0.0, until - time.monotonic()))
            _signal_group(p.process, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.process.wait(timeout=5)
            p.process = None
            p.state, p.why = STOPPED, "The plug-in runner stopped"
        self._write(self._clock(), force=True)

    # --- watching ---------------------------------------------------------------

    def _watch(self, p: Plugin, now: float) -> None:
        if p.process is None:
            if p.restart_at is not None and now >= p.restart_at:
                self._start(p, now)
            return
        code = p.process.poll()
        if code is not None:
            self._gone(p, now, code)
            return
        if p.kill_at is not None:
            if now >= p.kill_at:
                _signal_group(p.process, signal.SIGKILL)
            return
        if p.state == STARTING and now - p.started_at > START_WITHIN_SEC:
            self._end(p, f"It didn't answer within {START_WITHIN_SEC:.0f} s of starting", now)
            return
        if now >= p.next_health:
            self._check_health(p, now)
        if p.ending is None and now >= p.next_disk:
            p.next_disk = now + DISK_EVERY_SEC
            used = folder_mb(plugin_dir(self.data, p.plugin_id) / "data")
            if used > p.manifest.disk_mb:
                self._end(p, f"Its data folder holds {used:,} MB, more than its {p.manifest.disk_mb:,} MB: left "
                             "stopped", now, stay_stopped=True)

    def _check_health(self, p: Plugin, now: float) -> None:
        p.next_health = now + (HEALTH_STARTING_EVERY_SEC if p.state == STARTING else HEALTH_EVERY_SEC)
        try:
            answer = ask_health(socket_path(self.data, p.plugin_id))
            state = answer.get("state")
            if state not in ("ready", "preparing", "failed"):
                raise ValueError("its health isn't ready, preparing or failed")
        except (PluginAnswerError, ValueError) as exc:
            if p.state == STARTING:
                return  # not listening yet
            p.misses += 1
            if p.misses >= UNANSWERED:
                self._end(p, f"It stopped answering ({exc})", now)
            return
        p.misses = 0
        if state == "ready":
            p.state, p.why, p.detail, p.done, p.total = READY, None, None, None, None
        elif state == "preparing":
            p.state, p.why = PREPARING, None
            p.detail, p.done, p.total = _text(answer.get("detail")), _count(answer.get("done")), _count(answer.get("total"))
        else:
            p.state, p.why = FAILING, _text(answer.get("error")) or "It says it can't work"

    def _check_memory(self, p: Plugin, now: float, groups: dict[int, int]) -> None:
        p.next_memory = now + MEMORY_EVERY_SEC
        p.memory_mb = groups.get(p.process.pid)
        if p.memory_mb is not None and p.memory_mb > p.manifest.memory_mb:
            self._end(p, f"It used {p.memory_mb:,} MB of memory, more than its {p.manifest.memory_mb:,} MB", now)

    def _gone(self, p: Plugin, now: float, code: int) -> None:
        _signal_group(p.process, signal.SIGKILL)  # anything it left behind
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.process.wait(timeout=5)
        p.process = None
        uptime = now - p.started_at
        why = p.ending or (f"It was ended by signal {-code}" if code < 0 else f"It stopped (exit code {code})")
        if p.ending is None and p.output:
            why += f": {p.output[-1][:300]}"
        if p.stay_stopped:
            self._left_stopped(p, why)
            return
        if uptime >= STAYED_UP_SEC:
            p.soon, p.delay = 0, RESTART_FIRST_SEC
        else:
            p.soon += 1
            p.delay = min(max(p.delay * 2, RESTART_FIRST_SEC), RESTART_MAX_SEC)
        if p.soon >= GIVE_UP_AFTER:
            self._left_stopped(p, f"{why}. It stopped {p.soon} times in a row soon after starting, so it's left "
                                  "stopped: install it again to try again")
            return
        p.state, p.why, p.since, p.restarts = RESTARTING, why, utcnow(), p.restarts + 1
        p.restart_at = now + p.delay
        p.ending = None
        logger.warning("plug-in %s: %s; starting it again in %.0f s", p.plugin_id, why, p.delay)

    # --- its output, and what's said for the API ------------------------------------

    def _relay(self, p: Plugin, stream: IO[bytes]) -> None:
        minute, said, dropped = time.monotonic(), 0, 0
        try:
            while raw := stream.readline(LINE_MAX + 1):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                if time.monotonic() - minute >= 60:
                    if dropped:
                        logger.warning("plugin %s | (%d more lines in a minute, not said)", p.plugin_id, dropped)
                    minute, said, dropped = time.monotonic(), 0, 0
                said += 1
                if said > LINES_PER_MINUTE:
                    dropped += 1
                    continue
                p.output.append(line)
                logger.info("plugin %s | %s", p.plugin_id, line[:LINE_MAX])
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def _write(self, now: float, *, force: bool = False) -> None:
        said = {pid: p.said() for pid, p in sorted(self.plugins.items())}
        if not force and said == self._said and now - self._said_at < HEARTBEAT_SEC:
            return
        path = state_file(self.data)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(f".{STATE_FILE}.{os.getpid()}")
            part.write_text(json.dumps({"at": utcnow().isoformat(), "pid": os.getpid(), "plugins": said}))
            os.replace(part, path)
        except OSError as exc:
            logger.warning("plug-ins: can't write %s (%s)", path, exc)
            return
        self._said, self._said_at = said, now


_DOING = {STARTING: "it's starting", PREPARING: "it's preparing its data", RESTARTING: "it's restarting",
          FAILING: "it isn't answering", STOPPED: "it stopped"}


def not_ready(said: dict[str, Any] | None, plugin_id: str) -> str | None:
    """Why a plug-in can't be asked now, in words (None: it's running), from what the runner said (read_state)."""
    if said is None:
        return "the plug-in runner isn't running"
    mine = said.get(plugin_id)
    if not isinstance(mine, dict):
        return "it isn't started yet"
    if mine.get("state") == READY:
        return None
    doing = _DOING.get(mine.get("state"), "it isn't running")
    return f"{doing} ({mine['why']})" if mine.get("why") else doing


def read_state(data: Path, *, fresh_within: float = 60.0) -> dict[str, Any] | None:
    """What the runner last said, {plugin_id: {state, version, why, …}}; None when it hasn't said anything
    within fresh_within seconds (it isn't running)."""
    try:
        said = json.loads(state_file(data).read_text())
        at = datetime.fromisoformat(said["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (utcnow() - at).total_seconds() > fresh_within:
        return None
    plugins = said.get("plugins")
    return plugins if isinstance(plugins, dict) else None


def main() -> int:
    """lumiverb-plugins: run every installed plug-in until stopped (SIGTERM)."""
    from src.server.config import get_settings
    from src.server.database import set_application_name
    from src.shared import secrets_env
    from src.shared.stop_signals import set_on_signals

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    data = Path(get_settings().data_dir)
    if not data.is_absolute():
        logger.error("plug-ins: not starting: DATA_DIR must be a full path, not %s", data)
        return 1
    set_application_name("lumiverb-plugins")
    # Its settings read (and kept), its secrets leave the environment: nothing it starts has them.
    secrets_env.forget()
    (data / "plugins").mkdir(parents=True, exist_ok=True)
    lock = open(data / "plugins" / "runner.lock", "w")  # noqa: SIM115 — held for the process's life
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("plug-ins: another plug-in runner is running on this machine")
        return 1
    stop = threading.Event()
    set_on_signals(stop)  # never set from the handler itself: it could deadlock the main thread's wait
    logger.info("plug-ins: the runner started")
    Runner(data).run(stop)
    logger.info("plug-ins: the runner stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
