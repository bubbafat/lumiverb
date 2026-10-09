"""The scheduler service: the brain runs all processing (ADR-016 phase 4).

Each tick tops up the queue from each account's database, starts the best
job for every free slot of every pool, and collects what's done. A job
whose AI machines can't be used isn't handed out; storage work only goes
to libraries whose storage was reachable at the last look.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.server.scheduler.service import Scheduler, run


class FakeAccount:
    def __init__(self, tenant_id: str = "t1", *, vision: int = 2, transcripts: int = 1,
                 libraries: tuple[str, ...] = ("lib_1",), reachable: tuple[str, ...] = ("lib_1",)) -> None:
        self.tenant_id = tenant_id
        self.slots = {"vision": vision, "transcripts": transcripts}
        self.libraries = libraries
        self.reachable = reachable
        self.failures = MagicMock()
        self.refreshed = 0

    def refresh(self) -> None:
        self.refreshed += 1

    def capacity(self, job: str) -> int:
        return self.slots[job]

    def library_ids(self, *, storage: bool) -> list[str]:
        return list(self.reachable if storage else self.libraries)


class Recorder:
    """Runners that record what ran (optionally holding until released)."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, str, tuple[str, ...]]] = []
        self.hold = threading.Event()
        self.hold.set()
        self.lock = threading.Lock()

    def runner(self, kind: str):
        def run_(acct, job) -> None:
            self.hold.wait(5)
            with self.lock:
                self.ran.append((acct.tenant_id, kind, tuple(job.asset_ids)))
        return run_

    def all(self) -> dict:
        return {k: self.runner(k) for k in ("probe", "render", "clip", "vision", "ocr", "faces", "transcript",
                                            "scenes", "scene_vision")}


def _item(asset_id: str, added: str = "2026-10-01") -> dict:
    return {"asset_id": asset_id, "created_at": added, "library_id": "lib_1", "rel_path": asset_id}


def _scheduler(accounts: dict, due: dict[str, list[dict]], recorder: Recorder, *, scan=None,
               capacity: dict | None = None, asked: list | None = None) -> Scheduler:
    def candidates(tenant_id, kind, libraries):
        if asked is not None:
            asked.append((tenant_id, kind.name, tuple(libraries)))
        return [i for i in due.get(kind.name, []) if i["library_id"] in libraries]

    return Scheduler(lambda: accounts, capacity=capacity or {"scan": 1, "probe": 2, "render": 1, "clip": 1,
                                                            "faces": 1, "scenes": 1},
                     candidates=candidates, runners=recorder.all(), scan=scan or MagicMock())


def _settle(s: Scheduler) -> None:
    deadline = time.monotonic() + 5
    while s.running and time.monotonic() < deadline:
        s.collect(timeout=0.1)


@pytest.mark.fast
def test_every_pool_starts_its_best_job() -> None:
    rec = Recorder()
    s = _scheduler({"t1": FakeAccount()}, {"probe": [_item("p")], "vision": [_item("v")],
                                           "transcript": [_item("t")]}, rec)
    s.tick()
    _settle(s)
    assert sorted(kind for _, kind, _ in rec.ran) == ["probe", "transcript", "vision"]


@pytest.mark.fast
def test_a_scan_look_is_offered_to_each_account_and_not_twice_at_once() -> None:
    rec = Recorder()
    scans: list[str] = []
    release = threading.Event()

    def scan(acct, job, *, now) -> None:
        scans.append(acct.tenant_id)
        release.wait(5)

    s = _scheduler({"t1": FakeAccount()}, {}, rec, scan=scan)
    s.tick()
    s.tick()
    assert scans == ["t1"]
    release.set()
    _settle(s)


@pytest.mark.fast
def test_scans_of_two_accounts_take_turns() -> None:
    rec = Recorder()
    scans: list[str] = []
    s = _scheduler({"t1": FakeAccount("t1"), "t2": FakeAccount("t2")}, {}, rec,
                   scan=lambda acct, job, *, now: scans.append(acct.tenant_id))
    s.tick()
    _settle(s)
    s.tick()
    _settle(s)
    assert sorted(scans) == ["t1", "t2"]


@pytest.mark.fast
def test_ai_work_waits_while_its_machines_cannot_be_used() -> None:
    # No clip is charged: its jobs simply aren't handed out.
    rec = Recorder()
    asked: list = []
    s = _scheduler({"t1": FakeAccount(vision=0)}, {"vision": [_item("v")], "ocr": [_item("o")],
                                                   "clip": [_item("c")]}, rec, asked=asked)
    s.tick()
    _settle(s)
    assert [kind for _, kind, _ in rec.ran] == ["clip"]
    assert not any(kind in ("vision", "ocr", "scene_vision") for _, kind, _ in asked)


@pytest.mark.fast
def test_machines_that_go_down_stop_handing_out_their_work() -> None:
    rec = Recorder()
    acct = FakeAccount(vision=1)
    s = _scheduler({"t1": acct}, {"vision": [_item("a"), _item("b", "2026-10-02")]}, rec)
    rec.hold.clear()
    s.tick()
    acct.slots["vision"] = 0
    rec.hold.set()
    _settle(s)
    s.tick()
    _settle(s)
    assert [ids for _, kind, ids in rec.ran if kind == "vision"] == [("a",)]


