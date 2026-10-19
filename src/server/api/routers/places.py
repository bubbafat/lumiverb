"""Places by name, for setting a location: GET /v1/places?q=Port of Spain.
Signed in. Every installed plug-in on in the account that answers place
searches (ADR-023 phase 2d, src/server/plugin_lookups.py), GeoNames among
them since ADR-023 phase 3 (src/own_plugins/geonames/), each place labelled
with where it's from and every answer carrying the credits to show beside
them (GeoNames' names are under CC BY 4.0). None on here: 409 plugin_off. A
plug-in that can't answer now is said in `unavailable`, and the others'
places stand.

Until part 4, GeoNames on in an account by its own switch is answered from
the list the built-in Place names left in the control database while the
plug-in can't answer (not installed, not running, fetching its first list),
so search has no gap.

The built-in's producer is gone (the plug-in's work names clips now): POST
/v1/places/nearest and /names answer 410 gone.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.errors import ConflictError, GoneError
from src.server.database import get_control_session
from src.server.repository.places import PLUGIN, PlaceRepository, label, on_here, place_id

router = APIRouter(prefix="/v1/places", tags=["places"])


class Place(BaseModel):
    place_id: str  # "<plugin_id>:<its id>" ("geonames:<GeoNames' id>"): what PUT /v1/assets/locations takes
    name: str
    region: str | None = None
    country: str | None = None
    label: str  # "Port of Spain, Trinidad and Tobago"
    lat: float
    lon: float
    population: int  # 0 for a landmark
    kind: str | None = None  # a landmark's ("amusement park"); None: a town
    # The name in another language it was found by, when not its own ("Eiffel Tower" for Tour Eiffel).
    matched: str | None = None
    # Who found it: a plug-in's id ("geonames"), and its title.
    source: str = PLUGIN
    source_title: str | None = None


class Credit(BaseModel):
    text: str
    url: str
    license_url: str


class License(BaseModel):
    text: str  # "CC BY 4.0"
    url: str


class SourceCredit(BaseModel):
    source: str  # who found the places it's for: a plug-in's id
    text: str
    url: str | None = None
    license: License | None = None  # the license its places are under, when its credit names one


class Unavailable(BaseModel):
    plugin_id: str
    title: str
    why: str  # it isn't running, didn't answer, or answered what the protocol doesn't say


class PlaceList(BaseModel):
    places: list[Place]
    # Always null since every place is a plug-in's: credits says who to credit (GeoNames' license too).
    credit: Credit | None = None
    # Each source's whose places are among them, to show beside them.
    credits: list[SourceCredit] = []
    # Plug-ins that find places but couldn't answer now.
    unavailable: list[Unavailable] = []


def standing_in(session: Session) -> dict | None:
    """GeoNames as the built-in Place names left it, while that stands in for the plug-in here (part 3): on in
    the account by its own switch and its list still in the control database. {title, credit}, else None."""
    from src.server.own_plugins import shipped
    from src.server.plugin_lookups import credit_of

    manifest = shipped(PLUGIN)
    if manifest is None or not on_here(session):
        return None
    with get_control_session() as control:
        if not PlaceRepository(control).kept():
            return None
    return {"title": manifest.title, "credit": credit_of(manifest)}


def fallback_place(row: dict, title: str) -> Place:
    """A place of the built-in's list, as GeoNames' plug-in says it."""
    return Place(place_id=place_id(row["geonameid"]), name=row["name"], region=row.get("region"),
                 country=row.get("country"), label=label(row), lat=row["lat"], lon=row["lon"],
                 population=row["population"], kind=row.get("kind"), matched=row.get("matched"), source=PLUGIN,
                 source_title=title)


@router.get("", response_model=PlaceList, dependencies=[Depends(require_signed_in)])
def search_places(
    session: Annotated[Session, Depends(get_tenant_session)],
    q: Annotated[str, Query(min_length=1, max_length=200, description="A name's start; \"name, region or country\"")],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> PlaceList:
    """Places whose name starts with q, from each plug-in on here that finds places, in their title order, each
    as it ranks them (GeoNames: the exact name first, then the most populous; "Name, where" narrows it to a
    region or country), up to limit each."""
    from src.server.own_plugins import shipped
    from src.server.plugin_lookups import PLACES, find_places, offering

    theirs = offering(session, PLACES)
    stand_in = standing_in(session)
    if not theirs and stand_in is None:
        title = shipped(PLUGIN).title if shipped(PLUGIN) else PLUGIN
        raise ConflictError("plugin_off", f"No plug-in that finds places is on: an admin turns one on ({title},"
                            " say) in Settings › Plug-ins.", {"plugin_id": PLUGIN, "title": title})
    found, credits, unavailable = find_places(session, q, limit) if theirs else ([], [], [])
    places = [Place(**p) for p in found]
    answered = PLUGIN in {p.manifest.plugin_id for p in theirs} and PLUGIN not in {u["plugin_id"] for u in unavailable}
    if stand_in is not None and not answered:
        with get_control_session() as control:
            rows = PlaceRepository(control).search(q, limit)
        unavailable = [u for u in unavailable if u["plugin_id"] != PLUGIN]
        if rows:
            places += [fallback_place(r, stand_in["title"]) for r in rows]
            credits.append({"source": PLUGIN, **stand_in["credit"]})
    titles = {p.source: (p.source_title or p.source).lower() for p in places}
    places.sort(key=lambda p: (titles[p.source], p.source))  # stable: each source's own order within
    credits.sort(key=lambda c: (titles.get(c["source"], ""), c["source"]))
    return PlaceList(places=places, credits=[SourceCredit(**c) for c in credits],
                     unavailable=[Unavailable(**u) for u in unavailable])


# ---------------------------------------------------------------------------
# The built-in Place names producer's, retired
# ---------------------------------------------------------------------------

_GONE = ("The built-in Place names producer is retired: the GeoNames plug-in's work names clips (its producer, "
         "plugin_geonames, through POST /v1/plugins/geonames/inputs and /outputs).")


@router.post("/nearest", dependencies=[Depends(require_tenant_admin)])
def nearest_places() -> None:
    """Retired (410 gone): the GeoNames plug-in's work names each located clip."""
    raise GoneError(f"POST /v1/places/nearest is retired. {_GONE}", ["POST /v1/plugins/geonames/inputs"])


@router.post("/names", dependencies=[Depends(require_tenant_admin)])
def save_place_names() -> None:
    """Retired (410 gone): the GeoNames plug-in's work saves each clip's place names."""
    raise GoneError(f"POST /v1/places/names is retired. {_GONE}", ["POST /v1/plugins/geonames/outputs"])
