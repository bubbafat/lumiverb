"""Saving what a producer made for one clip, as the producers save it: the
batch routes (POST /v1/assets/batch-vision, batch-ocr, batch-embeddings)
with one item. The per-clip routes they replaced are gone (410).

`saved(client, "vision", clip, json=…)` takes the body a per-clip route
took, so a test says what it saves as before: `model_id`, `model_version`,
`description`, `tags` (vision), `ocr_text`, `model_id` (ocr), `model_id`,
`model_version`, `vector` (embeddings), each with its `lineage`.
"""

from __future__ import annotations

from typing import Any

ITEM = {
    "vision": ("model_id", "model_version", "description", "tags", "source_hash"),
    "ocr": ("ocr_text", "source_hash"),
    "embeddings": ("model_id", "model_version", "vector", "source_hash"),
}
BATCH = {"model_id"}  # ocr's: the reading model, for the whole batch


def saved(client: Any, kind: str, clip: str, *, json: dict, **kwargs: Any):
    """POST /v1/assets/batch-<kind> for one clip; the response as it is
    ({"updated", "skipped"}: a clip that isn't there, or is in the trash, is
    skipped)."""
    item = {"asset_id": clip, **{key: json[key] for key in ITEM[kind] if key in json}}
    body: dict[str, Any] = {"items": [item], "lineage": json.get("lineage")}
    if kind == "ocr":
        body.update({key: json[key] for key in BATCH if key in json})
    return client.post(f"/v1/assets/batch-{kind}", json=body, **kwargs)
