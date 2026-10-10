"""The account's AI machines on the control plane (routers/ai.py is their API)."""

from __future__ import annotations

from sqlmodel import Session, select
from ulid import ULID

from src.server.models.control_plane import AiMachine, Tenant

BUILT_IN_NAME = "Built in"


def machines(ctrl: Session, tenant_id: str) -> list[AiMachine]:
    """A tenant's machines: the built-in one first, then in the order added."""
    return list(ctrl.exec(select(AiMachine).where(AiMachine.tenant_id == tenant_id)
                          .order_by(AiMachine.built_in.desc(), AiMachine.created_at, AiMachine.machine_id)).all())


def job_model(tenant: Tenant | None, job: str) -> str:
    """The job's model for the tenant ("" when the job is off): the one kept
    under the job's name, else the one its producers declare."""
    from src.shared.ai_jobs import AI_JOBS

    if tenant is None or job not in AI_JOBS:
        return ""
    kept = tenant.ai_job_models or {}
    return str(kept[job] or "") if job in kept else AI_JOBS[job].default_model


def set_job_model(tenant: Tenant, job: str, model: str) -> None:
    tenant.ai_job_models = {**(tenant.ai_job_models or {}), job: model}


def account_job_models(tenant_id: str) -> dict[str, str]:
    """An account's model per AI job (Settings → AI), from the control plane."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    if not tenant_id:
        return {}
    with get_control_session() as ctrl:
        return job_models(TenantRepository(ctrl).get_by_id(tenant_id))


def job_models(tenant: Tenant | None) -> dict[str, str]:
    """{job: model} for the tenant, for the producers (src/shared/producers.py)."""
    from src.shared.ai_jobs import AI_JOBS

    return {job: job_model(tenant, job) for job in AI_JOBS}


def why_jobs_wait(ctrl: Session, tenant_id: str) -> dict[str, str | None]:
    """Why each AI job's work waits now, for Settings → Processing (None:
    it doesn't). Leaving a job without a machine never asks (Robert, Oct 9):
    this is where it says so."""
    from src.shared.ai_jobs import JOBS

    tenant = ctrl.get(Tenant, tenant_id)
    every = machines(ctrl, tenant_id)
    out: dict[str, str | None] = {}
    for job in JOBS:
        label = JOBS[job]
        doing = [m for m in every if m.enabled and job in m.jobs]
        if not job_model(tenant, job):
            out[job] = f"{label} are off: no model is chosen in Settings → AI."
        elif not doing:
            out[job] = f"No machine does {label.lower()}: its work waits until an admin adds one in Settings → AI."
        elif all(m.online is False for m in doing):
            out[job] = f"No machine doing {label.lower()} is online: its work waits. Settings → AI shows why."
        else:
            out[job] = None
    return out


def new_built_in_machine(tenant_id: str) -> AiMachine:
    """The tenant's built-in machine: the scheduler's own, doing the jobs it can (its Whisper), one at a time."""
    from src.shared.ai_jobs import BUILT_IN_JOBS

    return AiMachine(machine_id=f"aim_{ULID()}", tenant_id=tenant_id, name=BUILT_IN_NAME, api_url="",
                     jobs=sorted(BUILT_IN_JOBS), at_once=1, built_in=True)


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
