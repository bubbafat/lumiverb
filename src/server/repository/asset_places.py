"""A clip's place name as the built-in Place names left it (ADR-021):
asset_place, the nearest town to the location the clip shows (a person's,
else the file's GPS, else an applied guess) and the landmark it's at, kept
with that location. Nothing names clips here any more (ADR-023 phase 3):
the GeoNames plug-in's facts do. Until part 4 drops it, a clip's old name
is shown while it's all there is (STILL_SHOWN): GeoNames on in the account
by its own switch, the clip showing the location it was named from, and no
name of the plug-in's saved for it yet. Signed-in readers only (the asset
detail's `place`), never a public page.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.repository.clip_inputs import located
from src.server.repository.locations import CREDITS

PLUGIN = "geonames"


def still_shown_sql() -> str:
    """SQL on asset_place ap, active_assets a and its asset_location l: the clip's old name is still what it
    shows (part 3's rule): GeoNames on here by the account's own switch, the clip showing the location it was
    named from, and nothing of the plug-in's saved for it."""
    from src.server.repository.places import on_here_sql

    return (f"{on_here_sql()} AND ap.lat = {located('lat')} AND ap.lon = {located('lon')}"
            " AND NOT EXISTS (SELECT 1 FROM plugin_outputs po WHERE po.asset_id = ap.asset_id"
            f" AND po.plugin_id = '{PLUGIN}')")


def still_shown(session: Session) -> int:
    """How many clips still show their old name (part 4 drops them once none do)."""
    return int(session.execute(text(
        "SELECT count(*) FROM asset_place ap JOIN active_assets a ON a.asset_id = ap.asset_id"
        f" LEFT JOIN asset_location l ON l.asset_id = ap.asset_id WHERE {still_shown_sql()}")).scalar() or 0)


def for_asset(session: Session, asset_id: str) -> dict[str, Any] | None:
    """The clip's old place name as the detail shows it: {label, place_id, distance_m, landmark, credit}
    (label None: no town that near; landmark {place_id, name, label, distance_m}, None: at none), or None
    (never named, or not still shown: still_shown_sql)."""
    row = session.execute(text(
        "SELECT ap.place_id, ap.label, ap.distance_m, ap.landmark_id, ap.landmark_name, ap.landmark_label,"
        " ap.landmark_distance_m FROM asset_place ap JOIN active_assets a ON a.asset_id = ap.asset_id"
        f" LEFT JOIN asset_location l ON l.asset_id = ap.asset_id WHERE ap.asset_id = :a AND {still_shown_sql()}"
    ), {"a": asset_id}).mappings().first()
    if row is None:
        return None
    landmark = ({"place_id": row["landmark_id"], "name": row["landmark_name"], "label": row["landmark_label"],
                 "distance_m": row["landmark_distance_m"]} if row["landmark_id"] else None)
    return {"label": row["label"], "place_id": row["place_id"], "distance_m": row["distance_m"],
            "landmark": landmark, "credit": CREDITS["geonames"]}
