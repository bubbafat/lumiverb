"""Where a clip was shot (ADR-017): the rule for a real position, shared by
the scan (what it keeps from the file) and the server (what it stores)."""

from __future__ import annotations

import math


def is_fix(lat: object, lon: object) -> bool:
    """A real position: both present, finite, in range, and not (0, 0),
    which some devices write when they have no fix."""
    if lat is None or lon is None:
        return False
    try:
        la, lo = float(lat), float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(la) and math.isfinite(lo)):
        return False
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return False
    return not (la == 0 and lo == 0)
