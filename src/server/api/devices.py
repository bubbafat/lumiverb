"""What a device's key may do (ADR-018 decision 8): scan the libraries the
device reads, and nothing else.

A device is a computer that scans, not a person. Its key's role is
"device", which is no person's, and it reaches only the routes a scan calls
(checking in; the scan's calls; adding clips; sending their previews,
analysis proxies, probe facts and capture), each marked with may_call and
what it names its library by, and only for a library the device reads
(libraries.reader). It can't search, browse, play or download anything,
see faces or people, edit, or touch another library.

The gate runs before every route (main.py's app dependency), so a route
turns a device away (403 not_for_devices) unless it says otherwise,
whatever it is and whenever it was added: tests/test_device_keys.py tries a
device's key on every route. A library, scan, clip or upload of a library
the device doesn't read is a 404, as one that isn't there. Once the gate
lets a device by (request.state.device_allowed), the route's own check
takes it as it takes an editor (dependencies.require_editor,
require_reader).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from src.server.api.errors import ApiError
from src.server.repository.devices import NAMED_BY, reader_of, readers_of_assets

_MARK = "_lumiverb_device_may_call"
# What a route names its library by: its path's library_id, scan_id,
# asset_id or upload_id; "form", its form's library_id (POST /v1/ingest);
# "items", its JSON body's items, each a clip's asset_id and an artifact
# (POST /v1/producers/failures); "own", nothing: the route answers only for
# the device's own libraries (the check-in).
NAMED = (*NAMED_BY, "form", "items", "own")
_PATH_PARAM = {"library": "library_id", "scan": "scan_id", "asset": "asset_id", "upload": "upload_id"}
_NOT_FOUND = {"library": "Library not found", "scan": "Scan not found", "asset": "Asset not found",
              "upload": "Upload not found"}
NOT_FOR_DEVICES = "A device's key scans the libraries the device reads, and does nothing else."


def may_call(named_by: str, *, artifacts: tuple[str, ...] | None = None) -> Callable:
    """Mark a route a device may call for the libraries it reads, by what
    it names its library. artifacts: of the path's artifact_type (or each
    item's artifact), the ones a device sends (what it makes from the
    originals)."""
    if named_by not in NAMED:
        raise ValueError(f"not what a route names its library by: {named_by!r}")

    def mark(endpoint: Callable) -> Callable:
        setattr(endpoint, _MARK, (named_by, artifacts))
        return endpoint
    return mark


def marked(endpoint: Any) -> tuple[str, tuple[str, ...] | None] | None:
    """How a route said a device may call it, or None: it may not."""
    return getattr(endpoint, _MARK, None)


def is_device(request: Request) -> bool:
    return getattr(request.state, "device_id", None) is not None


async def gate(request: Request) -> None:
    """Before every route: a device only where the route lets it, for a
    library it reads. Anyone else goes by."""
    device_id = getattr(request.state, "device_id", None)
    if device_id is None:
        return
    how = marked(request.scope.get("endpoint"))
    if how is None:
        raise ApiError(403, "not_for_devices", NOT_FOR_DEVICES)
    named_by, artifacts = how
    if named_by == "items":
        # A body that can't be read as items does nothing: the route refuses it (422) before it runs.
        items = await _items(request) or []
        if artifacts is not None and any(a not in artifacts for _, a in items):
            raise ApiError(403, "not_for_devices", NOT_FOR_DEVICES)
        if items and await run_in_threadpool(_readers_of, request, [i for i, _ in items]) != {device_id}:
            raise HTTPException(status_code=404, detail="Asset not found")
        request.state.device_allowed = True
        return
    if artifacts is not None and request.path_params.get("artifact_type") not in artifacts:
        raise ApiError(403, "not_for_devices", NOT_FOR_DEVICES)
    if named_by != "own":
        if named_by == "form":
            named_by, id_ = "library", (await request.form()).get("library_id")
        else:
            id_ = request.path_params.get(_PATH_PARAM[named_by])
        if not isinstance(id_, str) or await run_in_threadpool(_reader_of, request, named_by, id_) != device_id:
            raise HTTPException(status_code=404, detail=_NOT_FOUND[named_by])
    request.state.device_allowed = True


async def _items(request: Request) -> list[tuple[str, str]] | None:
    """A JSON body's items as (asset_id, artifact), or None when they can't
    be read as such (the route would refuse them anyway)."""
    try:
        body = await request.json()
    except ValueError:
        return None
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not all(
            isinstance(i, dict) and isinstance(i.get("asset_id"), str) and isinstance(i.get("artifact"), str)
            for i in items):
        return None
    return [(i["asset_id"], i["artifact"]) for i in items]


def _readers_of(request: Request, asset_ids: list[str]) -> set[str | None]:
    from src.server.database import get_engine_for_url

    with Session(get_engine_for_url(request.state.connection_string)) as session:
        return readers_of_assets(session, asset_ids)


def _reader_of(request: Request, named_by: str, id_: str) -> str | None:
    from src.server.database import get_engine_for_url

    with Session(get_engine_for_url(request.state.connection_string)) as session:
        return reader_of(session, named_by, id_)
