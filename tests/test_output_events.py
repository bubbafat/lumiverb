"""Fast tests for shared worker JSONL output_events."""

import io
import json
import sys
from unittest.mock import patch

import pytest

from src.workers.output_events import emit_event


@pytest.mark.fast
def test_emit_event_human_mode_writes_nothing() -> None:
    """When output_mode is 'human', emit_event does not write to stdout."""
    buf = io.StringIO()
    with patch.object(sys, "stdout", buf):
        emit_event("human", {"event": "start", "stage": "proxy"})
    assert buf.getvalue() == ""


@pytest.mark.fast
def test_emit_event_jsonl_mode_writes_one_line() -> None:
    """When output_mode is 'jsonl', emit_event writes one JSON object per line."""
    buf = io.StringIO()
    with patch.object(sys, "stdout", buf):
        emit_event("jsonl", {"event": "complete", "stage": "exif", "processed": 5, "failed": 0})
    line = buf.getvalue().strip()
    assert "\n" not in line
    obj = json.loads(line)
    assert obj["event"] == "complete"
    assert obj["stage"] == "exif"
    assert obj["processed"] == 5
    assert obj["failed"] == 0
