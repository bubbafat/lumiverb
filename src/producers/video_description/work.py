"""A video described as a whole by the account's vision machines: its scenes' descriptions, in order and
with when each starts, sent as text (no picture), and the answer saved as its description and tags."""

from __future__ import annotations

from src.producers.names import VIDEO_DESCRIPTION
from src.producers.runner import Waits, Work


def scenes_text(scenes: list[dict]) -> str:
    """The scenes as the model reads them: one line each, in order, with when it starts."""
    lines = []
    for n, scene in enumerate(scenes, 1):
        seconds = int(scene["start_ms"]) // 1000
        line = f"{n}. ({seconds // 60}:{seconds % 60:02d}) {scene['description'].strip()}"
        tags = [t for t in scene.get("tags") or [] if isinstance(t, str) and t.strip()]
        if tags:
            line += f" [tags: {', '.join(tags)}]"
        lines.append(line)
    return "\n".join(lines)


class VideoDescription(Work):
    artifact = VIDEO_DESCRIPTION

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        guard = acct.guard("vision")
        self.model = guard.model
        self.used = acct.producers.with_model(self.artifact, self.model)
        self.provider = guard.provider(settings=self.used)

    def make(self, clip: dict) -> dict:
        from src.processing.workers.captions.base import CaptionError

        # The scenes it shows, in order (a redo's being found aren't shown yet).
        scenes = self.client.get(f"/v1/video/{clip['asset_id']}/scenes").json().get("scenes", [])
        described = [s for s in scenes if (s.get("description") or "").strip()]
        if not described or len(described) < len(scenes):
            raise Waits("its scenes aren't all described yet")  # made once they are (it needs them)
        try:
            result = self.provider.describe_text(scenes_text(described))
        except CaptionError as e:
            raise self.machines_or_clip(e) from e
        description = (result.get("description") or "").strip() if result else ""
        if not description:
            raise self.machines_or_clip("the model said nothing")
        return {"asset_id": clip["asset_id"], "model_id": self.model, "model_version": "1",
                "description": description,
                "tags": [t.strip() for t in result.get("tags") or [] if isinstance(t, str) and t.strip()]}

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, result in made:
            client.post("/v1/assets/batch-vision", json={
                "items": [{**result, "source_hash": clip.get("content_hash")}],
                "lineage": self.lineage(None, used=self.used)})  # each item says its file
