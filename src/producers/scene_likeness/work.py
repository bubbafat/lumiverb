"""A video's scenes' CLIP vectors: each scene's picture (its representative
frame, read from the analysis proxy) embedded by whichever of the CLIP job's
machines is free (ADR-022), as a photo's preview is (src/producers/clip/),
and saved a few scenes at a time as they're made
(POST /v1/video/{id}/scene-likeness), so a long video's progress is kept.

Its scenes are the scene describer's: a redo's found scenes while it has
them, else the ones shown. A scene with a vector of the model in use is
passed by, unless the video is handed out to be made again (made another
way than now: a scene's vector doesn't say how, so each is made again). A
scene whose frame can't be read, or whose picture the machine can't read,
is the video's failure once the rest are saved; the machines' trouble stops
the video, charging nothing (their guard decides); a scene dropped meanwhile
(its video's scenes were found again) is skipped by the server.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from src.producers.clip import CLIP_MODEL_ID
from src.producers.names import SCENE_LIKENESS
from src.producers.pools import CLIP_JOB
from src.producers.runner import Waits, Work

logger = logging.getLogger(__name__)

# Scenes saved a request: a long video's vectors are kept as they're made.
SAVE_EVERY = 50


@dataclass
class Vectors:
    """Some of a video's scenes' vectors, saved together: [{scene_id, vector}]."""

    scenes: list[dict]


class SceneLikeness(Work):
    artifact = SCENE_LIKENESS

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self.used = acct.producers.settings(self.artifact)
        self.embedder = acct.guard(CLIP_JOB.name).embedder(acct.models)
        # What its vectors must be (the model and weights in use, photos' too): one made with another isn't saved.
        self.expected = (CLIP_MODEL_ID, f"{self.used['model']}-{self.used['pretrained']}")

    def make(self, clip: dict) -> Iterator[Vectors]:
        from src.processing.video.clip_extractor import extract_video_frame_detailed
        from src.processing.workers.embeddings.base import EmbeddingError

        # Held while its scenes are embedded: a frame's read from it for each.
        with self.inputs.analysis_proxy(clip) as source:
            if source is None or not source.is_file():
                raise Waits("no analysis proxy")
            scenes = self.client.get(f"/v1/video/{clip['asset_id']}/scenes",
                                     params={"describing": "true"}).json().get("scenes", [])
            # Handed out to be made again: each scene's vector, whatever model it says.
            again = bool(clip.get("redo"))
            todo = [s for s in scenes if again or s.get("likeness_model") != self.expected[1]]
            errors: list[str] = []
            batch: list[dict] = []
            for scene in todo:
                # In the job's scratch while it's read: one scene's at a time.
                frame = self.scratch / "frame.jpg"
                frame.unlink(missing_ok=True)
                try:
                    attempt = extract_video_frame_detailed(source, frame, timestamp=scene["rep_frame_ms"] / 1000.0)
                    image = frame.read_bytes() if attempt.ok and frame.is_file() else b""
                finally:
                    frame.unlink(missing_ok=True)
                if not image:
                    logger.warning("scene-likeness: %s scene %s — no frame at %d ms", clip["rel_path"],
                                   scene["scene_id"], scene["rep_frame_ms"])
                    errors.append(f"no frame at {scene['rep_frame_ms']} ms")
                    continue
                try:
                    made = self.embedder.embed(image)
                except EmbeddingError as e:
                    if e.endpoint_fault:  # the machines': the video stops, judged (their guard decides)
                        raise
                    logger.warning("scene-likeness: %s scene %s — its picture can't be embedded: %s",
                                   clip["rel_path"], scene["scene_id"], e)
                    errors.append(str(e))
                    continue
                if (made.get("model_id"), made.get("model_version")) != self.expected:
                    # The machine's: it served another model under the name.
                    raise EmbeddingError(f"made with {made.get('model_id')} {made.get('model_version')}, not "
                                         f"{self.expected[1]}", endpoint_fault=True)
                batch.append({"scene_id": scene["scene_id"], "vector": made["vector"]})
                if len(batch) >= SAVE_EVERY:
                    yield Vectors(batch)
                    batch = []
            if batch:
                yield Vectors(batch)
            if errors:
                # Its scenes without a vector are handed out again once its turn comes.
                raise self.machines_or_clip(f"{len(errors)} of {len(todo)} scenes failed: {errors[0]}")

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, Vectors]]) -> None:
        for clip, vectors in made:
            client.post(f"/v1/video/{clip['asset_id']}/scene-likeness", json={
                "scenes": vectors.scenes, "model_id": self.expected[0], "model_version": self.expected[1],
                "lineage": self.lineage(clip, used=self.used)})
