"""The scheduler service: the server runs all processing (ADR-016 phase 4).

Each tick tops up the queue from each account's database, starts the best
job for every free slot of every pool, and collects what's done. A job
whose AI machines can't be used isn't handed out; storage work only goes
to libraries whose storage was reachable at the latest look, which the
scheduler makes itself, whatever is paused but Pause all.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.shared.readers import read_by_server
from src.producers.runner import Work
from src.producers.runner import run as run_work
from src.server.repository.lineage import Pauses
from src.server.scheduler.kinds import KINDS, QUEUED
from src.server.scheduler.service import Scheduler, run
from src.shared.producers import PAUSE_SCANS, pause_targets


def _pauses(work=(), redo=(), everything: bool = False) -> Pauses:
    """What an admin paused (lineage.pauses): switches' work, and producers'
    redo; "scans" among the work: every library's scans (each its own,
    ADR-018 phase 1); everything: Pause all (the "all" row: every switch and
    every library shows paused)."""
    seen = {"paused_by": None, "paused_at": None}
    scans = everything or PAUSE_SCANS in work
    paused = Pauses(work={t: seen for t in (pause_targets() if everything else work) if t != PAUSE_SCANS},
                    redo={a: seen for a in redo}, library_ids=("lib_1",),
                    libraries={"lib_1": seen} if scans else {})
    return dataclasses.replace(paused, everything=True) if everything else paused


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Scheduler made here without them reads nothing from a database."""
    from src.server.scheduler import service

    for name, stub in (("_on_hold_in_database", lambda tenant_id: _pauses()),
                       ("_retry_requested_in_database", lambda tenant_id: None),
                       ("_status_to_database", lambda tenant_id, status: None),
                       ("_status_from_database", lambda tenant_id: None),
                       ("_crashed_in_database", lambda tenant_id, artifact, asset_ids, error: []),
                       ("_hold_new_versions_in_database", lambda tenant_id: None)):
        monkeypatch.setattr(service, name, stub, raising=False)


class FakeAccount:
    def __init__(self, tenant_id: str = "t1", *, vision: int = 2, transcripts: int = 1, clip: int = 1, faces: int = 1,
                 libraries: tuple[str, ...] = ("lib_1",), reachable: tuple[str, ...] = ("lib_1",),
                 devices_read: tuple[str, ...] = ()) -> None:
        self.tenant_id = tenant_id
        self.slots = {"vision": vision, "transcripts": transcripts, "clip": clip, "faces": faces}
        self.libraries = libraries  # those this server reads
        self.reachable = reachable
        # Active libraries a device reads: the database lists their work from what's stored too.
        self.devices_read = devices_read
        self.failures = MagicMock()
        self.refreshed = 0
        self.settings_ready = True
        self.stopping = threading.Event()
        self.gpu_holds: list[int] = []
        self.followed: list[tuple[str, str | None]] = []
        self.seen: dict[str, dict] = {}
        self.seeded: list = []

    def look_at_storage(self) -> bool:
        return True  # reachable says what the looks found

    def storage_seen(self) -> dict[str, dict]:
        return dict(self.seen)

    def seed_storage(self, record) -> None:
        self.seeded.append(record)
        self.seen = {k: {**v, "checked": False} for k, v in (record or {}).items()}

    def follow_settings(self, artifact: str, settings_hash: str | None) -> None:
        self.followed.append((artifact, settings_hash))

    def set_gpu_hold(self, hold: int) -> None:
        if not self.gpu_holds or self.gpu_holds[-1] != hold:
            self.gpu_holds.append(hold)

    def refresh(self) -> None:
        self.refreshed += 1

    def capacity(self, job: str) -> int:
        return self.slots[job]

    def job_error(self, job: str) -> str:
        return f"No machine doing {job} is online."

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
        """A runner for every kind the scheduler runs (a producer added later too)."""
        return {k: self.runner(k) for k in dict.fromkeys(kind.base for kind in QUEUED)}


def _item(asset_id: str, added: str = "2026-10-01") -> dict:
    return {"asset_id": asset_id, "created_at": added, "library_id": "lib_1", "rel_path": asset_id}


def _scheduler(accounts: dict, due: dict[str, list[dict]], recorder: Recorder, *, scan=None,
               capacity: dict | None = None, asked: list | None = None, paused: set | None = None,
               held: set | None = None) -> Scheduler:
    def candidates(tenant_id, kind, libraries, skip=()):
        if asked is not None:
            asked.append((tenant_id, kind.name, None if libraries is None else tuple(libraries)))
        if libraries is None:  # every active library, whoever reads it (queue.candidates)
            acct = accounts[tenant_id]
            libraries = (*acct.libraries, *acct.devices_read)
        return [i for i in due.get(kind.name, []) if i["library_id"] in libraries and i["asset_id"] not in skip]

    return Scheduler(lambda: accounts, capacity=capacity or {"scan": 1, "probe": 2, "render": 1, "gpu": 1,
                                                            "scenes": 1},
                     candidates=candidates, runners=recorder.all(), scan=scan or MagicMock(), inline_refill=True,
                     on_hold=lambda tenant_id: _pauses(held or (), paused or ()),
                     retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)


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
    assert libs["vision"] is None, "what's made from what's stored: every active library (queue.candidates)"


@pytest.mark.fast
def test_a_library_a_device_reads_gets_the_work_from_what_s_stored_and_none_on_its_originals() -> None:
    """ADR-018 decision 2: the device makes what needs its originals; this
    server makes the rest from the previews and analysis proxies it sent."""
    rec = Recorder()
    device_clip = {**_item("dev-clip"), "library_id": "lib_dev"}
    due = {"vision": [device_clip], "probe": [device_clip], "render": [device_clip], "clip": [device_clip]}
    acct = FakeAccount(libraries=("lib_1",), reachable=("lib_1",), devices_read=("lib_dev",), vision=1)
    s = _scheduler({"t1": acct}, due, rec)
    s.tick()
    _settle(s)
    s.tick()
    _settle(s)
    ran = {kind for _, kind, ids in rec.ran if ids == ("dev-clip",)}
    assert {"vision", "clip"} <= ran
    assert not ran & {"probe", "render"}, "never on a device's originals"


