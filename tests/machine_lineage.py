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

# Everything an ingest can store, by name (ADR-024); it reads the lineage of what it stores and ignores the rest.
INGEST_KINDS = ("lumiverb.preview", "lumiverb.image.description", "lumiverb.image.likeness", "lumiverb.video.format",
                "lumiverb.capture_time")


def made(artifact: str, source_hash: str | None = None) -> dict:
    """How a worker made the artifact (its name, or a file's: the name it's under), with the registry's
    settings. Without a source hash the server records the clip's file as it is now."""
    from src.producers.names import FILES, made_by_file

    return _REGISTRY.lineage(made_by_file(artifact) if artifact in FILES else artifact, source_hash)


def made_json(artifact: str, source_hash: str | None = None) -> str:
    """made() as a multipart form field (a single artifact upload's lineage)."""
    return json.dumps(made(artifact, source_hash))


def ingest_made(source_hash: str | None = None) -> str:
    """An ingest's lineage form field: everything it can store, by name."""
    return json.dumps({kind: made(kind, source_hash) for kind in INGEST_KINDS})


def artifacts_made(source_hash: str | None = None) -> str:
    """The batch artifact upload's lineage form field: the preview and the video preview, by name."""
    return json.dumps({name: made(name, source_hash) for name in ("lumiverb.preview", "lumiverb.video.preview")})
