"""BrowseFilters — value object for all browse/search filter state.

Three operations:
  - **read**: attribute access (e.g., `filters.camera_make`)
  - **add**: set a field (e.g., `filters.camera_make = "Canon"`)
  - **clear**: reset to defaults (`BrowseFilters()`)

Serializable to/from query params (endpoint) and JSON (smart collections).
No filter is aware of any other — each is an independent field.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone


@dataclass
class BrowseFilters:
    """All filter state for browse/search queries. Value type."""

    # Sort
    sort: str = "taken_at"
    direction: str = "desc"

    # Scope
    library_ids: list[str] | None = None
    path_prefix: str | None = None

    # Media type (comma-separated in query params → list internally)
    media_types: list[str] | None = None

    # Tag
    tag: str | None = None

    # Camera / lens
    camera_make: str | None = None
    camera_model: str | None = None
    lens_model: str | None = None

    # EXIF ranges
    iso_min: int | None = None
    iso_max: int | None = None
    exposure_min_us: int | None = None
    exposure_max_us: int | None = None
    aperture_min: float | None = None
    aperture_max: float | None = None
    focal_length_min: float | None = None
    focal_length_max: float | None = None
    has_exposure: bool | None = None

    # GPS
    has_gps: bool | None = None
    near_lat: float | None = None
    near_lon: float | None = None
    near_radius_km: float = 1.0

    # Date
    date_from: datetime | None = None
    date_to: datetime | None = None

    # Rating (requires user context)
    favorite: bool | None = None
    star_min: int | None = None
    star_max: int | None = None
    color: list[str] | None = None
    has_rating: bool | None = None
    has_color: bool | None = None

    # Faces / people
    has_faces: bool | None = None
    person_id: str | None = None

    # Search text (only used by search endpoint / smart collections)
    q: str | None = None

    # Enrichment gaps (used by enrichment pipeline, not by browse UI)
    missing_vision: bool = False
    missing_embeddings: bool = False
    missing_faces: bool = False
    missing_face_embeddings: bool = False
    missing_video_scenes: bool = False
    missing_ocr: bool = False
    missing_scene_vision: bool = False
    missing_transcription: bool = False

    @property
    def needs_rating_join(self) -> bool:
        """Whether any rating-related filter is active."""
        return (
            self.favorite is not None
            or self.star_min is not None
            or self.star_max is not None
            or self.color is not None
            or self.has_rating is not None
            or self.has_color is not None
        )

    # ---- Serialization: JSON (for smart collection saved_query) ----

    def to_json(self) -> dict:
        """Serialize to JSON-safe dict. Omits None/default values."""
        result: dict = {}
        defaults = BrowseFilters()
        for f in fields(self):
            val = getattr(self, f.name)
            default_val = getattr(defaults, f.name)
            if val != default_val and val is not None:
                if isinstance(val, datetime):
                    result[f.name] = val.isoformat()
                else:
                    result[f.name] = val
        return result

    @classmethod
    def from_json(cls, data: dict) -> BrowseFilters:
        """Deserialize from JSON dict (as stored in saved_query)."""
        # Handle aliases: clients may use "dir" instead of "direction"
        normalized = dict(data)
        if "dir" in normalized and "direction" not in normalized:
            normalized["direction"] = normalized.pop("dir")

        kwargs: dict = {}
        for f in fields(cls):
            if f.name in normalized:
                val = normalized[f.name]
                if f.name in ("date_from", "date_to") and isinstance(val, str):
                    try:
                        kwargs[f.name] = datetime.fromisoformat(val)
                    except ValueError:
                        pass
                elif f.name == "color" and isinstance(val, str):
                    # Color can be stored as "red" or ["red", "blue"]
                    kwargs[f.name] = [c.strip() for c in val.split(",") if c.strip()]
                elif f.name in ("library_ids", "media_types") and isinstance(val, str):
                    kwargs[f.name] = [v.strip() for v in val.split(",") if v.strip()]
                else:
                    kwargs[f.name] = val
        return cls(**kwargs)

    # ---- Serialization: query params (for endpoints) ----

    @classmethod
    def from_query_params(
        cls,
        *,
        sort: str = "taken_at",
        direction: str = "desc",
        library_id: str | None = None,
        path_prefix: str | None = None,
        tag: str | None = None,
        media_type: str | None = None,
        camera_make: str | None = None,
        camera_model: str | None = None,
        lens_model: str | None = None,
        iso_min: int | None = None,
        iso_max: int | None = None,
        exposure_min_us: int | None = None,
        exposure_max_us: int | None = None,
        aperture_min: float | None = None,
        aperture_max: float | None = None,
        focal_length_min: float | None = None,
        focal_length_max: float | None = None,
        has_exposure: bool | None = None,
        has_gps: bool | None = None,
        near_lat: float | None = None,
        near_lon: float | None = None,
        near_radius_km: float = 1.0,
        date_from: str | None = None,
        date_to: str | None = None,
        favorite: bool | None = None,
        star_min: int | None = None,
        star_max: int | None = None,
        color: str | None = None,
        has_rating: bool | None = None,
        has_color: bool | None = None,
        has_faces: bool | None = None,
        person_id: str | None = None,
        q: str | None = None,
    ) -> BrowseFilters:
        """Build from endpoint query parameters."""
        f = cls()
        f.sort = sort
        f.direction = direction
        if library_id:
            f.library_ids = [lid.strip() for lid in library_id.split(",") if lid.strip()]
        f.path_prefix = path_prefix
        f.tag = tag
        if media_type:
            f.media_types = [m.strip() for m in media_type.split(",") if m.strip()]
        f.camera_make = camera_make
        f.camera_model = camera_model
        f.lens_model = lens_model
        f.iso_min = iso_min
        f.iso_max = iso_max
        f.exposure_min_us = exposure_min_us
        f.exposure_max_us = exposure_max_us
        f.aperture_min = aperture_min
        f.aperture_max = aperture_max
        f.focal_length_min = focal_length_min
        f.focal_length_max = focal_length_max
        f.has_exposure = has_exposure
        f.has_gps = has_gps if has_gps else None
        f.near_lat = near_lat
        f.near_lon = near_lon
        f.near_radius_km = near_radius_km
        f.favorite = favorite
        f.star_min = star_min
        f.star_max = star_max
        if color:
            f.color = [c.strip() for c in color.split(",") if c.strip()]
        f.has_rating = has_rating
        f.has_color = has_color
        f.has_faces = has_faces
        f.person_id = person_id
        f.q = q

        # Parse dates
        if date_from:
            try:
                f.date_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if date_to:
            try:
                f.date_to = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return f
