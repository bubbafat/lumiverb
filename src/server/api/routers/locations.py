"""Locations for clips (ADR-017 phases 2 and 3): a person's, and the producer's guesses.

PUT    /v1/assets/locations         set it: {lat, lon} or {same_as}
DELETE /v1/assets/locations         clear a person's location
POST   /v1/assets/locations/accept  a suggestion becomes a person's location

The location producer's (phase 3): admins only, as the scheduler's key is (editors 403):
POST   /v1/assets/locations/clocks          scene pairs a device's clock offset is voted from
GET    /v1/assets/locations/context/{id}    a clip's nearest fixes before and after it
PUT    /v1/assets/locations/guess/{id}      its guess, or that it found none

Editor or above. Every request names its clips: asset_ids is required and
non-empty (a missing or empty list is a 400, never "all"). Replacing a
location the file or a person already gave needs the request's say
(replace), or it's a 409 location_exists with the counts.

Included before the assets router: DELETE /v1/assets/{asset_id} would
otherwise take "locations" for a clip id.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import (
    get_current_user_id,
    get_tenant_session,
    require_editor,
    require_tenant_admin,
)
from src.server.api.errors import ApiError, DecisionRequiredError
from src.server.api.limits import MAX_IDS
from src.server.repository import locations
from src.shared.location import is_fix

router = APIRouter(prefix="/v1/assets/locations", tags=["locations"])



class SetLocationRequest(BaseModel):
    asset_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
    lat: float | None = Field(default=None, allow_inf_nan=False)
    lon: float | None = Field(default=None, allow_inf_nan=False)
    same_as: str | None = None  # another clip: its location (a person's, else the file's)
    # What may be replaced: "none", a person's location ("person"), or the file's too ("all").
    replace: Literal["none", "person", "all"] = "none"


class LocationIdsRequest(BaseModel):
    asset_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)


class AcceptLocationRequest(LocationIdsRequest):
    # A suggestion taken on a clip whose file has GPS replaces what shows: "all" says so.
    replace: Literal["none", "all"] = "none"


class SetLocationResponse(BaseModel):
    updated: list[str]
    skipped: list[str] = []  # not in sight (archived, in the trash, unknown), or the same_as clip itself


class ClearLocationResponse(BaseModel):
    cleared: list[str]
    skipped: list[str] = []  # no person's location, or not in sight


class AcceptLocationResponse(BaseModel):
    accepted: list[str]
    skipped: list[str] = []  # no suggestion, or not in sight


def _ids(body: SetLocationRequest | LocationIdsRequest | None) -> list[str]:
    """The clips the request names, once each; 400 scope_required without any (never "all")."""
    ids = list(dict.fromkeys(body.asset_ids)) if body and body.asset_ids else []
    if not ids:
        raise ApiError(400, "scope_required", "Give asset_ids.")
    return ids


def _who(request: Request) -> str:
    """Who's asking: their user id ("key:<id>" for an API key). Stored as
    set_by; a name is looked up when a signed-in person reads it, never
    stored (no email in basis: public viewers must never see one)."""
    return get_current_user_id(request)


def _refresh(session: Session, asset_ids: list[str]) -> None:
    if asset_ids:
        from src.server.api.routers.assets import _refresh_grids

        _refresh_grids(session, asset_ids)


@router.put("", response_model=SetLocationResponse, dependencies=[Depends(require_editor)])
def set_locations(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[SetLocationRequest | None, Body()] = None,
) -> SetLocationResponse:
    """Set a person's location on the named clips."""
    ids = _ids(body)
    assert body is not None
    by_point = body.lat is not None or body.lon is not None
    if by_point == (body.same_as is not None):
        raise HTTPException(status_code=400, detail="Send lat and lon, or same_as")
    basis: dict = {}
    if by_point:
        if body.lat is None or body.lon is None or not is_fix(body.lat, body.lon):
            raise ApiError(422, "invalid_location", "lat and lon must be a real place: -90..90, -180..180, not 0, 0")
        lat, lon = float(body.lat), float(body.lon)
    else:
        fix = locations.fix_of(session, body.same_as)  # type: ignore[arg-type]
        if fix is None:
            raise HTTPException(status_code=400, detail="The same_as clip has no location")
        lat, lon = fix
        basis["same_as"] = body.same_as

    targets = [i for i in ids if i != body.same_as]
    locations.lock(session, targets)
    states = locations.states(session, targets)
    person = [i for i in targets if i in states and states[i]["source"] == locations.PERSON]
    file_only = [i for i in targets if i in states and states[i]["has_file"]
                 and states[i]["source"] != locations.PERSON]
    blocked_person = person if body.replace == "none" else []
    blocked_file = file_only if body.replace != "all" else []
    if blocked_person or blocked_file:
        n = len(blocked_person) + len(blocked_file)
        raise DecisionRequiredError(
            "location_exists",
            f"{n} of these clips already {'has' if n == 1 else 'have'} a location"
            f" ({len(person)} set by a person, {len(file_only)} from the file)."
            ' Send replace: "person" to replace a person\'s, or "all" to replace the file\'s too.',
            {"count": n, "person": len(person), "file": len(file_only), "total": len(targets)},
        )

    updated = [i for i in targets if i in states]
    locations.set_person(session, updated, lat, lon, set_by=_who(request), basis=basis)
    session.commit()
    _refresh(session, updated)
    return SetLocationResponse(updated=updated, skipped=[i for i in ids if i not in set(updated)])


