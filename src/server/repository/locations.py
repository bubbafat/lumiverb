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

Guesses (phase 3) are the location producer's (src/producers/location/):
it reads a clip's nearest fixes and its device's scene pairs here and
saves what it found. A person's location is a fix; a guess never is.
Whenever a fix appears, moves or goes (a person sets, clears or accepts
one, a scan finds GPS, the capture time is read again), the guesses
around it go and are made again (remake_neighbours).
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
    # New fixes: the guesses around them are made again.
    remake_neighbours(session, asset_ids)


def clear_person(session: Session, asset_ids: list[str]) -> list[str]:
    """Remove a person's location from these clips in sight (guesses and
    suggestions stay). Returns those cleared. Doesn't commit."""
    cleared = [r[0] for r in session.execute(text(
        "DELETE FROM asset_location loc USING active_assets a"
        " WHERE a.asset_id = loc.asset_id AND loc.asset_id = ANY(:ids) AND loc.source = 'person'"
        " RETURNING loc.asset_id"
    ), {"ids": asset_ids}).all()]
    lineage.forget(session, cleared, ARTIFACT, commit=False)
    # Fixes gone: the guesses around them (and theirs) are made again.
    remake_neighbours(session, cleared)
    return cleared


def accept(session: Session, asset_ids: list[str], *, set_by: str | None) -> list[str]:
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
        "extra": json.dumps({"accepted": True})}).all()]
    for asset_id in accepted:
        lineage.record(session, asset_id, ARTIFACT, None, person=True, produced_at=now, commit=False)
    remake_neighbours(session, accepted)
    return accepted


def summary(session: Session, row: dict[str, Any]) -> str | None:
    """A short reason for a location, as the Lightbox shows it."""
    basis = row.get("basis") or {}
    same_as = basis.get("same_as")
    if same_as:
        rel = session.execute(text("SELECT rel_path FROM assets WHERE asset_id = :a"), {"a": same_as}).scalar()
        return f"Same place as {rel.rsplit('/', 1)[-1]}" if rel else None
    return basis.get("summary") or None


def display_name(user_id: str | None) -> str | None:
    """Who set it, for a signed-in reader: the user's email (users have no
    other name). None for an API key, a user gone, or the control plane away."""
    if not user_id or user_id.startswith("key:"):
        return None
    try:
        from src.server.database import get_control_session
        from src.server.repository.control_plane import UserRepository

        with get_control_session() as ctrl:
            user = UserRepository(ctrl).get_by_id(user_id)
            return user.email if user else None
    except Exception:
        return None


