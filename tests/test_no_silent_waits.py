# ruff: noqa: F811 — pytest fixtures are named as parameters
"""No silent waits (Robert, Oct 12): whenever the scheduler holds due work
back, Settings → Processing says why.

On Robert's Mac analysis proxies stopped for hours: work was due, nothing
ran, and the Processing page gave no reason. Every reason due work can wait
has a code (src/shared/waits.py), and GET /v1/producers lists the codes
holding some of each producer's work, with how many clips
(src/server/producer_waits.py); the web says each in a sentence
(src/ui/web/src/lib/waits.ts).

The invariant is held structurally: for every registered reason there's a
gate here (a test fails for a reason without one), and each gate is run
through the real scheduler (its refill, its database queries, its
dispatcher and the real runner) against a real database. With the gate
closed, the producer's due clip isn't handed out and its row names the
reason; opened, the clip is handed out and the reason is gone. The web's
list of reasons is held to the registry too, and vitest holds the web's
sentences to that list.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from src.producers.runner import Work
from src.producers.runner import run as run_work
from src.shared.producers import PRODUCERS
from src.shared.waits import HELD, REASONS, SCAN, Waiting
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _library
from tests.test_scheduler_service import FakeAccount

ROOT = Path(__file__).resolve().parent.parent
WEB_REASONS = ROOT / "src" / "ui" / "web" / "src" / "lib" / "waits.ts"


# ---------------------------------------------------------------------------
# One list of reasons: the server's, the web's
# ---------------------------------------------------------------------------


def _web_reasons() -> list[str]:
    """WAIT_REASONS as the web declares it (lib/waits.ts): the codes it has a sentence for."""
    found = re.search(r"export const WAIT_REASONS = \[(.*?)\] as const;", WEB_REASONS.read_text(), re.S)
    assert found, f"{WEB_REASONS} declares WAIT_REASONS"
    return re.findall(r'"([a-z_]+)"', found.group(1))


@pytest.mark.fast
def test_the_web_says_every_reason_the_server_sends_in_its_order():
    # vitest (lib/waits.test.ts) holds WAIT_SENTENCES to WAIT_REASONS: a reason
    # added here without a sentence there fails one or the other.
    assert _web_reasons() == list(REASONS)


@pytest.mark.fast
def test_every_reason_has_a_gate_tested_here():
    assert set(GATES) == set(REASONS)


# ---------------------------------------------------------------------------
# The scheduler keeps why it holds clips back, through the real runner
# ---------------------------------------------------------------------------


class Account(FakeAccount):
    """The scheduler's account as a job sees it: the library's storage at
    root (a folder), a client that saves nothing, and storage looks a test
    sets (what a restart's record would say is left alone)."""

    def __init__(self, tenant_id: str, root: Path, **kw: Any) -> None:
        super().__init__(tenant_id, **kw)
        self.client = MagicMock()
        self._root = root
        self.out_of_memory = False

    def root(self, library_id: str) -> Path | None:
        return self._root if library_id in self.reachable else None

    def storage_gone(self, library_id: str) -> bool:
        return library_id not in self.reachable

    def unreachable(self, library_id: str) -> None:
        pass

    def seed_storage(self, record) -> None:
        pass


class Reads(Work):
    """Reads each clip's original, as every producer working on the
    originals does (Work.original): a file missing from storage that's
    there waits for the scan. The GPU running out of memory is no clip's."""

    artifact = "capture"

    def make(self, clip: dict) -> None:
        self.original(clip)
        if self.acct.out_of_memory:
            raise RuntimeError("CUDA out of memory")
        return None  # made (nothing to save here)


@pytest.mark.fast
def test_a_job_says_why_each_clip_waits(tmp_path):
    from src.server.scheduler.dispatch import Job

    acct = Account("t1", tmp_path, libraries=("lib_1",), reachable=("lib_1",))
    (tmp_path / "here.jpg").write_bytes(b"x")
    items = tuple({"asset_id": a, "library_id": "lib_1", "rel_path": a} for a in ("gone.jpg", "here.jpg"))
    out = run_work(Reads, acct, Job("t1", "capture", 2, items))
    assert out == ["gone.jpg"] and out.reason("gone.jpg") == SCAN  # a scan comes first
    acct.out_of_memory = True
    out = run_work(Reads, acct, Job("t1", "capture", 2, items))
    assert out == ["gone.jpg", "here.jpg"] and out.why == {"gone.jpg": SCAN, "here.jpg": HELD}


@pytest.mark.fast
def test_the_dispatcher_counts_what_it_holds_back_and_why():
    from src.server.scheduler.dispatch import Dispatcher, KindSpec

    now = [0.0]
    d = Dispatcher({"capture": KindSpec(2, "exif"), "redo_capture": KindSpec(4, "exif", same_as="capture")},
                   {"exif": 2}, clock=lambda: now[0])
    d.offer("t1", "capture", [{"asset_id": a, "library_id": lib, "created_at": ""}
                              for a, lib in (("a", "lib_1"), ("b", "lib_1"), ("c", "lib_2"))])
    job = d.take("exif")
    d.done(job, waiting=Waiting(["a"], {"a": SCAN}))  # its scan comes first
    job = d.take("exif")
    d.done(job, waiting=["b"])  # an old runner's list says no why: held
    job = d.take("exif")
    d.done(job, waiting=[])  # made: settled, not held
    assert d.status("t1")["held"] == [{"kind": "capture", "why": "held", "library_id": "lib_1", "clips": 1},
                                      {"kind": "capture", "why": "scan", "library_id": "lib_1", "clips": 1}]
    now[0] += 3601  # the hold's over
    d.offer("t1", "capture", [])
    assert d.status("t1")["held"] == []


@pytest.mark.fast
def test_a_crash_keeps_why_the_rest_of_its_job_waits():
    from src.producers.runner import Crashed
    from src.server.scheduler.dispatch import Job
    from src.server.scheduler.service import Scheduler

    def crashes(acct, job):
        raise Crashed(["bad"], OSError("disk"), waiting=Waiting(["moved"], {"moved": SCAN}))

    s = Scheduler(lambda: {}, capacity={}, runners={"capture": crashes}, crashed=lambda *a: [],
                  write_status=lambda *a: None)
    items = tuple({"asset_id": a, "library_id": "lib_1"} for a in ("bad", "moved"))
    out = s._run(FakeAccount("t1"), Job("t1", "capture", 2, items))
    assert out == ["bad", "moved"] and out.why == {"moved": SCAN}  # bad: held, uncharged this time
    s.stop()


# ---------------------------------------------------------------------------
# Every gate, through the real scheduler and the real database
# ---------------------------------------------------------------------------

SHARED_SLOTS = 4


class Harness:
    """The real scheduler (inline refills, its real database reads, status
    written to the account's database) over one library, with runners that
    record what's handed out. A runner made `real` runs the real runner."""

    def __init__(self, tenant_id: str, library_id: str, root: Path) -> None:
        from src.producers import pools
        from src.server.scheduler.kinds import QUEUED
        from src.server.scheduler.service import Scheduler, _from_database

        self.tenant_id, self.library_id = tenant_id, library_id
        self.now = 1000.0
        self.acct = Account(tenant_id, root, libraries=(library_id,), reachable=(library_id,))
        self.ran: list[tuple[str, tuple[str, ...]]] = []
        self.real: dict[str, type[Work]] = {}  # kind -> the Work its runner runs
        self.unlistable: set[str] = set()  # artifacts whose listing fails
        self._lock = threading.Lock()

        def candidates(tenant_id, kind, libraries, skip=None):
            if kind.artifact in self.unlistable:
                raise RuntimeError("column a.nothing does not exist")
            return _from_database(tenant_id, kind, libraries, skip)

        self.scheduler = Scheduler(
            lambda: {tenant_id: self.acct}, capacity={p.name: SHARED_SLOTS for p in pools().values() if not p.per_account},
            candidates=candidates, runners={k: self._runner(k) for k in dict.fromkeys(q.base for q in QUEUED)},
            scan=MagicMock(), inline_refill=True, clock=lambda: self.now)

    def _runner(self, base: str) -> Callable:
        def run_(acct, job):
            with self._lock:
                self.ran.append((job.kind, tuple(job.asset_ids)))
            work = self.real.get(base)
            return run_work(work, acct, job) if work else None
        return run_

    def run(self) -> None:
        """Two refills, the second when the status is written again, so it
        says what the first's jobs left held."""
        from src.server.scheduler.service import STATUS_EVERY_SEC

        for _ in range(2):
            self.scheduler.tick()
            deadline = time.monotonic() + 10
            while self.scheduler.running and time.monotonic() < deadline:
                self.scheduler.collect(timeout=0.1)
            self.now += STATUS_EVERY_SEC

    def handed_out(self, artifact: str) -> set[str]:
        kind = PRODUCERS[artifact].kind
        with self._lock:
            return {i for k, ids in self.ran if k in (kind, f"redo_{kind}") for i in ids}

    def stop(self) -> None:
        self.scheduler.stop(grace=1)


@dataclass
class Case:
    lib: tuple
    h: Harness
    artifact: str
    clip: str
    root: Path
    saved: dict[str, Any] = field(default_factory=dict)

    def post(self, path: str, body: dict) -> None:
        client, headers, *_ = self.lib
        r = client.post(path, json=body, headers=headers)
        assert r.status_code in (200, 204), r.text


def _image(lib, name: str = "a.jpg") -> str:
    return _ingest_with(lib, name, _sha())


def _stale_vision(lib) -> str:
    """A photo described with settings other than now's."""
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "stale.jpg", sha)
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "old words",
                                                        "lineage": {**_want(lib, "vision", sha), "settings_hash": "older"}},
                    headers=headers)
    assert r.status_code == 200, r.text
    return clip


