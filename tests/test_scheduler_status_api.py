# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What the scheduler is doing, for Settings → Processing (ADR-016 phase 4).

The scheduler writes a snapshot every few seconds; the API serves it, and
says whether it's live (written in the last 30 seconds). An AI machine can
be marked as sharing the GPU video is decoded on: it gets fewer requests
while video work runs there (the built-in machine always shares it).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from src.shared.utils import utcnow
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db

pytestmark = pytest.mark.slow


def _write(env, status: dict) -> None:
    with _db(env) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v, now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": json.dumps(status)})
        s.commit()


def test_the_queue_says_whats_running_and_whether_the_scheduler_is(env):
    client, headers, *_ = env
    r = client.get("/v1/producers/queue", headers=headers)
    assert r.status_code == 200 and r.json()["live"] is False  # nothing written yet
    _write(env, {"at": utcnow().isoformat(), "running": {"render": 1, "vision": 2}, "waiting": {"vision": 40},
                 "pools": {"render": [1, 3], "vision": [2, 2]}, "gpu_hold": 1})
    body = client.get("/v1/producers/queue", headers=headers).json()
    assert body["live"] is True and body["running"] == {"render": 1, "vision": 2}
    assert body["pools"]["render"] == [1, 3] and body["gpu_hold"] == 1
    _write(env, {"at": (utcnow() - timedelta(minutes=2)).isoformat(), "running": {}, "waiting": {}, "pools": {}})
    assert client.get("/v1/producers/queue", headers=headers).json()["live"] is False


def test_a_machine_can_share_the_gpu_and_the_built_in_one_always_does(env):
    from tests.test_ai_machines import BRAIN, _machines

    client, headers, *_ = env
    fake, _ = _machines({BRAIN: ("qwen3-vl:8b-instruct",)})
    try:
        with fake:
            r = client.post("/v1/ai/machines", json={"name": "GPU box", "api_url": BRAIN, "jobs": ["vision"],
                                                     "at_once": 2, "shares_gpu": True}, headers=headers)
            assert r.status_code == 201, r.text
            machines = {m["name"]: m for m in r.json()["machines"]}
            assert machines["GPU box"]["shares_gpu"] is True
            assert all(m["shares_gpu"] for m in machines.values() if m["built_in"])
            mid = machines["GPU box"]["machine_id"]
            r = client.patch(f"/v1/ai/machines/{mid}", json={"shares_gpu": False}, headers=headers)
            assert r.status_code == 200, r.text
            assert {m["name"]: m for m in r.json()["machines"]}["GPU box"]["shares_gpu"] is False
            r = client.get("/v1/ai/jobs/vision", headers=headers)
            assert r.status_code == 200 and all("shares_gpu" in m for m in r.json()["machines"])
    finally:
        for m in client.get("/v1/ai", headers=headers).json()["machines"]:
            if not m.get("built_in"):
                client.delete(f"/v1/ai/machines/{m['machine_id']}", headers=headers)


def test_the_queue_says_how_long_until_each_producer_and_everything_is_made(env):
    from tests.test_lineage_api import _ingest_with, _sha
    from tests.test_reconciler import _library

    client, headers, *_ = env
    lib = _library(env, "Eta")
    _ingest_with(lib, "a.jpg", _sha(), None)  # a photo nothing has described yet
    _write(env, {"at": utcnow().isoformat(), "running": {"vision": 1}, "waiting": {},
                 "pools": {"vision": [1, 3], "gpu": [0, 1], "probe": [0, 1]},
                 "pace": {"vision": 6.0, "ocr": 3.0, "clip": 0.5, "faces": 0.5},
                 "jobs": [{"kind": "vision", "units": 1.0, "elapsed": 2.0}]})
    from src.server.api.routers import producers as producers_router

    producers_router._work_left_cache.clear()
    body = client.get("/v1/producers/queue", headers=headers).json()
    eta = body["eta"]
    left = client.get("/v1/producers", params={"library_id": lib[2]}, headers=headers).json()
    vision_pool = (6.0 * _missing(client, headers, "vision") + 3.0 * _missing(client, headers, "ocr")) / 3
    assert eta["pools"]["vision"] == pytest.approx(vision_pool)
    assert eta["producers"]["vision"] == eta["producers"]["ocr"] == pytest.approx(vision_pool)  # they share it
    assert eta["caught_up"] == pytest.approx(max(eta["pools"].values()))
    # Producers with work and no pace (transcripts, scenes...) are named, not guessed.
    assert all(n["why"] in ("no_machine", "not_known_yet") for n in eta["not_counted"])
    [job] = eta["jobs"]
    assert job["artifact"] == "vision" and job["left"] == pytest.approx(4.0, abs=1.0)
    assert {p["artifact"]: p["unit"] for p in left["producers"]}["transcript"] == "second"
    # A scheduler that stopped saying: nothing to time.
    _write(env, {"at": (utcnow() - timedelta(minutes=2)).isoformat(), "running": {}, "waiting": {}, "pools": {}})
    assert client.get("/v1/producers/queue", headers=headers).json()["eta"] is None


def _missing(client, headers, artifact: str) -> int:
    counts = {p["artifact"]: p["counts"] for p in client.get("/v1/producers", headers=headers).json()["producers"]}
    return counts[artifact]["missing"] + counts[artifact]["stale"] - counts[artifact]["given_up"]


def _work(env, artifact: str, **kw) -> dict:
    from src.server.repository import lineage
    from src.server.scheduler.service import _job_models

    with _db(env) as s:
        return lineage.work_left(s, artifact, lineage.desired(s, artifact, _job_models(env[4])), **kw)


def test_work_left_is_what_the_scheduler_will_do(env):
    """Not clips waiting out a failure, nor those given up, nor those whose
    first step was given up (they can't be made), nor storage that's away."""
    from tests.test_lineage_api import _ingest_with, _sha
    from tests.test_reconciler import _library

    lib = _library(env, "WorkLeft")
    client, headers, library_id, *_ = lib
    clips = [_ingest_with(lib, f"v{i}.mov", _sha(), None, media_type="video") for i in range(4)]
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 60 WHERE asset_id = ANY(:a)"), {"a": clips})
        s.commit()
    base = _work(env, "transcript", redo=False)
    with _db(env) as s:
        for clip, retry in ((clips[0], "now() + interval '1 hour'"), (clips[1], "'infinity'")):
            s.execute(text(
                "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                " produced_at, outcome, error, attempts, retry_at)"
                f" VALUES (:a, 'transcript', 'whisper', '1', 'h', now(), 'failed', 'no', 1, {retry})"), {"a": clip})
        # The third can't be heard: its analysis copy was given up.
        s.execute(text(
            "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
            " produced_at, outcome, error, attempts, retry_at)"
            " VALUES (:a, 'analysis_proxy', 'analysis-proxy', '1', 'h', now(), 'failed', 'no', 10, 'infinity')"),
            {"a": clips[2]})
        s.commit()
    now = _work(env, "transcript", redo=False)
    assert base["clips"] - now["clips"] == 3 and base["seconds"] - now["seconds"] == 180
    # Its library's storage is away: nothing it reads the originals for is counted.
    away = _work(env, "analysis_proxy", redo=False, away=[library_id])
    assert away["clips"] == _work(env, "analysis_proxy", redo=False)["clips"] - 3  # all but the given-up one
