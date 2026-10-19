"""A clip's place name (ADR-021, the Place names plug-in's producer):
asset_place, the nearest town to the location the clip shows (a person's,
else the file's GPS, else an applied guess) and the landmark it's at, kept
with that location. A
name whose location has changed since isn't shown: the clip is due again,
and the row stays until the new name is saved. Signed-in readers only
(the asset detail's `place`), never a public page.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.producers.place import located
from src.server.repository.clip_inputs import SOURCE_SQL as _SOURCE
from src.server.repository.clip_inputs import SURE_SQL as _SURE
from src.server.repository.locations import CREDITS
from src.shared.utils import utcnow


def locations(session: Session, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The location each of these clips in sight shows: {asset_id: {lat, lon, source, sure_m}} (person,
    file or guess; how sure, in meters); a clip with none is left out."""
    rows = session.execute(text(
        f"SELECT a.asset_id, {located('lat')} AS lat, {located('lon')} AS lon, {_SOURCE} AS source,"
        f" {_SURE} AS sure_m"
        " FROM active_assets a LEFT JOIN asset_location l ON l.asset_id = a.asset_id WHERE a.asset_id = ANY(:ids)"
    ), {"ids": list(asset_ids)}).mappings().all()
    return {r["asset_id"]: {"lat": float(r["lat"]), "lon": float(r["lon"]), "source": r["source"],
                            "sure_m": float(r["sure_m"] or 0)}
            for r in rows if r["lat"] is not None and r["lon"] is not None}


def save(session: Session, asset_id: str, *, place: dict[str, Any] | None, label: str | None,
         distance_m: int | None, lat: float, lon: float, landmark: dict[str, Any] | None = None) -> None:
    """The clip's place name (None: no town that near) and landmark ({place_id, name, label,
    distance_m}; None: at none), named from (lat, lon), instead of what it had. Doesn't commit."""
    session.execute(text(
        "INSERT INTO asset_place (asset_id, place_id, label, country_code, distance_m, lat, lon, named_at,"
        "   landmark_id, landmark_name, landmark_label, landmark_distance_m)"
        " VALUES (:a, :p, :label, :cc, :d, :lat, :lon, :now, :lm, :lm_name, :lm_label, :lm_d)"
        " ON CONFLICT (asset_id) DO UPDATE SET place_id = EXCLUDED.place_id, label = EXCLUDED.label,"
        "   country_code = EXCLUDED.country_code, distance_m = EXCLUDED.distance_m, lat = EXCLUDED.lat,"
        "   lon = EXCLUDED.lon, named_at = EXCLUDED.named_at, landmark_id = EXCLUDED.landmark_id,"
        "   landmark_name = EXCLUDED.landmark_name, landmark_label = EXCLUDED.landmark_label,"
        "   landmark_distance_m = EXCLUDED.landmark_distance_m"
    ), {"a": asset_id, "p": place["place_id"] if place else None, "label": label,
        "cc": place.get("country_code") if place else None, "d": distance_m if place else None,
        "lat": lat, "lon": lon, "now": utcnow(), "lm": landmark["place_id"] if landmark else None,
        "lm_name": landmark["name"] if landmark else None, "lm_label": landmark["label"] if landmark else None,
        "lm_d": landmark.get("distance_m") if landmark else None})


def for_asset(session: Session, asset_id: str) -> dict[str, Any] | None:
    """The clip's place name as the detail shows it: {label, place_id, distance_m, landmark, credit}
    (label None: no town that near; landmark {place_id, name, label, distance_m}, None: at none), or None
    (never named, or named from a location it no longer shows)."""
    row = session.execute(text(
        "SELECT ap.place_id, ap.label, ap.distance_m, ap.landmark_id, ap.landmark_name, ap.landmark_label,"
        f" ap.landmark_distance_m, ap.lat, ap.lon, {located('lat')} AS now_lat,"
        f" {located('lon')} AS now_lon FROM asset_place ap JOIN active_assets a ON a.asset_id = ap.asset_id"
        " LEFT JOIN asset_location l ON l.asset_id = ap.asset_id WHERE ap.asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    if row is None or row["now_lat"] is None or (row["lat"], row["lon"]) != (row["now_lat"], row["now_lon"]):
        return None
    landmark = ({"place_id": row["landmark_id"], "name": row["landmark_name"], "label": row["landmark_label"],
                 "distance_m": row["landmark_distance_m"]} if row["landmark_id"] else None)
    return {"label": row["label"], "place_id": row["place_id"], "distance_m": row["distance_m"],
            "landmark": landmark, "credit": CREDITS["geonames"]}