def _older_version_vision(lib) -> str:
    from tests.test_crashes_and_new_versions import _made_by_an_older_version

    return _made_by_an_older_version(lib, "old.jpg")


def _set_vision(env, model: str, *, machine: bool) -> None:
    """The account's vision model (Settings → AI), and an online machine doing it or none."""
    from sqlmodel import select

    from src.server.database import get_control_session
    from src.server.models.control_plane import AiMachine, Tenant
    from src.server.repository.ai_machines import set_job_model

    with get_control_session() as ctrl:
        tenant = ctrl.get(Tenant, env[4])
        set_job_model(tenant, "vision", model)
        ctrl.add(tenant)
        for m in ctrl.exec(select(AiMachine).where(AiMachine.machine_id == "aim_gate_vision")).all():
            ctrl.delete(m)
        if machine:
            ctrl.add(AiMachine(machine_id="aim_gate_vision", tenant_id=env[4], name="Gate", api_url="http://gate.test",
                               jobs=["vision"], enabled=True, online=True, models=[model]))
        ctrl.commit()


def _set_proxy(case: Case, key: str | None) -> None:
    with _db(case.lib) as s:
        if key is None:
            case.saved["proxy_key"] = s.execute(text("SELECT proxy_key FROM assets WHERE asset_id = :a"),
                                                {"a": case.clip}).scalar()
        s.execute(text("UPDATE assets SET proxy_key = :k WHERE asset_id = :a"), {"k": key, "a": case.clip})
        s.commit()


