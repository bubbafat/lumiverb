"""Visits (ADR-025): what public pages' visitors did, by place, as their people see it. The counting is
src/server/visits.py's, done where each visit happens; this is only reading it."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server import visits
from src.server.api.dependencies import (
    get_current_user_id,
    get_tenant_session,
    require_library_admin,
    require_tenant_admin,
)
from src.server.api.library_access import seen
from src.server.repository.libraries import LibraryRepository
from src.server.repository.projects import ProjectRepository
from src.server.repository.visits import Counted as CountedRow
from src.server.repository.visits import Report, VisitRepository

router = APIRouter(tags=["visits"])

MOST_DAYS = 366


class Counted(BaseModel):
    """Visitors (each day's added up: one who comes back another day counts again) and times."""

    visitors: int = 0
    times: int = 0


class Counts(BaseModel):
    view: Counted = Counted()  # a public page seen
    open: Counted = Counted()  # a clip opened there
    play: Counted = Counted()  # a video played there
    export: Counted = Counted()  # a project exported (signed in)
    demo: Counted = Counted()  # a demo started (the account's own)


class VisitDay(Counts):
    day: date


class ClipVisits(BaseModel):
    asset_id: str
    opened: int
    played: int


class Source(BaseModel):
    site: str  # its host
    visitors: int


class PlaceVisits(BaseModel):
    since: date
    days: list[VisitDay]
    totals: Counts
    clips: list[ClipVisits]  # most opened and played first
    sources: list[Source]  # where visitors came from, most first
    bots: int


class PlaceSummary(Counts):
    place: Literal["project", "library", "account"]
    place_id: str
    name: str | None  # None for one deleted since
    bots: int


class AccountVisits(BaseModel):
    since: date
    places: list[PlaceSummary]


def _since(days: int) -> date:
    return visits.now().date() - timedelta(days=days - 1)


def _counts(row: dict[str, CountedRow]) -> dict[str, Counted]:
    return {action: Counted(visitors=c.visitors, times=c.times) for action, c in row.items()}


def _place_visits(report: Report, since: date, shown=lambda _clip: True) -> PlaceVisits:
    return PlaceVisits(
        since=since,
        days=[VisitDay(day=day, **_counts(by_action)) for day, by_action in sorted(report.days.items())],
        totals=Counts(**_counts(report.totals)),
        clips=[ClipVisits(asset_id=c, opened=o, played=p) for c, o, p in report.clips if shown(c)],
        sources=[Source(site=s, visitors=n) for s, n in report.sources],
        bots=report.bots,
    )


DAYS = Query(30, ge=1, le=MOST_DAYS, description="The days to report, today and those before it")


@router.get("/v1/visits", response_model=AccountVisits, dependencies=[Depends(require_tenant_admin)])
def account_visits(session: Annotated[Session, Depends(get_tenant_session)], days: int = DAYS) -> AccountVisits:
    """Every place with visits in these days, its name and its totals, most visited first; the demos started.
    An account admin's."""
    since = _since(days)
    return AccountVisits(since=since, places=[
        PlaceSummary(place=p.place, place_id=p.place_id, name=p.name, bots=p.bots, **_counts(p.totals))
        for p in VisitRepository(session).places(since)])


@router.get("/v1/projects/{project_id}/visits", response_model=PlaceVisits)
def project_visits(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    days: int = DAYS,
) -> PlaceVisits:
    """A project's visits: its owner's, and an account admin's (one nobody owns, an account admin's only).
    Its clips are those still in it that the caller sees."""
    from src.server.api.routers.projects import _account_admin, _can_view

    repo = ProjectRepository(session, within=seen(request, session))
    col = repo.get_by_id(project_id)
    if col is None or not _can_view(col, user_id, repo):
        raise HTTPException(status_code=404, detail="Project not found")
    if not _account_admin(request) and (col.owner_user_id is None or col.owner_user_id != user_id):
        raise HTTPException(status_code=403, detail="A project's visits are its owner's and an account admin's")
    since = _since(days)
    report = VisitRepository(session).report(visits.PROJECT, project_id, since)
    return _place_visits(report, since, lambda clip: repo.has_asset(project_id, clip))


@router.get("/v1/libraries/{library_id}/visits", response_model=PlaceVisits,
            dependencies=[Depends(require_library_admin)])
def library_visits(
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    days: int = DAYS,
) -> PlaceVisits:
    """A library's visits, while it's been public: its admins'."""
    if LibraryRepository(session).get_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")
    since = _since(days)
    return _place_visits(VisitRepository(session).report(visits.LIBRARY, library_id, since), since)
