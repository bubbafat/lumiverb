"""What one account's jobs run with (ADR-016 phase 4).

Each library's storage is looked at by the scheduler itself every 30
seconds (and by the scan pass before it walks one), and jobs go by the
latest look; a job that finds the storage gone puts its storage work off
until the next look. Models unused for a while are let go of, so the GPU
is free for the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.processing.originals import Folder
from src.server.scheduler import account as account_mod
from src.server.scheduler.scans import ScanState

pytestmark = pytest.mark.fast


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def acct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    clock = Clock()
    wall = Wall()
    a = account_mod.Account("t1", MagicMock(), ScanState(), clock=clock, wall=wall)
    a.clock = clock
    a.wall = wall
    return a


class Wall:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now += timedelta(**kw)
        return self.now


def test_storage_work_goes_by_the_latest_look(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.library_ids(storage=True) == []  # not looked at yet
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1"), "lib_2": None})
    assert acct.library_ids(storage=True) == ["lib_1"]
    assert acct.library_ids(storage=False) == ["lib_1", "lib_2"]
    assert acct.originals("lib_1") == Folder(tmp_path, "lib_1") and acct.originals("lib_2") is None
    acct.unreachable("lib_1")  # a job found it gone
    assert acct.library_ids(storage=True) == [] and acct.reachable == {"lib_1": False, "lib_2": False}
    acct.set_reachable({"lib_2": Folder(tmp_path, "lib_2")})  # a scan's look at one library leaves the other's
    assert acct.library_ids(storage=True) == ["lib_2"] and acct.originals("lib_1") is None


def test_only_libraries_this_server_reads_are_looked_at_or_worked_in(acct, tmp_path: Path, monkeypatch) -> None:
    # ADR-018 phase 1: a library a device reads (from phase 6) is never this
    # server's to look at, scan or read originals in (src/shared/readers.py).
    looks: list = []
    monkeypatch.setattr("src.processing.roots.reachable_root",
                        lambda library, **kw: looks.append(library["library_id"]) or tmp_path)
    acct.set_libraries([{"library_id": "lib_1", "name": "A", "reader": "server"},
                        {"library_id": "lib_2", "name": "B", "reader": "device:dev_1"}])
    acct.look_at_storage()
    assert looks == ["lib_1"]
    assert acct.library_ids(storage=False) == ["lib_1"] and acct.library_ids(storage=True) == ["lib_1"]


def test_the_scheduler_looks_at_each_librarys_storage_every_30_seconds(acct, tmp_path: Path, monkeypatch) -> None:
    looks: list = []
    gone: set[str] = set()

    def reachable_root(library, *, require_entries=False, **kw):
        looks.append((library["library_id"], require_entries))
        return None if library["library_id"] in gone else tmp_path

    monkeypatch.setattr("src.processing.roots.reachable_root", reachable_root)
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    assert acct.look_at_storage() is True  # at once: not looked at since this start
    assert looks == [("lib_1", True)] and acct.library_ids(storage=True) == ["lib_1"]
    acct.unreachable("lib_1")  # a job found it gone
    acct.clock.now += account_mod.LOOK_EVERY_SEC - 1
    assert acct.look_at_storage() is False and acct.library_ids(storage=True) == []
    acct.clock.now += 1
    assert acct.look_at_storage() is True and acct.library_ids(storage=True) == ["lib_1"]  # back
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    gone.add("lib_2")
    assert acct.look_at_storage() is True  # a new library: at once
    assert acct.library_ids(storage=True) == ["lib_1"]
    assert acct.storage_seen()["lib_2"] == {"seen_at": None, "away_since": acct.wall.now.isoformat(), "checked": True}


def test_each_look_is_kept_as_when_last_seen_and_since_when_unreachable(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.storage_seen() == {}  # not looked at yet, no record
    t0 = acct.wall.now
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1"), "lib_2": None})
    assert acct.storage_seen() == {
        "lib_1": {"seen_at": t0.isoformat(), "away_since": None, "checked": True},
        "lib_2": {"seen_at": None, "away_since": t0.isoformat(), "checked": True},
    }
    t1 = acct.wall.advance(minutes=5)
    acct.set_reachable({"lib_1": None, "lib_2": None})  # gone now; lib_2 still gone since t0
    assert acct.storage_seen()["lib_1"] == {"seen_at": t0.isoformat(), "away_since": t1.isoformat(), "checked": True}
    assert acct.storage_seen()["lib_2"]["away_since"] == t0.isoformat()
    t2 = acct.wall.advance(minutes=5)
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1"), "lib_2": None})
    assert acct.storage_seen()["lib_1"] == {"seen_at": t2.isoformat(), "away_since": None, "checked": True}


def test_a_job_finding_the_storage_gone_is_a_look(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    t0 = acct.wall.now
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1")})
    t1 = acct.wall.advance(minutes=1)
    acct.unreachable("lib_1")
    assert acct.storage_seen()["lib_1"] == {"seen_at": t0.isoformat(), "away_since": t1.isoformat(), "checked": True}


def test_the_record_from_before_a_restart_is_kept_but_not_looked_at_yet(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    acct.seed_storage({"lib_1": {"seen_at": "2026-10-09T10:00:00+00:00", "away_since": None, "checked": True},
                       "lib_2": {"seen_at": None, "away_since": "2026-10-09T09:00:00+00:00"},
                       "lib_bad": "not a record"})
    assert acct.storage_seen() == {
        "lib_1": {"seen_at": "2026-10-09T10:00:00+00:00", "away_since": None, "checked": False},
        "lib_2": {"seen_at": None, "away_since": "2026-10-09T09:00:00+00:00", "checked": False},
    }
    assert acct.library_ids(storage=True) == []  # a record isn't a look: storage work waits for one
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1"), "lib_2": None})
    assert acct.storage_seen()["lib_2"] == {"seen_at": None, "away_since": "2026-10-09T09:00:00+00:00",
                                            "checked": True}  # still gone since then
    acct.seed_storage(None)  # nothing to restore
    assert acct.storage_seen()["lib_1"]["checked"] is True


def test_records_of_libraries_that_are_gone_are_let_go(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1"), "lib_2": Folder(tmp_path, "lib_2")})
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    assert list(acct.storage_seen()) == ["lib_1"]


def test_the_preview_cache_is_the_accounts_one_and_never_reads_an_original(acct, tmp_path: Path) -> None:
    """Its previews come from the API, whether the library's storage is
    reachable or not (ADR-020 decision 4)."""
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    first = acct.proxy_cache("lib_1")
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1")})
    assert acct.proxy_cache("lib_1") is first and acct.proxy_cache("lib_2") is first
    assert not hasattr(first, "_root_path")


def test_nothing_is_ready_until_the_servers_settings_were_read(acct) -> None:
    acct.guards["vision"].check = MagicMock(return_value=False)
    acct.guards["transcripts"].check = MagicMock(return_value=False)
    acct.client.get.side_effect = ConnectionError("the API isn't answering yet")
    acct.refresh()
    assert acct.settings_ready is False
    acct.client.get.side_effect = None
    acct.client.get.return_value.json.return_value = {"producers": [], "libraries": []}
    acct.clock.now += account_mod.RETRY_SEC  # tried again sooner than the usual minute
    acct.refresh()
    assert acct.settings_ready is True


def test_closing_tells_jobs_to_save_nothing_more(acct) -> None:
    acct.close()
    assert acct.stopping.is_set()


def test_storage_is_looked_at_as_the_scan_pass_does(acct, tmp_path: Path, monkeypatch) -> None:
    # Review round 3: a job that found a file missing listed the whole root
    # with no timeout; it looks as the scan pass does now.
    seen: list = []

    def reachable_root(library, *, require_entries=False, **kw):
        seen.append((library["library_id"], require_entries))
        return tmp_path if library["library_id"] == "lib_1" else None

    monkeypatch.setattr("src.processing.roots.reachable_root", reachable_root)
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.storage_gone("lib_1") is False and acct.storage_gone("lib_2") is True
    assert acct.storage_gone("lib_unknown") is True
    assert seen == [("lib_1", True), ("lib_2", True)]


def test_a_redo_made_with_settings_newer_than_those_read_reads_them_again(acct) -> None:
    from src.shared.producers import PRODUCERS, lineage

    acct.client.get.return_value.json.return_value = {"producers": []}
    acct.producers.refresh()
    now = lineage("lumiverb.video.transcript", acct.producers.settings("lumiverb.video.transcript"), None)["settings_hash"]
    calls = acct.client.get.call_count
    acct.follow_settings("lumiverb.video.transcript", now)  # what it has: nothing to read
    assert acct.client.get.call_count == calls
    newer = {**PRODUCERS["lumiverb.video.transcript"].defaults, "vad_min_silence_ms": 800}
    acct.client.get.return_value.json.return_value = {"producers": [{"artifact": "lumiverb.video.transcript", "settings": newer}]}
    acct.follow_settings("lumiverb.video.transcript", lineage("lumiverb.video.transcript", newer, None)["settings_hash"])
    assert acct.producers.settings("lumiverb.video.transcript")["vad_min_silence_ms"] == 800
    calls = acct.client.get.call_count
    acct.follow_settings("lumiverb.video.transcript", "another")  # still not what it has: not again so soon
    assert acct.client.get.call_count == calls
    acct.clock.now += account_mod.RETRY_SEC
    acct.follow_settings("lumiverb.video.transcript", "another")
    assert acct.client.get.call_count == calls + 1


def test_it_checks_in_as_the_reader_of_its_libraries(acct, tmp_path: Path) -> None:
    # ADR-018 phase 4: how each library is, and its last full scan; the answer is what it reads now.
    answers = [
        {"libraries": [{"library_id": "lib_1", "name": "A", "root_path": "/a", "reader": "server",
                        "scans_paused": False, "mark": 3, "scan_now": False},
                       {"library_id": "lib_2", "name": "B", "root_path": "/b", "reader": "server",
                        "scans_paused": True, "mark": 1, "scan_now": True}],
         "gone": [], "check_in_every_sec": 60},
    ]
    posted: list[dict] = []

    def post(path, *, json, retry):
        assert path == "/v1/readers/check-in" and retry
        posted.append(json)
        return MagicMock(json=lambda: answers[-1])

    acct.client.post.side_effect = post
    acct.check_in()
    assert posted[0]["libraries"] == [] and posted[0]["reader"] == "server" and posted[0]["protocol"] >= 1
    assert sorted(acct.libraries) == ["lib_1", "lib_2"] and acct.scan_now == {"lib_2"}
    assert acct.marks == {"lib_1": 3, "lib_2": 1}

    acct.set_reachable({"lib_1": None})  # its storage away at the last look
    acct.scan_state.last_full_scan["lib_1"] = datetime(2026, 10, 9, 11, 0, tzinfo=UTC).timestamp()
    acct.check_in()
    said = {s["library_id"]: s for s in posted[1]["libraries"]}
    assert said["lib_1"] == {"library_id": "lib_1", "state": "away", "mark": 3, "waiting_files": 0,
                             "waiting_bytes": 0, "last_full_scan_at": "2026-10-09T11:00:00+00:00"}
    assert said["lib_2"]["state"] == "paused" and said["lib_2"]["last_full_scan_at"] is None
    acct.set_reachable({"lib_1": Folder(tmp_path, "lib_1")})
    acct.scanning = "lib_1"
    acct.to_upload("lib_1", 212, 14 * 1024 ** 3)  # what its scan has left to send, as the scan pass says
    acct.check_in()
    said = {s["library_id"]: s for s in posted[2]["libraries"]}
    assert {i: s["state"] for i, s in said.items()} == {"lib_1": "scanning", "lib_2": "paused"}
    assert (said["lib_1"]["waiting_files"], said["lib_1"]["waiting_bytes"]) == (212, 14 * 1024 ** 3)
    assert said["lib_2"]["waiting_files"] == 0
    # A library it no longer reads takes what it had left with it.
    answers.append({**answers[0], "libraries": answers[0]["libraries"][1:]})
    acct.check_in()
    assert acct.to_send == {}


def test_a_mark_that_moved_has_the_settings_read_again_at_the_next_refresh(acct) -> None:
    lib = {"library_id": "lib_1", "name": "A", "root_path": "/a", "reader": "server", "scans_paused": False,
           "scan_now": False}
    answer = {"libraries": [{**lib, "mark": 1}], "gone": [], "check_in_every_sec": 60}
    acct.client.post.side_effect = lambda path, **kw: MagicMock(json=lambda: answer)
    acct.refresh()
    first = acct._refreshed_at
    acct.check_in()  # the same mark: the minute stands
    assert acct._refreshed_at == first
    answer = {"libraries": [{**lib, "mark": 2}], "gone": [], "check_in_every_sec": 60}
    acct.check_in()
    assert acct._refreshed_at is None and acct.marks == {"lib_1": 2}