def _fail(case: Case, times: int) -> None:
    for _ in range(times):
        case.post("/v1/producers/failures", {"items": [{"asset_id": case.clip, "artifact": case.artifact,
                                                        "error": "exiftool said no"}]})


def _hold_storage(case: Case) -> None:
    case.h.acct.reachable = ()
    case.h.acct.seen = {case.h.library_id: {"seen_at": "2026-10-12T17:55:00+00:00",
                                            "away_since": "2026-10-12T18:20:00+00:00", "checked": True}}


def _lift_storage(case: Case) -> None:
    case.h.acct.reachable = (case.h.library_id,)
    case.h.acct.seen = {case.h.library_id: {"seen_at": "2026-10-12T19:00:00+00:00", "away_since": None,
                                            "checked": True}}


def _prime(case: Case) -> None:
    """The clip is tried once, and waits."""
    case.h.run()
    assert case.clip in case.h.handed_out(case.artifact)


def _hold_scan(case: Case) -> None:
    case.h.real["capture"] = Reads  # its file isn't where its storage is
    _prime(case)


def _lift_scan(case: Case) -> None:
    (case.root / "a.jpg").write_bytes(b"x")  # the file is there again
    case.h.now += 4000


def _hold_held(case: Case) -> None:
    (case.root / "a.jpg").write_bytes(b"x")
    case.h.real["capture"] = Reads
    case.h.acct.out_of_memory = True
    _prime(case)


