"""A video's scenes described by the account's vision machines: each scene's
representative frame from the analysis proxy, described, and saved scene by
scene (the frame, the description, its search entry) as each is made.

One scene at a time: the job holds one of the vision machines' slots. A
scene whose frame can't be read, or that the model can't describe, is the
video's failure once the rest are done; the machines' trouble stops the
video, charging nothing; a scene dropped meanwhile (its video's scenes were
found again) is skipped.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from src.producers.runner import Waits, Work

logger = logging.getLogger(__name__)


def described(scene: dict, lineage: dict | None) -> bool:
    """Whether a scene is already described the way this job would describe
    it, so it's skipped. A scene described another way (an older model, an
    upgrade's) is described again; a server that doesn't say how a scene
    was made gets the old rule: described is done."""
    if scene.get("description") is None:
        return False
    if lineage is None or "lineage" not in scene:
        return True
    made = scene.get("lineage") or {}
    return all(made.get(k) == lineage.get(k) for k in ("producer", "version", "settings_hash"))


@dataclass
class Scene:
    """One scene described: its frame (a file, gone once saved) and what the model said."""

    scene: dict
    frame: Path
    description: str
    tags: list[str]


class SceneVision(Work):
    artifact = "scene_vision"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        guard = acct.guard("vision")
        self.model = guard.model
        self.used = acct.producers.with_model(self.artifact, self.model)
        self.provider = guard.provider(settings=self.used)

    def make(self, clip: dict) -> Iterator[Scene]:
        from src.processing.video.clip_extractor import extract_video_frame_detailed
        from src.processing.workers.captions.base import CaptionError

        source = self.acct.analysis_cache.get(clip["asset_id"])
        if source is None or not source.is_file():
            raise Waits("no analysis proxy")
        scenes = self.client.get(f"/v1/video/{clip['asset_id']}/scenes").json().get("scenes", [])
        lineage = self.lineage(clip, used=self.used)
        # A redo's video with every scene described as this job would: what's
        # stale is the video's own record (made before its file had a hash,
        # say), so they're all described again and it's recorded.
        again = bool(clip.get("redo")) and bool(scenes) and all(described(s, lineage) for s in scenes)
        errors: list[str] = []
        tried = 0
        for scene in scenes:
            if not again and described(scene, lineage):
                continue
            tried += 1
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                frame = Path(tmp.name)
            try:
                attempt = extract_video_frame_detailed(source, frame, timestamp=scene["rep_frame_ms"] / 1000.0)
                if not attempt.ok or not frame.exists() or frame.stat().st_size == 0:
                    raise _NoFrame(f"no frame at {scene['rep_frame_ms']} ms")
                result = self.provider.describe(frame)
                if not result or not (result.get("description") or "").strip():
                    raise _NoFrame("the model said nothing")
            except CaptionError as e:
                frame.unlink(missing_ok=True)
                if e.endpoint_fault:  # the machines': the video stops, its guard decides
                    raise self.machines_or_clip(e) from e
                logger.warning("scene-enrich: %s scene %s — the model couldn't describe it: %s",
                               clip["rel_path"], scene["scene_id"], e)
                errors.append(str(e))
                continue
            except _NoFrame as e:
                frame.unlink(missing_ok=True)
                logger.warning("scene-enrich: %s scene %s — %s", clip["rel_path"], scene["scene_id"], e)
                errors.append(str(e))
                continue
            except BaseException:
                frame.unlink(missing_ok=True)
                raise
            yield Scene(scene, frame, (result.get("description") or "").strip(),
                        [t.strip() for t in (result.get("tags") or []) if isinstance(t, str) and t.strip()])
        if errors:
            # Its undescribed scenes are handed out again once its turn comes.
            raise self.machines_or_clip(f"{len(errors)} of {tried} scenes failed: {errors[0]}")

    def save(self, client, made: list[tuple[dict, Scene]]) -> None:
        from src.processing.api import LumiverbAPIError

        for clip, s in made:
            scene_id, asset_id = s.scene["scene_id"], clip["asset_id"]
            try:
                with open(s.frame, "rb") as f:
                    client.post(f"/v1/assets/{asset_id}/artifacts/scene_rep",
                                files={"file": ("rep.jpg", f, "image/jpeg")},
                                data={"rep_frame_ms": str(s.scene["rep_frame_ms"])})
                client.patch(f"/v1/video/scenes/{scene_id}", json={
                    "model_id": self.model, "model_version": "1", "description": s.description, "tags": s.tags,
                    "lineage": self.lineage(clip, used=self.used)})
                client.post(f"/v1/video/scenes/{scene_id}/sync", json={"asset_id": asset_id})
            except LumiverbAPIError as e:
                if e.code != "scene_gone":
                    raise
                # Its video's scenes were found again: the new ones are described next.
                logger.info("scene-enrich: %s scene %s — gone, its scenes were found again", clip["rel_path"],
                            scene_id)
                continue
            logger.info("scene-enrich: %s scene %s — rep frame at %dms + vision", clip["rel_path"], scene_id,
                        s.scene["rep_frame_ms"])

    def done(self, clip: dict, made: Scene) -> None:
        made.frame.unlink(missing_ok=True)


class _NoFrame(Exception):
    """Nothing to save for a scene: its frame couldn't be read, or the model said nothing."""
