"""Naming where clips were: one request for the job's clips' locations and
the nearest town to each within the radius, then the names saved, each with
the location it was found for (the server refuses one whose clip shows
another location since: it's named again)."""

from __future__ import annotations

from typing import Any

from src.producers.runner import Waits, Work


class Name(Work):
    artifact = "place"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self.within_km = float(acct.producers.settings(self.artifact)["radius_km"])
        self._found: dict[str, dict] | None = None

    def found(self) -> dict[str, dict]:
        if self._found is None:
            r = self.acct.client.raw("POST", "/v1/places/nearest", retry=True, json={
                "asset_ids": [c["asset_id"] for c in self.job.items], "within_km": self.within_km})
            if r.status_code in (409, 503):
                # Turned off since it was handed out, or the place list isn't fetched yet: tried again later.
                raise Waits(((r.json() or {}).get("error") or {}).get("message") or f"HTTP {r.status_code}")
            self._found = self.acct.client.checked(r).json()["clips"]
        return self._found

    def make(self, clip: dict) -> dict[str, Any] | None:
        where = self.found().get(clip["asset_id"])
        if where is None:
            return None  # out of sight, or it shows no location now: nothing to name
        place = where.get("place")
        return {"place_id": place["place_id"] if place else None, "distance_m": where.get("distance_m"),
                "lat": where["lat"], "lon": where["lon"]}

    def save(self, client, made: list[tuple[dict, Any]]) -> None:
        items = [{"asset_id": clip["asset_id"], "source_hash": clip.get("content_hash"), **result}
                 for clip, result in made if result]
        if items:
            client.post("/v1/places/names", json={"items": items, "lineage": self.lineage(None)})