def _lift_held(case: Case) -> None:
    case.h.acct.out_of_memory = False
    case.h.now += 4000


def _hold_machines(case: Case) -> None:
    """Vision is off (no model), so the scheduler's guard can use no machine."""
    _set_vision(case.lib, "", machine=False)
    case.h.acct.slots["vision"] = 0


def _lift_machines(case: Case) -> None:
    _set_vision(case.lib, "qwen3-vl:8b-instruct", machine=True)
    case.h.acct.slots["vision"] = 2


def _retry(case: Case) -> None:
    case.post("/v1/producers/failures/retry", {"asset_ids": [case.clip]})


@dataclass(frozen=True)
class Gate:
    artifact: str
    hold: Callable[[Case], None]
    lift: Callable[[Case], None]
    clip: Callable[[tuple], str] = _image
    undo: Callable[[Case], None] = lambda case: None


def _post(path: str, body: dict) -> Callable[[Case], None]:
    return lambda case: case.post(path.format(artifact=case.artifact), body)


WORK, REDO = {"scope": "work"}, {"scope": "redo"}

# Every reason due work can wait, as the scheduler meets it: how to close
# the gate on a due clip, and how to open it.
GATES: dict[str, Gate] = {
    "paused": Gate("capture", _post("/v1/producers/{artifact}/pause", WORK), _post("/v1/producers/{artifact}/resume", WORK),
                   undo=_post("/v1/producers/{artifact}/resume", WORK)),
    "scheduler": Gate("capture", lambda case: setattr(case.h.acct, "settings_ready", False),
                      lambda case: setattr(case.h.acct, "settings_ready", True)),
    "ai_machines": Gate("vision", _hold_machines, _lift_machines,
                        undo=lambda case: _set_vision(case.lib, "", machine=False)),
    "needs": Gate("vision", lambda case: _set_proxy(case, None), lambda case: _set_proxy(case, case.saved["proxy_key"])),
    "storage": Gate("capture", _hold_storage, _lift_storage),
    "new_version": Gate("vision", lambda case: None, _post("/v1/producers/{artifact}/resume", REDO),
                        clip=_older_version_vision, undo=_post("/v1/producers/{artifact}/resume", REDO)),
    "redo_stopped": Gate("vision", _post("/v1/producers/{artifact}/pause", REDO),
                         _post("/v1/producers/{artifact}/resume", REDO), clip=_stale_vision,
                         undo=_post("/v1/producers/{artifact}/resume", REDO)),
    "scan": Gate("capture", _hold_scan, _lift_scan),
    "held": Gate("capture", _hold_held, _lift_held),
    "retry_later": Gate("capture", lambda case: _fail(case, 1), _retry),
    "given_up": Gate("capture", lambda case: _fail(case, 10), _retry),
}


def _row(case: Case) -> dict:
    client, headers, library_id, *_ = case.lib
    r = client.get("/v1/producers", params={"library_id": library_id}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}[case.artifact]


def _reasons(case: Case) -> dict[str, dict]:
    return {w["reason"]: w for w in _row(case)["waits"]}


