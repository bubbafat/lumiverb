"""The account's AI machines on the control plane (routers/ai.py is their API)."""

from __future__ import annotations

from sqlmodel import Session, select
from ulid import ULID

from src.server.models.control_plane import AiMachine


def machines(ctrl: Session, tenant_id: str) -> list[AiMachine]:
    """A tenant's machines, in the order added."""
    return list(ctrl.exec(select(AiMachine).where(AiMachine.tenant_id == tenant_id)
                          .order_by(AiMachine.created_at, AiMachine.machine_id)).all())


def first_vision_machine(ctrl: Session, tenant_id: str) -> AiMachine | None:
    """For clients that know one endpoint (the Mac app, /v1/tenant/context)."""
    return next((m for m in machines(ctrl, tenant_id) if m.enabled and "vision" in m.jobs), None)


def free_name(ctrl: Session, tenant_id: str, base: str) -> str:
    """base, or base (2), base (3)… whichever no machine of the tenant has."""
    taken = {m.name for m in machines(ctrl, tenant_id)}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base} ({n})"
    return name


def new_vision_machine(ctrl: Session, tenant_id: str, api_url: str, api_key: str = "") -> AiMachine:
    """A machine doing vision at api_url, named after its host (not added to the session)."""
    url = api_url.strip().rstrip("/")
    host = url.split("://")[-1].split("/")[0] or "Vision"
    return AiMachine(machine_id=f"aim_{ULID()}", tenant_id=tenant_id, name=free_name(ctrl, tenant_id, host),
                     api_url=url, api_key=api_key, jobs=["vision"])
