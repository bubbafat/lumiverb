"""An installed plug-in's work on each clip (ADR-023 phase 2): what the
scheduler asks for its clips' inputs and saves what the plug-in gave back
through. The scheduler's key (an account admin's) alone does this work;
the plug-in itself never calls Lumiverb.

POST /v1/plugins/{plugin_id}/inputs   — each clip's inputs, as its manifest declares them, and their hash
POST /v1/plugins/{plugin_id}/outputs  — what it gave back, checked against its manifest, kept with that hash

What it's handed is its declared inputs and nothing else
(src/server/repository/clip_inputs.py), for the clips in sight it applies to.
What it gives back is kept only once Lumiverb has checked it (each output it
declared, plain text of a length, an http(s) link: manifest.checked_outputs)
and only when the clip's inputs are what they were when they were handed
out; one whose inputs changed since is "stale" (due again).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.plugins.manifest import checked_outputs
from src.server import plugin_producers
from src.server.api.content_hashes import ContentHash
from src.server.api.dependencies import get_tenant_session, require_tenant_admin
from src.server.api.errors import ApiError
from src.server.api.library_access import names
from src.server.api.limits import MAX_IDS
from src.server.api.routers.plugins import require_on
from src.server.plugin_catalog import Installed, find
from src.server.repository import lineage
from src.server.repository.clip_inputs import ClipInputsRepository
from src.server.repository.plugin_outputs import PluginOutputRepository
from src.shared.producers import PRODUCERS

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


def _work_of(plugin_id: str) -> tuple[Installed, str]:
    """An installed plug-in with work, and its producer's artifact (404 otherwise)."""
    entry = find(plugin_id)
    if entry is None or entry.installed is None or entry.installed.manifest.work is None:
        raise HTTPException(status_code=404, detail="No installed plug-in with work by that id")
    artifact = plugin_producers.artifact_of(plugin_id)
    if artifact not in PRODUCERS:  # installed since this API last looked
        plugin_producers.refresh()
    return entry.installed, artifact


class InputsIn(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=MAX_IDS)


class ClipInputs(BaseModel):
    inputs: dict[str, Any]  # each input it declares, by name (src/server/repository/clip_inputs.py)
    inputs_hash: str  # sent back with what it made of them


class InputsOut(BaseModel):
    # The clips in sight it applies to now (the rest are left out).
    clips: dict[str, ClipInputs]


@router.post("/{plugin_id}/inputs", response_model=InputsOut, dependencies=[Depends(require_tenant_admin)])
@names("body.asset_ids")
def plugin_inputs(
    plugin_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[InputsIn, Body()],
) -> InputsOut:
    """The inputs of these clips its work declares, for those in sight it applies to now (on in this
    account, its media, every input there), each with their hash. 409 plugin_off when it's off here."""
    plugin, artifact = _work_of(plugin_id)
    require_on(session, plugin_id)
    found = ClipInputsRepository(session).of(list(dict.fromkeys(body.asset_ids)), plugin.manifest.work.inputs,
                                             applies=lineage.APPLIES[artifact])
    return InputsOut(clips={a: ClipInputs(**c) for a, c in found.items()})


class OutputItem(BaseModel):
    asset_id: str
    inputs_hash: str = Field(max_length=64)  # as POST /inputs gave it
    outputs: dict[str, Any] = Field(max_length=20)  # by output key: a fact {"text", "url"?}, or null
    source_hash: ContentHash | None = None  # the clip's file, for lineage


class OutputsIn(BaseModel):
    items: list[OutputItem] = Field(max_length=MAX_IDS)
    lineage: Any = None  # how they were made (LineageIn): require_lineage judges it


class OutputsOut(BaseModel):
    saved: int
    # Its inputs changed since they were handed out (it's due again), or it's out of sight or no longer one
    # it applies to.
    stale: list[str] = []
    gone: list[str] = []


@router.post("/{plugin_id}/outputs", response_model=OutputsOut, dependencies=[Depends(require_tenant_admin)])
@names("body.items[].asset_id")
def save_plugin_outputs(
    plugin_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    body: Annotated[OutputsIn, Body()],
) -> OutputsOut:
    """What its work gave back for each clip, kept with the hash of the inputs it was made from: each output
    it declares checked first (422 invalid_outputs naming the clip and why, nothing saved). One whose
    inputs changed since is "stale" (nothing written: it's due again), one out of sight or no longer one it
    applies to "gone". 409 plugin_off when it was turned off meanwhile."""
    from src.server.api.routers.producers import require_lineage, with_source

    plugin, artifact = _work_of(plugin_id)
    made = require_lineage(body.lineage, artifact)  # before anything is saved
    require_on(session, plugin_id)
    work = plugin.manifest.work
    checked: dict[str, dict[str, Any]] = {}
    for item in body.items:
        try:
            checked[item.asset_id] = checked_outputs(work, item.outputs)
        except ValueError as exc:
            raise ApiError(422, "invalid_outputs", f"{item.asset_id}: {exc}",
                           {"asset_id": item.asset_id, "why": str(exc)}) from None
    now = ClipInputsRepository(session).hashes([i.asset_id for i in body.items], work.inputs,
                                               applies=lineage.APPLIES[artifact])
    outputs = PluginOutputRepository(session)
    saved, stale, gone = 0, [], []
    for item in body.items:
        if item.asset_id not in now:
            gone.append(item.asset_id)
        elif now[item.asset_id] != item.inputs_hash:
            stale.append(item.asset_id)
        else:
            facts = checked[item.asset_id]
            outputs.save(item.asset_id, plugin_id, facts, item.inputs_hash)
            lineage.record(session, item.asset_id, artifact, with_source(made, item.source_hash),
                           outcome="ok" if any(facts.values()) else "empty", commit=False)
            saved += 1
    session.commit()
    return OutputsOut(saved=saved, stale=stale, gone=gone)
