"""Guessing where clips were: the job's clock offsets first (one request
for every device and day among its clips), then each clip's nearest fixes
from the server, the guess worked out here (infer.py) and saved."""

from __future__ import annotations

import logging
from typing import Any

from src.producers.location.infer import (
    NO_CLOCK,
    PAIR_SIMILARITY,
    PAIR_WINDOW_MIN,
    Clock,
    Fix,
    Stamp,
    clock_offset,
    gap_minutes,
    locate,
    parse_time,
)
from src.producers.runner import Waits, Work

logger = logging.getLogger(__name__)


def stamp(row: dict) -> Stamp:
    return Stamp(parse_time(row["taken_at"]), row.get("taken_at_offset_min"), row.get("device") or "")


def fix(row: dict) -> Fix:
    return Fix(asset_id=row["asset_id"], stamp=stamp(row), lat=float(row["lat"]), lon=float(row["lon"]),
               accuracy_m=row.get("accuracy_m"), label=row.get("label") or "")


def day_clock(day: dict) -> Clock:
    """A device's day: its clock offset from the scene pairs the server found."""
    camera_device = day.get("device") or ""
    votes = []
    for pair in day.get("pairs", []):
        if pair["similarity"] < PAIR_SIMILARITY:
            continue
        camera = Stamp(parse_time(pair["camera_taken_at"]), pair.get("camera_offset_min"), camera_device)
        other = Stamp(parse_time(pair["fix_taken_at"]), pair.get("fix_offset_min"), pair.get("fix_device") or "")
        votes.append(gap_minutes(camera, other))
    return clock_offset(votes)


class Locate(Work):
    artifact = "location"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self.on = bool(acct.producers.settings(self.artifact)["infer_location"])
        self.minutes = int(acct.producers.uses(self.artifact)["inference_minutes"])
        self._clocks: dict[str, Clock] | None = None

    def clock(self, asset_id: str) -> Clock:
        if self._clocks is None:
            r = self.acct.client.post("/v1/assets/locations/clocks", json={
                "asset_ids": [c["asset_id"] for c in self.job.items],
                "min_similarity": PAIR_SIMILARITY, "window_min": PAIR_WINDOW_MIN}).json()
            days = {key: day_clock(day) for key, day in r.get("days", {}).items()}
            self._clocks = {a: days.get(key, NO_CLOCK) for a, key in r.get("clips", {}).items()}
        return self._clocks.get(asset_id, NO_CLOCK)

    def make(self, clip: dict) -> dict[str, Any]:
        if not self.on:
            # The server's settings say on (it handed the clip out); these were read before.
            raise Waits("Infer location is off in the settings read")
        clock = self.clock(clip["asset_id"])
        ctx = self.acct.client.get(f"/v1/assets/locations/context/{clip['asset_id']}", params={
            "minutes": self.minutes, "clock_offset_min": clock.offset_min}).json()
        if not ctx["clip"].get("taken_at"):
            return {"guess": None, "why": "No time taken"}
        return locate(stamp(ctx["clip"]), [fix(f) for f in ctx["fixes"]], self.minutes, clock)

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        """Each guess with the window it was made in: the server keeps the
        old guess or takes this one (the recheck rules, save_guess), and
        refuses one made in a window that's no longer the setting ("stale":
        the settings are read again, and the clip is tried again later)."""
        stale = False
        for clip, result in made:
            r = client.put(f"/v1/assets/locations/guess/{clip['asset_id']}",
                           json={"guess": result["guess"], "minutes": self.minutes, "lineage": self.lineage(clip)})
            stale = stale or (r.json() or {}).get("result") == "stale"
        if stale:
            self.acct.producers.refresh()
