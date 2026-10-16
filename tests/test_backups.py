"""Backups' rules (src/server/backups.py): which are kept, when the nightly
one is due, where they go, what a folder holds, and how Settings and the
Admin page say it. No database: tests/test_backups_slow.py backs up and
restores real ones."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.server import backups
from src.server import system_health as h
from src.server.backups import Backup, BackupError

pytestmark = pytest.mark.fast


def _local(*args: int) -> datetime:
    """A wall-clock time here, aware."""
    return datetime(*args).astimezone()


def _backup(where: Path, made_at: datetime, kind: str = "nightly", *, partial: bool = False) -> Path:
    path = where / (backups._name(made_at, kind) + (backups.PARTIAL if partial else ""))
    path.mkdir(parents=True)
    (path / "control_plane.dump").write_bytes(b"x")
    (path / backups.MANIFEST).write_text(json.dumps({
        "made_at": made_at.astimezone(UTC).isoformat(), "kind": kind,
        "databases": [{"role": "control", "file": "control_plane.dump", "bytes": 1000,
                       "sha256": hashlib.sha256(b"x").hexdigest()},
                      {"role": "account", "tenant_id": "ten_a", "file": "ten_a.dump", "bytes": 24, "sha256": ""}]}))
    return path


# -- Kept --------------------------------------------------------------------------


def test_the_last_week_every_day_then_one_a_week_for_four_weeks():
    now = _local(2026, 10, 11, 12, 0)
    nightly = [Backup(Path(f"/b/{d}"), now - timedelta(days=d), "nightly") for d in range(60)]
    kept = sorted(int(p.name) for p in backups.to_keep(nightly, now))
    assert kept[:8] == [0, 1, 2, 3, 4, 5, 6, 7]  # 7 days back, inclusive
    weekly = kept[8:]
    assert len(weekly) <= 3 and all(d < 28 for d in weekly), weekly  # this week and the 3 before
    assert len({(now - timedelta(days=d)).isocalendar()[:2] for d in kept}) == 4


def test_the_newest_three_before_an_upgrade_and_before_a_restore_are_kept_however_old():
    now = _local(2026, 10, 11, 12, 0)
    nightly = [Backup(Path(f"/b/n{d}"), now - timedelta(days=d), "nightly") for d in range(40)]
    old = [Backup(Path(f"/b/u{d}"), now - timedelta(days=100 + 7 * d), "before-upgrade") for d in range(5)]
    old += [Backup(Path(f"/b/r{d}"), now - timedelta(days=200 + 7 * d), "before-restore") for d in range(5)]
    kept = {p.name for p in backups.to_keep(nightly + old, now)}
    assert {"u0", "u1", "u2", "r0", "r1", "r2"} <= kept
    assert not {"u3", "u4", "r3", "r4"} & kept


def test_the_newest_is_always_kept():
    now = _local(2026, 10, 11, 12, 0)
    only = [Backup(Path("/b/old"), now - timedelta(days=400), "now")]
    assert backups.to_keep(only, now) == {Path("/b/old")}


def _in_one_week(now: datetime, weeks_ago: int) -> tuple[datetime, datetime]:
    """A Wednesday and the Tuesday before it, at noon here, that many weeks
    ago: one week's two backups whatever day it is today (weeks are this
    machine's ISO weeks, Monday to Sunday)."""
    then = now.astimezone() - timedelta(weeks=weeks_ago)
    wednesday = (then - timedelta(days=then.weekday() - 2)).replace(hour=12, minute=0, second=0, microsecond=0)
    return wednesday, wednesday - timedelta(days=1)


@pytest.mark.parametrize("days_on", range(7))  # as if today were each day of the week
def test_prune_removes_only_its_own_and_unfinished_ones_a_day_old(tmp_path, days_on):
    now = datetime.now(UTC) + timedelta(days=days_on)
    fresh = _backup(tmp_path, now - timedelta(hours=1))
    newer, earlier = _in_one_week(now, 9)
    old = _backup(tmp_path, newer)
    older = _backup(tmp_path, earlier)
    died = _backup(tmp_path, now - timedelta(days=3), partial=True)
    os.utime(died, (died.stat().st_atime, (now - timedelta(days=2)).timestamp()))
    working = _backup(tmp_path, now, "now", partial=True)  # being made now
    os.utime(working, (now.timestamp(), now.timestamp()))
    (tmp_path / "lumiverb-photos").mkdir()  # someone else's, in the same folder
    (tmp_path / "notes.txt").write_text("mine")
    removed = backups.prune(tmp_path, now)
    assert set(removed) == {older, died}  # `old` is the newest of its week
    assert fresh.exists() and old.exists() and working.exists()
    assert (tmp_path / "lumiverb-photos").exists() and (tmp_path / "notes.txt").exists()