@pytest.mark.fast
def test_nothing_is_asked_where_no_library_can_be_used() -> None:
    rec = Recorder()
    asked: list = []
    acct = FakeAccount(reachable=())
    s = _scheduler({"t1": acct}, {}, rec, asked=asked)
    s.tick()
    assert not any(kind in ("probe", "render") for _, kind, _ in asked)
    # Review round 2: that wasn't an empty answer to wait on; once the
    # storage is there, it's asked at once.
    acct.reachable = ("lib_1",)
    s.tick()
    assert ("t1", "probe", ("lib_1",)) in asked


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
    ran = [ids[0] for _, kind, ids in rec.ran if kind == "vision"]
    # The first two ran side by side (either may finish first); then the next two.
    assert sorted(ran[:2]) == ["v1", "v2"] and sorted(ran[2:]) == ["v3", "v4"]


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
    s = Scheduler(lambda: {"t1": FakeAccount()}, capacity={"gpu": 1, "scan": 1},
                  candidates=lambda t, k, libs, skip=(): [_item("c1"), _item("c2", "2026-10-02")] if k.name == "clip" else [],
                  runners=runners, scan=MagicMock(), inline_refill=True, retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    s.tick()
    _settle(s)
    assert s.dispatcher.free("gpu") == 1
    assert s.dispatcher.running("gpu") == 0


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
    for _ in range(2):  # the two accounts take turns on the one GPU slot
        s.tick()
        _settle(s)
    assert ("t2", "clip", ("c",)) in rec.ran
    # Its settings read before stay: its own work goes on too.
    assert ("t1", "clip", ("c",)) in rec.ran


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
def test_importing_the_entry_point_starts_nothing() -> None:
    # Only running it as a program starts the scheduler.
    import runpy
    from unittest.mock import patch

    with patch("src.server.scheduler.service.entry") as entry:
        runpy.run_module("src.server.scheduler.__main__", run_name="__mp_main__")
    entry.assert_not_called()


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


@pytest.mark.fast
def test_nothing_is_handed_out_until_the_accounts_settings_were_read() -> None:
    # Made with the registry's defaults, it would be wrong (and stale) at once.
    rec = Recorder()
    acct = FakeAccount()
    acct.settings_ready = False
    s = _scheduler({"t1": acct}, {"clip": [_item("c")]}, rec)
    s.tick()
    _settle(s)
    assert [kind for _, kind, _ in rec.ran] == []
    acct.settings_ready = True
    s.tick()
    _settle(s)
    assert [kind for _, kind, _ in rec.ran] == ["clip"]


@pytest.mark.fast
def test_a_job_that_couldnt_try_is_offered_again_and_one_that_tried_waits() -> None:
    from src.producers.runner import NOT_TRIED

    rec = Recorder()
    outcomes = iter([NOT_TRIED, None, None])
    runners = rec.all()
    tries: list[str] = []

    def probe(acct, job):
        tries.append(job.asset_ids[0])
        return next(outcomes)

    runners["probe"] = probe
    s = Scheduler(lambda: {"t1": FakeAccount()}, capacity={"probe": 1, "scan": 0},
                  candidates=lambda t, k, libs, skip=(): [i for i in [_item("p")] if i["asset_id"] not in skip]
                  if k.name == "probe" else [], runners=runners, scan=MagicMock(), inline_refill=True, retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    for _ in range(4):
        s.tick()
        _settle(s)
        s.dispatcher._all_known_at.clear()  # ask the database every tick here
    assert tries == ["p", "p"]  # not tried: again at once; tried: held for the hour


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.fast
def test_what_a_job_says_of_each_clip_decides_how_long_its_held() -> None:
    # Saved or reported: held until the database has it. Waiting without
    # saying why, or a job's surprise: held the long while.
    def boom(acct, job):
        raise RuntimeError("a surprise")

    clock = Clock()
    runners = {**Recorder().all(), "vision": lambda acct, job: ["b"], "ocr": boom}
    due = {"vision": [_item("a"), _item("b", "2026-10-02")], "ocr": [_item("c")]}
    s = Scheduler(lambda: {"t1": FakeAccount(vision=3)}, capacity={"scan": 0},
                  candidates=lambda t, k, libs, skip=(): [i for i in due.get(k.name, []) if i["asset_id"] not in skip],
                  runners=runners, scan=MagicMock(), inline_refill=True, clock=clock)
    s.tick()
    _settle(s)
    clock.now += s.dispatcher.settle_after + 1
    assert s.dispatcher.held("t1", "vision") == ["b"]
    assert s.dispatcher.held("t1", "ocr") == ["c"]


@pytest.mark.fast
def test_the_database_is_asked_to_leave_out_clips_in_hand_or_just_tried() -> None:
    rec = Recorder()
    rec.hold.clear()
    skipped: list[tuple] = []

    def candidates(tenant_id, kind, libraries, skip=()):
        skipped.append((kind.name, tuple(sorted(skip))))
        return [i for i in [_item("a"), _item("b", "2026-10-02")] if i["asset_id"] not in skip] \
            if kind.name == "vision" else []

    s = Scheduler(lambda: {"t1": FakeAccount(vision=1)}, capacity={"scan": 0}, candidates=candidates,
                  runners=rec.all(), scan=MagicMock(), inline_refill=True, retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    s.tick()
    s.dispatcher._all_known_at.clear()
    s.dispatcher._buffers.clear()
    s.tick()
    assert ("vision", ("a",)) in skipped
    rec.hold.set()
    _settle(s)


@pytest.mark.fast
def test_one_kinds_trouble_doesnt_hold_up_the_others() -> None:
    rec = Recorder()

    def candidates(tenant_id, kind, libraries, skip=()):
        if kind.name == "probe":
            raise RuntimeError("statement timeout")
        return [_item("c")] if kind.name == "clip" else []

    s = Scheduler(lambda: {"t1": FakeAccount()}, capacity={"probe": 1, "gpu": 1, "scan": 0},
                  candidates=candidates, runners=rec.all(), scan=MagicMock(), inline_refill=True, retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    s.tick()
    _settle(s)
    assert [kind for _, kind, _ in rec.ran] == ["clip"]


@pytest.mark.fast
def test_refills_happen_off_the_dispatching_thread() -> None:
    # A slow database or AI machine check doesn't hold up starting jobs.
    rec = Recorder()
    gate = threading.Event()
    acct = FakeAccount()
    acct.refresh = lambda: gate.wait(60)
    s = Scheduler(lambda: {"t1": acct}, capacity={"gpu": 1, "scan": 0},
                  candidates=lambda t, k, libs, skip=(): [_item("c")] if k.name == "clip" else [],
                  runners=rec.all(), scan=MagicMock(), retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    start = time.monotonic()
    s.tick()
    assert time.monotonic() - start < 30, "the refresh's minute wasn't waited for (never how fast: a busy machine)"
    gate.set()
    deadline = time.monotonic() + 5
    while not rec.ran and time.monotonic() < deadline:
        s.tick()
        _settle(s)
        time.sleep(0.02)
    assert [kind for _, kind, _ in rec.ran] == ["clip"]
    s.stop()


@pytest.mark.fast
def test_stopping_tells_jobs_in_hand_to_save_nothing_more() -> None:
    rec = Recorder()
    acct = FakeAccount()
    s = _scheduler({"t1": acct}, {}, rec)
    s.stop({"t1": acct}, grace=0)
    assert acct.stopping.is_set()


@pytest.mark.fast
def test_an_account_whose_key_was_revoked_gets_a_new_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.scheduler import service

    made: list[str] = []

    class Acct:
        def __init__(self, tenant_id):
            self.client = MagicMock(unauthorized=False)
            self.closed = False
            made.append(tenant_id)

        def close(self):
            self.closed = True

    accounts = service.Accounts(MagicMock(), url="http://api")

    def listing(self):
        for t in ("t1",):
            if t not in self._accounts:
                self._accounts[t] = Acct(t)

    monkeypatch.setattr(service.Accounts, "_list", listing)
    first = accounts()["t1"]
    assert accounts()["t1"] is first
    first.client.unauthorized = True
    second = accounts()["t1"]
    assert second is not first and first.closed and made == ["t1", "t1"]


# ---------------------------------------------------------------------------
# Redo on change: tier 4, after everything else in its pool
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_redo_runs_after_everything_else_in_its_pool() -> None:
    # Missing beats stale: a redo only fills slots the first makings leave idle.
    rec = Recorder()
    rec.hold.clear()
    s = _scheduler({"t1": FakeAccount(vision=1)}, {"redo_vision": [_item("old", "2020-01-01")],
                                                   "vision": [_item("new", "2026-10-09")]}, rec)
    s.tick()
    rec.hold.set()
    _settle(s)
    s.tick()
    _settle(s)
    assert [(kind, ids) for _, kind, ids in rec.ran if "vision" in kind] == [("vision", ("new",)),
                                                                            ("vision", ("old",))]


@pytest.mark.fast
def test_a_redo_runs_with_its_first_kinds_runner() -> None:
    rec = Recorder()
    s = _scheduler({"t1": FakeAccount()}, {"redo_clip": [_item("c")]}, rec)
    s.tick()
    _settle(s)
    assert ("t1", "clip", ("c",)) in rec.ran


@pytest.mark.fast
def test_a_stopped_redo_hands_out_nothing_and_is_not_asked_for() -> None:
    rec = Recorder()
    asked: list = []
    s = _scheduler({"t1": FakeAccount()}, {"redo_vision": [_item("old")], "clip": [_item("c")]}, rec,
                   asked=asked, paused={"vision"})
    s.tick()
    _settle(s)
    assert [kind for _, kind, _ in rec.ran] == ["clip"]
    assert "redo_vision" not in [kind for _, kind, _ in asked]


@pytest.mark.fast
def test_stopping_a_redo_clears_what_was_waiting_at_once() -> None:
    rec = Recorder()
    rec.hold.clear()
    stopped: set = set()
    s = _scheduler({"t1": FakeAccount(vision=1)},
                   {"redo_vision": [_item("a"), _item("b", "2026-10-02")]}, rec, paused=stopped)
    s._on_hold = lambda tenant_id: _pauses((), stopped)
    s.tick()  # a in hand, b waiting
    stopped.add("vision")
    rec.hold.set()
    _settle(s)
    s.tick()
    _settle(s)
    assert [ids for _, kind, ids in rec.ran] == [("a",)]


# ---------------------------------------------------------------------------
# Pausing (Robert, Oct 9): all of an account's processing, or one producer's
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_paused_account_starts_nothing_and_asks_for_nothing() -> None:
    rec = Recorder()
    asked: list = []
    scan = MagicMock()
    s = _scheduler({"t1": FakeAccount()}, {"probe": [_item("p")], "vision": [_item("v")]}, rec,
                   asked=asked, held={*pause_targets(), PAUSE_SCANS}, scan=scan)
    s.tick()
    _settle(s)
    assert rec.ran == [] and asked == [] and not scan.called
    assert s.status("t1")["waiting"] == {}


@pytest.mark.fast
def test_a_paused_account_still_says_what_its_doing() -> None:
    written: list = []
    s = _scheduler({"t1": FakeAccount()}, {}, Recorder(), held=set(pause_targets()))
    s._write_status = lambda tenant_id, status: written.append(tenant_id)
    s.tick()
    assert written == ["t1"]


@pytest.mark.fast
def test_paused_scans_stop_only_scans() -> None:
    # Robert, Oct 9: scans can be paused on their own; the rest goes on.
    rec = Recorder()
    scan = MagicMock()
    s = _scheduler({"t1": FakeAccount()}, {"probe": [_item("p")], "clip": [_item("c")]}, rec,
                   held={"scans"}, scan=scan)
    s.tick()
    _settle(s)
    assert not scan.called
    assert sorted(kind for _, kind, _ in rec.ran) == ["clip", "probe"]


@pytest.mark.fast
def test_pausing_lets_running_jobs_finish_and_starts_nothing_more() -> None:
    rec = Recorder()
    rec.hold.clear()
    held: set = set()
    s = _scheduler({"t1": FakeAccount(vision=1)}, {"vision": [_item("a"), _item("b", "2026-10-02")]}, rec)
    s._on_hold = lambda tenant_id: _pauses(held)
    s.tick()  # a in hand, b waiting
    held.update(pause_targets())
    s.tick()
    assert s.status("t1")["waiting"] == {}
    rec.hold.set()
    _settle(s)
    s.tick()
    _settle(s)
    assert [ids for _, kind, ids in rec.ran] == [("a",)]


@pytest.mark.fast
def test_other_accounts_go_on_while_one_is_paused() -> None:
    rec = Recorder()
    s = _scheduler({"t1": FakeAccount("t1"), "t2": FakeAccount("t2")}, {"clip": [_item("c")]}, rec)
    s._on_hold = lambda tenant_id: _pauses(pause_targets() if tenant_id == "t1" else ())
    s.tick()
    _settle(s)
    assert [(t, kind) for t, kind, _ in rec.ran] == [("t2", "clip")]


@pytest.mark.fast
def test_a_paused_producer_starts_nothing_of_its_own_and_the_rest_go_on() -> None:
    rec = Recorder()
    asked: list = []
    s = _scheduler({"t1": FakeAccount()}, {"vision": [_item("v")], "redo_vision": [_item("old")],
                                           "ocr": [_item("o")], "clip": [_item("c")]}, rec,
                   asked=asked, held={"vision"})
    s.tick()
    _settle(s)
    assert sorted(kind for _, kind, _ in rec.ran) == ["clip", "ocr"]
    assert not {"vision", "redo_vision"} & {kind for _, kind, _ in asked}


@pytest.mark.fast
def test_resuming_hands_out_what_was_waiting_at_once() -> None:
    # Pausing let go of the waiting clips; resuming asks for them again
    # without waiting out the pause before the database is asked again.
    rec = Recorder()
    rec.hold.clear()
    held: set = set()
    s = _scheduler({"t1": FakeAccount(vision=1)}, {"vision": [_item("a"), _item("b", "2026-10-02")]}, rec)
    s._on_hold = lambda tenant_id: _pauses(held)
    s.tick()  # a in hand, b waiting (all of them known)
    held.add("vision")
    s.tick()
    rec.hold.set()
    _settle(s)
    held.clear()
    s.tick()
    _settle(s)
    assert [ids for _, kind, ids in rec.ran] == [("a",), ("b",)]


@pytest.mark.fast
def test_until_what_was_paused_can_be_read_nothing_starts() -> None:
    def unreadable(tenant_id):
        raise RuntimeError("database down")

    rec = Recorder()
    s = _scheduler({"t1": FakeAccount()}, {"clip": [_item("c")]}, rec)
    s._on_hold = unreadable
    s.tick()
    _settle(s)
    assert rec.ran == []


@pytest.mark.fast
def test_a_pause_that_came_and_went_while_listing_doesnt_hold_the_kind_back() -> None:
    # Paused after this refill read what's paused, resumed before the next:
    # the listing said "paused", and the kind is asked for again at once,
    # not after the wait an empty answer gets.
    rec = Recorder()
    answers = [None, [_item("a")]]

    def candidates(tenant_id, kind, libraries, skip=()):
        return answers.pop(0) if kind.name == "vision" and answers else []

    s = _scheduler({"t1": FakeAccount()}, {}, rec)
    s._candidates = candidates
    s.tick()
    s.tick()
    _settle(s)
    assert [ids for _, kind, ids in rec.ran if kind == "vision"] == [("a",)]


@pytest.mark.fast
def test_a_pause_stops_jobs_starting_while_a_slow_refill_is_still_running() -> None:
    # The refill can take a while (a machine check that times out); what's
    # paused is read on its own, every second or so, and the dispatcher
    # hands out nothing of it from then on.
    rec = Recorder()
    rec.hold.clear()
    release = threading.Event()  # a holds its slot until told, however slow the machine
    vision = rec.runner("vision")
    held: set = set()
    slow = threading.Event()
    slow.set()
    acct = FakeAccount(vision=1)
    acct.refresh = lambda: slow.wait(10)
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 1, "probe": 2, "render": 1, "gpu": 1, "scenes": 1},
                  candidates=lambda t, kind, libs, skip=(): [_item("a"), _item("b", "2026-10-02")]
                  if kind.name == "vision" else [],
                  runners={**rec.all(), "vision": lambda acct, job: (release.wait(30), vision(acct, job))[1]},
                  scan=MagicMock(), on_hold=lambda t: _pauses(held),
                  retry_requested=lambda t: None, write_status=lambda t, st: None)
    def vision_running() -> int:  # the scan job (instant here) may be in flight too: it isn't counted
        return s.dispatcher.status("t1")["running"].get("vision", 0)

    deadline = time.monotonic() + 5
    while vision_running() == 0 and time.monotonic() < deadline:  # a in hand, b waiting
        s.tick()
        time.sleep(0.02)
    assert vision_running() == 1
    deadline = time.monotonic() + 10
    slow.clear()
    refreshing = threading.Event()
    acct.refresh = lambda: (refreshing.set(), slow.wait(10))
    while not refreshing.is_set() and time.monotonic() < deadline:  # a refill, past its look at what's paused, hangs
        s.tick()
        time.sleep(0.02)
    assert refreshing.is_set()
    held.add("vision")
    deadline = time.monotonic() + 10
    while s.dispatcher.status("t1")["waiting"] and time.monotonic() < deadline:
        s.tick()
        time.sleep(0.05)
    assert not s.dispatcher.status("t1")["waiting"]
    rec.hold.set()
    release.set()
    for _ in range(20):
        s.tick()
        s.collect(timeout=0.05)
    slow.set()
    _settle(s)
    s.stop(grace=1)
    assert [ids for _, kind, ids in rec.ran if kind == "vision"] == [("a",)]


@pytest.mark.fast
def test_asking_to_try_failing_clips_again_lets_go_of_the_hour() -> None:
    # The scheduler keeps a clip that waited without saying why for an hour;
    # someone asking to try again shouldn't wait that out.
    rec = Recorder()
    asked: list[str | None] = [None]
    s = _scheduler({"t1": FakeAccount()}, {"clip": [_item("c")]}, rec)
    clip = s._runners["clip"]
    s._runners["clip"] = lambda acct, job: clip(acct, job) or list(job.asset_ids)  # it waits
    s._retry_requested = lambda tenant_id: asked[0]
    for _ in range(2):
        s.tick()
        _settle(s)
        s.dispatcher._all_known_at.clear()
    assert [ids for _, kind, ids in rec.ran if kind == "clip"] == [("c",)]  # held for the hour
    asked[0] = "2026-10-09T01:00:00"
    for _ in range(2):
        s.tick()
        _settle(s)
    assert [ids for _, kind, ids in rec.ran if kind == "clip"] == [("c",), ("c",)]


# ---------------------------------------------------------------------------
# Video work comes first on this machine's GPU; what's running, on screen
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_ai_machines_sharing_the_gpu_give_way_while_a_render_decodes() -> None:
    rec = Recorder()
    rec.hold.clear()
    acct = FakeAccount()
    s = Scheduler(lambda: {"t1": acct}, capacity={"render": 2, "scan": 0},
                  candidates=lambda t, k, libs, skip=(): [_item("r")] if k.name == "render" else [],
                  runners=rec.all(), scan=MagicMock(), inline_refill=True, gpu_decodes=1,
                  retry_requested=lambda t: None, write_status=lambda t, st: None)
    s.tick()
    assert acct.gpu_holds == [1]
    rec.hold.set()
    _settle(s)
    s.tick()
    assert acct.gpu_holds == [1, 0]


@pytest.mark.fast
def test_without_gpu_decoding_nothing_gives_way() -> None:
    rec = Recorder()
    acct = FakeAccount()
    s = Scheduler(lambda: {"t1": acct}, capacity={"render": 1, "scan": 0},
                  candidates=lambda t, k, libs, skip=(): [_item("r")] if k.name == "render" else [],
                  runners=rec.all(), scan=MagicMock(), inline_refill=True, gpu_decodes=0,
                  retry_requested=lambda t: None, write_status=lambda t, st: None)
    s.tick()
    _settle(s)
    assert acct.gpu_holds == [0]


@pytest.mark.fast
def test_what_the_scheduler_is_doing_is_written_for_processing() -> None:
    rec = Recorder()
    rec.hold.clear()
    written: list[tuple[str, dict]] = []
    s = Scheduler(lambda: {"t1": FakeAccount(vision=1)}, capacity={"scan": 0},
                  candidates=lambda t, k, libs, skip=(): [_item("a"), _item("b", "2026-10-02")] if k.name == "vision" else [],
                  runners=rec.all(), scan=MagicMock(), inline_refill=True,
                  retry_requested=lambda t: None,
                  write_status=lambda t, st: written.append((t, st)))
    s.tick()
    s.tick()  # written every few seconds, not every tick
    assert len(written) == 1
    s._status_at.clear()
    s.tick()
    tenant, status = written[-1]
    assert tenant == "t1" and status["running"] == {"vision": 1} and status["waiting"]["vision"] >= 1
    assert status["pools"]["vision"] == [1, 1] and "at" in status and status["gpu_hold"] == 0
    rec.hold.set()
    _settle(s)


@pytest.mark.fast
def test_the_log_has_no_line_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 2: httpx says every request at INFO, several per clip.
    import logging

    from src.server.scheduler.service import configure_logging

    monkeypatch.setenv("LOG_LEVEL", "info")
    levels = {name: logging.getLogger(name).level for name in ("", "httpx", "httpcore", "pyvips")}
    try:
        configure_logging()
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert logging.getLogger("src.server.scheduler.service").isEnabledFor(logging.INFO)
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.fast
def test_a_listing_that_failed_is_tried_again_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 2: it waited the full five minutes.
    from src.server.scheduler import service

    clock = Clock()
    tries: list[float] = []

    def failing_list(self) -> None:
        tries.append(clock.now)
        raise ConnectionError("the control plane isn't answering")

    monkeypatch.setattr(service.Accounts, "_list", failing_list)
    accounts = service.Accounts(MagicMock(), url="http://api", clock=clock)
    assert accounts() == {}
    clock.now += service.Accounts.LIST_RETRY_SEC + 1
    accounts()
    assert len(tries) == 2


@pytest.mark.fast
def test_a_new_account_is_worked_for_within_half_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Mac's first-run setup makes its account after the scheduler started:
    # at five minutes between listings, its first library waited that long.
    from src.server.scheduler import service

    clock = Clock()
    listings: list[float] = []
    monkeypatch.setattr(service.Accounts, "_list", lambda self: listings.append(clock.now))
    accounts = service.Accounts(MagicMock(), url="http://api", clock=clock)
    accounts()
    clock.now += 29
    accounts()
    assert len(listings) == 1
    clock.now += 2
    accounts()
    assert len(listings) == 2


@pytest.mark.fast
def test_failures_are_sent_off_the_dispatching_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 2: a slow server held up every tick (up to 120 s).
    from src.server.scheduler import service

    monkeypatch.setattr(service, "FLUSH_EVERY_SEC", 0)
    gate = threading.Event()
    acct = FakeAccount()
    acct.failures.flush.side_effect = lambda: gate.wait(60)
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, candidates=lambda *a, **kw: [],
                  runners=Recorder().all(), scan=MagicMock())
    started = time.monotonic()
    s.tick()
    assert time.monotonic() - started < 30, "the flush's minute wasn't waited for"
    gate.set()
    s.stop()


@pytest.mark.fast
def test_losing_the_database_lock_stops_the_scheduler() -> None:
    # Review round 2: after a Postgres restart the lock is gone; a second
    # scheduler could take it. This one stops (systemd starts it again).
    rec = Recorder()
    s = _scheduler({"t1": FakeAccount()}, {}, rec)
    stop = threading.Event()
    checks: list[int] = []

    def holds() -> bool:
        checks.append(1)
        return len(checks) < 2

    t = threading.Thread(target=run, kwargs={"stop": stop, "scheduler": s, "tick_sec": 0.01, "holds_lock": holds,
                                             "lock_check_sec": 0}, daemon=True)
    t.start()
    try:
        t.join(5)
        assert not t.is_alive() and len(checks) == 2
    finally:
        stop.set()


@pytest.mark.fast
def test_the_program_exits_without_waiting_on_jobs_past_their_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 2: job threads still running (a render past the 25 s
    # grace) were joined at exit, until systemd's SIGKILL at 40 s.
    from src.server.scheduler import service

    exited: list[int] = []
    monkeypatch.setattr(service, "main", lambda: 3)
    monkeypatch.setattr(service.os, "_exit", exited.append)
    service.entry()
    assert exited == [3]

    def boom() -> int:
        raise RuntimeError("a surprise")

    monkeypatch.setattr(service, "main", boom)
    with pytest.raises(RuntimeError):
        service.entry()
    assert exited == [3, 1]  # out at once all the same


@pytest.mark.fast
def test_running_the_module_runs_the_program() -> None:
    # The unit's ExecStart is python -m src.server.scheduler.
    import runpy
    from unittest.mock import patch

    with patch("src.server.scheduler.service.entry") as entry:
        runpy.run_module("src.server.scheduler.__main__", run_name="__main__")
    entry.assert_called_once_with()


@pytest.mark.fast
def test_a_lock_connection_that_doesnt_answer_counts_as_lost() -> None:
    # Review round 3: a dead TCP connection can block SELECT 1 for many minutes.
    from src.server.scheduler.service import _still_holds

    hung = MagicMock()
    hung.execute.side_effect = lambda *a, **kw: threading.Event().wait(60)
    started = time.monotonic()
    assert _still_holds(hung, timeout=0.1) is False
    assert time.monotonic() - started < 30, "the hung minute wasn't waited for"
    assert _still_holds(MagicMock(), timeout=1) is True
    gone = MagicMock()
    gone.execute.side_effect = ConnectionError("server closed the connection")
    assert _still_holds(gone, timeout=1) is False


@pytest.mark.fast
def test_the_lock_is_checked_before_every_tick() -> None:
    # A scheduler that lost the lock (a dropped connection) mustn't hand out
    # jobs for up to a minute beside the one that took it.
    from src.server.scheduler import service

    rec = Recorder()
    s = _scheduler({"t1": FakeAccount()}, {}, rec)
    stop = threading.Event()
    seen: list[str] = []
    s.tick = lambda: (seen.append("tick"), 0)[1]  # type: ignore[method-assign]

    def holds() -> bool:
        seen.append("check")
        return seen.count("check") < 4

    assert service.LOCK_CHECK_SEC == 0
    run(stop=stop, scheduler=s, tick_sec=0.01, holds_lock=holds)
    assert seen == ["check", "tick"] * 3 + ["check"]


@pytest.mark.slow
def test_the_lock_check_asks_postgres_whether_the_lock_is_held() -> None:
    from sqlalchemy import create_engine, text
    from testcontainers.postgres import PostgresContainer

    from src.server.scheduler.service import LOCK_ID, _still_holds
    from tests.conftest import PG_IMAGE, _ensure_psycopg2

    with PostgresContainer(PG_IMAGE) as pg:
        engine = create_engine(_ensure_psycopg2(pg.get_connection_url()))
        try:
            with engine.connect() as held, engine.connect() as other:
                assert held.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}).scalar()
                held.commit()
                assert _still_holds(held) is True
                assert _still_holds(other) is False  # a connection without it
                held.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
                held.commit()
                assert _still_holds(held) is False
        finally:
            engine.dispose()


@pytest.mark.fast
def test_one_failure_flush_at_a_time_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.scheduler import service

    monkeypatch.setattr(service, "FLUSH_EVERY_SEC", 0)
    gate = threading.Event()
    acct = FakeAccount()
    acct.failures.flush.side_effect = lambda: gate.wait(60)
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, candidates=lambda *a, **kw: [],
                  runners=Recorder().all(), scan=MagicMock())
    for _ in range(3):
        s.tick()
    gate.set()
    s.stop()
    assert acct.failures.flush.call_count == 1


