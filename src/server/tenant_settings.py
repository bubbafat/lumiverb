"""Account-wide settings kept in the tenant database (system_metadata)."""

from __future__ import annotations

from sqlmodel import Session

from src.server.models.tenant import SystemMetadata
from src.shared.utils import utcnow

VIDEO_PREVIEW_MAX_SECONDS = "video_preview_max_seconds"
PUBLIC_VIDEO_PREVIEW_MAX_SECONDS = "public_video_preview_max_seconds"
# Public pages play this much of a video until an admin says otherwise.
PUBLIC_DEFAULT_SECONDS = 10
_WHOLE = "full"


def _get(session: Session, key: str) -> str | None:
    row = session.get(SystemMetadata, key)
    return None if row is None else row.value


def _put(session: Session, key: str, value: str | None) -> None:
    row = session.get(SystemMetadata, key)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(SystemMetadata(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
        session.add(row)
    session.commit()


def _seconds(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def get_video_preview_max_seconds(session: Session) -> int | None:
    """How many seconds of a video playback serves signed in; None means all of it."""
    return _seconds(_get(session, VIDEO_PREVIEW_MAX_SECONDS))


def set_video_preview_max_seconds(session: Session, value: int | None) -> None:
    _put(session, VIDEO_PREVIEW_MAX_SECONDS, None if value is None else str(value))


def get_public_video_preview_max_seconds(session: Session) -> int | None:
    """How many seconds public pages play; 10 until set, None once lifted."""
    value = _get(session, PUBLIC_VIDEO_PREVIEW_MAX_SECONDS)
    if value == _WHOLE:
        return None
    # Unset, or unreadable: public pages stay capped.
    return _seconds(value) or PUBLIC_DEFAULT_SECONDS


def set_public_video_preview_max_seconds(session: Session, value: int | None) -> None:
    _put(session, PUBLIC_VIDEO_PREVIEW_MAX_SECONDS, _WHOLE if value is None else str(value))


def playback_cap(session: Session, *, public: bool) -> int | None:
    """Seconds a video plays for this audience: the account's cap, and on public pages the public one too."""
    caps = [get_video_preview_max_seconds(session)]
    if public:
        caps.append(get_public_video_preview_max_seconds(session))
    caps = [c for c in caps if c is not None]
    return min(caps) if caps else None
