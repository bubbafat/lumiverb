"""A video's faces: frames from its analysis proxy (the times frames.py
picks for the account's Detail), each looked at by the faces' process
(src/producers/faces/detect.py, every account's), the faces grouped into
the people seen (appearances.py), and each person's crop cut from the frame
their face is shown from. Saved in one request (POST /v1/video/{id}/faces).

A frame ffmpeg or the machine can't read is left out (the others are still
looked at); a video none of whose frames can be read is its failure. The
machines' trouble (the GPU out of memory, a worker gone) waits, as for photos.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.producers.names import VIDEO_FACES
from src.producers.runner import Failed, Stopped, Waits, Work

logger = logging.getLogger(__name__)


@dataclass
class Found:
    """A video's people, as the server takes them, and their crops (WebP), in the same order."""

    detection_model: str = "insightface"
    detection_model_version: str = "buffalo_l"
    embedding_model: str = "buffalo_l"
    faces: list[dict] = field(default_factory=list)
    crops: list[bytes] = field(default_factory=list)
    frames: int = 0  # frames looked at


class VideoFaces(Work):
    artifact = VIDEO_FACES

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        # Whichever of the faces job's machines is free (ADR-022): the built-in one or an AI worker.
        self.detector = acct.guard("faces").detector(acct.models)
        # One read for the job: what's looked at and what its lineage says agree.
        self.used = acct.producers.settings(self.artifact)
        self.detail = acct.producers.uses(self.artifact).get("detail")

    def make(self, clip: dict) -> Found:
        from PIL import Image

        from src.processing.video.clip_extractor import extract_video_frame_detailed
        from src.processing.workers.faces import FaceError
        from src.producers.video_faces.appearances import appearances
        from src.producers.video_faces.frames import frame_times
        from src.shared.face_crop import face_crop

        asset_id = clip["asset_id"]
        # Held while its frames are read from it.
        with self.inputs.analysis_proxy(clip) as source:
            if source is None or not source.is_file():
                raise Waits("no analysis proxy")
            scenes = self.client.get(f"/v1/video/{asset_id}/scenes").json().get("scenes", [])
            times = frame_times(scenes, round((clip.get("duration_sec") or 0) * 1000), self.detail)
            out = Found()
            found: list[tuple[int, dict]] = []
            unread: list[str] = []
            with self.scratch_folder() as tmp:
                frames: dict[int, Path] = {}
                for ms in times:
                    if self.acct.stopping.is_set():
                        raise Stopped(asset_id)
                    frame = tmp / f"{ms}.jpg"
                    attempt = extract_video_frame_detailed(source, frame, timestamp=ms / 1000.0)
                    if not attempt.ok or not frame.is_file():
                        unread.append(f"no frame at {ms} ms")
                        continue
                    # Asked for each frame: in use, so not let go of as idle on a long video.
                    try:
                        result = self.detector.detect(frame.read_bytes(), self.used)
                    except FaceError as e:
                        if e.endpoint_fault:  # the machines' trouble (the GPU, a worker gone): the video waits
                            raise
                        unread.append(f"{ms} ms: {e}")  # this frame's: the others are still looked at
                        frame.unlink(missing_ok=True)
                        continue
                    out.frames += 1
                    out.detection_model = result["detection_model"]
                    out.detection_model_version = result["detection_model_version"]
                    out.embedding_model = result["embedding_model"]
                    if result["faces"]:
                        frames[ms] = frame  # kept for the crops
                        found += [(ms, face) for face in result["faces"]]
                    else:
                        frame.unlink(missing_ok=True)
                if unread:
                    logger.warning("video-faces: %s — %d of %d frames not looked at: %s", clip.get("rel_path"),
                                   len(unread), len(times), unread[0])
                if not out.frames:
                    raise Failed(f"no frame could be looked at: {unread[0] if unread else 'none chosen'}")
                for person in appearances(found):
                    with Image.open(frames[person.frame_ms]) as img:
                        crop = face_crop(img.convert("RGB"), person.face["bounding_box"])
                    if crop is None:  # a box with nothing in it
                        continue
                    out.faces.append({"bounding_box": person.face["bounding_box"],
                                      "detection_confidence": person.face["detection_confidence"],
                                      "embedding": person.embedding, "frame_ms": person.frame_ms,
                                      "seen_ms": person.seen_ms})
                    out.crops.append(crop)
        logger.info("video-faces: %s — %d people in %d frames", clip.get("rel_path"), len(out.faces), out.frames)
        return out

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, Found]]) -> None:
        from src.shared.producers import level_of

        level = level_of(self.artifact, {"detail": self.detail})
        for clip, found in made:
            client.post(f"/v1/video/{clip['asset_id']}/faces", data={
                "faces": json.dumps(found.faces),
                "detection_model": found.detection_model,
                "detection_model_version": found.detection_model_version,
                "embedding_model": found.embedding_model,
                "source_hash": clip.get("content_hash") or "",
                "lineage": json.dumps({**self.lineage(clip, used=self.used), "level": level}),
            }, files=[("crops", (f"{i}.webp", crop, "image/webp")) for i, crop in enumerate(found.crops)])
