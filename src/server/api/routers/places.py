"""Places by name (ADR-021's Place names plug-in; ADR-017 phase 4), for
setting a location: GET /v1/places?q=Port of Spain. Signed in, with the
plug-in on in the account (409 plugin_off) and its list fetched (503
places_not_ready). The names are GeoNames', under CC BY 4.0: every answer
carries the credit to show beside them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.server import plugin_data
from src.server.api.dependencies import get_tenant_session, require_signed_in
from src.server.api.errors import ApiError
from src.server.api.routers.plugins import require_on
from src.server.database import get_control_session
from src.server.repository.places import PlaceRepository, label, place_id

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
    population: int


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
                 population=row["population"])


def require_places(session: Session) -> None:
    """409 plugin_off unless Place names is on here; 503 places_not_ready until its list is fetched."""
    require_on(session, PLUGIN)
    data = plugin_data.status(PLUGIN) or {}
    if data.get("state") != "ready":
        raise ApiError(503, "places_not_ready", "The place list isn't fetched yet: Settings › Plug-ins says how far.",
                       {"state": data.get("state", "missing")})


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