@pytest.mark.fast
def test_a_save_the_server_refuses_is_the_clips_failure() -> None:
    # Review round 3: a result the API can't take (400 "Invalid SRT format")
    # was made again every hour, never shown as failing.
    from src.client.cli.client import LumiverbAPIError

    def refused(acct, job):
        raise LumiverbAPIError("bad_request", "Invalid SRT format", 400)

    def gone(acct, job):
        raise LumiverbAPIError("not_found", "Asset not found", 404)

    acct = FakeAccount()
    clock = Clock()
    due = {"transcript": [_item("t")], "render": [_item("r")]}
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0, "render": 1},
                  candidates=lambda t, k, libs, skip=(): [i for i in due.get(k.name, []) if i["asset_id"] not in skip],
                  runners={**Recorder().all(), "transcript": refused, "render": gone}, scan=MagicMock(),
                  inline_refill=True, clock=clock)
    s.tick()
    _settle(s)
    acct.failures.add.assert_called_once()
    assert acct.failures.add.call_args.args[:2] == ("transcript", "t")
    assert "Invalid SRT format" in str(acct.failures.add.call_args.args[2])
    clock.now += s.dispatcher.settle_after + 1
    assert s.dispatcher.held("t1", "transcript") == []  # reported: the server says when to try again
    assert s.dispatcher.held("t1", "render") == ["r"]  # gone meanwhile: it waits


