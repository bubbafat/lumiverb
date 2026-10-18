"""Places by name (ADR-021's Place names plug-in; ADR-017 phase 4), for
setting a location: GET /v1/places?q=Port of Spain, towns and landmarks.
Signed in, with the plug-in on in the account (409 plugin_off) and its list
fetched (503 places_not_ready). The names are GeoNames', under CC BY 4.0:
every answer carries the credit to show beside them.

The plug-in's producer (src/producers/place/, the scheduler's key; admins):
POST /v1/places/nearest   each clip's location, the nearest town within a radius and the landmark it's at
POST /v1/places/names     what it found, kept with the location it was found for
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server import plugin_data
from src.server.api.content_hashes import ContentHash
from src.server.api.dependencies import get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.errors import ApiError
from src.server.api.library_access import names
from src.server.api.limits import MAX_IDS
from src.server.api.routers.plugins import require_on
from src.server.database import get_control_session
from src.server.repository import asset_places, lineage
from src.server.repository.places import PlaceRepository, geonameid_of, label, place_id

router = APIRouter(prefix="/v1/places", tags=["places"])

PLUGIN = "geonames"
# What shows beside the names (the license asks it).
CREDIT = {"text": "Place names from GeoNames, CC BY 4.0", "url": "https://www.geonames.org/",
          "license_url": "https://creativecommons.org/licenses/by/4.0/"}


class Place(BaseModel):
    place_id: str  # "geonames:<id>": what PUT /v1/assets/locations takes as place_id
    name: str
    region: str | None = None
    country: str | None = None
    label: str  # "Port of Spain, Trinidad and Tobago"
    lat: float
    lon: float
    population: int  # 0 for a landmark
    kind: str | None = None  # a landmark's ("amusement park"); None: a town


class Credit(BaseModel):
    text: str
    url: str
    license_url: str


class PlaceList(BaseModel):
    places: list[Place]
    credit: Credit


def place_out(row: dict) -> Place:
    return Place(place_id=place_id(row["geonameid"]), name=row["name"], region=row.get("region"),
                 country=row.get("country"), label=label(row), lat=row["lat"], lon=row["lon"],
                 population=row["population"], kind=row.get("kind"))


def require_places(session: Session, *, current: bool = False) -> None:
    """409 plugin_off unless Place names is on here; 503 places_not_ready until its list is fetched
    (current: this release's list, which it starts fetching when what's there is older; a search uses
    what's there meanwhile)."""
    require_on(session, PLUGIN)
    data = plugin_data.status(PLUGIN) or {}
    if data.get("ready_at") is None:
        raise ApiError(503, "places_not_ready", "The place list isn't fetched yet: Settings › Plug-ins says how far.",
                       {"state": data.get("state", "missing")})
    if current and not data.get("current"):
        plugin_data.ensure(PLUGIN)
        raise ApiError(503, "places_not_ready", "The place list is being fetched again for this release: "
                       "Settings › Plug-ins says how far.", {"state": data.get("state", "missing")})


@router.get("", response_model=PlaceList, dependencies=[Depends(require_signed_in)])
def search_places(
    session: Annotated[Session, Depends(get_tenant_session)],
    q: Annotated[str, Query(min_length=1, max_length=200, description="A name's start; \"name, region or country\"")],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> PlaceList:
    """Places whose name starts with q, the exact name first, then the most
    populous; "Name, where" narrows it to a region or country."""
    require_places(session)
    with get_control_session() as control:
        rows = PlaceRepository(control).search(q, limit)
    return PlaceList(places=[place_out(r) for r in rows], credit=Credit(**CREDIT))


# ---------------------------------------------------------------------------
# The Place names producer's (the scheduler's key)
# ---------------------------------------------------------------------------


class NearestRequest(BaseModel):
    asset_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
    within_km: float = Field(gt=0, le=1000)  # its radius setting


class Located(BaseModel):
    lat: float  # the location the clip shows, as named from
    lon: float
    source: str  # person, file or guess
    sure_m: float = 0  # how sure it is (a guess's radius, the file's GPS accuracy; 0 for a person's)
    place: Place | None = None  # the nearest town within the radius; None: none that near
    distance_m: int | None = None
    landmark: Place | None = None  # the landmark it's at (within its kind's reach, as sure); None: at none
    landmark_distance_m: int | None = None


class NearestResponse(BaseModel):
    # The clips in sight that show a location (the rest are left out).
    clips: dict[str, Located]


@router.post("/nearest", response_model=NearestResponse, dependencies=[Depends(require_tenant_admin)])
@names("body.asset_ids")
def nearest_places(
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[NearestRequest, Body()],
) -> NearestResponse:
    """Each named clip's location as it shows (a person's, else the file's,
    else an applied guess), the nearest town within within_km and the
    landmark it's at. 400 scope_required without asset_ids; 409 plugin_off;
    503 places_not_ready, this release's list too."""
    ids = list(dict.fromkeys(body.asset_ids or []))
    if not ids:
        raise ApiError(400, "scope_required", "Give asset_ids.")
    require_places(session, current=True)
    found = asset_places.locations(session, ids)
    out: dict[str, Located] = {}
    with get_control_session() as control:
        places = PlaceRepository(control)
        for asset_id, where in found.items():
            row = places.nearest(where["lat"], where["lon"], body.within_km * 1000)
            at = places.landmark_at(where["lat"], where["lon"], where["sure_m"])
            out[asset_id] = Located(**where, place=place_out(row) if row else None,
                                    distance_m=round(row["distance_m"]) if row else None,
                                    landmark=place_out(at) if at else None,
                                    landmark_distance_m=round(at["distance_m"]) if at else None)
    return NearestResponse(clips=out)


class NameItem(BaseModel):
    asset_id: str
    place_id: str | None = Field(default=None, max_length=64)  # None: no town within the radius
    distance_m: int | None = Field(default=None, ge=0)
    landmark_id: str | None = Field(default=None, max_length=64)  # None: at no landmark
    landmark_distance_m: int | None = Field(default=None, ge=0)
    lat: float  # the location it was named from, as POST /nearest said it
    lon: float
    source_hash: ContentHash | None = None  # the clip's file, for lineage


class NamesRequest(BaseModel):
    items: list[NameItem] = Field(max_length=MAX_IDS)
    lineage: Any = None  # how they were made (LineageIn): require_lineage judges it


class NamesResponse(BaseModel):
    saved: int
    # Its location changed since it was named (named again later), or it's out of sight.
    stale: list[str] = []
    gone: list[str] = []


@router.post("/names", response_model=NamesResponse, dependencies=[Depends(require_tenant_admin)])
@names("body.items[].asset_id")
def save_place_names(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[NamesRequest, Body()],
) -> NamesResponse:
    """The producer's place names, each kept with the location it was named
    from. One whose clip shows another location now is "stale" (nothing
    written: it's named again), one out of sight "gone". 422 unknown_place
    for a place_id or landmark_id not in the list; 409 plugin_off when the
    plug-in was turned off meanwhile."""
    from src.server.api.routers.producers import require_lineage, with_source

    made = require_lineage(body.lineage, "place")  # before anything is saved
    require_on(session, PLUGIN)
    now = asset_places.locations(session, [i.asset_id for i in body.items])
    rows: dict[str, dict] = {}
    with get_control_session() as control:
        places = PlaceRepository(control)
        for item in body.items:
            for named in (item.place_id, item.landmark_id):
                if named is None or named in rows:
                    continue
                geonameid = geonameid_of(named)
                row = places.get(geonameid) if geonameid is not None else None
                if row is None:
                    raise ApiError(422, "unknown_place", f"No such place: {named}")
                rows[named] = row
    saved, stale, gone = 0, [], []
    for item in body.items:
        where = now.get(item.asset_id)
        if where is None:
            gone.append(item.asset_id)
            continue
        if (where["lat"], where["lon"]) != (item.lat, item.lon):
            stale.append(item.asset_id)
            continue
        row = rows.get(item.place_id) if item.place_id else None
        at = rows.get(item.landmark_id) if item.landmark_id else None
        asset_places.save(session, item.asset_id, place={**row, "place_id": item.place_id} if row else None,
                          label=label(row) if row else None, distance_m=item.distance_m,
                          landmark={**at, "place_id": item.landmark_id, "label": label(at),
                                    "distance_m": item.landmark_distance_m} if at else None,
                          lat=item.lat, lon=item.lon)
        lineage.record(session, item.asset_id, "place", with_source(made, item.source_hash),
                       outcome="ok" if row or at else "empty", commit=False)
        saved += 1
    session.commit()
    return NamesResponse(saved=saved, stale=stale, gone=gone)