def test_a_monday_starts_another_week(tmp_path):
    # A Sunday's backup and the next Monday's are two weeks': each is the
    # newest of its own, so both are kept (in one week the Sunday's would go).
    now = datetime.now(UTC)
    wednesday, _ = _in_one_week(now, 9)
    monday = _backup(tmp_path, wednesday - timedelta(days=2))
    sunday = _backup(tmp_path, wednesday - timedelta(days=3))
    assert backups.prune(tmp_path, now) == []
    assert monday.exists() and sunday.exists()


def test_only_whole_backups_are_listed_newest_first(tmp_path):
    now = datetime.now(UTC)
    a = _backup(tmp_path, now - timedelta(days=1))
    b = _backup(tmp_path, now, "now")
    _backup(tmp_path, now, "nightly", partial=True)
    (tmp_path / "lumiverb-2026-01-01-000000-nightly").mkdir()  # no manifest
    listed = backups.list_backups(tmp_path)
    assert [x.path for x in listed] == [b, a]
    assert listed[0].bytes == 1024 and listed[0].accounts == 1
    assert backups.list_backups(tmp_path / "missing") == []


def test_an_unfinished_backup_isnt_read_as_one(tmp_path):
    with pytest.raises(BackupError, match="never finished"):
        backups.read(_backup(tmp_path, datetime.now(UTC), partial=True))
    (tmp_path / "other").mkdir()
    with pytest.raises(BackupError, match="isn't a Lumiverb backup"):
        backups.read(tmp_path / "other")


def test_verify_says_which_file_is_missing_or_changed(tmp_path):
    path = _backup(tmp_path, datetime.now(UTC))
    with pytest.raises(BackupError, match=r"ten_a\.dump is missing"):
        backups.verify(backups.read(path))
    (path / "ten_a.dump").write_bytes(b"y")
    with pytest.raises(BackupError, match="SHA-256 differs"):
        backups.verify(backups.read(path))


# -- When --------------------------------------------------------------------------


def test_nightly_is_due_once_a_day_after_three_in_the_morning():
    at_two = _local(2026, 10, 11, 2, 0)
    at_four = _local(2026, 10, 11, 4, 0)
    last_night = _local(2026, 10, 10, 3, 1)
    assert not backups.nightly_due(last_night, at_two)  # tonight's 03:00 hasn't come
    assert backups.nightly_due(last_night, at_four)
    assert not backups.nightly_due(_local(2026, 10, 11, 3, 2), at_four)
    assert backups.nightly_due(None, at_two)  # none yet: now


def test_a_mac_asleep_at_three_backs_up_on_waking():
    woke = _local(2026, 10, 11, 9, 30)
    assert backups.nightly_due(_local(2026, 10, 9, 23, 0), woke)


def test_one_that_failed_is_tried_again_an_hour_later_not_every_upkeep():
    now = _local(2026, 10, 11, 4, 0)
    assert not backups.nightly_due(None, now, last_failed_at=now - timedelta(minutes=10))
    assert backups.nightly_due(None, now, last_failed_at=now - timedelta(minutes=61))


def test_the_next_nightly_is_the_next_three_in_the_morning():
    assert backups.next_nightly(_local(2026, 10, 11, 2, 0)) == _local(2026, 10, 11, 3, 0)
    assert backups.next_nightly(_local(2026, 10, 11, 3, 0)) == _local(2026, 10, 12, 3, 0)


def test_next_is_now_when_due_an_hour_after_a_failure_else_three_in_the_morning():
    now = _local(2026, 10, 11, 9, 0)
    assert backups.next_at(None, now) == now
    assert backups.next_at(None, now, last_failed_at=now - timedelta(minutes=20)) == now + timedelta(minutes=40)
    assert backups.next_at(_local(2026, 10, 11, 3, 1), now) == _local(2026, 10, 12, 3, 0)