@pytest.mark.slow
@pytest.mark.parametrize("reason", list(REASONS))
def test_due_work_held_back_always_says_why(env, reason, tmp_path):
    """The gate closed: the scheduler doesn't hand the clip out, and the
    producer's row says why (with how many clips). Opened: the clip is
    handed out, and the reason is gone."""
    gate = GATES[reason]
    lib = _library(env, f"Gate_{reason}")
    case = Case(lib, Harness(env[4], lib[2], tmp_path), gate.artifact, gate.clip(lib), tmp_path)
    try:
        gate.hold(case)
        case.h.ran.clear()
        case.h.run()
        assert case.clip not in case.h.handed_out(case.artifact), f"{reason}: the gate didn't hold the clip"
        said = _reasons(case)
        assert reason in said, f"{case.artifact}'s work waits for {reason}, and its row says {list(said)}"
        assert said[reason]["clips"] >= 1 or reason == "ai_machines"

        gate.lift(case)
        case.h.now += 600  # past every empty listing's wait
        case.h.ran.clear()
        case.h.run()
        assert case.clip in case.h.handed_out(case.artifact), f"{reason}: lifted, the clip still wasn't handed out"
        assert reason not in _reasons(case), f"{reason} is still said once it's lifted"
    finally:
        case.h.stop()
        gate.undo(case)


# ---------------------------------------------------------------------------
# What each reason says, beyond the gate
# ---------------------------------------------------------------------------


def _status(env, **over: Any) -> None:
    """Write the scheduler's status as it would (live unless at says otherwise)."""
    import json

    from src.shared.utils import utcnow

    status = {"at": utcnow().isoformat(), "running": {}, "waiting": {}, "pools": {}, "storage_waits": [],
              "storage": {}, **over}
    with _db(env) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v, now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": json.dumps(status)})
        s.commit()


@pytest.mark.slow
def test_a_scheduler_not_running_or_unable_to_list_says_so(env, tmp_path):
    from src.shared.utils import utcnow

    lib = _library(env, "SchedulerState")
    case = Case(lib, Harness(env[4], lib[2], tmp_path), "capture", _image(lib), tmp_path)
    try:
        case.h.unlistable.add("capture")
        case.h.run()
        assert case.clip not in case.h.handed_out("capture")
        said = _reasons(case)["scheduler"]
        assert said["why"] == "cant_list" and "does not exist" in said["message"] and said["clips"] == 1
    finally:
        case.h.stop()
    _status(env, at=(utcnow() - timedelta(minutes=5)).isoformat(),
            held=[{"kind": "capture", "why": "scan", "library_id": lib[2], "clips": 1}])
    said = _reasons(case)
    # What only a running scheduler knows (what it holds back) isn't said: that it isn't running is.
    assert said["scheduler"] == {"reason": "scheduler", "clips": 1, "why": "not_running"} and "scan" not in said


@pytest.mark.slow
def test_machines_the_scheduler_cant_use_are_said_whatever_settings_say(env, tmp_path):
    """Settings → AI has a model and an online machine, yet the scheduler
    can use none (its check hasn't reached them): its own word is said."""
    lib = _library(env, "MachinesUnusable")
    case = Case(lib, Harness(env[4], lib[2], tmp_path), "vision", _image(lib), tmp_path)
    _set_vision(lib, "qwen3-vl:8b-instruct", machine=True)
    try:
        case.h.acct.slots["vision"] = 0
        case.h.run()
        assert case.clip not in case.h.handed_out("vision")
        said = _reasons(case)["ai_machines"]
        assert said == {"reason": "ai_machines", "clips": 1, "job": "vision", "why": "unusable",
                        "message": "No machine doing vision is online."}  # the account's guard's words
    finally:
        case.h.stop()
        _set_vision(lib, "", machine=False)


@pytest.mark.slow
def test_held_clips_are_said_by_library_and_not_for_a_project(env):
    lib = _library(env, "HeldElsewhere")
    client, headers, library_id, *_ = lib
    clip = _image(lib)
    _status(env, held=[{"kind": "capture", "why": "scan", "library_id": library_id, "clips": 2},
                       {"kind": "capture", "library_id": library_id, "clips": 1},  # no why: held
                       {"kind": "capture", "why": "held", "library_id": "lib_other", "clips": 5}])
    case = Case(lib, MagicMock(), "capture", "", Path())
    said = _reasons(case)
    assert said["scan"] == {"reason": "scan", "clips": 2, "paused": False}
    assert said["held"]["clips"] == 1  # the other library's aren't this one's
    every = {p["artifact"]: p for p in client.get("/v1/producers", headers=headers).json()["producers"]}
    assert {w["reason"]: w["clips"] for w in every["capture"]["waits"]}["held"] == 6
    r = client.post("/v1/projects", json={"name": "HeldProject", "asset_ids": [clip]}, headers=headers)
    assert r.status_code in (200, 201), r.text
    in_project = {p["artifact"]: p for p in client.get(
        "/v1/producers", params={"project_id": r.json()["project_id"]}, headers=headers).json()["producers"]}
    # The scheduler doesn't say which clips it holds: a project's row doesn't guess.
    assert not {"scan", "held"} & {w["reason"] for w in in_project["capture"]["waits"]}


