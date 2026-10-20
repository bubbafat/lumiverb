"""What a server answers from anywhere but its own machine
(LUMIVERB_EXPOSURE). Unset, every route. "demo" (try.lumiverb.io,
docs/demo-server.md): only what a visitor's pages call, a public library's
or project's and those of trying it as a viewer (src/server/demo.py); every
other route answers 404, as one that isn't there, before anything else runs:
no sign-in with a password, no setup, admin, settings or upload, nothing a
scanner or device calls. A request from the machine itself
(client_address.on_this_machine, an SSH tunnel's too) is never held: the
server's admin works that way.

Decided by the route the app would run (its first match, as FastAPI
matches), never by a pattern of its own: a route listed is that route alone.
tests/test_exposure.py holds every listed route to the app's and to what
the web app's pages call."""

from __future__ import annotations

import logging

from fastapi.routing import iter_route_contexts
from starlette.requests import Request
from starlette.routing import Match

from src.server.api.client_address import client_address, on_this_machine
from src.server.api.errors import envelope
from src.server.config import get_settings

logger = logging.getLogger(__name__)

# (method, the route's path) a visitor's pages call: the sign-in page's, trying it as a viewer and its
# session's, browsing, search, a clip's detail and files, people, a public project's. Never typed
# sign-in or a password's reset, nor Settings' AI, Processing and Network (the web app hides them from a
# demo session): what a visitor doesn't need isn't there.
DEMO: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"),
    ("GET", "/v1/setup"),
    ("GET", "/v1/auth/demo"), ("POST", "/v1/auth/demo"),
    ("POST", "/v1/auth/refresh"), ("POST", "/v1/auth/logout"),
    ("GET", "/v1/me"),
    ("GET", "/v1/tenant/settings"),
    ("GET", "/v1/views"),
    ("GET", "/v1/libraries"), ("GET", "/v1/libraries/health"),
    ("GET", "/v1/libraries/{library_id}"), ("GET", "/v1/libraries/{library_id}/revision"),
    ("GET", "/v1/libraries/{library_id}/directories"),
    ("GET", "/v1/query"), ("GET", "/v1/assets/facets"), ("GET", "/v1/similar"),
    ("GET", "/v1/assets/{asset_id}"), ("GET", "/v1/assets/{asset_id}/artifacts/{artifact_type}"),
    ("GET", "/v1/assets/{asset_id}/playback"), ("GET", "/v1/stream/{token}"),
    ("GET", "/v1/assets/{asset_id}/faces"), ("GET", "/v1/faces/{face_id}/crop"),
    ("GET", "/v1/people"), ("GET", "/v1/people/dismissed"), ("GET", "/v1/people/{person_id}"),
    ("GET", "/v1/people/{person_id}/faces"),
    ("GET", "/v1/faces/clusters"), ("GET", "/v1/faces/clusters/{cluster_id}/faces"),
    ("GET", "/v1/public/projects/{project_id}"), ("GET", "/v1/public/projects/{project_id}/assets"),
})

EXPOSURES: dict[str, frozenset[tuple[str, str]]] = {"demo": DEMO}


def route_of(app, scope) -> str | None:
    """The path of the route the app runs for this request, or None when none answers it."""
    for context in _contexts(app):
        match, _ = context.matches(scope)
        if match == Match.FULL:
            return context.path
    return None


_routes: dict[int, list] = {}


def _contexts(app) -> list:
    if id(app) not in _routes:
        _routes[id(app)] = list(iter_route_contexts(app.routes))
    return _routes[id(app)]


class ExposureGate:
    """The outermost middleware: a request the exposure doesn't list never reaches the rest."""

    def __init__(self, app, headers: dict[str, str]) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope, receive, send) -> None:
        listed = EXPOSURES.get(get_settings().lumiverb_exposure) if scope["type"] == "http" else None
        if listed is None:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if not on_this_machine(request):
            route = route_of(scope["app"], scope)
            if (scope["method"], route) not in listed:
                if route is not None:
                    logger.info("%s %s from %s: not exposed here (LUMIVERB_EXPOSURE=%s), 404",
                                scope["method"], route, client_address(request), get_settings().lumiverb_exposure)
                await envelope(404, "not_found", "Not Found", headers=self.headers)(scope, receive, send)
                return
        await self.app(scope, receive, send)
