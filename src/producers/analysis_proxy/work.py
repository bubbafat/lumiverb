"""Rendering a video's analysis proxy from its original, uploading it, and
keeping it in this machine's cache where the API doesn't keep it on this
disk (transcripts and scenes read it there: src/server/scheduler/inputs.py)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


class Render(Work):
    artifact = "analysis_proxy"

    def __init__(self, acct, job) -> None:
        from src.processing.video.analysis_proxy import AnalysisProxySettings

        super().__init__(acct, job)
        # The account's settings, rendered with this machine's encoder and decoder.
        here = acct.machine
        self.settings = AnalysisProxySettings.for_producer(acct.producers.settings(self.artifact),
                                                           here.analysis_proxy_encoder, here.analysis_proxy_decoder,
                                                           here.gpu_decodes)

    def make(self, clip: dict) -> Path:
        from src.processing.video.analysis_proxy import RenderError, render_analysis_proxy

        # Rendered in the job's scratch (an install's is on the data disk, as
        # the cache is: put() is a rename); saved, then let go of in done.
        work = self.scratch / "analysis.mp4"
        with self.inputs.original(clip) as source:
            try:
                render_analysis_proxy(source, work, self.settings)
            except RenderError as e:
                work.unlink(missing_ok=True)
                logger.warning("Rendering the analysis proxy for %s failed: %s", clip["rel_path"], e)
                raise Failed(e) from e
            except BaseException:
                work.unlink(missing_ok=True)
                raise
        return work

    def save(self, client, made: list[tuple[dict, Path]]) -> None:
        for clip, work in made:
            data = {"lineage": json.dumps(self.lineage(clip, used=self.settings.output()))}
            client.post(f"/v1/assets/{clip['asset_id']}/artifacts/analysis_proxy",
                        files={"file": ("analysis.mp4", work, "video/mp4")}, data=data)
            self.inputs.rendered(clip, work)

    def done(self, clip: dict, work: Path) -> None:
        work.unlink(missing_ok=True)  # moved into the cache when it was saved, if it was