def for_asset(session: Session, asset_id: str) -> dict[str, Any] | None:
    """A clip's asset_location row as the detail shows it, or None. Signed-in
    requests only: set_by is resolved to a name here, at read time."""
    row = session.execute(text(
        "SELECT lat, lon, radius_m, source, status, basis, set_by, set_at FROM asset_location WHERE asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    if row is None:
        return None
    row = dict(row)
    return {
        "lat": row["lat"], "lon": row["lon"], "radius_m": row["radius_m"],
        "source": row["source"], "status": row["status"],
        "basis_summary": summary(session, row),
        "set_by": display_name(row["set_by"]) if row["source"] == PERSON else None,
        "set_at": row["set_at"].isoformat() if row["set_at"] else None,
    }


# ---------------------------------------------------------------------------
# Guesses: the location producer's reads and writes (phase 3)
# ---------------------------------------------------------------------------

def device_sql(alias: str) -> str:
    """A clip's device: make + model + serial number, the serial empty when
    the file has none (ADR-017). The producer compares these strings as the
    server gives them."""
    return (f"concat_ws('|', COALESCE({alias}.camera_make, ''), COALESCE({alias}.camera_model, ''),"
            f" COALESCE({alias}.exif->>'SerialNumber', ''))")


def _is_fix(f: str, pl: str) -> str:
    """A fix: the file's GPS or a person's location, never a guess or a
    suggestion. On active_assets f with asset_location pl LEFT JOINed for
    source = 'person'."""
    return f"({pl}.asset_id IS NOT NULL OR ({f}.gps_lat IS NOT NULL AND {f}.gps_lon IS NOT NULL))"


def _fix_cols(f: str, pl: str) -> str:
    """A fix's point: a person's wins over the file's (ADR-017)."""
    return (f"CASE WHEN {pl}.asset_id IS NOT NULL THEN {pl}.lat ELSE {f}.gps_lat END AS lat,"
            f" CASE WHEN {pl}.asset_id IS NOT NULL THEN {pl}.lon ELSE {f}.gps_lon END AS lon,"
            f" CASE WHEN {pl}.asset_id IS NOT NULL THEN NULL ELSE {f}.gps_accuracy_m END AS accuracy_m,"
            f" CASE WHEN {pl}.asset_id IS NOT NULL THEN 'person' ELSE 'file' END AS source")


def _gap_sql(c: str, f: str, c_device: str) -> str:
    """How long after fix f clip c was taken, in minutes, as
    src/producers/location/infer.py gap_minutes says: real instants when both
    zones are known, else wall clocks; a fix from another device corrected
    by :delta (the clip's device's clock offset)."""
    return (f"(EXTRACT(EPOCH FROM ({c}.taken_at - {f}.taken_at)) / 60.0"
            f" - CASE WHEN {c}.taken_at_offset_min IS NOT NULL AND {f}.taken_at_offset_min IS NOT NULL"
            f"   THEN {c}.taken_at_offset_min - {f}.taken_at_offset_min ELSE 0 END"
            f" - CASE WHEN {device_sql(f)} <> {c_device} THEN CAST(:delta AS double precision) ELSE 0 END)")


def settings(session: Session) -> dict[str, Any]:
    """The location producer's settings as the account has them now."""
    from src.shared.producers import effective_settings

    return effective_settings(ARTIFACT, lineage.overrides(session, ARTIFACT))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _label(make: str | None, model: str | None, media_type: str, source: str) -> str:
    """What a guess's reason calls a fix: "iPhone 15 Pro photos", "placed clips"."""
    if source == PERSON:
        return "placed clips"
    noun = "photos" if media_type == "image" else "clips"
    name = (model or make or "").strip()
    return f"{name} {noun}" if name else f"other {noun}"


def clocks(session: Session, asset_ids: list[str], *, min_similarity: float, window_min: int,
           most: int = 300) -> dict[str, Any]:
    """Each clip's device and day, and for each such day the scene pairs its
    clock offset is voted from: a clip of that device and day with no fix,
    and the fix from another device in its library within window_min minutes
    (as written) that looks most like it (CLIP), when at least min_similarity
    alike. At most `most` of the device's clips a day.
    {"clips": {asset_id: key}, "days": {key: {device, pairs: [...]}}}."""
    from src.shared.producers import CLIP_MODEL_ID

    rows = session.execute(text(
        f"SELECT a.asset_id, a.library_id, {device_sql('a')} AS device,"
        " CAST((a.taken_at AT TIME ZONE 'UTC') AS date) AS day"
        " FROM active_assets a WHERE a.asset_id = ANY(:ids) AND a.taken_at IS NOT NULL"
    ), {"ids": asset_ids}).mappings().all()
    out: dict[str, Any] = {"clips": {}, "days": {}}
    for r in rows:
        key = f"{r['library_id']}|{r['day'].isoformat()}|{r['device']}"
        out["clips"][r["asset_id"]] = key
        if key in out["days"]:
            continue
        pairs = session.execute(text(
            "WITH cam AS ("
            "  SELECT a.asset_id, a.taken_at, a.taken_at_offset_min, e.embedding_vector AS v"
            "  FROM active_assets a"
            "  JOIN asset_embeddings e ON e.asset_id = a.asset_id AND e.model_id = :model"
            "  LEFT JOIN asset_location pl ON pl.asset_id = a.asset_id AND pl.source = 'person'"
            f"  WHERE a.library_id = :lib AND {device_sql('a')} = :device"
            "    AND CAST((a.taken_at AT TIME ZONE 'UTC') AS date) = :day"
            f"    AND NOT {_is_fix('a', 'pl')}"
            "  ORDER BY a.taken_at, a.asset_id LIMIT :most)"
            " SELECT cam.asset_id AS camera_id, cam.taken_at AS camera_taken_at,"
            "   cam.taken_at_offset_min AS camera_offset_min, best.*"
            " FROM cam CROSS JOIN LATERAL ("
            "   SELECT f.asset_id AS fix_id, f.taken_at AS fix_taken_at, f.taken_at_offset_min AS fix_offset_min,"
            f"    {device_sql('f')} AS fix_device, 1 - (fe.embedding_vector <=> cam.v) AS similarity"
            "   FROM active_assets f"
            "   JOIN asset_embeddings fe ON fe.asset_id = f.asset_id AND fe.model_id = :model"
            "   LEFT JOIN asset_location pl ON pl.asset_id = f.asset_id AND pl.source = 'person'"
            f"  WHERE f.library_id = :lib AND {device_sql('f')} <> :device AND {_is_fix('f', 'pl')}"
            "     AND f.taken_at BETWEEN cam.taken_at - make_interval(mins => :window)"
            "                        AND cam.taken_at + make_interval(mins => :window)"
            "   ORDER BY fe.embedding_vector <=> cam.v, f.asset_id LIMIT 1) best"
            " WHERE best.similarity >= :min_sim"
            " ORDER BY cam.taken_at, cam.asset_id"
        ), {"model": CLIP_MODEL_ID, "lib": r["library_id"], "device": r["device"], "day": r["day"],
            "most": most, "window": int(window_min), "min_sim": float(min_similarity)}).mappings().all()
        out["days"][key] = {"device": r["device"], "pairs": [
            {**dict(p), "camera_taken_at": _iso(p["camera_taken_at"]), "fix_taken_at": _iso(p["fix_taken_at"]),
             "similarity": float(p["similarity"])} for p in pairs]}
    return out


def context(session: Session, asset_id: str, *, minutes: int, clock_offset_min: float) -> dict[str, Any] | None:
    """A clip's time and device, and the nearest fix before and after it in
    its library within `minutes` (gaps as infer.gap_minutes, with the clip's
    device's clock offset). None for a clip not in sight."""
    clip = session.execute(text(
        "SELECT a.asset_id, a.library_id, a.taken_at, a.taken_at_offset_min,"
        f" {device_sql('a')} AS device FROM active_assets a WHERE a.asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    if clip is None:
        return None
    out: dict[str, Any] = {"clip": {**dict(clip), "taken_at": _iso(clip["taken_at"])}, "fixes": []}
    if clip["taken_at"] is None:
        return out
    # Zones up to 28 h apart and the clock offset can lie between the wall
    # clocks: looked for that far, then judged by the gap itself.
    reach = int(minutes + abs(clock_offset_min) + 28 * 60)
    fixes = session.execute(text(
        "WITH c AS (SELECT a.asset_id, a.library_id, a.taken_at, a.taken_at_offset_min,"
        f"   {device_sql('a')} AS device FROM active_assets a WHERE a.asset_id = :a),"
        " fx AS ("
        "  SELECT f.asset_id, f.taken_at, f.taken_at_offset_min, f.camera_make, f.camera_model, f.media_type,"
        f"   {device_sql('f')} AS device, {_fix_cols('f', 'pl')}, {_gap_sql('c', 'f', 'c.device')} AS gap"
        "  FROM c JOIN active_assets f ON f.library_id = c.library_id AND f.asset_id <> c.asset_id"
        "  LEFT JOIN asset_location pl ON pl.asset_id = f.asset_id AND pl.source = 'person'"
        f"  WHERE {_is_fix('f', 'pl')}"
        "    AND f.taken_at BETWEEN c.taken_at - make_interval(mins => :reach)"
        "                       AND c.taken_at + make_interval(mins => :reach))"
        " (SELECT * FROM fx WHERE gap >= 0 AND gap <= :minutes ORDER BY gap, asset_id LIMIT 1)"
        " UNION ALL"
        " (SELECT * FROM fx WHERE gap < 0 AND gap >= -CAST(:minutes AS double precision)"
        "  ORDER BY gap DESC, asset_id LIMIT 1)"
    ), {"a": asset_id, "minutes": float(minutes), "delta": float(clock_offset_min), "reach": reach}
    ).mappings().all()
    for f in fixes:
        out["fixes"].append({
            "asset_id": f["asset_id"], "taken_at": _iso(f["taken_at"]),
            "taken_at_offset_min": f["taken_at_offset_min"], "device": f["device"], "lat": f["lat"],
            "lon": f["lon"], "accuracy_m": f["accuracy_m"], "source": f["source"], "gap_min": float(f["gap"]),
            "label": _label(f["camera_make"], f["camera_model"], f["media_type"], f["source"])})
    return out


def save_guess(session: Session, asset_id: str, guess: dict[str, Any] | None, made: dict[str, Any]) -> str:
    """The producer's guess for a clip ("stored"), or that it found none
    ("empty": its guess goes, and lineage says it tried). Never over a
    person's location or the file's GPS ("kept": nothing written), nor over
    a suggestion (its lineage is recorded, the row left). "gone" for a clip
    not in sight. Doesn't commit."""
    lock(session, [asset_id])
    row = session.execute(text(
        "SELECT (a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL) AS has_file, loc.source"
        " FROM active_assets a LEFT JOIN asset_location loc ON loc.asset_id = a.asset_id WHERE a.asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    if row is None:
        return "gone"
    if row["source"] == PERSON or row["has_file"]:
        return "kept"
    if guess is None:
        session.execute(text("DELETE FROM asset_location WHERE asset_id = :a AND source = :time"),
                        {"a": asset_id, "time": TIME})
        lineage.record(session, asset_id, ARTIFACT, made, outcome="empty", commit=False)
        return "empty"
    session.execute(text(
        "INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_by, set_at)"
        " VALUES (:a, :lat, :lon, :radius, :time, :applied, CAST(:basis AS jsonb), NULL, :now)"
        " ON CONFLICT (asset_id) DO UPDATE SET lat = EXCLUDED.lat, lon = EXCLUDED.lon,"
        "   radius_m = EXCLUDED.radius_m, basis = EXCLUDED.basis, set_at = EXCLUDED.set_at"
        " WHERE asset_location.source = :time"
    ), {"a": asset_id, "lat": guess["lat"], "lon": guess["lon"], "radius": int(guess["radius_m"]), "time": TIME,
        "applied": APPLIED, "basis": json.dumps(guess.get("basis") or {}), "now": utcnow()})
    lineage.record(session, asset_id, ARTIFACT, made, commit=False)
    return "stored"


def remake_neighbours(session: Session, asset_ids: list[str]) -> list[str]:
    """These clips gained, lost or moved a fix (file GPS, a person's
    location, a new time): the guesses that may change go, with their
    lineage, so the producer makes them again (as missing, before anything
    stale). That's every clip in the same library whose time is within the
    inference window of one of them, plus the 14 h a camera's clock may be
    off by, the clips themselves, and any guess made from one of them. A
    person's location is never touched. Nothing while inferring is off.
    Returns the clips whose guesses went. Doesn't commit."""
    if not asset_ids:
        return []
    now = settings(session)
    if not now.get("infer_location"):
        return []
    window = int(now["inference_minutes"]) + 14 * 60
    near = [r[0] for r in session.execute(text(
        "SELECT x.asset_id FROM assets x WHERE x.asset_id = ANY(:ids)"
        " UNION"
        # Zones up to 28 h apart can lie between two wall clocks: the index range, then the gap.
        " SELECT n.asset_id FROM assets x JOIN assets n ON n.library_id = x.library_id"
        "   AND n.taken_at BETWEEN x.taken_at - make_interval(mins => :reach)"
        "                      AND x.taken_at + make_interval(mins => :reach)"
        " WHERE x.asset_id = ANY(:ids)"
        "   AND abs(EXTRACT(EPOCH FROM (n.taken_at - x.taken_at)) / 60.0"
        "     - CASE WHEN n.taken_at_offset_min IS NOT NULL AND x.taken_at_offset_min IS NOT NULL"
        "       THEN n.taken_at_offset_min - x.taken_at_offset_min ELSE 0 END) <= :window"
        " UNION"
        " SELECT loc.asset_id FROM asset_location loc"
        " JOIN assets g ON g.asset_id = loc.asset_id"
        " WHERE loc.source = :time AND g.library_id IN (SELECT library_id FROM assets WHERE asset_id = ANY(:ids))"
        "   AND jsonb_typeof(loc.basis -> 'fixes') = 'array'"
        "   AND EXISTS (SELECT 1 FROM jsonb_array_elements(loc.basis -> 'fixes') fx"
        "     WHERE fx ->> 'asset_id' = ANY(:ids))"
    ), {"ids": list(asset_ids), "window": window, "reach": window + 28 * 60, "time": TIME}).all()]
    if not near:
        return []
    # Only clips with a guess or a try to forget, and never a person's.
    gone = [r[0] for r in session.execute(text(
        "SELECT l.asset_id FROM artifact_lineage l WHERE l.asset_id = ANY(:ids) AND l.artifact = :artifact"
        "   AND l.producer <> :person"
        "   AND NOT EXISTS (SELECT 1 FROM asset_location p WHERE p.asset_id = l.asset_id AND p.source = :person)"
        " UNION SELECT loc.asset_id FROM asset_location loc WHERE loc.asset_id = ANY(:ids) AND loc.source = :time"
    ), {"ids": near, "artifact": ARTIFACT, "person": PERSON, "time": TIME}).all()]
    if gone:
        session.execute(text("DELETE FROM asset_location WHERE asset_id = ANY(:ids) AND source = :time"),
                        {"ids": gone, "time": TIME})
        lineage.start_over(session, gone, ARTIFACT)
    return sorted(gone)


def file_changed(session: Session, asset_id: str, before: dict[str, Any] | None, after: dict[str, Any]) -> None:
    """What the file says about a clip changed (a scan, the capture facts read
    again): before and after as {taken_at, taken_at_offset_min, gps_lat,
    gps_lon}. A fix that appeared, moved or went remakes the guesses around
    it; a clip without one whose time changed remakes only its own. Doesn't commit."""
    keys = ("taken_at", "taken_at_offset_min", "gps_lat", "gps_lon")
    before = before or {}
    if all(before.get(k) == after.get(k) for k in keys):
        return

    def fixed(v: dict[str, Any]) -> bool:
        return v.get("gps_lat") is not None and v.get("gps_lon") is not None

    if fixed(before) or fixed(after):
        remake_neighbours(session, [asset_id])
        return
    if not settings(session).get("infer_location"):
        return
    own = session.execute(text(
        "SELECT 1 FROM artifact_lineage WHERE asset_id = :a AND artifact = :artifact AND producer <> :person"
    ), {"a": asset_id, "artifact": ARTIFACT, "person": PERSON}).first()
    if own is not None:
        session.execute(text("DELETE FROM asset_location WHERE asset_id = :a AND source = :time"),
                        {"a": asset_id, "time": TIME})
        lineage.start_over(session, [asset_id], ARTIFACT)


def settings_changed(session: Session, before: dict[str, Any], after: dict[str, Any], *, apply: bool) -> int:
    """The location producer's on_settings: Infer location turned off, every
    guess goes, with the producer's tries (a person's location stays).
    Returns how many clips have a guess."""
    if not before.get("infer_location") or after.get("infer_location"):
        return 0
    n = int(session.execute(text("SELECT count(*) FROM asset_location WHERE source = :time"),
                            {"time": TIME}).scalar() or 0)
    if apply:
        session.execute(text("DELETE FROM asset_location WHERE source = :time"), {"time": TIME})
        session.execute(text("DELETE FROM artifact_lineage WHERE artifact = :artifact AND producer <> :person"),
                        {"artifact": ARTIFACT, "person": PERSON})
    return n