@pytest.mark.fast
def test_a_redo_is_listed_with_its_settings_and_the_account_follows_them() -> None:
    rec = Recorder()
    acct = FakeAccount()
    s = _scheduler({"t1": acct}, {"redo_vision": [{**_item("old"), "settings_hash": "new"}],
                                  "clip": [_item("c")]}, rec)
    s.tick()
    _settle(s)
    assert acct.followed == [("vision", "new")]  # before its clips are handed out


@pytest.mark.fast
def test_each_accounts_pace_starts_from_its_last_status_after_a_restart() -> None:
    s = Scheduler(lambda: {"t1": FakeAccount()}, capacity={"scan": 0}, candidates=lambda *a, **kw: [],
                  runners=Recorder().all(), scan=MagicMock(), inline_refill=True, retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None,
                  last_status=lambda tenant_id: {"pace": {"render": 0.4, "vision": 9.0}})
    s.tick()
    assert s.status("t1")["pace"] == {"render": 0.4, "vision": 9.0}


@pytest.mark.fast
def test_a_kind_learns_its_pace_only_from_the_clips_its_jobs_made() -> None:
    from src.processing.failure_report import FailureReport

    acct = FakeAccount()
    acct.failures = FailureReport(None)

    def vision(account, job):
        account.failures.add("vision", "bad", "the model said no")
        return None

    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, inline_refill=True, candidates=lambda t, k, libs, skip=(): [i for i in [_item("bad")] if i["asset_id"] not in skip]
                  if k.name == "vision" else [],
                  runners={**Recorder().all(), "vision": vision}, scan=MagicMock(),
                  retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    s.tick()
    _settle(s)
    s.collect(timeout=1)
    assert s.status("t1")["pace"] == {}  # a failure isn't work done


