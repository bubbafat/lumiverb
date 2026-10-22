"""A page's cursor as a client sends it back (`after`): what the last page
ended with, in the shape its list made it.

Every list reads its cursor through here, so one that isn't a cursor the list
gave is a 400 bad_cursor (api/errors.py), never a value bound into a query as
it came: a dict, text where a time goes or a NUL would otherwise be the
database's error, a 500. GET /v1/query's is reachable from a public page.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

# Integers PostgreSQL compares (bigint's range).
_BIGINT = 2**63

MESSAGE = "That cursor isn't one this list gave: ask for the first page again."


class BadCursorError(ValueError):
    """A cursor this list didn't make (or a value in it it never holds): a 400 bad_cursor."""

    def __init__(self, why: str = "") -> None:
        super().__init__(MESSAGE)
        self.why = why


def text(value: Any) -> str:
    """Text, as an id or a path is: never with a NUL or a lone surrogate (neither reaches the database)."""
    if not isinstance(value, str) or "\x00" in value:
        raise BadCursorError("not text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BadCursorError("not text") from None
    return value


def whole(value: Any) -> int:
    """A whole number in bigint's range (a count, a size, a position)."""
    if isinstance(value, bool) or not isinstance(value, int) or not -_BIGINT <= value < _BIGINT:
        raise BadCursorError("not a whole number")
    return value


def number(value: Any) -> int | float:
    """A finite number (an aperture, a focal length)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BadCursorError("not a number")
    return value


def time(value: Any) -> datetime:
    """A time as Lumiverb writes one (ISO 8601; a trailing Z too)."""
    if not isinstance(value, str) or "\x00" in value:
        raise BadCursorError("not a time")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError:
        raise BadCursorError("not a time") from None


def plain(after: str | None) -> str | None:
    """A cursor that's the last row's id or path as it is (None: the first page)."""
    return None if after is None else text(after)


def or_none(read: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """read, or None when the value is null (a clip without that value: sorted last)."""
    return lambda value: None if value is None else read(value)


def unpacked(after: str, **fields: Callable[[Any], Any]) -> dict[str, Any]:
    """The base64 JSON object a list's next_cursor is: exactly these fields, each read by its reader."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(text(after) + "=" * (-len(after) % 4)))
    except (ValueError, TypeError):  # not base64, not JSON (binascii.Error and JSONDecodeError are ValueErrors)
        raise BadCursorError("not a cursor") from None
    if not isinstance(decoded, dict) or set(decoded) != set(fields):
        raise BadCursorError("not this list's cursor")
    return {name: read(decoded[name]) for name, read in fields.items()}
