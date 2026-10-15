"""Every API route of the app, for tests that check each one.

FastAPI keeps an included router as one entry of app.routes (0.143+), so a
walk of app.routes alone finds none of the API's routes and a test over them
checks nothing; iter_route_contexts gives each route with its full path.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts


def api_routes(app: FastAPI) -> list[tuple[str, frozenset[str]]]:
    """(full path, methods) for every API route, in the order they match."""
    return [(context.path, frozenset(context.methods or ()))
            for context in iter_route_contexts(app.routes) if isinstance(context.original_route, APIRoute)]