@pytest.mark.fast
def test_the_status_names_libraries_whose_storage_work_waits() -> None:
    written: list[dict] = []
    acct = FakeAccount(libraries=("lib_1", "lib_2"), reachable=("lib_1",))
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, candidates=lambda *a, **kw: [], inline_refill=True,
                  runners=Recorder().all(), scan=MagicMock(), retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: written.append(status))
    s.tick()
    assert written and written[-1]["storage_waits"] == ["lib_2"] and "unreachable" not in written[-1]


@pytest.mark.fast
def test_the_status_keeps_each_librarys_looks_and_a_restart_starts_from_them() -> None:
    record = {"lib_1": {"seen_at": "2026-10-09T10:00:00+00:00", "away_since": None, "checked": True}}
    written: list[dict] = []
    acct = FakeAccount()
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, candidates=lambda *a, **kw: [], inline_refill=True,
                  runners=Recorder().all(), scan=MagicMock(), retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: written.append(status),
                  last_status=lambda tenant_id: {"storage": record})
    s.tick()
    s.tick()
    assert acct.seeded == [record]  # once
    assert written[0]["storage"] == {"lib_1": {"seen_at": "2026-10-09T10:00:00+00:00", "away_since": None,
                                               "checked": False}}


@pytest.mark.fast
def test_a_restart_without_a_record_starts_with_none() -> None:
    acct = FakeAccount()
    written: list[dict] = []
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, candidates=lambda *a, **kw: [], inline_refill=True,
                  runners=Recorder().all(), scan=MagicMock(), retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: written.append(status),
                  last_status=lambda tenant_id: None)
    s.tick()
    assert acct.seeded == [None] and written[0]["storage"] == {}


