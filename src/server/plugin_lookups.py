"""A plug-in's lookups (ADR-023 phase 2d): what a screen asks a plug-in,
rather than a clip. Lumiverb defines each lookup and the screen that asks it;
the first is `places`, Set location's Place search.

A plug-in that declares the lookup (its manifest's `lookups`) and is on in
the account is asked over its socket, `POST /v1/lookups/places`, with the
account's settings (and the server's) and either what was typed
(`{"q", "limit", "settings"}`: places whose name starts with it) or one
place's id (`{"id", "settings"}`: that place). It answers `{"places":
[{"id", "name", "region", "country", "kind", "matched", "lat", "lon",
"population"}]}`. Lumiverb checks every place (an id, plain text of a
length, a real point) and refuses an answer with one that isn't: that
plug-in is "unavailable" for the search, with why, as it is when it isn't
running or doesn't answer within LOOKUP_TIMEOUT_SEC. A person's search waits
on no plug-in longer than that.

A plug-in's place is `<plugin_id>:<its id>`, labelled with the plug-in that
found it and credited as its manifest says (its credit, else its title).
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from sqlmodel import Session

LOOKUP_TIMEOUT_SEC = 3.0
PLACES = "places"
_TEXT = re.compile(r"^[^\x00-\x1f\x7f<>]+$")  # plain text: no control character, no tag


class LookupUnavailableError(Exception):
    """The plug-in can't answer now: not running, too slow, or what it said isn't what the protocol says."""


def offering(session: Session, lookup: str) -> list[Any]:
    """The installed plug-ins on in this account that answer this lookup, each its Installed, in title order."""
    from src.server.plugin_catalog import installed
    from src.server.repository.plugins import ON, PluginRepository, state_of

    rows = PluginRepository(session).rows()
    return sorted((p for pid, p in installed().items() if lookup in p.manifest.lookups
                   and state_of(p.manifest.spec(), rows.get(pid), p.default) == ON),
                  key=lambda p: (p.manifest.title.lower(), p.manifest.plugin_id))


def credit_of(manifest: Any) -> dict[str, Any]:
    """Who to credit for what it answers: its manifest's credit (with its license, if it names one), else its
    title (and its homepage)."""
    if manifest.credit is not None:
        return manifest.credit.shown()
    return {"text": manifest.title, "url": manifest.homepage or None, "license": None}


def _text(value: Any, *, most: int, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError("a name is required")
        return None
    if not isinstance(value, str) or len(value) > most or not _TEXT.match(value):
        raise ValueError(f"plain text up to {most} characters, not {str(value)[:40]!r}")
    return " ".join(value.split())


def checked_place(plugin: Any, raw: Any) -> dict[str, Any]:
    """One place a plug-in answered, as Lumiverb shows it (ValueError saying why it isn't one)."""
    from src.shared.location import is_fix

    if not isinstance(raw, dict):
        raise ValueError("a place is an object")
    pid = _text(raw.get("id") if not isinstance(raw.get("id"), int) else str(raw["id"]), most=100, required=True)
    if pid is None or " " in pid:
        raise ValueError("its id: up to 100 characters, no spaces")
    name = _text(raw.get("name"), most=200, required=True)
    lat, lon = raw.get("lat"), raw.get("lon")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)) \
            or not is_fix(float(lat), float(lon)):
        raise ValueError(f"{name}: lat and lon, a real point")
    population = raw.get("population") or 0
    if isinstance(population, bool) or not isinstance(population, int) or population < 0:
        raise ValueError(f"{name}: population is a whole number")
    region, country = _text(raw.get("region"), most=100), _text(raw.get("country"), most=100)
    m = plugin.manifest
    parts = [name, *([region] if region and region != name else []), *([country] if country else [])]
    return {"place_id": f"{m.plugin_id}:{pid}", "name": name, "region": region, "country": country,
            "label": ", ".join(parts), "lat": float(lat), "lon": float(lon), "population": population,
            "kind": _text(raw.get("kind"), most=60), "matched": _text(raw.get("matched"), most=200),
            "source": m.plugin_id, "source_title": m.title}


def _ask(session: Session, plugin: Any, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Its places for this request (LookupUnavailableError when it can't answer as the protocol says)."""
    from src.server.config import get_settings
    from src.server.database import get_control_session
    from src.server.plugin_client import PluginAnswerError, ask, socket_path
    from src.server.plugin_runner import not_ready, read_state
    from src.server.plugin_settings import ACCOUNT, SERVER, PluginSettingsRepository, values

    m = plugin.manifest
    data = Path(get_settings().data_dir)
    if (why := not_ready(read_state(data), m.plugin_id)) is not None:
        raise LookupUnavailableError(why)
    with get_control_session() as control:
        server = PluginSettingsRepository(control, SERVER).get(m.plugin_id)
    settings = values(m, PluginSettingsRepository(session, ACCOUNT).get(m.plugin_id), server)
    try:
        answer = ask(socket_path(data, m.plugin_id), "POST", f"/v1/lookups/{PLACES}",
                     body={**body, "settings": settings}, timeout=LOOKUP_TIMEOUT_SEC)
    except PluginAnswerError as exc:
        raise LookupUnavailableError(f"it didn't answer ({exc})") from exc
    said = answer.get("places")
    if not isinstance(said, list) or len(said) > 100:
        raise LookupUnavailableError("it answered what the protocol doesn't say (places: a list, up to 100)")
    try:
        return [checked_place(plugin, p) for p in said]
    except ValueError as exc:
        raise LookupUnavailableError(f"it answered what the protocol doesn't say ({exc})") from exc


def find_places(session: Session, q: str, limit: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Places whose name starts with q, from every plug-in on here that finds places: (places, each one's
    credit {source, text, url} for those that found some, those unavailable now {plugin_id, title, why})."""
    places: list[dict] = []
    credits: list[dict] = []
    unavailable: list[dict] = []
    for plugin in offering(session, PLACES):
        m = plugin.manifest
        try:
            found = _ask(session, plugin, {"q": q, "limit": limit})[:limit]
        except LookupUnavailableError as exc:
            unavailable.append({"plugin_id": m.plugin_id, "title": m.title, "why": str(exc)})
            continue
        if found:
            places.extend(found)
            credits.append({"source": m.plugin_id, **credit_of(m)})
    return places, credits, unavailable


def one_place(session: Session, place: str) -> dict[str, Any] | None:
    """A plug-in's place by its place_id (<plugin_id>:<id>), as the plug-in says it now; None when no plug-in on
    here finds places by that name or it says there's no such place. LookupUnavailableError when it can't answer."""
    plugin_id, _, own = place.partition(":")
    plugin = next((p for p in offering(session, PLACES) if p.manifest.plugin_id == plugin_id), None)
    if plugin is None or not own:
        return None
    return next((p for p in _ask(session, plugin, {"id": own}) if p["place_id"] == place), None)
