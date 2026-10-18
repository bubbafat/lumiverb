"""The faces in a photo, found in its preview by the faces job's machines
(ADR-022: the built-in one is the scheduler's face process,
src/producers/faces/detect.py), and a job's photos saved together. The
preview is the photo as it was hashed (changed content is a new clip): the
original is never read here (ADR-020 decision 4)."""

from __future__ import annotations

import logging

from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


class Faces(Work):
    artifact = "faces"
    together = True  # one request for the job's photos

    def __init__(self, acct, job) -> None:
        from src.producers.faces.levels import gates

        super().__init__(acct, job)
        # Whichever of the faces job's machines is free (ADR-022): the built-in one or an AI worker.
        self.detector = acct.guard("faces").detector(acct.models)
        # One read: what's found and its lineage agree.
        self.used = acct.producers.settings(self.artifact)
        self.detail = acct.producers.uses(self.artifact).get("detail")
        # The gates of its level: which faces count.
        self.gates = {**self.used, **gates(self.detail)}

    def make(self, clip: dict) -> dict:
        image = self.inputs.preview_bytes(clip)
        if image is None:
            raise Failed("no proxy")
        return self.detector.detect(image, self.gates)

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        from src.shared.producers import level_of

        client.post("/v1/assets/batch-faces", json={
            "items": [{"asset_id": clip["asset_id"], "source_hash": clip.get("content_hash"), **found}
                      for clip, found in made],
            "lineage": {**self.lineage(None, used=self.used), "level": level_of(self.artifact, {"detail": self.detail})}})
