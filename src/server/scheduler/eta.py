"""How long until everything is made, for Settings → Processing (and the CLI).

The scheduler learns each kind's pace from the jobs it finishes: seconds a
slot spends per unit of work, a second of video or a clip (the producer's
`unit`). A producer's time left is its work left at that pace, over its
pool's slots; producers sharing a pool take turns, so a pool's time is
theirs added up; pools run side by side, so being caught up is the slowest
pool's time. A running job's time left is its size at that pace, less how
long it has run. No pace yet, or no slots (an AI job without a machine):
no time, rather than a guess.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

from src.server.scheduler.kinds import KINDS
from src.shared.producers import PRODUCERS


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# A job this many times past its pace is late: it says so rather than "about to finish".
LATE_AFTER = 2.0


def eta(status: Mapping[str, Any], left: Mapping[str, float], *, now: Any,
        paused: Collection[str] = ()) -> dict[str, Any]:
    """status: the scheduler's last (pace, pools, jobs, at); left: each
    producer's work left in its unit (artifact → seconds of video or clips).
    Returns {"producers": {artifact: seconds | None}, "pools": {pool: seconds},
    "caught_up": seconds | None, "not_counted": [{artifact, title, why}],
    "jobs": [{kind, artifact, unit, units, elapsed, left, late}]}. A producer
    with work but no machine doing it ("no_machine") or no pace yet
    ("not_known_yet") has no time, and caught up leaves it out, naming it;
    caught up is None only when nothing with work is counted. A producer an
    admin paused (paused) is left out the same way ("paused"): it has no time
    while paused, and its pool's slots go to the rest."""
    pace: Mapping[str, float] = status.get("pace") or {}
    slots = {pool: (counts[1] if len(counts) > 1 else 0) for pool, counts in (status.get("pools") or {}).items()}

    pools: dict[str, float] = {}
    members: dict[str, list[str]] = {}
    not_counted: list[dict[str, str]] = []
    for artifact, work in left.items():
        p = PRODUCERS[artifact]
        if not p.scheduled:
            continue
        pool = p.pool.name
        members.setdefault(pool, [])
        per, n = pace.get(p.kind), slots.get(pool, 0)
        if work <= 0:
            continue
        if artifact in paused:
            not_counted.append({"artifact": artifact, "title": p.title, "why": "paused"})
            continue
        if not n or not per:
            not_counted.append({"artifact": artifact, "title": p.title, "why": "no_machine" if not n else "not_known_yet"})
            continue
        members[pool].append(artifact)
        pools[pool] = pools.get(pool, 0.0) + work * per / n
    # Producers sharing a pool take turns at its slots, oldest clip first: each
    # finishes about when the pool does.
    producers: dict[str, float | None] = {}
    for artifact, work in left.items():
        p = PRODUCERS[artifact]
        if p.scheduled:
            pool = p.pool.name
            producers[artifact] = 0.0 if work <= 0 else pools.get(pool) if artifact in members.get(pool, ()) else None
    if pools:
        caught_up: float | None = max(pools.values())
    else:
        # Only what no machine is doing now is left: caught up, not counting it.
        # Only paused work left: no estimate either.
        caught_up = None if any(n["why"] in ("not_known_yet", "paused") for n in not_counted) else 0.0

    written, now_at = _at(status.get("at")), _at(now)
    since = max(0.0, (now_at - written).total_seconds()) if written and now_at else 0.0
    jobs = []
    for job in status.get("jobs") or []:
        kind = KINDS.get(job.get("kind", ""))
        if kind is None or not kind.flag:  # a scan: how long one takes varies too much to say
            continue
        artifact = kind.artifact
        units, elapsed = float(job.get("units") or 0), float(job.get("elapsed") or 0) + since
        per = pace.get(kind.base)
        expected = units * per if per else None
        jobs.append({"kind": job.get("kind"), "artifact": artifact, "unit": PRODUCERS[artifact].unit,
                     "units": units, "elapsed": elapsed,
                     "left": None if expected is None else max(0.0, expected - elapsed),
                     "late": bool(expected) and elapsed > LATE_AFTER * expected})
    return {"producers": producers, "pools": pools, "caught_up": caught_up, "not_counted": not_counted,
            "jobs": jobs}
