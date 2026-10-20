"""What a plug-in is handed for each clip (ADR-023, protocol 1's inputs): the
clip's location, when it was taken and its metadata, as the clip shows them
now, and the hash of them that what the plug-in gives back is kept with.

The hash is SQL's (hash_sql), one expression wherever it's asked: when a
plug-in's work counts as made (its output kept with the hash its clip has
now), when its inputs are handed out, and when what it gives back is saved
(refused when the clip's inputs have changed since: it's due again). So a
person moving a clip's location makes its plug-in output due again, and
what it showed stays until the new one is saved. The preview isn't in it:
it changes with the file, which lineage follows (its source).

- location: {lat, lon, sure_m, source}: a person's, else the file's GPS,
  else an applied guess, never a suggestion (source person, file or guess;
  how sure, in meters: a person's 0, the file's its GPS accuracy, a guess's
  its radius). A clip without one isn't one the plug-in applies to.
- capture_time: {utc, offset_min}: when it was taken (ISO 8601, UTC) and its
  time zone's offset when known. A clip without one isn't one it applies to.
- metadata: {media_type, width, height, duration_sec, camera_make,
  camera_model, lens}: every clip has it (a field it doesn't know is null).
- preview (photos): its preview image, handed by the scheduler (not here).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC
from typing import Any

from sqlalchemy import text
from sqlmodel import Session


def located(coord: str, a: str = "a", loc: str = "l") -> str:
    """SQL: the clip's latitude or longitude (coord "lat" or "lon") as it shows, on assets a with its
    asset_location loc LEFT JOINed: a person's, else the file's, else an applied guess; NULL without one."""
    gps = f"{a}.gps_{coord}"
    return (f"(CASE WHEN {loc}.source = 'person' THEN {loc}.{coord}"
            f" WHEN {a}.gps_lat IS NOT NULL AND {a}.gps_lon IS NOT NULL THEN {gps}"
            f" WHEN {loc}.source = 'time' AND {loc}.status = 'applied' THEN {loc}.{coord} END)")


# SQL on assets a: it shows a location: a person's, the file's GPS, or an applied guess (never a suggestion).
SHOWS_LOCATION = ("(a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL) OR EXISTS (SELECT 1 FROM asset_location pl"
                  " WHERE pl.asset_id = a.asset_id AND (pl.source = 'person' OR (pl.source = 'time'"
                  " AND pl.status = 'applied')))")

# On assets a with its asset_location l LEFT JOINed: where the location it shows comes from, and how sure it is,
# in meters (a person's its radius, 0; the file's its GPS accuracy when it says, else exact; a guess's its radius).
SOURCE_SQL = ("CASE WHEN l.source = 'person' THEN 'person' WHEN a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL"
              " THEN 'file' WHEN l.source = 'time' AND l.status = 'applied' THEN 'guess' END")
SURE_SQL = ("CASE WHEN l.source = 'person' THEN l.radius_m WHEN a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL"
            " THEN COALESCE(a.gps_accuracy_m, 0) WHEN l.source = 'time' AND l.status = 'applied' THEN l.radius_m END")

# Each input's parts, on a and l: what's handed over and hashed.
_PARTS: dict[str, tuple[str, ...]] = {
    "location": (located("lat"), located("lon"), f"COALESCE({SURE_SQL}, 0)", SOURCE_SQL),
    # Seconds since 1970 (a time zone's text would be the session's).
    "capture_time": ("extract(epoch FROM a.taken_at)", "a.taken_at_offset_min"),
    "metadata": ("a.media_type", "a.width", "a.height", "a.duration_sec", "a.camera_make", "a.camera_model",
                 "a.lens_model"),
}
# A clip without it isn't one the plug-in applies to.
_HAS: dict[str, str] = {"location": SHOWS_LOCATION, "capture_time": "a.taken_at IS NOT NULL"}
# Each of those, in a few words: "4,392 have no location".
LACKING: dict[str, str] = {"location": "location", "capture_time": "date taken"}


def has_sql(name: str) -> str | None:
    """SQL on active_assets a: the clip has this input; None for one every clip has."""
    return _HAS.get(name)


def present_sql(inputs: Iterable[str]) -> str:
    """SQL on active_assets a: the clip has every one of these inputs it could lack ("true" when none can be)."""
    return " AND ".join(f"({_HAS[i]})" for i in sorted(set(inputs)) if i in _HAS) or "true"


def hash_sql(inputs: Iterable[str]) -> str:
    """SQL on active_assets a: the hash of these inputs as the clip shows them now (md5 of their JSON)."""
    parts = [f"'{i}', {', '.join(_PARTS[i])}" for i in sorted(set(inputs)) if i in _PARTS]
    return ("(SELECT md5(jsonb_build_array(" + (", ".join(parts) or "''") + ")::text)"
            " FROM (SELECT 1) one LEFT JOIN asset_location l ON l.asset_id = a.asset_id)")


class ClipInputsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def of(self, asset_ids: list[str], inputs: Iterable[str], applies: str = "true") -> dict[str, dict[str, Any]]:
        """Each of these clips in sight that has every input and that applies says (a producer's, SQL on
        active_assets a): its "inputs" as handed over, by name, and their "inputs_hash"; the rest are left out."""
        wanted = sorted(set(inputs))
        rows = self._session.execute(text(
            "SELECT a.asset_id, a.media_type, a.width, a.height, a.duration_sec, a.camera_make, a.camera_model,"
            f" a.lens_model, a.taken_at, a.taken_at_offset_min, {located('lat')} AS lat, {located('lon')} AS lon,"
            f" {SOURCE_SQL} AS source, {SURE_SQL} AS sure_m, {hash_sql(wanted)} AS inputs_hash"
            " FROM active_assets a LEFT JOIN asset_location l ON l.asset_id = a.asset_id"
            f" WHERE a.asset_id = ANY(:ids) AND {present_sql(wanted)} AND ({applies})"
        ), {"ids": list(asset_ids)}).mappings().all()
        return {r["asset_id"]: {"inputs": {i: _value(i, r) for i in wanted if i in _PARTS},
                                "inputs_hash": r["inputs_hash"]} for r in rows}

    def hashes(self, asset_ids: list[str], inputs: Iterable[str], applies: str = "true") -> dict[str, str]:
        """The inputs' hash each of these clips in sight shows now, of those that have every input and that
        applies says."""
        wanted = sorted(set(inputs))
        return {r[0]: r[1] for r in self._session.execute(text(
            f"SELECT a.asset_id, {hash_sql(wanted)} FROM active_assets a"
            f" WHERE a.asset_id = ANY(:ids) AND {present_sql(wanted)} AND ({applies})"
        ), {"ids": list(asset_ids)}).all()}


def _value(name: str, r: Any) -> dict[str, Any]:
    if name == "location":
        return {"lat": float(r["lat"]), "lon": float(r["lon"]), "sure_m": float(r["sure_m"] or 0),
                "source": r["source"]}
    if name == "capture_time":
        taken = r["taken_at"].astimezone(UTC)
        return {"utc": taken.isoformat().replace("+00:00", "Z"), "offset_min": r["taken_at_offset_min"]}
    return {"media_type": r["media_type"], "width": r["width"], "height": r["height"],
            "duration_sec": r["duration_sec"], "camera_make": r["camera_make"], "camera_model": r["camera_model"],
            "lens": r["lens_model"]}
