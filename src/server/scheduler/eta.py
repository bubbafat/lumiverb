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

from collections.abc import Mapping
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


def eta(status: Mapping[str, Any], left: Mapping[str, float], *, now: Any) -> dict[str, Any]:
    """status: the scheduler's last (pace, pools, jobs, at); left: each
    producer's work left in its unit (artifact → seconds of video or clips).
    Returns {"producers": {artifact: seconds | None}, "pools": {pool: seconds
    | None}, "caught_up": seconds | None, "jobs": [{kind, artifact, units,
    elapsed, left}]}."""
    pace: Mapping[str, float] = status.get("pace") or {}
    slots = {pool: (counts[1] if len(counts) > 1 else 0) for pool, counts in (status.get("pools") or {}).items()}

    producers: dict[str, float | None] = {}
    pools: dict[str, float | None] = {}
    for artifact, work in left.items():
        p = PRODUCERS[artifact]
        if not p.scheduled:
            continue
        per, n = pace.get(p.kind), slots.get(p.pool, 0)
        took = 0.0 if work <= 0 else (work * per / n if per and n else None)
        producers[artifact] = took
        pools.setdefault(p.pool, 0.0)
        if work > 0:
            before = pools[p.pool]
            pools[p.pool] = None if took is None or before is None else before + took
    caught_up = None if any(v is None for v in pools.values()) else max(pools.values(), default=0.0)

    written, now_at = _at(status.get("at")), _at(now)
    since = max(0.0, (now_at - written).total_seconds()) if written and now_at else 0.0
    jobs = []
    for job in status.get("jobs") or []:
        kind = KINDS.get(job.get("kind", ""))
        units, elapsed = float(job.get("units") or 0), float(job.get("elapsed") or 0) + since
        per = pace.get(kind.base) if kind else None
        artifact = kind.artifact if kind and kind.flag else None
        jobs.append({"kind": job.get("kind"), "artifact": artifact,
                     "unit": PRODUCERS[artifact].unit if artifact in PRODUCERS else "clip",
                     "units": units, "elapsed": elapsed, "left": max(0.0, units * per - elapsed) if per else None})
    return {"producers": producers, "pools": pools, "caught_up": caught_up, "jobs": jobs}