@pytest.mark.slow
def test_scans_paused_is_said_with_what_waits_for_a_scan_or_for_proxies(env):
    lib = _library(env, "ScansPaused")
    client, headers, library_id, *_ = lib
    clip = _image(lib)
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET proxy_key = NULL WHERE asset_id = :a"), {"a": clip})
        s.commit()
    _status(env, held=[{"kind": "capture", "why": "scan", "library_id": library_id, "clips": 1}])
    assert client.post("/v1/producers/scans/pause", json=WORK, headers=headers).status_code == 204
    try:
        rows = {p["artifact"]: p for p in client.get("/v1/producers", params={"library_id": library_id},
                                                     headers=headers).json()["producers"]}
        assert {"reason": "scan", "clips": 1, "paused": True} in rows["capture"]["waits"]
        # Proxies are made by scans: what waits for one waits for Scans.
        assert {"reason": "needs", "clips": 1, "artifact": "proxy", "paused": True} in rows["vision"]["waits"]
        assert rows["proxy"]["waits"] == []  # scans make it: "Paused with scans" says so
    finally:
        client.post("/v1/producers/scans/resume", json=WORK, headers=headers)


@pytest.mark.slow
def test_storage_isnt_said_while_everything_is_paused(env):
    """Pause all touches no drive, so nothing looks at them: paused says it."""
    lib = _library(env, "PausedDrives")
    client, headers, library_id, *_ = lib
    _image(lib)
    _status(env, storage_waits=[library_id], storage={library_id: {"seen_at": None, "away_since": None,
                                                                   "checked": False}})
    case = Case(lib, MagicMock(), "capture", "", Path())
    said = _reasons(case)
    assert said["storage"] == {"reason": "storage", "clips": 1, "library_id": library_id,
                               "library_name": "PausedDrives", "checked": False}
    assert client.post("/v1/producers/all/pause", json=WORK, headers=headers).status_code == 204
    try:
        said = _reasons(case)
        assert "storage" not in said and said["paused"]["clips"] == 1
    finally:
        client.post("/v1/producers/all/resume", json=WORK, headers=headers)


@pytest.mark.slow
def test_a_new_version_and_an_admins_stop_are_told_apart(env):
    lib = _library(env, "WhoStopped")
    client, headers, library_id, *_ = lib
    _older_version_vision(lib)
    with _db(lib) as s:
        from src.server.repository import lineage

        assert lineage.hold_new_version(s, "vision") is True
    try:
        row = {p["artifact"]: p for p in client.get("/v1/producers", params={"library_id": library_id},
                                                    headers=headers).json()["producers"]}["vision"]
        assert row["redo_stopped"] is True and row["new_version"] is True
        assert {"reason": "new_version", "clips": 1} in row["waits"]
        client.post("/v1/producers/vision/resume", json=REDO, headers=headers)
        assert client.post("/v1/producers/vision/pause", json=REDO, headers=headers).status_code == 204
        row = {p["artifact"]: p for p in client.get("/v1/producers", params={"library_id": library_id},
                                                    headers=headers).json()["producers"]}["vision"]
        assert row["redo_stopped"] is True and row["new_version"] is False
        assert {"reason": "redo_stopped", "clips": 1} in row["waits"]
    finally:
        client.post("/v1/producers/vision/resume", json=REDO, headers=headers)


@pytest.mark.slow
def test_without_counts_nothing_is_said_of_waits(env):
    client, headers, *_ = env
    for p in client.get("/v1/producers", params={"counts": "false"}, headers=headers).json()["producers"]:
        assert p["waits"] is None and p["counts"] is None
