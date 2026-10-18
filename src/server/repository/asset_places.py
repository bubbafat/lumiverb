"""A clip's place name (ADR-021, the Place names plug-in's producer):
asset_place, the nearest town to the location the clip shows (a person's,
else the file's GPS, else an applied guess), kept with that location. A
name whose location has changed since isn't shown: the clip is due again,
and the row stays until the new name is saved. Signed-in readers only
(the asset detail's `place`), never a public page.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.producers.place import located
from src.server.repository.locations import CREDITS
from src.shared.utils import utcnow

_SOURCE = ("CASE WHEN l.source = 'person' THEN 'person' WHEN a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL"
           " THEN 'file' WHEN l.source = 'time' AND l.status = 'applied' THEN 'guess' END")


def locations(session: Session, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The location each of these clips in sight shows: {asset_id: {lat, lon, source}} (person, file or
    guess); a clip with none is left out."""
    rows = session.execute(text(
        f"SELECT a.asset_id, {located('lat')} AS lat, {located('lon')} AS lon, {_SOURCE} AS source"
        " FROM active_assets a LEFT JOIN asset_location l ON l.asset_id = a.asset_id WHERE a.asset_id = ANY(:ids)"
    ), {"ids": list(asset_ids)}).mappings().all()
    return {r["asset_id"]: {"lat": float(r["lat"]), "lon": float(r["lon"]), "source": r["source"]}
            for r in rows if r["lat"] is not None and r["lon"] is not None}


def save(session: Session, asset_id: str, *, place: dict[str, Any] | None, label: str | None,
         distance_m: int | None, lat: float, lon: float) -> None:
    """The clip's place name (None: no town that near), named from (lat, lon), instead of what it had.
    Doesn't commit."""
    session.execute(text(
        "INSERT INTO asset_place (asset_id, place_id, label, country_code, distance_m, lat, lon, named_at)"
        " VALUES (:a, :p, :label, :cc, :d, :lat, :lon, :now)"
        " ON CONFLICT (asset_id) DO UPDATE SET place_id = EXCLUDED.place_id, label = EXCLUDED.label,"
        "   country_code = EXCLUDED.country_code, distance_m = EXCLUDED.distance_m, lat = EXCLUDED.lat,"
        "   lon = EXCLUDED.lon, named_at = EXCLUDED.named_at"
    ), {"a": asset_id, "p": place["place_id"] if place else None, "label": label,
        "cc": place.get("country_code") if place else None, "d": distance_m if place else None,
        "lat": lat, "lon": lon, "now": utcnow()})


def for_asset(session: Session, asset_id: str) -> dict[str, Any] | None:
    """The clip's place name as the detail shows it: {label, place_id, distance_m, credit} (label None:
    no town that near), or None (never named, or named from a location it no longer shows)."""
    row = session.execute(text(
        f"SELECT ap.place_id, ap.label, ap.distance_m, ap.lat, ap.lon, {located('lat')} AS now_lat,"
        f" {located('lon')} AS now_lon FROM asset_place ap JOIN active_assets a ON a.asset_id = ap.asset_id"
        " LEFT JOIN asset_location l ON l.asset_id = ap.asset_id WHERE ap.asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    if row is None or row["now_lat"] is None or (row["lat"], row["lon"]) != (row["now_lat"], row["now_lon"]):
        return None
    return {"label": row["label"], "place_id": row["place_id"], "distance_m": row["distance_m"],
            "credit": CREDITS["geonames"]}
