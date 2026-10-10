"""A clip's location beyond what its file says (ADR-017): asset_location.

One row per clip: a person's location, a guess, or a suggestion. The
file's GPS stays on assets and only the scan writes it. Which location a
clip shows: a person's, then the file's, then an applied guess (only when
guesses are asked for); a suggestion never counts. Filters compute it in
SQL (src/server/models/query_filter.py, location_sql); nothing here is
copied onto assets.

A person's location records lineage with producer "person"
(src/shared/producers.py), so it's never made again over; clearing it
forgets that lineage, so the machine may guess again.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.repository import lineage
from src.shared.utils import utcnow

ARTIFACT = "location"
PERSON, TIME, SUGGESTION = "person", "time", "suggestion"
APPLIED, SUGGESTED = "applied", "suggested"


def states(session: Session, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Clips in sight among these: whether the file has GPS, and their
    asset_location row's source and status (None without one)."""
    rows = session.execute(text(
        "SELECT a.asset_id, a.library_id, (a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL) AS has_file,"
        " loc.source, loc.status"
        " FROM active_assets a LEFT JOIN asset_location loc ON loc.asset_id = a.asset_id"
        " WHERE a.asset_id = ANY(:ids)"
    ), {"ids": asset_ids}).mappings().all()
    return {r["asset_id"]: dict(r) for r in rows}


def lock(session: Session, asset_ids: list[str]) -> None:
    """Hold these clips until the transaction ends, so what states() saw is
    what's replaced (two requests can't both pass the 409 check)."""
    session.execute(text("SELECT 1 FROM assets WHERE asset_id = ANY(:ids) ORDER BY asset_id FOR UPDATE"),
                    {"ids": asset_ids})


def fix_of(session: Session, asset_id: str) -> tuple[float, float] | None:
    """A clip's location that counts as a fix: a person's, else the file's.
    Guesses and suggestions aren't (None then, or for a clip not in sight)."""
    row = session.execute(text(
        "SELECT CASE WHEN loc.source = 'person' THEN loc.lat ELSE a.gps_lat END,"
        "       CASE WHEN loc.source = 'person' THEN loc.lon ELSE a.gps_lon END"
        " FROM active_assets a LEFT JOIN asset_location loc ON loc.asset_id = a.asset_id"
        " WHERE a.asset_id = :a"
    ), {"a": asset_id}).first()
    if row is None or row[0] is None or row[1] is None:
        return None
    return float(row[0]), float(row[1])


def set_person(session: Session, asset_ids: list[str], lat: float, lon: float, *,
               set_by: str | None, basis: dict[str, Any]) -> None:
    """A person's exact point for these clips, over whatever row they had
    (a guess, a suggestion, another person's). Doesn't commit."""
    now = utcnow()
    for asset_id in asset_ids:
        session.execute(text(
            "INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_by, set_at)"
            " VALUES (:a, :lat, :lon, 0, 'person', 'applied', CAST(:basis AS jsonb), :by, :now)"
            " ON CONFLICT (asset_id) DO UPDATE SET lat = EXCLUDED.lat, lon = EXCLUDED.lon, radius_m = 0,"
            "   source = 'person', status = 'applied', basis = EXCLUDED.basis, set_by = EXCLUDED.set_by,"
            "   set_at = EXCLUDED.set_at"
        ), {"a": asset_id, "lat": lat, "lon": lon, "basis": json.dumps(basis), "by": set_by, "now": now})
        lineage.record(session, asset_id, ARTIFACT, None, person=True, produced_at=now, commit=False)


def clear_person(session: Session, asset_ids: list[str]) -> list[str]:
    """Remove a person's location from these clips in sight (guesses and
    suggestions stay). Returns those cleared. Doesn't commit."""
    cleared = [r[0] for r in session.execute(text(
        "DELETE FROM asset_location loc USING active_assets a"
        " WHERE a.asset_id = loc.asset_id AND loc.asset_id = ANY(:ids) AND loc.source = 'person'"
        " RETURNING loc.asset_id"
    ), {"ids": asset_ids}).all()]
    lineage.forget(session, cleared, ARTIFACT, commit=False)
    return cleared


def accept(session: Session, asset_ids: list[str], *, set_by: str | None, by_name: str | None) -> list[str]:
    """Suggestions on these clips in sight become a person's location (their
    point and radius kept). Returns those accepted. Doesn't commit."""
    now = utcnow()
    accepted = [r[0] for r in session.execute(text(
        "UPDATE asset_location loc SET source = 'person', status = 'applied', set_by = :by, set_at = :now,"
        "   basis = loc.basis || CAST(:extra AS jsonb)"
        " FROM active_assets a"
        " WHERE a.asset_id = loc.asset_id AND loc.asset_id = ANY(:ids) AND loc.status = 'suggested'"
        " RETURNING loc.asset_id"
    ), {"ids": asset_ids, "by": set_by, "now": now,
        "extra": json.dumps({"accepted": True, "by": by_name})}).all()]
    for asset_id in accepted:
        lineage.record(session, asset_id, ARTIFACT, None, person=True, produced_at=now, commit=False)
    return accepted


def summary(session: Session, row: dict[str, Any]) -> str | None:
    """A short reason for a location, as the Lightbox shows it."""
    basis = row.get("basis") or {}
    same_as = basis.get("same_as")
    if same_as:
        rel = session.execute(text("SELECT rel_path FROM assets WHERE asset_id = :a"), {"a": same_as}).scalar()
        return f"Same place as {rel.rsplit('/', 1)[-1]}" if rel else None
    return basis.get("summary") or None


def for_asset(session: Session, asset_id: str) -> dict[str, Any] | None:
    """A clip's asset_location row as the detail shows it (signed-in only), or None."""
    row = session.execute(text(
        "SELECT lat, lon, radius_m, source, status, basis, set_at FROM asset_location WHERE asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    if row is None:
        return None
    row = dict(row)
    basis = row["basis"] or {}
    return {
        "lat": row["lat"], "lon": row["lon"], "radius_m": row["radius_m"],
        "source": row["source"], "status": row["status"],
        "basis_summary": summary(session, row),
        "set_by": basis.get("by") if row["source"] == PERSON else None,
        "set_at": row["set_at"].isoformat() if row["set_at"] else None,
    }
