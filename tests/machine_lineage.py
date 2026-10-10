"""Lineage for tests' machine writes.

The API refuses a machine write that doesn't say how it was made (Robert,
Oct 9), so a test that writes as a worker says, as a worker would: the
producer, its version and the hash of the registry's settings (what
ProducerSettings gives before it reads the server). A test about what's
current asks the account's settings instead (test_lineage_api._want).
"""

from __future__ import annotations

import json

from src.processing.producer_settings import ProducerSettings

_REGISTRY = ProducerSettings(None)

# Everything an ingest can store; it reads the lineage of what it stores and ignores the rest.
INGEST_KINDS = ("proxy", "vision", "clip", "probe", "capture")


def made(artifact: str, source_sha256: str | None = None) -> dict:
    """How a worker made the artifact, with the registry's settings. Without
    a source hash the server records the clip's file as it is now."""
    return _REGISTRY.lineage(artifact, source_sha256)


def made_json(artifact: str, source_sha256: str | None = None) -> str:
    """made() as a multipart form field (a single artifact upload's lineage)."""
    return json.dumps(made(artifact, source_sha256))


def ingest_made(source_sha256: str | None = None) -> str:
    """An ingest's lineage form field: every kind it can store, by kind."""
    return json.dumps({kind: made(kind, source_sha256) for kind in INGEST_KINDS})


def artifacts_made(source_sha256: str | None = None) -> str:
    """The batch artifact upload's lineage form field: the proxy and the preview, by kind."""
    return json.dumps({kind: made(kind, source_sha256) for kind in ("proxy", "video_preview")})
