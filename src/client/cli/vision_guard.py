"""Vision work only while a machine doing it offers the account's model (ADR-016 phase 3).

Descriptions, OCR and scene descriptions are the vision job's: see
src/client/cli/job_guard.py for when its work goes ahead, stops, and when a
clip is charged a failure.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.client.cli.ai_pool import PooledCaptionProvider
from src.client.cli.job_guard import JobGuard

if TYPE_CHECKING:
    from src.client.cli.failure_report import FailureReport
    from src.client.workers.captions.base import CaptionProvider


class VisionGuard(JobGuard):
    def __init__(self, client: Any, failures: FailureReport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(client, "vision", failures, clock)

    def provider(self, settings: dict | None = None, ocr_settings: dict | None = None) -> CaptionProvider:
        """Describes and reads images on whichever online machine is free."""
        from src.client.workers.captions.factory import get_caption_provider

        # Each machine is asked for the model by the name it lists it as.
        return PooledCaptionProvider(self.pool, lambda m: get_caption_provider(
            m.serves or self.pool.model, m.api_url, m.api_key, settings=settings, ocr_settings=ocr_settings))
