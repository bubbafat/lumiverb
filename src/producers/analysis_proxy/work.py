"""Rendering a video's analysis proxy from its original, uploading it (in
chunks: src/processing/uploads.py), and
keeping it in this machine's cache where the API doesn't keep it on this
disk (transcripts and scenes read it there: src/server/scheduler/inputs.py)."""

from __future__ import annotations

import logging
from pathlib import Path

from src.producers.names import ANALYSIS_PROXY
from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


class Render(Work):
    artifact = ANALYSIS_PROXY

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
            # In chunks: an hour's proxy is hundreds of MB (ADR-018 decision 9).
            client.upload(clip["asset_id"], ANALYSIS_PROXY, work, self.lineage(clip, used=self.settings.output()))
            self.inputs.rendered(clip, work)

    def done(self, clip: dict, work: Path) -> None:
        work.unlink(missing_ok=True)  # moved into the cache when it was saved, if it was
