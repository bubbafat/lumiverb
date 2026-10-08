"""Account-wide settings kept in the tenant database (system_metadata)."""

from __future__ import annotations

from sqlmodel import Session

from src.server.models.tenant import SystemMetadata
from src.shared.utils import utcnow

VIDEO_PREVIEW_MAX_SECONDS = "video_preview_max_seconds"


def get_video_preview_max_seconds(session: Session) -> int | None:
    """How many seconds of a video playback serves; None means all of it."""
    row = session.get(SystemMetadata, VIDEO_PREVIEW_MAX_SECONDS)
    if row is None:
        return None
    try:
        return int(row.value)
    except ValueError:
        return None


def set_video_preview_max_seconds(session: Session, value: int | None) -> None:
    row = session.get(SystemMetadata, VIDEO_PREVIEW_MAX_SECONDS)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(SystemMetadata(key=VIDEO_PREVIEW_MAX_SECONDS, value=str(value)))
    else:
        row.value = str(value)
        row.updated_at = utcnow()
        session.add(row)
    session.commit()