@router.delete("", response_model=ClearLocationResponse, dependencies=[Depends(require_editor)])
def clear_locations(
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[LocationIdsRequest | None, Body()] = None,
) -> ClearLocationResponse:
    """Clear a person's location from the named clips. The file's GPS,
    guesses and suggestions stay."""
    ids = _ids(body)
    cleared = locations.clear_person(session, ids)
    session.commit()
    _refresh(session, cleared)
    return ClearLocationResponse(cleared=cleared, skipped=[i for i in ids if i not in set(cleared)])


@router.post("/accept", response_model=AcceptLocationResponse, dependencies=[Depends(require_editor)])
def accept_locations(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[AcceptLocationRequest | None, Body()] = None,
) -> AcceptLocationResponse:
    """The named clips' suggestions become a person's location. Over the
    file's GPS only with replace: "all" (409 location_exists otherwise)."""
    ids = _ids(body)
    assert body is not None
    locations.lock(session, ids)
    states = locations.states(session, ids)
    filed = [i for i in ids if i in states and states[i]["status"] == locations.SUGGESTED and states[i]["has_file"]]
    if filed and body.replace != "all":
        n = len(filed)
        raise DecisionRequiredError(
            "location_exists",
            f"{n} of these clips already {'has' if n == 1 else 'have'} a location from the file."
            ' Send replace: "all" to replace it.',
            {"count": n, "person": 0, "file": n, "total": len(ids)},
        )
    accepted = locations.accept(session, ids, set_by=_who(request))
    session.commit()
    _refresh(session, accepted)
    return AcceptLocationResponse(accepted=accepted, skipped=[i for i in ids if i not in set(accepted)])


# ---------------------------------------------------------------------------
# The location producer's (phase 3): what it reads to guess, and its guesses
# ---------------------------------------------------------------------------


class ClocksRequest(BaseModel):
    asset_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
    min_similarity: float = Field(default=0.9, ge=0, le=1)
    window_min: int = Field(default=14 * 60, ge=1, le=48 * 60)


@router.post("/clocks", dependencies=[Depends(require_tenant_admin)])
def location_clocks(
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[ClocksRequest | None, Body()] = None,
) -> dict:
    """For the named clips: each one's device and day, and that day's scene
    pairs (a clip of the device with no fix, and the most alike fix from
    another device), which the producer votes the device's clock offset from."""
    ids = _ids(body)
    assert body is not None
    return locations.clocks(session, ids, min_similarity=body.min_similarity, window_min=body.window_min)


@router.get("/context/{asset_id}", dependencies=[Depends(require_tenant_admin)])
def location_context(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    minutes: Annotated[int, Query(ge=1, le=1440)] = 360,
    clock_offset_min: Annotated[float, Query(ge=-48 * 60, le=48 * 60, allow_inf_nan=False)] = 0.0,
) -> dict:
    """A clip's time and device, and the nearest fix before and after it in
    its library (the file's GPS or a person's location; never a guess)
    within minutes, with its device's clock offset applied."""
    found = locations.context(session, asset_id, minutes=minutes, clock_offset_min=clock_offset_min)
    if found is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return found


class GuessIn(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    radius_m: int = Field(ge=0, le=20_100_000)
    basis: dict = Field(default_factory=dict)


class GuessRequest(BaseModel):
    guess: GuessIn | None = None  # None: nothing within the window
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


class GuessResponse(BaseModel):
    # stored | empty (none found: the clip's guess goes) | kept (a person's
    # location or the file's GPS: nothing written) | gone (not in sight)
    result: str


@router.put("/guess/{asset_id}", response_model=GuessResponse, dependencies=[Depends(require_tenant_admin)])
def save_location_guess(
    asset_id: str,
    body: GuessRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> GuessResponse:
    """The location producer's guess for a clip, or that it found none. A
    person's location and the file's GPS are never written over."""
    import json

    from src.server.api.routers.producers import require_lineage

    made = require_lineage(body.lineage, "location")  # before anything is saved
    guess = body.guess.model_dump() if body.guess is not None else None
    if guess is not None:
        if not is_fix(guess["lat"], guess["lon"]):
            raise ApiError(422, "invalid_location", "lat and lon must be a real place: not 0, 0")
        if len(json.dumps(guess["basis"])) > 16_000:
            raise ApiError(422, "basis_too_big", "basis is at most 16,000 characters of JSON")
    result = locations.save_guess(session, asset_id, guess, made)
    session.commit()
    return GuessResponse(result=result)
