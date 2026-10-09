"""How long until everything is made (Settings → Processing).

Each kind's pace (seconds a slot spends per second of video, or per clip)
comes from the jobs the scheduler finished; with the work left and the
pool's slots it's each producer's time left. Pools run side by side, so
being caught up is the slowest pool's time, not the sum. A running job's
time left is its size at that pace, less how long it's run.
"""

from __future__ import annotations

import pytest

from src.server.scheduler.eta import eta

pytestmark = pytest.mark.fast

AT = "2026-10-09T12:00:00+00:00"


def _status(**over) -> dict:
    return {"at": AT, "pace": {"render": 0.5, "vision": 6.0, "ocr": 2.0, "transcript": 0.1},
            "pools": {"render": [1, 2], "vision": [2, 3], "transcripts": [0, 1]}, "jobs": [], **over}


def test_a_producers_time_left_is_its_work_at_its_pace_over_its_pools_slots():
    out = eta(_status(), {"analysis_proxy": 3600.0}, now=AT)
    assert out["producers"]["analysis_proxy"] == pytest.approx(3600 * 0.5 / 2)


def test_producers_sharing_a_pool_add_up_and_pools_run_side_by_side():
    out = eta(_status(), {"vision": 30.0, "ocr": 30.0, "analysis_proxy": 600.0}, now=AT)
    assert out["pools"]["vision"] == pytest.approx((30 * 6 + 30 * 2) / 3)
    assert out["pools"]["render"] == pytest.approx(600 * 0.5 / 2)
    assert out["caught_up"] == pytest.approx(max(80.0, 150.0))


def test_nothing_left_is_caught_up_now():
    out = eta(_status(), {"vision": 0.0, "analysis_proxy": 0.0}, now=AT)
    assert out["caught_up"] == 0 and out["producers"]["vision"] == 0


def test_a_kind_with_no_pace_yet_or_no_slots_has_no_time():
    out = eta(_status(pace={}), {"vision": 10.0}, now=AT)
    assert out["producers"]["vision"] is None and "vision" not in out["pools"] and out["caught_up"] is None
    out = eta(_status(pools={"vision": [0, 0]}), {"vision": 10.0}, now=AT)  # no machine does it
    assert out["producers"]["vision"] is None and out["caught_up"] == 0  # caught up, not counting it


def test_a_running_jobs_time_left_counts_from_when_the_status_was_written():
    status = _status(jobs=[{"kind": "render", "units": 600.0, "elapsed": 100.0},
                           {"kind": "redo_vision", "units": 1.0, "elapsed": 2.0},
                           {"kind": "probe", "units": 1.0, "elapsed": 1.0}])
    out = eta(status, {}, now="2026-10-09T12:00:10+00:00")
    render, redo, probe = out["jobs"]
    assert render == {"kind": "render", "artifact": "analysis_proxy", "unit": "second", "units": 600.0,
                      "elapsed": 110.0, "left": pytest.approx(600 * 0.5 - 110), "late": False}
    assert redo["artifact"] == "vision" and redo["left"] == 0  # past its pace: about to finish
    assert probe["left"] is None  # no pace yet


def test_a_producer_waiting_for_a_machine_or_a_pace_is_left_out_of_caught_up_and_named():
    status = _status(pools={"render": [1, 2], "vision": [0, 0], "transcripts": [0, 1]}, pace={"render": 0.5})
    out = eta(status, {"analysis_proxy": 600.0, "vision": 10.0, "transcript": 120.0}, now=AT)
    assert out["caught_up"] == pytest.approx(150.0)
    assert out["not_counted"] == [{"artifact": "vision", "title": "Descriptions and tags", "why": "no_machine"},
                                  {"artifact": "transcript", "title": "Transcripts", "why": "not_known_yet"}]


def test_producers_sharing_a_pool_each_finish_about_when_the_pool_does():
    out = eta(_status(), {"vision": 30.0, "ocr": 30.0}, now=AT)
    assert out["producers"]["vision"] == out["producers"]["ocr"] == pytest.approx((30 * 6 + 30 * 2) / 3)


def test_a_job_far_past_its_pace_is_late_and_scans_arent_timed():
    status = _status(jobs=[{"kind": "render", "units": 60.0, "elapsed": 100.0},
                           {"kind": "scan", "units": 1.0, "elapsed": 30.0}])
    out = eta(status, {}, now=AT)
    assert [j["kind"] for j in out["jobs"]] == ["render"]
    assert out["jobs"][0]["late"] is True  # 100 s for what takes 30


def test_only_work_no_machine_is_doing_left_is_caught_up_not_counting_it():
    out = eta(_status(pools={"transcripts": [0, 0]}), {"transcript": 120.0, "vision": 0.0}, now=AT)
    assert out["caught_up"] == 0 and [n["why"] for n in out["not_counted"]] == ["no_machine"]
    out = eta(_status(pace={}), {"vision": 10.0}, now=AT)  # its pace isn't known: neither is when
    assert out["caught_up"] is None


def test_a_job_with_no_length_to_go_by_isnt_late():
    out = eta(_status(jobs=[{"kind": "render", "units": 0.0, "elapsed": 50.0}]), {}, now=AT)
    assert out["jobs"][0]["late"] is False
