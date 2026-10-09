"""The scheduler's dispatcher: which job runs next (ADR-016 phase 4).

One ranked queue of every job: by tier (1 see it, 2 prepare, 3 find it,
4 redo), then oldest first. Each resource is a pool with so many slots; a
free slot takes the best job that uses it, so a lower tier fills what a
higher one leaves idle. A job just run isn't taken again for a while, so
one the server still lists (it failed without saying so) can't spin.
"""

from __future__ import annotations

import pytest

from src.server.scheduler.dispatch import Dispatcher, KindSpec

KINDS = {
    "probe": KindSpec(tier=1, pool="probe"),
    "render": KindSpec(tier=2, pool="render"),
    "vision": KindSpec(tier=3, pool="vision"),
    "ocr": KindSpec(tier=3, pool="vision"),
    "redo_vision": KindSpec(tier=4, pool="vision", same_as="vision"),
    "faces": KindSpec(tier=3, pool="faces", batch=3),
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _item(asset_id: str, added: str) -> dict:
    return {"asset_id": asset_id, "created_at": added}


def _d(capacity: dict[str, int] | None = None, clock: Clock | None = None) -> Dispatcher:
    return Dispatcher(KINDS, capacity or {"probe": 1, "render": 1, "vision": 2, "faces": 1},
                      retake_after=3600, clock=clock or Clock())


@pytest.mark.fast
def test_oldest_first_within_a_tier_across_kinds() -> None:
    d = _d()
    d.offer("t1", "vision", [_item("b", "2026-10-02"), _item("d", "2026-10-04")])
    d.offer("t1", "ocr", [_item("a", "2026-10-01"), _item("c", "2026-10-03")])
    taken = [d.take("vision"), d.take("vision")]
    assert [(j.kind, j.items[0]["asset_id"]) for j in taken] == [("ocr", "a"), ("vision", "b")]


@pytest.mark.fast
def test_a_higher_tier_goes_first_however_new() -> None:
    d = _d({"vision": 1})
    d.offer("t1", "redo_vision", [_item("old", "2020-01-01")])
    d.offer("t1", "vision", [_item("new", "2026-10-09")])
    assert d.take("vision").items[0]["asset_id"] == "new"


@pytest.mark.fast
def test_a_lower_tier_fills_what_a_higher_one_leaves_idle() -> None:
    # Probes wait on storage; the AI machines still describe meanwhile.
    d = _d()
    d.offer("t1", "probe", [_item("p", "2026-10-01")])
    d.offer("t1", "vision", [_item("v", "2026-10-01")])
    assert d.take("probe").kind == "probe"
    assert d.take("probe") is None  # its one slot is busy
    assert d.take("vision").kind == "vision"


@pytest.mark.fast
def test_a_pool_takes_no_more_than_its_slots() -> None:
    d = _d()
    d.offer("t1", "vision", [_item(str(i), f"2026-10-0{i}") for i in range(1, 5)])
    jobs = [d.take("vision"), d.take("vision"), d.take("vision")]
    assert jobs[2] is None and d.free("vision") == 0
    d.done(jobs[0])
    assert d.free("vision") == 1
    assert d.take("vision").items[0]["asset_id"] == "3"


@pytest.mark.fast
def test_a_job_in_hand_is_not_offered_again() -> None:
    d = _d()
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    job = d.take("vision")
    # The server still lists it while it's being made.
    d.offer("t1", "vision", [_item("a", "2026-10-01"), _item("b", "2026-10-02")])
    assert d.take("vision").items[0]["asset_id"] == "b"
    assert job.items[0]["asset_id"] == "a"


@pytest.mark.fast
def test_a_job_just_run_waits_before_it_is_taken_again() -> None:
    # Made, the server stops listing it. Not made (it failed and nothing said
    # so), it would be first again at once.
    clock = Clock()
    d = _d(clock=clock)
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.done(d.take("vision"))
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    assert d.take("vision") is None
    clock.now += 3601
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    assert d.take("vision").items[0]["asset_id"] == "a"


@pytest.mark.fast
def test_a_clip_saved_or_reported_is_held_only_until_the_database_says_so() -> None:
    # Review round 2: an hour for every clip tried made the server's back-off
    # (5, 10, 20 minutes) an hour, and kept a clip just made from being redone.
    clock = Clock()
    d = _d(clock=clock)
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.done(d.take("vision"), waiting=())
    clock.now += d.settle_after - 1  # a listing read before the save still names it
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    assert d.take("vision") is None
    clock.now += 2
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    assert d.take("vision") is not None


@pytest.mark.fast
def test_only_clips_that_wait_without_saying_why_are_held_the_long_while() -> None:
    clock = Clock()
    d = _d(clock=clock)
    items = [_item("a", "2026-10-01"), _item("b", "2026-10-02"), _item("c", "2026-10-03")]
    d.offer("t1", "faces", items)
    d.done(d.take("faces"), waiting=["b"])  # a saved, c reported, b neither
    clock.now += d.settle_after + 1
    assert d.held("t1", "faces") == ["b"]
    d.offer("t1", "faces", items)
    assert d.take("faces").asset_ids == ["a", "c"]


@pytest.mark.fast
def test_a_kind_can_wait_less_before_its_job_is_taken_again() -> None:
    clock = Clock()
    kinds = {**KINDS, "scan": KindSpec(tier=1, pool="scan", retake_after=30)}
    d = Dispatcher(kinds, {"scan": 1}, retake_after=3600, clock=clock)
    d.offer("t1", "scan", [_item("scan:t1", "")])
    d.done(d.take("scan"))
    clock.now += 31
    d.offer("t1", "scan", [_item("scan:t1", "")])
    assert d.take("scan") is not None


@pytest.mark.fast
def test_a_batch_kind_takes_up_to_its_batch_oldest_first() -> None:
    d = _d()
    d.offer("t1", "faces", [_item(str(i), f"2026-10-0{i}") for i in range(1, 6)])
    job = d.take("faces")
    assert [i["asset_id"] for i in job.items] == ["1", "2", "3"]
    d.done(job)
    assert [i["asset_id"] for i in d.take("faces").items] == ["4", "5"]


@pytest.mark.fast
def test_accounts_share_one_queue_oldest_first() -> None:
    d = _d({"vision": 3})
    d.offer("t1", "vision", [_item("t1-new", "2026-10-09")])
    d.offer("t2", "vision", [_item("t2-old", "2026-10-01")])
    first = d.take("vision")
    assert (first.tenant_id, first.items[0]["asset_id"]) == ("t2", "t2-old")


@pytest.mark.fast
def test_a_kind_wants_more_when_its_buffer_runs_low() -> None:
    d = _d({"vision": 100})
    assert d.wanted("t1", "vision")
    d.offer("t1", "vision", [_item(str(i), f"2026-10-{i:02d}") for i in range(1, 30)], complete=False)
    assert not d.wanted("t1", "vision")
    for _ in range(25):
        d.take("vision")
    assert d.wanted("t1", "vision")


@pytest.mark.fast
def test_a_kind_whose_due_clips_are_all_known_is_not_asked_again_at_once() -> None:
    clock = Clock()
    d = _d({"vision": 100}, clock=clock)
    d.offer("t1", "vision", [_item("a", "2026-10-01")], complete=True)
    assert not d.wanted("t1", "vision")
    clock.now += 16
    assert d.wanted("t1", "vision")


@pytest.mark.fast
def test_an_answer_of_only_clips_in_hand_is_not_asked_again_at_once() -> None:
    d = _d({"vision": 100})
    d.offer("t1", "vision", [_item("a", "2026-10-01")], complete=False)
    d.take("vision")
    d.offer("t1", "vision", [_item("a", "2026-10-01")], complete=False)
    assert not d.wanted("t1", "vision")


@pytest.mark.fast
def test_an_empty_answer_is_not_asked_again_at_once_and_less_often_while_it_stays_empty() -> None:
    clock = Clock()
    d = _d(clock=clock)
    waits = []
    for _ in range(5):
        d.offer("t1", "vision", [])
        waited = 0
        while not d.wanted("t1", "vision"):
            clock.now += 1
            waited += 1
        waits.append(waited)
    assert waits == [15, 30, 60, 120, 120]
    d.offer("t1", "vision", [_item("a", "2026-10-01")])  # something new: as often as usual again
    d.take("vision")
    d.offer("t1", "vision", [])
    clock.now += 16
    assert d.wanted("t1", "vision")


@pytest.mark.fast
def test_a_kind_switched_off_hands_out_nothing() -> None:
    # Descriptions while no machine can do them: their jobs wait in the
    # database, not here.
    d = _d()
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.clear("t1", "vision")
    assert d.take("vision") is None


@pytest.mark.fast
def test_waiting_and_running_count_a_pool() -> None:
    d = _d()
    d.offer("t1", "render", [_item("a", "2026-10-01"), _item("b", "2026-10-02")])
    assert d.waiting("render") == 2 and d.running("render") == 0
    d.take("render")
    assert d.waiting("render") == 1 and d.running("render") == 1


@pytest.mark.fast
def test_a_pool_with_no_slots_takes_nothing() -> None:
    d = _d({"vision": 0})
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    assert d.take("vision") is None
    d.set_capacity("vision", 1)
    assert d.take("vision") is not None


@pytest.mark.fast
def test_an_accounts_own_pool_serves_only_it() -> None:
    # Each account has its own AI machines (Settings → AI).
    kinds = {"vision": KindSpec(tier=3, pool="vision", per_account=True)}
    d = Dispatcher(kinds, {"vision@t1": 1, "vision@t2": 1}, clock=Clock())
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.offer("t2", "vision", [_item("b", "2026-10-02")])
    assert d.take("vision@t2").items[0]["asset_id"] == "b"
    assert d.take("vision@t2") is None
    assert d.take("vision@t1").items[0]["asset_id"] == "a"
    assert d.pools(["t1", "t2"]) == ["vision@t1", "vision@t2"]


@pytest.mark.fast
def test_a_clip_is_never_in_hand_for_its_first_making_and_its_redo_at_once() -> None:
    d = _d({"vision": 3})
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.offer("t1", "redo_vision", [_item("a", "2026-10-01"), _item("b", "2026-10-02")])
    first = d.take("vision")
    second = d.take("vision")
    assert (first.kind, first.items[0]["asset_id"]) == ("vision", "a")
    assert (second.kind, second.items[0]["asset_id"]) == ("redo_vision", "b")
    assert d.take("vision") is None
    d.done(first)
    # Just made: its redo doesn't take it again at once either.
    d.offer("t1", "redo_vision", [_item("a", "2026-10-01")])
    assert d.take("vision") is None


@pytest.mark.fast
def test_held_clips_are_what_the_database_should_leave_out() -> None:
    d = _d({"vision": 2})
    d.offer("t1", "vision", [_item("a", "2026-10-01"), _item("b", "2026-10-02"), _item("c", "2026-10-03")])
    first = d.take("vision")
    d.take("vision")
    d.done(first)
    assert sorted(d.held("t1", "vision")) == ["a", "b"]  # one just tried, one in hand
    assert d.held("t1", "ocr") == [] and d.held("t2", "vision") == []
    # Its redo leaves the same clips out.
    assert sorted(d.held("t1", "redo_vision")) == ["a", "b"]


@pytest.mark.fast
def test_a_job_that_couldnt_try_holds_nothing_back() -> None:
    d = _d()
    d.offer("t1", "probe", [_item("p", "2026-10-01")])
    d.done(d.take("probe"), tried=False)
    assert d.held("t1", "probe") == []
    d.offer("t1", "probe", [_item("p", "2026-10-01")])
    assert d.take("probe") is not None


@pytest.mark.fast
def test_forgetting_an_accounts_taken_clips() -> None:
    d = _d()
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.offer("t2", "vision", [_item("b", "2026-10-01")])
    d.done(d.take("vision"))
    d.done(d.take("vision"))
    d.forget_taken("t1")
    d.offer("t1", "vision", [_item("a", "2026-10-01")])
    d.offer("t2", "vision", [_item("b", "2026-10-01")])
    job = d.take("vision")
    assert (job.tenant_id, job.items[0]["asset_id"]) == ("t1", "a")
    assert d.take("vision") is None  # t2's still waits its hour


# --- How long things take: each kind's pace, from the jobs it finished ------
# Seconds a slot spends per unit of work: a second of video for the kinds
# whose work grows with it (by_seconds), else a clip. Settings → Processing
# turns it into time left.

PACED = {
    "render": KindSpec(tier=2, pool="render", by_seconds=True),
    "vision": KindSpec(tier=3, pool="vision"),
    "redo_vision": KindSpec(tier=4, pool="vision", same_as="vision"),
}


def _video(asset_id: str, seconds: float) -> dict:
    return {"asset_id": asset_id, "created_at": "2026-10-01", "duration_sec": seconds}


def _run(d: Dispatcher, clock: Clock, pool: str, took: float) -> None:
    """Take a job, let it run so long, and say each clip was saved (as the scheduler does)."""
    job = d.take(pool)
    assert job is not None
    clock.now += took
    d.done(job, waiting=())


@pytest.mark.fast
def test_a_finished_job_teaches_its_kinds_pace_per_second_of_video() -> None:
    clock = Clock()
    d = Dispatcher(PACED, {"render": 1}, clock=clock)
    d.offer("t1", "render", [_video("a", 120), _video("b", 120)], complete=True)
    _run(d, clock, "render", 60)
    assert d.status("t1")["pace"] == {"render": 0.5}
    _run(d, clock, "render", 120)  # smoothed, not replaced
    assert d.status("t1")["pace"]["render"] == pytest.approx(0.6)


@pytest.mark.fast
def test_a_redo_and_its_kind_share_one_pace_per_clip() -> None:
    clock = Clock()
    d = Dispatcher(PACED, {"vision": 1}, clock=clock)
    d.offer("t1", "redo_vision", [_item("a", "2026-10-01")], complete=True)
    _run(d, clock, "vision", 8)
    assert d.status("t1")["pace"] == {"vision": 8.0}


@pytest.mark.fast
@pytest.mark.parametrize("done", [{"tried": False}, {"waiting": None}, {"waiting": ["a"]}])
def test_a_job_that_made_nothing_it_can_count_teaches_nothing(done) -> None:
    clock = Clock()
    d = Dispatcher(PACED, {"vision": 1}, clock=clock)
    d.offer("t1", "vision", [_item("a", "2026-10-01")], complete=True)
    job = d.take("vision")
    clock.now += 5
    if "waiting" in done and done["waiting"] is None:
        d.done(job)  # it can't say what it saved
    else:
        d.done(job, **done)
    assert d.status("t1")["pace"] == {}


@pytest.mark.fast
def test_running_jobs_say_how_big_they_are_and_how_long_theyve_run() -> None:
    clock = Clock()
    d = Dispatcher(PACED, {"render": 2, "vision": 1}, clock=clock)
    d.offer("t1", "render", [_video("a", 90)], complete=True)
    d.offer("t1", "vision", [_item("p", "2026-10-01")], complete=True)
    d.take("render")
    clock.now += 10
    d.take("vision")
    clock.now += 5
    jobs = sorted(d.status("t1")["jobs"], key=lambda j: j["kind"])
    assert jobs == [{"kind": "render", "units": 90.0, "elapsed": 15.0},
                    {"kind": "vision", "units": 1.0, "elapsed": 5.0}]
    assert d.status("t2")["jobs"] == []


@pytest.mark.fast
def test_a_pace_known_before_a_restart_is_where_it_starts() -> None:
    clock = Clock()
    d = Dispatcher(PACED, {"render": 1}, clock=clock)
    d.seed_pace("t1", {"render": 0.4, "gone_kind": 3.0, "vision": "nonsense"})
    assert d.status("t1")["pace"] == {"render": 0.4}
    d.offer("t1", "render", [_video("a", 100)], complete=True)
    _run(d, clock, "render", 100)
    assert d.status("t1")["pace"]["render"] == pytest.approx(0.8 * 0.4 + 0.2 * 1.0)
