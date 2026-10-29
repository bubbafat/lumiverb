"""Visits (ADR-025): what a public page's visitors do, counted on the server without keeping who they are.

A visitor is a hash of the address nginx saw and the browser's user agent (an exporter's, their user id), keyed
with a salt made for the day and deleted once upkeep adds the day up (src/server/repository/visits.py add_up):
nothing kept says who anyone is. The same visitor doing the same thing to the same place or clip within WINDOW
counts once (a page refreshed, a window focused again, a video's link renewed); an export or a demo every time. A
user agent that says it's a crawler or a script counts as a bot, apart. Counting never fails a request: what can't
be counted is logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import re
from datetime import date, datetime, timedelta

from sqlmodel import Session
from starlette.requests import Request

from src.server.api.client_address import client_address
from src.server.repository.visits import VisitRepository

logger = logging.getLogger(__name__)

PROJECT, LIBRARY, ACCOUNT = "project", "library", "account"
VIEW, OPEN, PLAY, EXPORT, DEMO = "view", "open", "play", "export", "demo"
WINDOW = timedelta(minutes=30)
WINDOWED = (VIEW, OPEN, PLAY)
BOT = "bot"  # a bot's visitor, the same for every one
# What a crawler, a link preview or a script says it is.
_BOTS = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|headless|lighthouse|curl|wget|python|httpx"
                   r"|go-http-client|okhttp|axios|node-fetch|scrapy|libwww|java/", re.IGNORECASE)
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
MOST_HOST = 253


def now() -> datetime:
    """The time here (the day is the server's own)."""
    return datetime.now().astimezone()


def came_from(value: str | None) -> str:
    """The site a page was opened from, as its host (`facebook.com`, `www.` left off), or "" for anything else."""
    host = (value or "").strip().lower().rstrip(".")
    if len(host) > MOST_HOST or not _HOST.fullmatch(host):
        return ""
    return host.removeprefix("www.")


def is_bot(user_agent: str) -> bool:
    return not user_agent.strip() or _BOTS.search(user_agent) is not None


def visitor(salt: bytes, who: str) -> str:
    return hmac.new(salt, who.encode(), hashlib.sha256).hexdigest()[:32]


def is_public(request: Request) -> bool:
    """A public page's visitor's request (not signed in)."""
    return getattr(request.state, "is_public_request", False) is True


def public_place(request: Request) -> tuple[str, str] | None:
    """The public page a visitor's request about a clip comes from: its project or its library."""
    if not is_public(request):
        return None
    if project_id := request.query_params.get("public_project_id"):
        return PROJECT, project_id
    if library_id := request.query_params.get("public_library_id"):
        return LIBRARY, library_id
    return None


def count(session: Session, request: Request, place: str, place_id: str, action: str, *, clip_id: str = "",
          person: str | None = None) -> None:
    """A visit, in its own savepoint, committed. person: who's signed in (an export's), else the request's
    address and user agent say who."""
    try:
        at = now()
        day: date = at.date()
        user_agent = request.headers.get("user-agent", "")
        repo = VisitRepository(session)
        with session.begin_nested():
            if person is None and is_bot(user_agent):
                who = BOT
            else:
                said = f"person\n{person}" if person is not None else f"{client_address(request)}\n{user_agent}"
                who = visitor(repo.salt(day), said)
            repo.count(day=day, place=place, place_id=place_id, action=action, clip_id=clip_id, visitor=who,
                       now=at, window_start=at - WINDOW if action in WINDOWED else None,
                       came_from=came_from(request.query_params.get("from")) if action == VIEW else "")
        session.commit()
    except Exception:  # noqa: BLE001 — a visit not counted never fails the page
        logger.warning("A visit wasn't counted (%s %s, %s)", place, place_id, action, exc_info=True)
        with contextlib.suppress(Exception):
            session.rollback()


def count_in(tenant_id: str, request: Request, place: str, place_id: str, action: str) -> None:
    """A visit in an account the request has no session of (a demo started: the sign-in's is the control
    database's)."""
    from src.server.database import get_tenant_session

    try:
        with get_tenant_session(tenant_id) as session:
            count(session, request, place, place_id, action)
    except Exception:  # noqa: BLE001 — never fails the sign-in
        logger.warning("A visit wasn't counted in %s (%s)", tenant_id, action, exc_info=True)


def add_up(session: Session) -> dict[str, int]:
    """Upkeep: every day before today as totals, its visitors and salt deleted."""
    added = VisitRepository(session).add_up(now().date())
    session.commit()
    return {"added_up": added}
