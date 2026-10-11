"""Upkeep on a Mac: what a Linux install's systemd timers do (deploy-api.sh).

  lumiverb-upkeep.timer        POST /v1/upkeep?tenants=all, 2 min after boot, then every 5 min
  lumiverb-upkeep-daily.timer  POST /v1/upkeep/cleanup?tenants=all&dry_run=false at 03:00,
                               Persistent: one missed while off runs at the next boot

A Mac is often asleep at 03:00, so a cleanup missed then runs as soon as the
service is up again (start or wake): the last one is kept in a state file.
A first start, with none kept yet, waits for the first 03:00, as the
Persistent timer does with no stamp. Nothing is posted until the API answers
/health.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections.abc import Callable
from datetime import datetime, time, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

FIRST_AFTER = timedelta(minutes=2)
EVERY = timedelta(minutes=5)
CLEANUP_AT = time(3, 0)
UPKEEP_PATH = "/v1/upkeep?tenants=all"
CLEANUP_PATH = "/v1/upkeep/cleanup?tenants=all&dry_run=false"
# Each account's work is bounded (cursor-api.md, Upkeep API), but a run
# with many accounts can take a while.
POST_TIMEOUT_SEC = 600


def last_cleanup_time(now: datetime) -> datetime:
    """The latest 03:00 at or before `now`."""
    today = now.replace(hour=CLEANUP_AT.hour, minute=CLEANUP_AT.minute, second=0, microsecond=0)
    return today if now >= today else today - timedelta(days=1)


class Schedule:
    """When upkeep and cleanup are due. `now` is local wall-clock time, so
    a Mac waking from sleep finds what came due while it slept."""

    def __init__(self, started: datetime, state_file: Path) -> None:
        self.started = started
        self.state_file = state_file
        self.upkeep_at: datetime | None = None
        self.cleanup_at = self._load() or started  # none kept: the first 03:00 after it started

    def _load(self) -> datetime | None:
        try:
            return datetime.fromisoformat(json.loads(self.state_file.read_text())["cleanup_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def due(self, now: datetime) -> list[str]:
        """Which posts are due now: "upkeep", "cleanup" or both (upkeep first)."""
        due = []
        if now - self.started >= FIRST_AFTER and (self.upkeep_at is None or now - self.upkeep_at >= EVERY):
            due.append("upkeep")
        if self.cleanup_at < last_cleanup_time(now):
            due.append("cleanup")
        return due

    def done(self, what: str, now: datetime) -> None:
        if what == "upkeep":
            self.upkeep_at = now
        else:
            self.cleanup_at = now
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"cleanup_at": now.isoformat()}))


def post(base_url: str, path: str, admin_key: str) -> None:
    request = urllib.request.Request(
        base_url + path, method="POST", data=b"{}",
        headers={"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SEC) as response:
        response.read()


def healthy(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def run_upkeep(
    schedule: Schedule,
    stop: threading.Event,
    *,
    is_healthy: Callable[[], bool],
    send: Callable[[str], None],
    now: Callable[[], datetime] = datetime.now,
    poll_sec: float = 30.0,
) -> None:
    """Posts what's due until `stop`. A failed post is logged and tried
    again at its next turn, as a failed timer run is."""
    paths = {"upkeep": UPKEEP_PATH, "cleanup": CLEANUP_PATH}
    while not stop.is_set():
        due = schedule.due(now())
        if due and is_healthy():
            for what in due:
                try:
                    send(paths[what])
                    logger.info("upkeep: %s done", what)
                except Exception as exc:  # noqa: BLE001 — the timer's run failing, not the service
                    logger.warning("upkeep: %s failed: %s", what, exc)
                schedule.done(what, now())  # tried: next turn, as a failed timer run
        stop.wait(poll_sec)
