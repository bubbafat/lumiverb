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
    "redo_vision": KindSpec(tier=4, pool="vision"),
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
def test_an_empty_answer_is_not_asked_again_at_once() -> None:
    clock = Clock()
    d = _d(clock=clock)
    d.offer("t1", "vision", [])
    assert not d.wanted("t1", "vision")
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
