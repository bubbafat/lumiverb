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
