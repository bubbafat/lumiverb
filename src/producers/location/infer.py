"""Where a clip probably was, from fixes taken around the same time
(ADR-017 phase 3). Pure: no I/O, so every rule is tested on its own.

Times. A clip's taken_at is the camera's wall clock (stored as if UTC) and
taken_at_offset_min its zone when the file says. How long after a fix a
clip was taken (gap_minutes): with both zones known, between the real
instants; otherwise between the wall clocks as written (the camera on the
same local time as the fix: the ADR's "offset 0"). A fix from another
device is also corrected by the clip's device's clock offset for that day.

Clock offset (per device, per day). Each camera clip that looks like the
same scene as a fix from another device within ±14 h (CLIP, the server
pairs them) is one vote: its gap. At least 3 votes agreeing within 10
minutes: their median is the offset. Otherwise none (0).

A guess. The nearest fix before the clip and the nearest after it, from
any device, within the inference window. Both: interpolated by time, radius
half their distance + 100 m, unless going from one to the other is faster
than 900 km/h (a clock or data error: no guess). One: its point, radius
100 m + 4 km/h × the gap. None: nothing. A fix's GPS accuracy, when known,
is added to the radius. Guesses are never fixes: the server hands over
only the file's GPS and a person's locations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

# Bump the producer's version when any of these change: they change guesses.
SPEED_LIMIT_KMH = 900.0
BASE_RADIUS_M = 100.0
DRIFT_KMH = 4.0  # how far someone may have gone per hour, with a fix on one side only
PAIR_WINDOW_MIN = 14 * 60  # a scene pair's clocks are at most this far apart
PAIR_SIMILARITY = 0.9  # CLIP cosine similarity for "the same scene"
MIN_VOTES = 3
AGREE_MIN = 10.0
EARTH_M = 6_371_008.8


@dataclass(frozen=True)
class Stamp:
    """A clip's time as its file says: the wall clock (as UTC), its zone, its device."""

    taken_at: datetime
    offset_min: int | None
    device: str


@dataclass(frozen=True)
class Fix:
    asset_id: str
    stamp: Stamp
    lat: float
    lon: float
    accuracy_m: float | None = None
    label: str = ""  # "iPhone 15 Pro photos": what the summary calls it


@dataclass(frozen=True)
class Clock:
    """A device's clock offset for one day: minutes its clock reads ahead of
    the fixes' (taken off gaps to other devices' fixes)."""

    offset_min: float = 0.0
    votes: int = 0  # the agreeing pairs it came from (0: none)

    @property
    def method(self) -> str:
        return "scenes" if self.votes else "as written"


NO_CLOCK = Clock()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gap_minutes(clip: Stamp, fix: Stamp, clock: Clock = NO_CLOCK) -> float:
    """How long after the fix the clip was taken, in minutes (negative: before)."""
    gap = (clip.taken_at - fix.taken_at).total_seconds() / 60.0
    if clip.offset_min is not None and fix.offset_min is not None:
        gap -= clip.offset_min - fix.offset_min
    if fix.device != clip.device:
        gap -= clock.offset_min
    return gap


def clock_offset(votes: list[float]) -> Clock:
    """The offset from scene pairs' gaps: the median of the largest group of
    at least MIN_VOTES agreeing within AGREE_MIN minutes (of equal groups,
    the one nearest no offset), or none."""
    ordered = sorted(v for v in votes if math.isfinite(v))
    best: list[float] = []
    start = 0
    for end, value in enumerate(ordered):
        while value - ordered[start] > AGREE_MIN:
            start += 1
        group = ordered[start:end + 1]
        if len(group) > len(best) or (len(group) == len(best) and abs(median(group)) < abs(median(best))):
            best = group
    if len(best) < MIN_VOTES:
        return NO_CLOCK
    return Clock(round(median(best), 1), len(best))


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def _vector(lat: float, lon: float) -> tuple[float, float, float]:
    p, lam = math.radians(lat), math.radians(lon)
    return math.cos(p) * math.cos(lam), math.cos(p) * math.sin(lam), math.sin(p)


