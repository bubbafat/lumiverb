"""Places by name, for setting a location: GET /v1/places?q=Port of Spain.
Signed in. Every installed plug-in on in the account that answers place
searches (ADR-023 phase 2d, src/server/plugin_lookups.py), each place
labelled with where it's from and every answer carrying the credits to show
beside them (a license may ask for it). None on here: 409 plugin_off, naming
those installed that would. A plug-in that can't answer now (not running,
fetching its first list) is said in `unavailable`, and the others' places
stand.

The built-in Place names producer is gone (an installed plug-in's work names
clips now): POST /v1/places/nearest and /names answer 410 gone.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.errors import ConflictError, GoneError

router = APIRouter(prefix="/v1/places", tags=["places"])


class Place(BaseModel):
    place_id: str  # "<plugin_id>:<its id>": what PUT /v1/assets/locations takes
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
    # Who found it: a plug-in's id, and its title.
    source: str
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
    # Always null since every place is a plug-in's: credits says who to credit.
    credit: Credit | None = None
    # Each source's whose places are among them, to show beside them.
    credits: list[SourceCredit] = []
    # Plug-ins that find places but couldn't answer now.
    unavailable: list[Unavailable] = []


def _none_on() -> ConflictError:
    """409 plugin_off: no plug-in on here finds places. details.plugins: those installed that would, off here
    ({plugin_id, title}); none installed, the server admin installs one."""
    from src.server.plugin_catalog import installed
    from src.server.plugin_lookups import PLACES

    would = sorted(({"plugin_id": pid, "title": p.manifest.title} for pid, p in installed().items()
                    if PLACES in p.manifest.lookups), key=lambda p: (p["title"].lower(), p["plugin_id"]))
    if not would:
        return ConflictError("plugin_off", "No plug-in that finds places is installed on this server: the server "
                             "admin installs one.", {"plugins": []})
    titles = ", ".join(p["title"] for p in would)
    return ConflictError("plugin_off", f"No plug-in that finds places is on: an admin turns one on in Settings ›"
                         f" Plug-ins ({titles}).", {"plugins": would})


@router.get("", response_model=PlaceList, dependencies=[Depends(require_signed_in)])
def search_places(
    session: Annotated[Session, Depends(get_tenant_session)],
    q: Annotated[str, Query(min_length=1, max_length=200, description="A name's start; \"name, region or country\"")],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> PlaceList:
    """Places whose name starts with q, from each plug-in on here that finds places, in their title order, each
    as it ranks them, up to limit each."""
    from src.server.plugin_lookups import PLACES, find_places, offering

    if not offering(session, PLACES):
        raise _none_on()
    found, credits, unavailable = find_places(session, q, limit)
    return PlaceList(places=[Place(**p) for p in found], credits=[SourceCredit(**c) for c in credits],
                     unavailable=[Unavailable(**u) for u in unavailable])


# ---------------------------------------------------------------------------
# The built-in Place names producer's, retired
# ---------------------------------------------------------------------------

_GONE = ("The built-in Place names producer is retired: an installed plug-in's work names clips (its producer, "
         "plugin_<its id>, through POST /v1/plugins/{plugin_id}/inputs and /outputs).")


@router.post("/nearest", dependencies=[Depends(require_tenant_admin)])
def nearest_places() -> None:
    """Retired (410 gone): an installed plug-in's work names each located clip."""
    raise GoneError(f"POST /v1/places/nearest is retired. {_GONE}", ["POST /v1/plugins/{plugin_id}/inputs"])


@router.post("/names", dependencies=[Depends(require_tenant_admin)])
def save_place_names() -> None:
    """Retired (410 gone): an installed plug-in's work saves each clip's place names."""
    raise GoneError(f"POST /v1/places/names is retired. {_GONE}", ["POST /v1/plugins/{plugin_id}/outputs"])