# ---------------------------------------------------------------------------
# Crashes are charged after CRASHES_BEFORE_CHARGE in a row (review, Oct 9)
# ---------------------------------------------------------------------------


class CrashCounter:
    """lineage.note_crashes, in memory: (tenant, artifact, asset) → crashes in a row."""

    def __init__(self) -> None:
        from src.server.repository.lineage import CRASHES_BEFORE_CHARGE

        self.limit = CRASHES_BEFORE_CHARGE
        self.counts: dict[tuple[str, str, str], int] = {}
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, tenant_id: str, artifact: str, asset_ids: list[str], error: str) -> list[str]:
        self.calls.append((artifact, list(asset_ids)))
        reached = []
        for i in asset_ids:
            key = (tenant_id, artifact, i)
            self.counts[key] = self.counts.get(key, 0) + 1
            if self.counts[key] >= self.limit:
                reached.append(i)
                del self.counts[key]
        return reached


def _crash_scheduler(runner, counter: CrashCounter, kind: str = "ocr") -> Scheduler:
    return Scheduler(lambda: {}, capacity={"scan": 0}, candidates=lambda *a, **kw: [], inline_refill=True,
                     runners={**Recorder().all(), kind: runner}, scan=MagicMock(), crashed=counter)


def _job(kind: str, *ids: str):
    from src.server.scheduler.dispatch import Job

    return Job("t1", kind, 3, tuple(_item(i) for i in ids))


@pytest.mark.fast
def test_a_clip_that_keeps_crashing_is_charged_after_so_many_in_a_row() -> None:
    # Review (Oct 9): any surprise returned every clip as waiting: held an
    # hour, uncharged, tried forever and never shown as failing.
    def boom(acct, job):
        raise RuntimeError("segfault in the model")

    counter = CrashCounter()
    s = _crash_scheduler(boom, counter)
    acct = FakeAccount()
    for _ in range(counter.limit - 1):
        assert s._run(acct, _job("ocr", "c")) == ["c"]  # waits, uncharged
        acct.failures.add.assert_not_called()
    assert s._run(acct, _job("ocr", "c")) is None  # charged: the server's back-off and giving up apply
    acct.failures.add.assert_called_once()
    artifact, asset_id, error = acct.failures.add.call_args.args
    assert (artifact, asset_id) == ("ocr", "c") and "segfault in the model" in error


@pytest.mark.fast
def test_trouble_reaching_the_server_charges_nothing_and_isnt_counted() -> None:
    import httpx

    from src.client.cli.client import LumiverbAPIError

    errors = iter([LumiverbAPIError("database_unavailable", "away", 503), httpx.ConnectError("refused"),
                   LumiverbAPIError("bad_gateway", "proxy", 502)])

    def away(acct, job):
        raise next(errors)

    counter = CrashCounter()
    s = _crash_scheduler(away, counter)
    acct = FakeAccount()
    for _ in range(3):
        assert s._run(acct, _job("ocr", "c")) == ["c"]
    assert counter.calls == [] and not acct.failures.add.called


@pytest.mark.fast
def test_a_server_error_counts_as_a_crash() -> None:
    from src.client.cli.client import LumiverbAPIError

    def internal(acct, job):
        raise LumiverbAPIError("internal", "Internal Server Error", 500)

    counter = CrashCounter()
    s = _crash_scheduler(internal, counter, kind="vision")
    assert s._run(FakeAccount(), _job("vision", "v")) == ["v"]
    assert counter.calls == [("vision", ["v"])]


@pytest.mark.fast
def test_only_the_clips_that_crashed_are_counted() -> None:
    from src.producers.runner import Crashed

    def faces(acct, job):
        raise Crashed(["bad"], "face detection's process died or hung", waiting=["changed"])

    counter = CrashCounter()
    s = _crash_scheduler(faces, counter, kind="faces")
    assert sorted(s._run(FakeAccount(), _job("faces", "ok", "bad", "changed"))) == ["bad", "changed"]
    assert counter.calls == [("faces", ["bad"])]


