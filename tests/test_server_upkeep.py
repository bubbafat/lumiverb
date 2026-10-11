"""Upkeep on a Mac (src/server/lifecycle/upkeep.py): what a Linux install's
systemd timers post, on their schedule, with a cleanup missed while asleep
run on waking, and nothing posted before the API is healthy."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.server.lifecycle.upkeep import (
    CLEANUP_PATH,
    UPKEEP_PATH,
    Schedule,
    last_cleanup_time,
    run_upkeep,
)

pytestmark = pytest.mark.fast

START = datetime(2026, 10, 10, 15, 0)


def _schedule(tmp_path: Path, started: datetime = START) -> Schedule:
    return Schedule(started, tmp_path / "upkeep-state.json")


def test_the_latest_cleanup_time():
    assert last_cleanup_time(datetime(2026, 10, 10, 15, 0)) == datetime(2026, 10, 10, 3, 0)
    assert last_cleanup_time(datetime(2026, 10, 10, 3, 0)) == datetime(2026, 10, 10, 3, 0)
    assert last_cleanup_time(datetime(2026, 10, 10, 2, 59)) == datetime(2026, 10, 9, 3, 0)


def test_upkeep_two_minutes_after_start_then_every_five(tmp_path):
    s = _schedule(tmp_path)
    assert s.due(START + timedelta(minutes=1)) == []
    assert s.due(START + timedelta(minutes=2)) == ["upkeep"]
    s.done("upkeep", START + timedelta(minutes=2))
    assert s.due(START + timedelta(minutes=6)) == []
    assert s.due(START + timedelta(minutes=7)) == ["upkeep"]


def test_a_first_start_waits_for_the_first_three_oclock_then_each_day(tmp_path):
    # As the Persistent timer with no stamp yet: no cleanup the moment it's installed.
    s = _schedule(tmp_path)
    s.done("upkeep", START)
    assert "cleanup" not in s.due(START)
    assert "cleanup" not in s.due(datetime(2026, 10, 11, 2, 59))
    assert "cleanup" in s.due(datetime(2026, 10, 11, 3, 0))
    s.done("cleanup", datetime(2026, 10, 11, 3, 0))
    assert "cleanup" not in s.due(datetime(2026, 10, 12, 2, 59))
    assert "cleanup" in s.due(datetime(2026, 10, 12, 3, 0))


def test_a_cleanup_missed_while_asleep_runs_on_waking(tmp_path):
    s = _schedule(tmp_path)
    s.done("cleanup", datetime(2026, 10, 10, 3, 0))
    s.done("upkeep", datetime(2026, 10, 11, 1, 55))
    # Asleep from 02:00 to 09:00: 03:00 went by.
    assert s.due(datetime(2026, 10, 11, 9, 0)) == ["upkeep", "cleanup"]


def test_the_last_cleanup_is_kept_across_restarts(tmp_path):
    # A missed one is still due after a restart: kept, not taken for a first start.
    first = _schedule(tmp_path)
    first.done("cleanup", datetime(2026, 10, 9, 3, 0))  # then asleep through the 10th's
    again = _schedule(tmp_path, START + timedelta(hours=1))
    assert "cleanup" in again.due(START + timedelta(hours=1))
    again.done("cleanup", START + timedelta(hours=1))
    assert "cleanup" not in _schedule(tmp_path, START + timedelta(hours=2)).due(START + timedelta(hours=2))


def _drive(schedule, *, healthy, send, clock):
    """run_upkeep with a clock that moves a minute per turn, for 12 turns."""
    stop = threading.Event()
    turns = iter(range(12))

    def now():
        return clock[0]

    def tick(timeout=None):
        clock[0] += timedelta(minutes=1)
        if next(turns, None) is None:
            stop.set()

    stop.wait = tick  # each turn's wait moves the clock
    run_upkeep(schedule, stop, is_healthy=healthy, send=send, now=now, poll_sec=0)


def test_nothing_is_posted_until_the_api_is_healthy(tmp_path):
    sent: list[str] = []
    health = {"ok": False}
    clock = [START]

    def healthy():
        if clock[0] >= START + timedelta(minutes=5):
            health["ok"] = True
        return health["ok"]

    s = _schedule(tmp_path)
    s.done("cleanup", START - timedelta(days=2))  # last night's was missed
    _drive(s, healthy=healthy, send=sent.append, clock=clock)
    assert sent[:2] == [UPKEEP_PATH, CLEANUP_PATH]  # first at 5 min, once healthy
    assert sent.count(CLEANUP_PATH) == 1


def test_a_failed_post_waits_for_its_next_turn(tmp_path):
    tries: list[str] = []
    clock = [START]

    def send(path):
        tries.append(path)
        raise OSError("connection refused")

    _drive(_schedule(tmp_path), healthy=lambda: True, send=send, clock=clock)
    # 12 minutes: at 2, 7 and 12 min, not every turn.
    assert tries == [UPKEEP_PATH, UPKEEP_PATH, UPKEEP_PATH]
