"""A clip for a test about something else (a page, an artifact, faces).

Clips are made by ingest (POST /v1/ingest) and found by scans (the Scans
API, ADR-018); a client can no longer write a clip row from its own word
(tests/test_retired_endpoints.py). A test that only needs a clip to try
something else on makes it here, with the repository call ingest makes it
with (AssetRepository.create_asset): at its path, with its file's size,
time and media type, pending and online, its content (content_hash) and previews
not known yet. A test about ingest or scans makes its clips through those.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session

from src.server.database import get_engine_for_url
from src.server.repository.tenant import AssetRepository

# The time most tests give their clips' files.
MTIME = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def make_clip(db: str | Session, library_id: str, rel_path: str, *, media_type: str = "image",
              file_size: int = 1000, file_mtime: datetime | None = MTIME) -> str:
    """A new clip in the account's database (its URL, or a session): its asset_id.
    A clip in sight already at the path is an error, as in the database."""
    if isinstance(db, Session):
        return AssetRepository(db).create_asset(library_id, rel_path, file_size, file_mtime, media_type).asset_id
    with Session(get_engine_for_url(db)) as session:
        return AssetRepository(session).create_asset(library_id, rel_path, file_size, file_mtime, media_type).asset_id