@pytest.mark.fast
def test_a_redo_counts_crashes_against_its_artifact() -> None:
    def boom(acct, job):
        raise RuntimeError("boom")

    counter = CrashCounter()
    s = _crash_scheduler(boom, counter, kind="vision")
    s._run(FakeAccount(), _job("redo_vision", "v"))
    assert counter.calls == [("vision", ["v"])]


@pytest.mark.fast
def test_counting_crashes_failing_charges_nothing() -> None:
    def boom(acct, job):
        raise RuntimeError("boom")

    def broken(*args):
        raise RuntimeError("database away")

    s = Scheduler(lambda: {}, capacity={"scan": 0}, candidates=lambda *a, **kw: [], inline_refill=True,
                  runners={**Recorder().all(), "ocr": boom}, scan=MagicMock(), crashed=broken)
    acct = FakeAccount()
    assert s._run(acct, _job("ocr", "c")) == ["c"]
    acct.failures.add.assert_not_called()


@pytest.mark.fast
def test_a_job_stopped_mid_save_counts_as_not_tried() -> None:
    from src.producers.runner import NOT_TRIED, Stopped

    def stopped(acct, job):
        raise Stopped("/v1/assets/x/video-facet")

    counter = CrashCounter()
    s = _crash_scheduler(stopped, counter, kind="probe")
    assert s._run(FakeAccount(), _job("probe", "p")) == NOT_TRIED
    assert counter.calls == []


@pytest.mark.fast
def test_the_scheduler_sends_no_nul_in_what_it_saves() -> None:
    # Postgres takes no NUL in text: a description with one was refused with
    # a 500, every hour, forever. Every save goes through the runner's client
    # (src/producers/runner.py), the scan pass's too.
    from src.producers.runner import Saving

    sent: list = []
    inner = MagicMock()
    for method in ("post", "put", "patch"):
        setattr(inner, method, lambda path, _m=method, **kw: sent.append((_m, kw.get("json"))))
    client = Saving(inner)
    body = {"items": [{"description": "a\x00dog", "tags": ["be\x00ach"], "k\x00": 1}], "n": 2}
    client.post("/v1/assets/batch-vision", json=body)
    client.put("/x", json="a\x00")
    client.patch("/y", json=["\x00"])
    assert sent == [("post", {"items": [{"description": "adog", "tags": ["beach"], "k": 1}], "n": 2}),
                    ("put", "a"), ("patch", [""])]


# ---------------------------------------------------------------------------
# A pause stops its own work only (Robert, Oct 12). Whether a library's
# storage can be read, and a new version's hold, are the scheduler's to find
# out, never a job's: pausing Scans stops only looking for new and changed
# files. Pause all touches no library drive, the storage look included.
# ---------------------------------------------------------------------------


class Machines:
    """An account's machines for one AI job (its guard): one online, a request at a time."""

    model = "a-model"
    down = False
    error = None

    def __init__(self) -> None:
        self.pool = MagicMock()

    def check(self) -> bool:
        return True

    def capacity(self) -> int:
        return 1


class Api:
    """The API as an account reads it (the registry's settings, its
    libraries) and its jobs save through it (each kind's, in events)."""

    def __init__(self, libraries: list[dict], events: list) -> None:
        self.libraries = libraries
        self.events = events

    def get(self, path: str, **kw):
        body = self.libraries if path == "/v1/libraries" else {"producers": []}
        return SimpleNamespace(json=lambda: body)

    def post(self, path: str, **kw):
        if path.startswith("/made/"):
            self.events.append(("made", path.removeprefix("/made/")))
        if path == "/v1/readers/check-in":  # the libraries this server reads, as the API answers
            return SimpleNamespace(json=lambda: {"libraries": [{"mark": 0, "scan_now": False, **lib}
                                                               for lib in self.libraries if read_by_server(lib)],
                                                 "gone": [], "check_in_every_sec": 60})
        return None

    def close(self) -> None:
        pass


class Storage:
    """Each library's storage as src.processing.roots.reachable_root finds it:
    reachable at root unless gone. Every look is noted, in events too."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.gone: set[str] = set()
        self.looks: list[tuple[str, bool]] = []
        self.events: list[tuple[str, str]] = []

    def __call__(self, library: dict, *, require_entries: bool = False, **kw) -> Path | None:
        self.looks.append((library["library_id"], require_entries))
        self.events.append(("look", library["library_id"]))
        return None if library["library_id"] in self.gone else self.root

    def originals(self, there: bool) -> None:
        """Every due clip's original on the storage, or none (an unmounted share)."""
        for kind in QUEUED:
            path = self.root / f"{kind.name}-clip"
            if there:
                path.write_bytes(b"x")
            else:
                path.unlink(missing_ok=True)


class ReadsOriginals(Work):
    """A producer reading each clip's original, as probe, capture and render
    do: through its inputs (Work.inputs.original), which go by the latest
    look and note storage found gone since. One that reads the storage."""

    artifact = "probe"

    def make(self, clip: dict) -> str:
        with self.inputs.original(clip):
            return clip["asset_id"]

    def save(self, client, made) -> None:
        client.post(f"/made/{self.job.kind}")


LIBRARY = {"library_id": "lib_1", "name": "Footage", "root_path": "/Volumes/media-01/Footage"}
STORAGE_KINDS = {k.name for k in QUEUED if k.storage}


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Storage:
    """lib_1's storage, reachable, with every due clip's original on it."""
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    (tmp_path / "Footage").mkdir()
    s = Storage(tmp_path / "Footage")
    s.originals(there=True)
    monkeypatch.setattr("src.processing.roots.reachable_root", s)
    return s


class Fresh:
    """A scheduler just started, with an account made as Accounts makes one
    (nothing looked at yet), lib_1's storage reachable and a clip due for
    every kind; a slot in every pool. What's offered is noted (offered),
    and what each job made (made)."""

    def __init__(self, storage: Storage, *, held=(), everything: bool = False) -> None:
        from src.producers import pools
        from src.server.scheduler.account import Account
        from src.server.scheduler.scans import ScanState

        self.storage = storage
        self.clock = Clock()
        self.acct = Account("t1", Api([LIBRARY], storage.events), ScanState(), clock=self.clock)
        self.acct.guards = {job: Machines() for job in self.acct.guards}
        self.paused = _pauses(held, everything=everything)
        self.scan = MagicMock()
        self.offered: list[str] = []
        due = {k.name: [_item(f"{k.name}-clip")] for k in QUEUED}

        def candidates(tenant_id, kind, libraries, skip=()):
            among = (LIBRARY["library_id"],) if libraries is None else libraries  # None: every active library
            return [i for i in due[kind.name] if i["library_id"] in among and i["asset_id"] not in skip]

        def makes(acct, job) -> None:
            acct.client.post(f"/made/{job.kind}")

        self.s = Scheduler(lambda: {"t1": self.acct}, capacity={p.name: 1 for p in pools().values() if not p.per_account},
                           candidates=candidates, scan=self.scan, inline_refill=True, clock=self.clock,
                           runners={k.base: partial(run_work, ReadsOriginals) if k.storage else makes for k in QUEUED},
                           on_hold=lambda tenant_id: self.paused, retry_requested=lambda tenant_id: None,
                           write_status=lambda tenant_id, status: None)
        offer = self.s.dispatcher.offer

        def offered(tenant_id, kind, items, **kw) -> None:
            if items:
                self.offered.append(kind)
            offer(tenant_id, kind, items, **kw)

        self.s.dispatcher.offer = offered

    def tick(self, seconds: float = 1.0) -> None:
        """So many seconds later, a tick; its jobs finish."""
        self.clock.now += seconds
        self.s.tick()
        _settle(self.s)

    def made(self, since: int = 0) -> list[str]:
        return [kind for what, kind in self.storage.events[since:] if what == "made"]


