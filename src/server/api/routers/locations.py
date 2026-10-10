"""A person's location for clips (ADR-017 phase 2).

PUT    /v1/assets/locations         set it: {lat, lon} or {same_as}
DELETE /v1/assets/locations         clear a person's location
POST   /v1/assets/locations/accept  a suggestion becomes a person's location

Editor or above. Every request names its clips: asset_ids is required and
non-empty (a missing or empty list is a 400, never "all"). Replacing a
location the file or a person already gave needs the request's say
(replace), or it's a 409 location_exists with the counts.

Included before the assets router: DELETE /v1/assets/{asset_id} would
otherwise take "locations" for a clip id.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_editor
from src.server.api.errors import DecisionRequiredError
from src.server.repository import locations
from src.shared.location import is_fix

router = APIRouter(prefix="/v1/assets/locations", tags=["locations"])

MAX_IDS = 10_000


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
    """The clips the request names, once each; 400 without any (never "all")."""
    ids = list(dict.fromkeys(body.asset_ids)) if body and body.asset_ids else []
    if not ids:
        raise HTTPException(status_code=400, detail="asset_ids is required: name the clips")
    return ids


def _who(request: Request) -> tuple[str | None, str | None]:
    """(user id, name to show) of the person asking; an API key has no name."""
    user_id = getattr(request.state, "user_id", None)
    return (user_id or get_current_user_id(request)), (getattr(request.state, "email", None) or None)


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
            raise HTTPException(status_code=400, detail="lat and lon must be a real place: -90..90, -180..180, not 0, 0")
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
    user_id, name = _who(request)
    basis["by"] = name
    locations.set_person(session, updated, lat, lon, set_by=user_id, basis=basis)
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
    user_id, name = _who(request)
    accepted = locations.accept(session, ids, set_by=user_id, by_name=name)
    session.commit()
    _refresh(session, accepted)
    return AcceptLocationResponse(accepted=accepted, skipped=[i for i in ids if i not in set(accepted)])
