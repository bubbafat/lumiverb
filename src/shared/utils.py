"""Shared utility functions used across core, models, repositories, and workers."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)



def escape_like(value: str) -> str:
    """value taken literally in a LIKE pattern with ESCAPE '\\'."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
