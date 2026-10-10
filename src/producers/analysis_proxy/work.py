"""Rendering a video's analysis proxy from its original, uploading it, and
keeping it in this machine's cache (transcripts and scenes read it there)."""

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
        from src.processing.video.analysis_proxy import RenderError, render_analysis_proxy, render_timeout

        source = self.original(clip)
        # Rendered beside the cache (same filesystem, so put() is a rename),
        # under a name eviction ignores.
        work = self.acct.analysis_cache.path_for(clip["asset_id"]).with_suffix(".rendering")
        work.parent.mkdir(parents=True, exist_ok=True)
        try:
            render_analysis_proxy(source, work, self.settings, timeout=render_timeout(clip.get("duration_sec")))
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
            with open(work, "rb") as f:
                client.post(f"/v1/assets/{clip['asset_id']}/artifacts/analysis_proxy",
                            files={"file": ("analysis.mp4", f, "video/mp4")}, data=data)
            self.acct.analysis_cache.put(clip["asset_id"], work)

    def done(self, clip: dict, work: Path) -> None:
        work.unlink(missing_ok=True)  # moved into the cache when it was saved