@pytest.mark.fast
def test_storage_work_only_in_libraries_reachable_at_the_last_look() -> None:
    rec = Recorder()
    asked: list = []
    acct = FakeAccount(libraries=("lib_1", "lib_2"), reachable=("lib_2",))
    s = _scheduler({"t1": acct}, {}, rec, asked=asked)
    s.tick()
    libs = {kind: libs for _, kind, libs in asked}
    assert libs["probe"] == ("lib_2",) and libs["render"] == ("lib_2",)
    assert libs["vision"] == ("lib_1", "lib_2")


@pytest.mark.fast
def test_nothing_is_asked_where_no_library_can_be_used() -> None:
    rec = Recorder()
    asked: list = []
    s = _scheduler({"t1": FakeAccount(reachable=())}, {}, rec, asked=asked)
    s.tick()
    assert not any(kind in ("probe", "render") for _, kind, _ in asked)


@pytest.mark.fast
def test_a_pool_runs_no_more_than_its_slots() -> None:
    rec = Recorder()
    rec.hold.clear()
    s = _scheduler({"t1": FakeAccount(vision=2)},
                   {"vision": [_item(f"v{i}", f"2026-10-0{i}") for i in range(1, 6)]}, rec)
    started = s.tick()
    assert started == 3  # two descriptions and the scan look
    rec.hold.set()
    _settle(s)
    s.tick()
    _settle(s)
    assert [ids[0] for _, kind, ids in rec.ran if kind == "vision"] == ["v1", "v2", "v3", "v4"]


@pytest.mark.fast
def test_transcripts_get_twice_their_machines_slots() -> None:
    # Each clip's audio is got ready while the machines work on others.
    rec = Recorder()
    rec.hold.clear()
    s = _scheduler({"t1": FakeAccount(transcripts=1)},
                   {"transcript": [_item(f"t{i}", f"2026-10-0{i}") for i in range(1, 5)]}, rec)
    s.tick()
    assert s.dispatcher.running("transcripts@t1") == 2
    rec.hold.set()
    _settle(s)


@pytest.mark.fast
def test_a_job_that_fails_frees_its_slot_and_the_rest_go_on() -> None:
    rec = Recorder()
    runners = rec.all()

    def boom(acct, job):
        raise RuntimeError("model crashed")

    runners["clip"] = boom
    s = Scheduler(lambda: {"t1": FakeAccount()}, capacity={"clip": 1, "scan": 1},
                  candidates=lambda t, k, libs: [_item("c1"), _item("c2", "2026-10-02")] if k.name == "clip" else [],
                  runners=runners, scan=MagicMock())
    s.tick()
    _settle(s)
    assert s.dispatcher.free("clip") == 1
    assert s.dispatcher.running("clip") == 0


@pytest.mark.fast
def test_each_account_has_its_own_ai_machines() -> None:
    rec = Recorder()
    s = _scheduler({"t1": FakeAccount("t1", vision=1), "t2": FakeAccount("t2", vision=0)},
                   {"vision": [_item("v")]}, rec)
    s.tick()
    _settle(s)
    assert [(t, kind) for t, kind, _ in rec.ran if kind == "vision"] == [("t1", "vision")]


@pytest.mark.fast
def test_one_accounts_trouble_does_not_stop_the_others() -> None:
    rec = Recorder()
    broken = FakeAccount("t1")
    broken.refresh = MagicMock(side_effect=RuntimeError("database gone"))
    s = _scheduler({"t1": broken, "t2": FakeAccount("t2")}, {"clip": [_item("c")]}, rec)
    s.tick()
    _settle(s)
    assert ("t2", "clip", ("c",)) in rec.ran


@pytest.mark.fast
def test_run_ticks_until_stopped_and_lets_jobs_in_hand_finish() -> None:
    rec = Recorder()
    rec.hold.clear()
    s = _scheduler({"t1": FakeAccount()}, {"clip": [_item("c")]}, rec)
    stop = threading.Event()
    saves: list[int] = []
    t = threading.Thread(target=run, kwargs={"stop": stop, "scheduler": s, "save": lambda: saves.append(1),
                                             "tick_sec": 0.01})
    t.start()
    deadline = time.monotonic() + 5
    while not s.running and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    rec.hold.set()
    t.join(10)
    assert not t.is_alive()
    assert ("t1", "clip", ("c",)) in rec.ran
    assert saves  # the scan state is saved, also on the way out


@pytest.mark.fast
def test_failures_are_sent_to_the_server_regularly(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.scheduler import service

    monkeypatch.setattr(service, "FLUSH_EVERY_SEC", 0)
    rec = Recorder()
    acct = FakeAccount()
    s = _scheduler({"t1": acct}, {}, rec)
    s.tick()
    assert acct.failures.flush.called


@pytest.mark.fast
def test_running_the_module_in_a_spawned_child_starts_nothing() -> None:
    # Face detection's subprocess is started with "spawn": the child imports
    # the main module again (as __mp_main__), and must not start a scheduler.
    import runpy
    from unittest.mock import patch

    with patch("src.server.scheduler.service.main") as main:
        runpy.run_module("src.server.scheduler.__main__", run_name="__mp_main__")
    main.assert_not_called()


@pytest.mark.fast
def test_accounts_are_listed_once_however_many_ask_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.scheduler.service import Accounts

    listings: list[int] = []
    gate = threading.Event()

    def slow_list(self) -> None:
        listings.append(1)
        gate.wait(1)

    monkeypatch.setattr(Accounts, "_list", slow_list)
    accounts = Accounts(MagicMock(), url="http://api")
    threads = [threading.Thread(target=accounts) for _ in range(4)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert listings == [1]