def _own_kinds(switch: str) -> set[str]:
    """The kinds a switch's pause stops: every library's scans, the scan; a
    producer's its kind and its redo; Upkeep none of the scheduler's."""
    if switch == PAUSE_SCANS:
        return {"scan"}
    return {k.name for k in KINDS.values() if k.flag and k.artifact == switch}


@pytest.mark.fast
def test_with_scans_paused_a_restarted_scheduler_still_reads_originals(storage: Storage) -> None:
    # The live Mac, Oct 12: Scans paused, and after a restart no library was
    # ever looked at, so analysis proxies (and all that's made from them)
    # stopped for hours, and nothing on Processing said why.
    f = Fresh(storage, held={PAUSE_SCANS})
    f.tick()
    assert storage.looks == [("lib_1", True)]  # one look, as the scan pass looks
    assert {k.name for k in QUEUED if k.storage and not k.redo} <= set(f.made())
    assert "scan" not in f.offered and not f.scan.called


@pytest.mark.fast
def test_the_first_look_after_a_restart_keeps_when_the_storage_was_last_seen(storage: Storage) -> None:
    # The look comes after the record from before the restart is read: a
    # library unreachable now still says when it was last seen.
    f = Fresh(storage)
    record = {"lib_1": {"seen_at": "2026-10-12T09:00:00+00:00", "away_since": None, "checked": True}}
    f.s._last_status = lambda tenant_id: {"storage": record}
    storage.gone.add("lib_1")
    f.tick()
    seen = f.acct.storage_seen()["lib_1"]
    assert seen["seen_at"] == "2026-10-12T09:00:00+00:00" and seen["away_since"] and seen["checked"]
    assert not STORAGE_KINDS & set(f.offered)


@pytest.mark.fast
@pytest.mark.parametrize("switch", [*pause_targets(), PAUSE_SCANS])
def test_a_pause_stops_its_own_work_only(storage: Storage, switch: str) -> None:
    # Every switch there is (one added later too), and every library's scans:
    # paused alone on a fresh start, every other kind still gets its due
    # work offered, storage work included. The guard against the next
    # hidden coupling.
    f = Fresh(storage, held={switch})
    f.tick()
    assert set(f.offered) == set(KINDS) - _own_kinds(switch)


@pytest.mark.fast
def test_storage_a_job_found_gone_is_used_again_after_the_next_look_with_scans_paused(storage: Storage) -> None:
    # One blip used to stop a library's storage work for good while Scans
    # were paused: only the scan's look said it was back.
    from src.server.scheduler.account import LOOK_EVERY_SEC

    f = Fresh(storage, held={PAUSE_SCANS})
    f.tick()  # looked at; the first of each storage kind made, their redos waiting
    storage.gone.add("lib_1")
    storage.originals(there=False)  # the share went away
    f.tick()  # the redos find it gone
    assert f.acct.library_ids(storage=True) == [] and f.acct.storage_seen()["lib_1"]["away_since"]
    storage.gone.clear()
    storage.originals(there=True)  # and came back
    looks, mark = len(storage.looks), len(storage.events)
    f.tick()
    assert len(storage.looks) == looks and not STORAGE_KINDS & set(f.made(mark))  # until the next look
    f.tick(LOOK_EVERY_SEC)
    assert len(storage.looks) == looks + 1 and f.acct.library_ids(storage=True) == ["lib_1"]
    assert {k.name for k in QUEUED if k.storage and k.redo} <= set(f.made(mark))


@pytest.mark.fast
def test_while_all_processing_is_paused_nothing_touches_the_drives_and_resuming_looks_first(
        storage: Storage) -> None:
    # Robert, Oct 12: Pause all is for working on the hardware. Nothing
    # reads the library drives then, the scheduler's look included; on
    # resuming, a look comes before any storage work.
    from src.server.scheduler.account import LOOK_EVERY_SEC

    f = Fresh(storage, everything=True)
    for _ in range(3):
        f.tick(LOOK_EVERY_SEC)
    assert storage.looks == [] and f.made() == [] and not f.scan.called
    for _ in range(2):  # resumed on a fresh start, and again after a long pause
        f.paused = _pauses()
        mark = len(storage.events)
        f.tick()
        after = storage.events[mark:]
        assert after[0] == ("look", "lib_1") and STORAGE_KINDS & set(f.made(mark))
        f.paused = _pauses(everything=True)
        looks = len(storage.looks)
        for _ in range(4):
            f.tick(LOOK_EVERY_SEC)
        assert len(storage.looks) == looks


@pytest.mark.fast
def test_a_new_versions_hold_is_looked_for_whatever_is_paused_before_any_redo_is_listed() -> None:
    # Oct 12: the hold was made only when a redo was listed, so not while
    # the producer (or the storage its redo reads) was held, and Processing
    # couldn't show it.
    from src.server.scheduler.service import NEW_VERSIONS_EVERY_SEC

    clock = Clock()
    asked: list[tuple[str, str]] = []
    looked_for: list = [None]

    def candidates(tenant_id, kind, libraries, skip=()):
        asked.append(("listed", kind.name))
        return []

    def new_versions(tenant_id) -> None:
        asked.append(("new versions", tenant_id))
        if looked_for[0] is not None:
            raise looked_for[0]

    s = Scheduler(lambda: {"t1": FakeAccount()}, capacity={"scan": 0}, candidates=candidates, inline_refill=True,
                  runners=Recorder().all(), scan=MagicMock(), clock=clock, new_versions=new_versions,
                  on_hold=lambda tenant_id: _pauses({"vision"}),
                  retry_requested=lambda tenant_id: None, write_status=lambda tenant_id, status: None)
    s.tick()
    assert asked[0] == ("new versions", "t1")  # before anything is listed
    assert ("listed", "redo_vision") not in asked  # vision is paused; its hold is still looked for
    asked.clear()
    clock.now += NEW_VERSIONS_EVERY_SEC / 2
    s.tick()
    assert ("new versions", "t1") not in asked  # once a minute
    clock.now += NEW_VERSIONS_EVERY_SEC / 2
    looked_for[0] = RuntimeError("database away")
    s.tick()
    s.tick()
    assert asked.count(("new versions", "t1")) == 2  # tried again at the next refill
    s._on_hold = lambda tenant_id: _pauses(everything=True)
    looked_for[0] = None
    asked.clear()
    s.tick()
    assert asked == [("new versions", "t1")]  # only the database is asked: Pause all doesn't stop it
