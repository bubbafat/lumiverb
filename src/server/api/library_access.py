"""A person's role on the library a request is about (ADR-018 decision 8,
phase 8): a library they can't see doesn't exist for them, and editing
needs "editor" on the library, not in the account.

The gate runs before every route (main.py's app dependency, after the
device gate), for a person (a person signed in, or a key a person made:
request.state.person) who isn't an account admin. Whatever a request names
by its path (a library, a clip, a scan, an upload, a video's chunk or
scene, a face) is looked up; one in a library they can't see is a 404, as
one that isn't there. What a route names in its body, form or query it says
with names(), each field one it edits or one it only needs to see. The
roles on the libraries it edits are request.state.library_roles, which
dependencies.require_editor asks before the account role (role_here).

A key no person made (the scheduler's, an operator's), a device (the
device gate's) and a visitor to a public page go by: they're no person.
What spans libraries (search, projects, people) shows only what a person
can see: see ADR-018 decision 8, Roles per library.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from src.server.repository.library_roles import RANK, LibraryRoleRepository, Person, libraries_of

_MARK = "_lumiverb_names"
# A route's path parameter and what it names, each in one library.
PATH = {"library_id": "library", "asset_id": "asset", "scan_id": "scan", "upload_id": "upload",
        "chunk_id": "chunk", "scene_id": "scene", "face_id": "face"}
# What a body, form or query field names, by its name.
FIELDS = {"library_id": "library", "library_ids": "library", "asset_id": "asset", "asset_ids": "asset",
          "same_as": "asset", "face_ids": "face"}
_NOT_FOUND = {"library": "Library not found", "asset": "Asset not found", "scan": "Scan not found",
              "upload": "Upload not found", "chunk": "Chunk not found", "scene": "Scene not found",
              "face": "Face not found"}
_WHERE = ("body", "form", "query")


def _field(spec: str) -> tuple[str, str, str | None, str]:
    """"body.asset_ids" or "body.items[].asset_id" as (where, name, item
    field or None, kind)."""
    where, _, path = spec.partition(".")
    name, _, sub = path.partition("[].")
    kind = FIELDS.get(sub or name)
    if where not in _WHERE or kind is None or (sub and not name):
        raise ValueError(f"not a field a route names a library by: {spec!r}")
    return where, name, sub or None, kind


def names(*edits: str, sees: tuple[str, ...] = ()) -> Callable:
    """Mark what a route names in its body, form or query: the fields it
    edits (the role it needs is on their libraries) and the ones it only
    needs to see ("body.asset_ids", "query.library_id",
    "body.items[].asset_id")."""
    fields = tuple((_field(f), True) for f in edits) + tuple((_field(f), False) for f in sees)

    def mark(endpoint: Callable) -> Callable:
        setattr(endpoint, _MARK, fields)
        return endpoint
    return mark


def marked(endpoint: Any) -> tuple | None:
    return getattr(endpoint, _MARK, None)


def person_of(request: Request) -> Person | None:
    """Whose roles on each library the request has; None when it isn't a
    person's, or is an account admin's (every library's admin)."""
    person = getattr(request.state, "person", None)
    if not isinstance(person, Person) or person.account_admin:
        return None  # an account admin's key: its own role, request.state.role, on every library
    return person


async def gate(request: Request) -> None:
    """Before every route: what the request names, in a library the person
    can see, or a 404; the roles on what it edits kept for require_editor."""
    person = person_of(request)
    if person is None:
        return
    named: list[tuple[str, list[str], bool]] = []
    for param, kind in PATH.items():
        value = request.path_params.get(param)
        if isinstance(value, str):
            named.append((kind, [value], True))
    for (where, name, sub, kind), edits in marked(request.scope.get("endpoint")) or ():
        named.append((kind, await _values(request, where, name, sub), edits))
    if not named:
        return
    roles = await run_in_threadpool(_roles_named, request, person, named)
    request.state.library_roles = roles


async def _values(request: Request, where: str, name: str, sub: str | None) -> list[str]:
    """The ids a field names; none when it can't be read as such (the route refuses it)."""
    if where == "query":
        return [v for v in request.query_params.getlist(name) if v]
    if where == "form":
        value = (await request.form()).get(name)
    else:
        try:
            body = await request.json()
        except ValueError:
            return []
        value = body.get(name) if isinstance(body, dict) else None
    values = value if isinstance(value, list) else [value]
    if sub is not None:
        values = [v.get(sub) for v in values if isinstance(v, dict)]
    return [v for v in values if isinstance(v, str)]


def _roles_named(request: Request, person: Person, named: list[tuple[str, list[str], bool]]) -> dict[str, str]:
    from src.server.database import get_engine_for_url

    with Session(get_engine_for_url(request.state.connection_string)) as session:
        roles = LibraryRoleRepository(session).roles_of(person)
        edited: dict[str, str] = {}
        for kind, ids, edits in named:
            for library_id in libraries_of(session, kind, ids):
                role = roles.get(library_id, "none")
                if role == "none":
                    raise HTTPException(status_code=404, detail=_NOT_FOUND[kind])
                if edits:
                    edited[library_id] = role
    return edited


def role_here(request: Request) -> str | None:
    """The request's role where it acts: the lowest on the libraries it
    edits when it names any, else the account's (request.state.role)."""
    roles = getattr(request.state, "library_roles", None) or {}
    if roles:
        return min(roles.values(), key=RANK.__getitem__)
    return getattr(request.state, "role", None)
