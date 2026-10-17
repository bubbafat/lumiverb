"""Reading a clip's capture facts from its original: a job's files in one
exiftool, then each clip's saved (PUT /v1/assets/{id}/capture)."""

from __future__ import annotations

import logging

from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


class Capture(Work):
    artifact = "capture"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self._read: dict[str, dict] = {}  # asset_id -> its tags, read ahead
        self._ahead = False

    def _read_ahead(self) -> None:
        """Every clip of the job's tags, in one exiftool: those whose originals
        are there (quietly: each one's make() says why another isn't)."""
        from src.processing.workers.exif_extract import extract_exif_many

        self._ahead = True
        with self.inputs.originals(self.job.items) as paths:
            if not paths:
                return
            try:
                tags = extract_exif_many(list(paths.values()))
            except Exception:  # noqa: BLE001 — each clip is read alone instead, and judged then
                logger.warning("capture: reading %d files at once failed; reading each alone", len(paths),
                               exc_info=True)
                return
            self._read = {asset_id: tags[str(p)] for asset_id, p in paths.items() if str(p) in tags}

    def make(self, clip: dict) -> dict:
        from src.processing.workers.exif_extract import capture_facts, extract_exif_many

        if not self._ahead:
            self._read_ahead()
        with self.inputs.original(clip) as source:  # the storage or the file gone: the inputs' rules
            tags = self._read.pop(clip["asset_id"], None)
            if tags is None:
                tags = extract_exif_many([source]).get(str(source))  # OSError: this machine's, a crash
                if tags is None:
                    raise Failed("exiftool couldn't read the file")
        return capture_facts(tags)

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, facts in made:
            client.put(f"/v1/assets/{clip['asset_id']}/capture", json={**facts, "lineage": self.lineage(clip)})
