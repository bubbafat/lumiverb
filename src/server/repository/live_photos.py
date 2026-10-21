"""Live Photos: an iPhone's photo and the few seconds of video around it, two files with one identifier
(src/shared/live_photos.py; assets.live_photo_id).

A video whose identifier a photo of its library has (in sight, in the trash or archived) is that photo's
motion: out of sight as part of it (deleted_reason 'live_photo'), as an archived clip is, so nothing lists,
searches or counts it on its own and no producer works on it; the photo plays it (its playback,
routers/playback.py), so it's hidden only once it has its preview, which the scan uploads after the clip.
Paired where either half arrives: a photo's ingest (the scan sends each file's identifier with its EXIF),
a video's preview saved (routers/artifacts.py record_kept), the Live Photos producer's route (clips scanned
before), and a scan's finish (any left). A scan skips a hidden motion's file while it's unchanged
(src/server/scans.py MOTIONS). A photo deleted for good takes its motion along when no other photo has its
identifier (routers/trash.py purge_assets), and remembers its file as deleted (ignored_files).

Only the identifier pairs: a name shared with a file of the other kind means nothing (two phones'
IMG_2502, a JPEG export of a HEIC without the identifier, a short ProRes clip named as its still).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, select

from src.server.models.tenant import Asset
from src.shared.utils import utcnow

# Of a photo of the library, in any state a person can bring back (a copy handed over is gone).
_A_PHOTO_HAS_IT = """
    EXISTS (SELECT 1 FROM assets p WHERE p.library_id = {v}.library_id AND p.live_photo_id = {v}.live_photo_id
            AND p.media_type = 'image' AND p.deleted_reason IS DISTINCT FROM 'handed_over'{also})
"""


class LivePhotoRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def set_identifier(self, asset_id: str, live_photo_id: str | None) -> str | None:
        """A clip in sight's identifier, as read from its file: its library's id, or None when it isn't in
        sight. No commit."""
        return self._session.execute(text(
            "UPDATE assets SET live_photo_id = :v WHERE asset_id = :a AND deleted_at IS NULL RETURNING library_id"
        ), {"a": asset_id, "v": live_photo_id}).scalar()

    def pair(self, library_id: str, live_photo_ids: list[str] | None = None) -> list[str]:
        """The library's videos in sight that a photo's identifier makes its motion, out of sight as part
        of it, once each has its preview to play: those of these identifiers (None: of every one). Their
        ids. No commit."""
        hidden = list(self._session.execute(text(f"""
            UPDATE assets v SET deleted_at = :now, deleted_reason = 'live_photo'
            WHERE v.library_id = :lib AND v.deleted_at IS NULL AND v.media_type = 'video'
              AND v.live_photo_id IS NOT NULL AND v.video_preview_key IS NOT NULL
              AND (CAST(:ids AS text[]) IS NULL OR v.live_photo_id = ANY(CAST(:ids AS text[])))
              AND {_A_PHOTO_HAS_IT.format(v="v", also="")}
            RETURNING v.asset_id
        """), {"now": utcnow(), "lib": library_id, "ids": live_photo_ids}).scalars())
        if hidden:
            from src.server.repository import locations

            locations.fixes_gone(self._session, hidden)  # out of sight, as archived
        return hidden

    def identifier_of(self, asset_id: str) -> tuple[str, str] | None:
        """A clip in sight's library and identifier, when it has one."""
        row = self._session.execute(text(
            "SELECT library_id, live_photo_id FROM assets"
            " WHERE asset_id = :a AND deleted_at IS NULL AND live_photo_id IS NOT NULL"), {"a": asset_id}).first()
        return (row[0], row[1]) if row else None

    def motion_of(self, photo: Asset) -> Asset | None:
        """The photo's motion: its library's video with its identifier, out of sight as part of it (the
        newest, should there be several)."""
        if photo.media_type != "image":
            return None
        motion_id = self._session.execute(text("""
            SELECT v.asset_id FROM assets p
            JOIN assets v ON v.library_id = p.library_id AND v.live_photo_id = p.live_photo_id
            WHERE p.asset_id = :photo AND v.media_type = 'video' AND v.deleted_reason = 'live_photo'
            ORDER BY v.created_at DESC, v.asset_id LIMIT 1
        """), {"photo": photo.asset_id}).scalar()
        return self._session.get(Asset, motion_id) if motion_id else None

    def left_by(self, photo_ids: list[str]) -> list[Asset]:
        """The motions these photos leave without a photo when they go for good: hidden motions of their
        identifiers that no other photo of their library has."""
        if not photo_ids:
            return []
        ids = list(self._session.execute(text(f"""
            SELECT DISTINCT v.asset_id FROM assets p
            JOIN assets v ON v.library_id = p.library_id AND v.live_photo_id = p.live_photo_id
                         AND v.deleted_reason = 'live_photo'
            WHERE p.asset_id = ANY(CAST(:ids AS text[])) AND p.media_type = 'image' AND p.live_photo_id IS NOT NULL
              AND NOT {_A_PHOTO_HAS_IT.format(v="v", also=" AND NOT (p.asset_id = ANY(CAST(:ids AS text[])))")}
        """), {"ids": photo_ids}).scalars())
        if not ids:
            return []
        return list(self._session.exec(select(Asset).where(Asset.asset_id.in_(ids))).all())  # type: ignore[union-attr]
