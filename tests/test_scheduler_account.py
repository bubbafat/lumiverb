"""What one account's jobs run with (ADR-016 phase 4).

Each library's storage is looked at by the scan pass only, and jobs go by
that look; a job that finds the storage gone puts its storage work off
until the next look. Models unused for a while are let go of, so the GPU
is free for the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


def test_storage_work_goes_by_the_scan_passs_look(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.library_ids(storage=True) == []  # not looked at yet
    acct.set_reachable({"lib_1": tmp_path, "lib_2": None})
    assert acct.library_ids(storage=True) == ["lib_1"]
    assert acct.library_ids(storage=False) == ["lib_1", "lib_2"]
    assert acct.root("lib_1") == tmp_path and acct.root("lib_2") is None
    acct.unreachable("lib_1")  # a job found it gone
    assert acct.library_ids(storage=True) == [] and acct.reachable == {"lib_1": False, "lib_2": False}


def test_each_look_is_kept_as_when_last_seen_and_since_when_unreachable(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.storage_seen() == {}  # not looked at yet, no record
    t0 = acct.wall.now
    acct.set_reachable({"lib_1": tmp_path, "lib_2": None})
    assert acct.storage_seen() == {
        "lib_1": {"seen_at": t0.isoformat(), "away_since": None, "checked": True},
        "lib_2": {"seen_at": None, "away_since": t0.isoformat(), "checked": True},
    }
    t1 = acct.wall.advance(minutes=5)
    acct.set_reachable({"lib_1": None, "lib_2": None})  # gone now; lib_2 still gone since t0
    assert acct.storage_seen()["lib_1"] == {"seen_at": t0.isoformat(), "away_since": t1.isoformat(), "checked": True}
    assert acct.storage_seen()["lib_2"]["away_since"] == t0.isoformat()
    t2 = acct.wall.advance(minutes=5)
    acct.set_reachable({"lib_1": tmp_path, "lib_2": None})
    assert acct.storage_seen()["lib_1"] == {"seen_at": t2.isoformat(), "away_since": None, "checked": True}


def test_a_job_finding_the_storage_gone_is_a_look(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    t0 = acct.wall.now
    acct.set_reachable({"lib_1": tmp_path})
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
    acct.set_reachable({"lib_1": tmp_path, "lib_2": None})
    assert acct.storage_seen()["lib_2"] == {"seen_at": None, "away_since": "2026-10-09T09:00:00+00:00",
                                            "checked": True}  # still gone since then
    acct.seed_storage(None)  # nothing to restore
    assert acct.storage_seen()["lib_1"]["checked"] is True


def test_records_of_libraries_that_are_gone_are_let_go(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    acct.set_reachable({"lib_1": tmp_path, "lib_2": tmp_path})
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    assert list(acct.storage_seen()) == ["lib_1"]


def test_a_proxy_cache_follows_its_librarys_storage(acct, tmp_path: Path) -> None:
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}])
    first = acct.proxy_cache("lib_1")
    assert first._root_path is None  # not reachable at first: proxies come from the server
    acct.set_reachable({"lib_1": tmp_path})
    assert acct.proxy_cache("lib_1")._root_path == tmp_path  # made from the originals now


def test_nothing_is_ready_until_the_servers_settings_were_read(acct) -> None:
    acct.vision.check = MagicMock(return_value=False)
    acct.transcripts.check = MagicMock(return_value=False)
    acct.client.get.side_effect = ConnectionError("the API isn't answering yet")
    acct.refresh()
    assert acct.settings_ready is False
    acct.client.get.side_effect = None
    acct.client.get.return_value.json.return_value = {"producers": [], "libraries": []}
    acct.clock.now += account_mod.RETRY_SEC  # tried again sooner than the usual minute
    acct.refresh()
    assert acct.settings_ready is True


def test_idle_models_are_let_go_of(acct) -> None:
    acct.vision.check = MagicMock(return_value=True)
    acct.transcripts.check = MagicMock(return_value=True)
    acct.client.get.return_value.json.return_value = {"producers": []}
    acct._clip = (("ViT-B-32", "openai"), MagicMock())
    acct._used["clip"] = acct.clock.now
    faces = MagicMock(idle=True)
    acct._faces = faces
    acct._used["faces"] = acct.clock.now
    acct.clock.now += account_mod.IDLE_SEC - 1
    acct.refresh()
    assert acct._clip is not None and not faces.close.called
    acct.clock.now += 2
    acct.refresh()
    assert acct._clip is None and faces.close.called


def test_letting_go_of_the_face_process_is_said_once(acct, caplog) -> None:
    # Review round 2: every refresh after the first said it again (1 a second).
    from src.server.scheduler.runners import FaceRunner

    acct.vision.check = MagicMock(return_value=True)
    acct.transcripts.check = MagicMock(return_value=True)
    acct.client.get.return_value.json.return_value = {"producers": []}
    runner = FaceRunner(acct.client, MagicMock(), pool_factory=MagicMock)
    runner._pool = MagicMock()  # a batch ran
    acct._faces = runner
    acct._used["faces"] = acct.clock.now
    caplog.set_level("INFO", logger="src.server.scheduler.account")
    for _ in range(5):
        acct.clock.now += account_mod.IDLE_SEC + 1
        acct.refresh()
    assert sum("face detection process" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.fast
def test_a_face_process_in_the_middle_of_a_batch_isnt_let_go_of(acct) -> None:
    acct.vision.check = MagicMock(return_value=True)
    acct.transcripts.check = MagicMock(return_value=True)
    acct.client.get.return_value.json.return_value = {"producers": []}
    faces = MagicMock(idle=False)
    acct._faces = faces
    acct._used["faces"] = acct.clock.now
    acct.clock.now += account_mod.IDLE_SEC + 1
    acct.refresh()
    assert not faces.close.called


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

    monkeypatch.setattr("src.client.cli.roots.reachable_root", reachable_root)
    acct.set_libraries([{"library_id": "lib_1", "name": "A"}, {"library_id": "lib_2", "name": "B"}])
    assert acct.storage_gone("lib_1") is False and acct.storage_gone("lib_2") is True
    assert acct.storage_gone("lib_unknown") is True
    assert seen == [("lib_1", True), ("lib_2", True)]


def test_a_redo_made_with_settings_newer_than_those_read_reads_them_again(acct) -> None:
    from src.shared.producers import PRODUCERS, lineage

    acct.client.get.return_value.json.return_value = {"producers": []}
    acct.producers.refresh()
    now = lineage("transcript", acct.producers.settings("transcript"), None)["settings_hash"]
    calls = acct.client.get.call_count
    acct.follow_settings("transcript", now)  # what it has: nothing to read
    assert acct.client.get.call_count == calls
    newer = {**PRODUCERS["transcript"].defaults, "vad_min_silence_ms": 800}
    acct.client.get.return_value.json.return_value = {"producers": [{"artifact": "transcript", "settings": newer}]}
    acct.follow_settings("transcript", lineage("transcript", newer, None)["settings_hash"])
    assert acct.producers.settings("transcript")["vad_min_silence_ms"] == 800
    calls = acct.client.get.call_count
    acct.follow_settings("transcript", "another")  # still not what it has: not again so soon
    assert acct.client.get.call_count == calls
    acct.clock.now += account_mod.RETRY_SEC
    acct.follow_settings("transcript", "another")
    assert acct.client.get.call_count == calls + 1