def between(lat1: float, lon1: float, lat2: float, lon2: float, t: float) -> tuple[float, float]:
    """The point t of the way along the great circle from the first to the
    second (across the antimeridian the short way)."""
    a, b = _vector(lat1, lon1), _vector(lat2, lon2)
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))
    omega = math.acos(dot)
    if omega < 1e-12:
        return lat1, lon1
    s = math.sin(omega)
    if s < 1e-9:  # opposite sides of the earth: no one way between them
        return (lat1, lon1) if t < 0.5 else (lat2, lon2)
    k1, k2 = math.sin((1 - t) * omega) / s, math.sin(t * omega) / s
    x, y, z = (k1 * p + k2 * q for p, q in zip(a, b, strict=True))
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def one_side_radius_m(gap_min: float) -> float:
    return BASE_RADIUS_M + DRIFT_KMH * 1000.0 * abs(gap_min) / 60.0


def both_sides_radius_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return distance_m(lat1, lon1, lat2, lon2) / 2 + BASE_RADIUS_M


def speed_kmh(before: Fix, after: Fix, minutes_apart: float) -> float:
    if minutes_apart <= 0:
        return math.inf if distance_m(before.lat, before.lon, after.lat, after.lon) > 0 else 0.0
    return distance_m(before.lat, before.lon, after.lat, after.lon) / 1000.0 / (minutes_apart / 60.0)


def _hhmm(fix: Fix) -> str:
    return fix.stamp.taken_at.strftime("%H:%M")


def _summary(used: list[Fix], clock: Clock) -> str:
    labels = list(dict.fromkeys(f.label or "other clips" for f in used))
    times = sorted({_hhmm(f) for f in used})
    when = f"at {times[0]}" if len(times) == 1 else f"{times[0]}–{times[-1]}"
    text = f"From {' and '.join(labels)} {when}"
    if clock.votes and abs(clock.offset_min) >= 1:
        mins = abs(clock.offset_min)
        amount = f"{mins / 60:.1f} h" if mins >= 90 else f"{mins:.0f} min"
        text += f" · clock {amount} {'ahead' if clock.offset_min > 0 else 'behind'}"
    return text


def locate(clip: Stamp, fixes: list[Fix], minutes: float, clock: Clock = NO_CLOCK) -> dict[str, Any]:
    """{"guess": {lat, lon, radius_m, basis} | None, "why": why not, or ""}."""
    gaps = [(gap_minutes(clip, f.stamp, clock), f) for f in fixes]
    befores = [(g, f) for g, f in gaps if 0 <= g <= minutes]
    afters = [(-g, f) for g, f in gaps if -minutes <= g < 0]
    before = min(befores, key=lambda x: (x[0], x[1].asset_id), default=None)
    after = min(afters, key=lambda x: (x[0], x[1].asset_id), default=None)
    if before is None and after is None:
        return {"guess": None, "why": f"No fix within {minutes:g} min"}
    if before is not None and after is not None and before[0] > 0:
        (gb, fb), (ga, fa) = before, after
        speed = speed_kmh(fb, fa, gb + ga)
        if speed > SPEED_LIMIT_KMH:
            return {"guess": None, "why": f"The fixes either side are {speed:,.0f} km/h apart"}
        lat, lon = between(fb.lat, fb.lon, fa.lat, fa.lon, gb / (gb + ga))
        radius = both_sides_radius_m(fb.lat, fb.lon, fa.lat, fa.lon)
        used, method, used_gaps = [fb, fa], "between", [gb, -ga]
    else:
        gap, fix = (before[0], before[1]) if before is not None else (-after[0], after[1])  # type: ignore[index]
        lat, lon, radius = fix.lat, fix.lon, one_side_radius_m(gap)
        used, method, used_gaps = [fix], "nearest", [gap]
    accuracy = max((f.accuracy_m for f in used if f.accuracy_m is not None), default=0.0)
    basis = {
        "method": method,
        "fixes": [{"asset_id": f.asset_id, "gap_min": round(g, 1)} for f, g in zip(used, used_gaps, strict=True)],
        "clock_offset_min": clock.offset_min,
        "clock": clock.method,
        "votes": clock.votes,
        "summary": _summary(used, clock),
    }
    return {"guess": {"lat": lat, "lon": lon, "radius_m": int(round(radius + accuracy)), "basis": basis}, "why": ""}
