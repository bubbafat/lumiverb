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
        self.settings_ready = True
        self.stopping = threading.Event()

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
    def candidates(tenant_id, kind, libraries, skip=()):
        if asked is not None:
            asked.append((tenant_id, kind.name, tuple(libraries)))
        return [i for i in due.get(kind.name, []) if i["library_id"] in libraries and i["asset_id"] not in skip]

    return Scheduler(lambda: accounts, capacity=capacity or {"scan": 1, "probe": 2, "render": 1, "gpu": 1,
                                                            "scenes": 1},
                     candidates=candidates, runners=recorder.all(), scan=scan or MagicMock(), inline_refill=True)


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
                  runners=runners, scan=MagicMock(), inline_refill=True)
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
    from src.server.scheduler.runners import NOT_TRIED

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
                  if k.name == "probe" else [], runners=runners, scan=MagicMock(), inline_refill=True)
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
                  runners=rec.all(), scan=MagicMock(), inline_refill=True)
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
                  candidates=candidates, runners=rec.all(), scan=MagicMock(), inline_refill=True)
    s.tick()
    _settle(s)
    assert [kind for _, kind, _ in rec.ran] == ["clip"]


@pytest.mark.fast
def test_refills_happen_off_the_dispatching_thread() -> None:
    # A slow database or AI machine check doesn't hold up starting jobs.
    rec = Recorder()
    gate = threading.Event()
    acct = FakeAccount()
    acct.refresh = lambda: gate.wait(5)
    s = Scheduler(lambda: {"t1": acct}, capacity={"gpu": 1, "scan": 0},
                  candidates=lambda t, k, libs, skip=(): [_item("c")] if k.name == "clip" else [],
                  runners=rec.all(), scan=MagicMock())
    start = time.monotonic()
    s.tick()
    assert time.monotonic() - start < 1
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
def test_failures_are_sent_off_the_dispatching_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 2: a slow server held up every tick (up to 120 s).
    from src.server.scheduler import service

    monkeypatch.setattr(service, "FLUSH_EVERY_SEC", 0)
    gate = threading.Event()
    acct = FakeAccount()
    acct.failures.flush.side_effect = lambda: gate.wait(5)
    s = Scheduler(lambda: {"t1": acct}, capacity={"scan": 0}, candidates=lambda *a, **kw: [],
                  runners=Recorder().all(), scan=MagicMock())
    started = time.monotonic()
    s.tick()
    assert time.monotonic() - started < 1
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
    hung.execute.side_effect = lambda *a, **kw: threading.Event().wait(5)
    started = time.monotonic()
    assert _still_holds(hung, timeout=0.1) is False
    assert time.monotonic() - started < 2
    assert _still_holds(MagicMock(), timeout=1) is True
    gone = MagicMock()
    gone.execute.side_effect = ConnectionError("server closed the connection")
    assert _still_holds(gone, timeout=1) is False


@pytest.mark.fast
def test_one_failure_flush_at_a_time_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.scheduler import service

    monkeypatch.setattr(service, "FLUSH_EVERY_SEC", 0)
    gate = threading.Event()
    acct = FakeAccount()
    acct.failures.flush.side_effect = lambda: gate.wait(5)
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
