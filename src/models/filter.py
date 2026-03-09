"""Asset filter spec for targeted reprocessing. See ADR-002."""

from datetime import datetime

from pydantic import BaseModel


class AssetFilterSpec(BaseModel):
    # Scope
    library_id: str
    asset_id: str | None = None  # single asset — overrides all other filters

    # Path filters
    path_prefix: str | None = None  # rel_path LIKE 'path_prefix/%'
    path_exact: str | None = None  # rel_path = 'exact/path/file.jpg'

    # Time filters
    mtime_after: datetime | None = None
    mtime_before: datetime | None = None

    # Status filters
    missing_proxy: bool = False
    missing_thumbnail: bool = False

    # EXIF / camera filters
    camera_make: str | None = None  # camera_make ILIKE value
    camera_model: str | None = None  # camera_model ILIKE value
    missing_exif: bool = False  # exif_extracted_at IS NULL
    taken_after: datetime | None = None
    taken_before: datetime | None = None
