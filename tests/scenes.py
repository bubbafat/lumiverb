"""A video's scenes, found and described, for a test about something else (a search, a saved project, a public page).

The scenes producer finds a video's scenes through the chunks API: the
video's chunks are made (POST /v1/video/{id}/chunks), each claimed in turn
(POST /v1/video/{id}/chunks/next) and completed with the scenes that start
in it (POST /v1/video/chunks/{id}/complete). The scene producer then
describes each one (PATCH /v1/video/scenes/{id}). A test that only needs a
video showing described scenes makes them here, through those same routes,
each saying how it was made as the workers do (tests/machine_lineage.py).
A test about the chunks API or scene descriptions themselves uses the routes
directly.
"""

from __future__ import annotations

from typing import NamedTuple

from fastapi.testclient import TestClient

from tests.machine_lineage import made

# The scene window the chunks are cut to (src/producers/scenes/detail.py window_sec).
SCENE_SEC = 30.0


class Scene(NamedTuple):
    """One scene of a video: its times and what its description says."""

    start_ms: int
    end_ms: int
    description: str
    tags: tuple[str, ...] = ()


def described_scenes(client: TestClient, headers: dict, asset_id: str, scenes: list[Scene]) -> list[str]:
    """A video's scenes (it has none yet), found and then described: their
    scene_ids, in order. Each scene is found in the chunk it starts in; none
    has a picture hash, so none is merged with another."""
    scenes = sorted(scenes)
    duration_sec = max(s.end_ms for s in scenes) / 1000
    r = client.post(f"/v1/video/{asset_id}/chunks", json={"duration_sec": duration_sec, "scene_sec": SCENE_SEC},
                    headers=headers)
    assert r.status_code == 200 and not r.json()["already_initialized"], r.text
    while (r := client.post(f"/v1/video/{asset_id}/chunks/next", headers=headers)).status_code == 200:
        work = r.json()
        start, end = round(work["start_ts"] * 1000), round(work["end_ts"] * 1000)
        found = [{"start_ms": s.start_ms, "end_ms": s.end_ms, "rep_frame_ms": s.start_ms}
                 for s in scenes if start <= s.start_ms < end]
        done = client.post(f"/v1/video/chunks/{work['chunk_id']}/complete", headers=headers,
                           json={"worker_id": work["worker_id"], "scenes": found, "lineage": made("scenes")})
        assert done.status_code == 200, done.text
    assert r.status_code == 204, r.text

    r = client.get(f"/v1/video/{asset_id}/scenes", headers=headers)
    assert r.status_code == 200, r.text
    shown = r.json()["scenes"]
    assert [(s["start_ms"], s["end_ms"]) for s in shown] == [(s.start_ms, s.end_ms) for s in scenes]
    for row, scene in zip(shown, scenes, strict=True):
        r = client.patch(f"/v1/video/scenes/{row['scene_id']}", headers=headers, json={
            "model_id": "test-vision", "model_version": "1", "description": scene.description,
            "tags": list(scene.tags), "lineage": made("scene_vision")})
        assert r.status_code == 200, r.text
    return [row["scene_id"] for row in shown]
