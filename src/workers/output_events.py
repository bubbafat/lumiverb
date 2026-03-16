"""Shared JSONL event emission for workers when --output jsonl is requested.

Event contract (one JSON object per line to stdout):
- event: "start" | "batch" | "complete" | "error" | "warning"
- stage: worker job type (e.g. proxy, exif, ai_vision, embed, search_sync, video-index, ...)
- Optional: library_id, path_prefix, rel_path, processed, failed, synced, skipped, batches, message
"""

from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger(__name__)


def emit_event(output_mode: str, event: dict) -> None:
    """Write a single JSONL event to stdout when output_mode is "jsonl"; no-op otherwise."""
    if output_mode != "jsonl":
        return
    try:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        logger.debug("Failed to emit worker event", exc_info=True)