def test_upkeep_starts_it_only_when_due_and_not_running(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(backups, "start", lambda *a, **k: started.append(a[2]))
    now = datetime.now(UTC)
    assert backups.start_nightly_if_due(tmp_path, "postgresql://x/y", now=now) == "started"
    assert started == ["nightly"]
    _backup(backups.folder(tmp_path), now)
    assert backups.start_nightly_if_due(tmp_path, "postgresql://x/y", now=now) == "not due"
    with backups._one_at_a_time(tmp_path):
        assert backups.start_nightly_if_due(tmp_path, "postgresql://x/y", now=now) == "running"
    assert started == ["nightly"]


def test_a_failure_is_recorded_and_the_lock_is_let_go(tmp_path, monkeypatch):
    def fails(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(backups, "make", fails)
    with pytest.raises(BackupError, match="OSError: disk full"):
        backups.back_up(tmp_path, "postgresql://x/y", "now")
    state = backups.read_state(tmp_path)
    assert state["last_error"] == "OSError: disk full" and state["last_failed_at"] and state["running"] is None
    assert not backups.running(tmp_path)


def test_one_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(backups, "LOOK_WAIT_SEC", 0.2)
    with backups._one_at_a_time(tmp_path):
        assert backups.running(tmp_path)
        with pytest.raises(BackupError, match="already"):
            backups.back_up(tmp_path, "postgresql://x/y", "now")
    assert not backups.running(tmp_path)
    assert "last_error" not in backups.read_state(tmp_path), "a second one waiting isn't a failure"


# -- Where ---------------------------------------------------------------------------


def test_the_folder_is_the_data_folder_s_until_an_admin_chooses(tmp_path):
    data, chosen = tmp_path / "data", tmp_path / "external"
    chosen.mkdir()
    assert backups.folder(data) == data / "backups"
    assert backups.set_folder(data, str(chosen)) == chosen
    assert backups.folder(data) == chosen
    assert backups.set_folder(data, None) == data / "backups"
    assert backups.folder(data) == data / "backups"


@pytest.mark.parametrize(("given", "said"), [
    ("", "Say which folder"),
    ("relative/path", "full path"),
    ("/no/such/folder", "There's no drive at /no. Is it plugged in"),
])
def test_a_folder_it_cant_use_is_refused_with_why(tmp_path, given, said):
    with pytest.raises(BackupError, match=said):
        backups.set_folder(tmp_path, given)
    assert backups.folder(tmp_path) == tmp_path / "backups"


def test_a_folder_not_there_on_a_drive_that_is_says_so(tmp_path):
    with pytest.raises(BackupError, match="There's no folder at"):
        backups.set_folder(tmp_path, tmp_path / "nowhere")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_folder_it_cant_write_is_refused(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(BackupError, match="can't write"):
            backups.check_folder(locked)
    finally:
        locked.chmod(stat.S_IRWXU)


def test_the_password_goes_in_the_environment_never_the_command_line():
    args, env = backups._libpq("postgresql+psycopg2://app:s3cret@127.0.0.1:6467/control_plane")
    assert args == ["--host", "127.0.0.1", "--port", "6467", "--username", "app"]
    assert env["PGPASSWORD"] == "s3cret"
    assert "s3cret" not in " ".join(args)


def test_a_tool_missing_from_pg_bin_is_said(tmp_path):
    with pytest.raises(BackupError, match="no pg_dump in PG_BIN"):
        backups.pg_tool("pg_dump", str(tmp_path))


# -- Said ---------------------------------------------------------------------------------


def test_the_admin_row():
    now = datetime.now(UTC)
    row = h.backups_row(newest=now - timedelta(hours=5), newest_bytes=12 * 1024 * 1024, now=now)
    assert (row.state, row.reason, row.link) == ("green", "Last backup 5 hours ago (12.0 MB).", "/settings/backups")
    row = h.backups_row(newest=None, now=now)
    assert row.state == "yellow" and "within minutes" in row.reason
    row = h.backups_row(newest=None, last_error="pg_dump couldn't back up x: boom", last_failed_at=now, now=now)
    assert row.state == "red" and "boom" in row.reason
    row = h.backups_row(newest=now - timedelta(hours=20), last_error="The disk is full.",
                        last_failed_at=now - timedelta(hours=1), now=now)
    assert row.state == "yellow" and row.reason.startswith("The last backup failed: The disk is full.")
    row = h.backups_row(newest=now - timedelta(days=3), now=now)
    assert row.state == "red" and row.reason == "No backup for 3 days."
    row = h.backups_row(newest=now - timedelta(hours=1), running={"done": 1, "of": 3}, now=now)
    assert row.state == "green" and row.reason.endswith("Backing up now (2 of 3).")
    # A failure before the newest one is history.
    row = h.backups_row(newest=now - timedelta(hours=1), last_error="old", last_failed_at=now - timedelta(hours=2), now=now)
    assert row.state == "green"


def test_how_to_restore_on_a_mac_and_on_linux(monkeypatch):
    from src.server.api.routers.system import restore_commands

    path = Path("/Volumes/Backup/lumiverb-2026-10-11-030000-nightly")
    monkeypatch.setenv("LUMIVERB_ENV_FILE", "/opt/homebrew/etc/lumiverb/env")
    assert restore_commands(path) == [
        "brew services stop lumiverb",
        "lumiverb-server restore --env /opt/homebrew/etc/lumiverb/env /Volumes/Backup/lumiverb-2026-10-11-030000-nightly",
        "brew services start lumiverb",
    ]
    monkeypatch.delenv("LUMIVERB_ENV_FILE")
    stop, restore, start = restore_commands(Path("/var/lib/lumiverb/backups/a b"))
    assert stop == "sudo systemctl stop lumiverb-scheduler lumiverb-api"
    assert restore.startswith("sudo ") and restore.endswith("restore --env /etc/lumiverb/env '/var/lib/lumiverb/backups/a b'")
    assert start == "sudo systemctl start lumiverb-api lumiverb-scheduler"


def test_the_service_tells_only_the_api_which_env_file_is_the_install_s(tmp_path):
    from src.server.lifecycle.cli import server_children

    env = {"DATA_DIR": str(tmp_path), "CONTROL_PLANE_DATABASE_URL": "postgresql://x/y"}
    children = {c.name: c for c in server_children(env, env_file=tmp_path / "env")}
    assert children["api"].env["LUMIVERB_ENV_FILE"] == str(tmp_path / "env")
    assert "LUMIVERB_ENV_FILE" not in children["scheduler"].env


@pytest.mark.skipif(os.geteuid() == 0, reason="the folder made here would be root's")
def test_as_root_the_commands_go_on_as_the_data_folder_s_owner(tmp_path, monkeypatch):
    """sudo, to read a Linux install's env file: what they write stays the API's."""
    from src.server.lifecycle import cli

    did = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda groups: did.append(("groups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: did.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: did.append(("uid", uid)))
    owner = tmp_path.stat()
    cli.as_data_owner(tmp_path)
    assert did == [("groups", []), ("gid", owner.st_gid), ("uid", owner.st_uid)]
    did.clear()
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    cli.as_data_owner(tmp_path)
    assert did == [], "not root: nothing to change"


def test_a_look_at_whether_one_runs_never_stops_a_backup_starting(tmp_path, monkeypatch):
    """running() takes the lock for an instant to see whether it's free. A
    backup starting in that instant used to give up, silently ("being made
    already", raised before anything was recorded): Back up now, or the
    nightly one, did nothing. Found as a move's twin under load."""
    import threading
    import time

    made = []
    monkeypatch.setattr(backups, "make", lambda dest, url, kind, **k: made.append(kind) or Backup(
        dest / "lumiverb-2026-10-11-030000-now", datetime.now(UTC), kind, []))
    monkeypatch.setattr(backups, "prune", lambda where, now=None: [])
    monkeypatch.setattr(backups, "LOOK_WAIT_SEC", 60)  # however slow the machine, the look is let go first
    with backups._one_at_a_time(tmp_path):  # a look, held a moment as the backup starts
        worker = threading.Thread(target=backups.back_up, args=(tmp_path, "postgresql://x/y", "now"))
        worker.start()
        time.sleep(0.2)
    worker.join(60)
    assert made == ["now"], "it waited its turn and made the backup"
