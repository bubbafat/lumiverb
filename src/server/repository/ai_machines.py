"""The account's AI machines on the control plane (routers/ai.py is their API). A machine's key is kept sealed
(src/server/sealing.py): set_key seals it, key_of opens it, nothing else reads ai_machines.api_key."""

from __future__ import annotations

import re
from typing import NamedTuple

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


class JobWait(NamedTuple):
    """Why an AI job's work waits: "off" (no model), "no_machine" (none does
    it) or "offline" (none doing it is online), and that in a sentence."""

    why: str
    message: str


def why_jobs_wait(ctrl: Session, tenant_id: str) -> dict[str, JobWait | None]:
    """Why each AI job's work waits now, for Settings → Processing (None:
    it doesn't). Leaving a job without a machine never asks (Robert, Oct 9):
    this is where it says so."""
    from src.shared.ai_jobs import JOBS

    tenant = ctrl.get(Tenant, tenant_id)
    every = machines(ctrl, tenant_id)
    out: dict[str, JobWait | None] = {}
    for job in JOBS:
        label = JOBS[job]
        doing = [m for m in every if m.enabled and job in m.jobs]
        if not job_model(tenant, job):
            out[job] = JobWait("off", f"{label} are off: no model is chosen in Settings → AI.")
        elif not doing:
            out[job] = JobWait("no_machine", f"No machine does {label.lower()}: its work waits until an admin adds"
                                             " one in Settings → AI.")
        elif all(m.online is False for m in doing):
            out[job] = JobWait("offline", f"No machine doing {label.lower()} is online: its work waits."
                                          " Settings → AI shows why.")
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


def key_of(machine: AiMachine) -> str | None:
    """Its key: "" for none; None when this install can't read it (sealed with another install's key: a backup
    restored elsewhere), so it's entered again."""
    from src.server.sealing import opened

    return opened(machine.api_key)


def set_key(machine: AiMachine, key: str) -> None:
    """Its key, sealed ("" for none). SecretsKeyMissingError when the install has no key to seal with."""
    from src.server.sealing import seal

    machine.api_key = seal(key)


def key_for(machine: AiMachine | None, scheduler: bool) -> str:
    """A machine's key as the caller may read it: the scheduler's own key's, to send it work; "" for anyone else,
    an account admin too (write-only: Settings › AI says only whether one is saved), and for one this install
    can't read."""
    return (key_of(machine) or "") if machine is not None and scheduler else ""


def address_for(machine: AiMachine | None, admin: bool) -> str | None:
    """A machine's address as the caller may read it: an account admin's (the scheduler's own key is one, to send
    it work), "" for the built-in machine or none at all; None for anyone else, since an address can name a host on
    the server's own network."""
    if not admin:
        return None
    return machine.api_url if machine is not None else ""


# An address in what's said of a machine: a URL, with whatever path it was asked at, its end not the sentence's.
_ADDRESS = re.compile(r"https?://[^\s'\"<>]*[^\s'\"<>.,:;!?)]")


def unaddressed(said: str, name: str | None = None) -> str:
    """What's said of a machine (its status, why a job's work waits, a clip's failure on one) as anyone but an
    account admin reads it: each address in it said as the machine's name where it's known, else as "the AI
    machine" (a capital at a sentence's start). The scheduler writes the address in, for the log and the admin."""
    def instead_of(found: re.Match) -> str:
        if name is not None:
            return name
        before = said[:found.start()].rstrip()
        return "The AI machine" if not before or before[-1] in ".!?" else "the AI machine"

    return _ADDRESS.sub(instead_of, said)


def seal_stored(ctrl: Session) -> int:
    """Upkeep: every machine's key stored before sealing, sealed. How many. Commits."""
    from src.server.sealing import is_sealed

    found = [m for m in ctrl.exec(select(AiMachine)).all() if m.api_key and not is_sealed(m.api_key)]
    for machine in found:
        set_key(machine, machine.api_key)
        ctrl.add(machine)
    ctrl.commit()
    return len(found)


def free_name(ctrl: Session, tenant_id: str, base: str) -> str:
    """base, or base (2), base (3)… whichever no machine of the tenant has."""
    taken = {m.name for m in machines(ctrl, tenant_id)}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base} ({n})"
    return name


def update_vision(ctrl: Session, tenant: Tenant, *, api_url: str | None, api_key: str | None,
                  model: str | None) -> AiMachine | None:
    """The one vision endpoint the operator's admin API knows (the Mac app's):
    api_url "" removes its machine, another makes or moves it; api_key its
    key; model the vision job's. None: as it is. Commits; the machine, or
    None when there's none."""
    machine = first_vision_machine(ctrl, tenant.tenant_id)
    if api_url is not None:
        url = api_url.strip().rstrip("/")
        if not url:
            if machine is not None:
                ctrl.delete(machine)
            machine = None
        elif machine is None:
            machine = new_vision_machine(ctrl, tenant.tenant_id, url)
        else:
            machine.api_url = url
    if api_key is not None and machine is not None:
        set_key(machine, api_key)
    if machine is not None:
        ctrl.add(machine)
    if model is not None:
        set_job_model(tenant, "vision", model)
    ctrl.add(tenant)
    ctrl.commit()
    ctrl.refresh(tenant)
    return machine


def new_vision_machine(ctrl: Session, tenant_id: str, api_url: str, api_key: str = "") -> AiMachine:
    """A machine doing vision at api_url, named after its host (not added to the session)."""
    url = api_url.strip().rstrip("/")
    host = url.split("://")[-1].split("/")[0] or "Vision"
    machine = AiMachine(machine_id=f"aim_{ULID()}", tenant_id=tenant_id, name=free_name(ctrl, tenant_id, host),
                        api_url=url, jobs=["vision"])
    set_key(machine, api_key)
    return machine
