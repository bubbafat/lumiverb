"""A photo's faces for a test about something else (people, clusters, what a
person said about a face).

Photos' faces are found by the scheduler's faces producer and saved with
POST /v1/assets/batch-faces (src/producers/faces/work.py); the Mac app's
POST /v1/assets/{id}/faces is retired (tests/test_retired_endpoints.py). A
test plants faces the way the producer saves them: one clip's in a batch,
with the faces producer's lineage, through the real route (its lineage,
face crops and the library's revision with them).
"""

from __future__ import annotations

import json
from typing import Any

from src.server.api.routers.assets import _normalize_bounding_box
from tests.machine_lineage import made


def _box(bounding_box: dict) -> str:
    return json.dumps(bounding_box, sort_keys=True)


def plant_faces(client: Any, asset_id: str, faces: list[dict], *, headers: dict | None = None,
                lineage: dict | None = None, **item: Any) -> list[str]:
    """Save one photo's faces as the faces producer does, and return their
    ids: one per face sent, in order (a face found again keeps its id). item:
    the batch item's other fields (detection_model, detection_model_version,
    embedding_model, source_hash). lineage: the producer's, as a worker
    sends it (the registry's settings unless given)."""
    auth = {"headers": headers} if headers is not None else {}
    r = client.post("/v1/assets/batch-faces", json={
        "items": [{"asset_id": asset_id, "faces": faces, **item}],
        "lineage": lineage if lineage is not None else made("faces")}, **auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"processed": 1, "skipped": 0}, r.text
    if not faces:
        return []
    # The batch says how many it saved, not which: the clip's faces, by the box each was saved at.
    r = client.get(f"/v1/assets/{asset_id}/faces", **auth)
    assert r.status_code == 200, r.text
    by_box: dict[str, list[str]] = {}
    for face in r.json()["faces"]:
        by_box.setdefault(_box(face["bounding_box"]), []).append(face["face_id"])
    ids = []
    for face in faces:
        [face_id] = by_box[_box(_normalize_bounding_box(face["bounding_box"]))]  # one face at each box sent
        ids.append(face_id)
    return ids


def face_count(client: Any, asset_id: str, *, headers: dict | None = None) -> int | None:
    """The clip's face_count, as its detail says it."""
    r = client.get(f"/v1/assets/{asset_id}", **({"headers": headers} if headers is not None else {}))
    assert r.status_code == 200, r.text
    return r.json()["face_count"]
