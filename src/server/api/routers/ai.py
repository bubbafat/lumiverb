"""The account's AI machines, and each job's model (ADR-016 phase 3).

One list of GPU machines (Robert's call, Oct 8): each is an
OpenAI-compatible endpoint with an optional key, the jobs it does, and how
many requests it takes at once. Each job has one model for the account, and
a machine does a job only if it offers that model, so an artifact is the
same model's whatever machine made it (which machine isn't tracked, like
the encoder). An admin adds a machine by URL and presses Connect, which
lists its models. The worker checks each machine before sending it work and
reports what it found here; Settings shows it. Keys are never shown back,
and a viewer never gets one. Changing a job's model makes its artifacts
stale.

Every tenant has one built-in machine: the worker's own computer, with no
URL or key, doing what the worker does itself (Whisper: transcripts). It
can be renamed, given more at once or turned off, not removed. The server
can't reach it: it offers the models faster-whisper knows, and only the
worker says whether it's online. A faster-whisper model is known by one
name, whatever a server lists it as (src/shared/whisper_models.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictBool, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from src.server.api.dependencies import (
    get_tenant_session,
    require_editor,
    require_signed_in,
    require_tenant_admin,
)
from src.server.api.errors import ConflictError, DecisionRequiredError, UpstreamError
from src.server.models.control_plane import AiMachine, Tenant
from src.server.repository.ai_machines import first_vision_machine, job_model, machines, set_job_model as _store_model
from src.shared.ai_jobs import BUILT_IN_JOBS, JOBS
from src.shared.utils import utcnow
from src.shared.vision_endpoint import VisionEndpointError, list_models
from src.shared.whisper_models import BUILT_IN_MODELS, canonical, canonical_models

router = APIRouter(prefix="/v1/ai", tags=["ai"])


class MachineStatus(BaseModel):
    online: bool
    error: str = ""
    models: list[str] = []
    checked_at: datetime | None = None


class MachineOut(BaseModel):
    machine_id: str
    name: str
    api_url: str
    has_key: bool
    jobs: list[str]
    at_once: int
    enabled: bool
    # The worker's own computer: no URL or key; can't be removed.
    built_in: bool = False
    # The latest check (the worker's, or Connect's when saved); None until there is one.
    status: MachineStatus | None = None


class JobOut(BaseModel):
    job: str
    label: str
    model: str
    # Enabled machines doing the job, and how many offered its model when last checked.
    machines: int
    offering: int
    # The models those machines offer, to pick the job's from.
    choices: list[str] = []
    # The built-in machine (the worker's own computer) can do it.
    built_in: bool = False


class AiSettings(BaseModel):
    machines: list[MachineOut]
    jobs: list[JobOut]


def _clean_jobs(jobs: list[str]) -> list[str]:
    unknown = [j for j in jobs if j not in JOBS]
    if unknown:
        raise ValueError(f"unknown jobs: {', '.join(unknown)} (known: {', '.join(JOBS)})")
    return list(dict.fromkeys(jobs))


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("a machine needs a name")
    return name


class MachineIn(BaseModel):
    name: str = Field(max_length=100)
    api_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    jobs: list[str] = Field(default_factory=list)
    at_once: int = Field(default=2, ge=1, le=32)
    enabled: bool = True

    _jobs = field_validator("jobs")(_clean_jobs)
    _name = field_validator("name")(_clean_name)


class MachinePatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    api_url: str | None = Field(default=None, min_length=1, max_length=500)
    # None: keep the saved key; "": none.
    api_key: str | None = Field(default=None, max_length=500)
    jobs: list[str] | None = None
    at_once: int | None = Field(default=None, ge=1, le=32)
    enabled: bool | None = None

    @field_validator("jobs")
    @classmethod
    def _jobs(cls, jobs: list[str] | None) -> list[str] | None:
        return None if jobs is None else _clean_jobs(jobs)

    @field_validator("name")
    @classmethod
    def _name(cls, name: str | None) -> str | None:
        return None if name is None else _clean_name(name)


class ConnectIn(BaseModel):
    api_url: str = Field(min_length=1, max_length=500)
    # None: a saved machine's key (machine_id), when it's that machine's URL; "": none.
    api_key: str | None = Field(default=None, max_length=500)
    machine_id: str | None = None


class Models(BaseModel):
    models: list[str]


class JobModelIn(BaseModel):
    # "": the job is off.
    model: str = Field(default="", max_length=200)
    # Answers 409 upgrades_stop: a new model stops upgrades to the old one.
    stop_upgrades: bool = False


class StatusIn(BaseModel):
    online: StrictBool
    error: str = Field(default="", max_length=2000)
    models: list[str] = Field(default_factory=list, max_length=1000)


class WorkerMachine(BaseModel):
    machine_id: str
    name: str
    api_url: str
    api_key: str
    at_once: int
    # The worker's own computer: it does the job itself.
    built_in: bool


class WorkerJob(BaseModel):
    job: str
    model: str
    machines: list[WorkerMachine]


def _url(api_url: str) -> str:
    return api_url.strip().rstrip("/")


def _job_model(tenant: Tenant | None, job: str) -> str:
    return job_model(tenant, job)


def _offers(machine: AiMachine) -> list[str]:
    """The models a machine offers, by their one names: the built-in one,
    what faster-whisper knows; any other, what it listed when last checked."""
    return BUILT_IN_MODELS if machine.built_in else list(machine.models)


def _built_in_error(message: str) -> ConflictError:
    return ConflictError("built_in_machine", message, {})


def _machines(ctrl: Session, tenant_id: str) -> list[AiMachine]:
    return machines(ctrl, tenant_id)


def _machine(ctrl: Session, request: Request, machine_id: str) -> AiMachine:
    machine = ctrl.get(AiMachine, machine_id)
    if machine is None or machine.tenant_id != request.state.tenant_id:
        raise HTTPException(status_code=404, detail="Machine not found")
    return machine


def _status(machine: AiMachine) -> MachineStatus | None:
    if machine.online is None:
        return None
    return MachineStatus(online=machine.online, error=machine.status_error, models=list(machine.models),
                         checked_at=machine.checked_at)


def _settings(ctrl: Session, tenant_id: str) -> AiSettings:
    tenant = ctrl.get(Tenant, tenant_id)
    machines = _machines(ctrl, tenant_id)
    jobs = []
    for job, label in JOBS.items():
        model = _job_model(tenant, job)
        doing = [m for m in machines if m.enabled and job in m.jobs]
        offering = sum(1 for m in doing if m.online and model and model in _offers(m))
        choices = sorted({x for m in doing for x in _offers(m)})
        jobs.append(JobOut(job=job, label=label, model=model, machines=len(doing), offering=offering,
                           choices=choices, built_in=job in BUILT_IN_JOBS))
    return AiSettings(
        machines=[MachineOut(machine_id=m.machine_id, name=m.name, api_url=m.api_url, has_key=bool(m.api_key),
                             jobs=list(m.jobs), at_once=m.at_once, enabled=m.enabled, built_in=m.built_in,
                             status=_status(m))
                  for m in machines],
        jobs=jobs,
    )


def _ask(api_url: str, api_key: str | None) -> tuple[list[str], str]:
    """(models by their one names, "") when the machine answers; ([], why not) when it can't."""
    try:
        return canonical_models(list_models(api_url, api_key or None)), ""
    except VisionEndpointError as e:
        return [], str(e)


def _record(machine: AiMachine, models: list[str], error: str) -> None:
    machine.online = not error
    machine.status_error = error
    machine.models = canonical_models(models)
    machine.checked_at = utcnow()


def _models_or_502(api_url: str, api_key: str | None) -> list[str]:
    models, error = _ask(api_url, api_key)
    if error:
        raise UpstreamError("machine_unreachable", error, {"api_url": api_url})
    return models


def _check_jobs(tenant: Tenant, jobs: list[str], models: list[str]) -> None:
    """409 model_not_offered when the machine doesn't offer a job's model."""
    for job in jobs:
        model = _job_model(tenant, job)
        if model and model not in models:
            raise ConflictError("model_not_offered",
                                f"It doesn't offer {model}, the model for {JOBS[job].lower()}.",
                                {"job": job, "model": model, "models": models})


def _ask_before_leaving_jobs(ctrl: Session, tenant_id: str, machine: AiMachine, leave_jobs: bool,
                             did: set[str], remove: bool = False) -> None:
    """409 job_left_without_machine when this change leaves a job that has a
    model with no machine doing it (it would wait), unless leave_jobs says so.
    did: the jobs the machine was doing (enabled) before the change; only
    those can be left by it (a job already without a machine isn't asked about)."""
    if leave_jobs:
        return
    tenant = ctrl.get(Tenant, tenant_id)
    left = []
    for job, label in JOBS.items():
        if job not in did:
            continue
        model = _job_model(tenant, job)
        others = [m for m in _machines(ctrl, tenant_id)
                  if m.machine_id != machine.machine_id and m.enabled and job in m.jobs]
        still = not remove and machine.enabled and job in machine.jobs
        if model and not others and not still:
            left.append({"job": job, "label": label, "model": model})
    if left:
        what = " and ".join(j["label"].lower() for j in left)
        raise DecisionRequiredError(
            "job_left_without_machine",
            f"No other machine does {what}: without this one it waits. Send leave_jobs=true to go ahead.",
            {"jobs": left})


def _name_free(ctrl: Session, tenant_id: str, name: str, machine_id: str | None = None) -> None:
    if any(m.name == name and m.machine_id != machine_id for m in _machines(ctrl, tenant_id)):
        raise ConflictError("name_taken", f"There's already a machine called {name}.", {"name": name})


def _control():
    from src.server.database import get_control_session

    return get_control_session()


@router.get("", response_model=AiSettings, dependencies=[Depends(require_signed_in)])
def get_ai(request: Request) -> AiSettings:
    """The machines (never their keys), what each was last found doing, and each job's model."""
    with _control() as ctrl:
        return _settings(ctrl, request.state.tenant_id)


@router.post("/connect", response_model=Models, dependencies=[Depends(require_tenant_admin)])
def connect(body: ConnectIn, request: Request) -> Models:
    """Ask a machine which models it offers. 502 machine_unreachable with why
    when it can't say (unreachable, refused the key, not OpenAI-compatible,
    no models)."""
    api_url = _url(body.api_url)
    key = body.api_key
    if key is None and body.machine_id:
        with _control() as ctrl:
            machine = _machine(ctrl, request, body.machine_id)
            key = machine.api_key if machine.api_url == api_url else None
    return Models(models=_models_or_502(api_url, key))


@router.post("/machines", response_model=AiSettings, status_code=201, dependencies=[Depends(require_tenant_admin)])
def add_machine(body: MachineIn, request: Request) -> AiSettings:
    """Add a machine. It's asked for its models: 502 machine_unreachable when
    it can't say, 409 model_not_offered when it lacks a job's model, 409
    name_taken when another machine has the name."""
    from ulid import ULID

    tenant_id = request.state.tenant_id
    api_url = _url(body.api_url)
    with _control() as ctrl:
        _name_free(ctrl, tenant_id, body.name)
        models = _models_or_502(api_url, body.api_key)
        _check_jobs(ctrl.get(Tenant, tenant_id), body.jobs, models)
        machine = AiMachine(machine_id=f"aim_{ULID()}", tenant_id=tenant_id, name=body.name, api_url=api_url,
                            api_key=body.api_key, jobs=body.jobs, at_once=body.at_once, enabled=body.enabled)
        _record(machine, models, "")
        ctrl.add(machine)
        _commit_named(ctrl, body.name)
        return _settings(ctrl, tenant_id)


def _commit_named(ctrl: Session, name: str | None) -> None:
    """Commit; two saves taking one name at once: 409 name_taken, not a 500."""
    try:
        ctrl.commit()
    except IntegrityError:
        ctrl.rollback()
        raise ConflictError("name_taken", f"There's already a machine called {name}.", {"name": name}) from None


@router.patch("/machines/{machine_id}", response_model=AiSettings, dependencies=[Depends(require_tenant_admin)])
def update_machine(machine_id: str, body: MachinePatch, request: Request, leave_jobs: bool = False) -> AiSettings:
    """Change a machine: only the fields sent. When where it is, its key or
    jobs change, or it's turned back on, it's asked again (502, 409 as when
    added). Turning off the last machine doing a job, or taking the job from
    it, asks first (409 job_left_without_machine) unless leave_jobs."""
    tenant_id = request.state.tenant_id
    with _control() as ctrl:
        machine = _machine(ctrl, request, machine_id)
        if machine.built_in:
            if body.api_url is not None or body.api_key is not None:
                raise _built_in_error("The built-in machine is the worker's own computer: it has no URL or key.")
            if body.jobs is not None and set(body.jobs) - BUILT_IN_JOBS:
                can = " and ".join(JOBS[j].lower() for j in JOBS if j in BUILT_IN_JOBS)
                raise _built_in_error(f"The built-in machine only does {can}: the rest need a server.")
        was = (machine.api_url, machine.api_key, set(machine.jobs), machine.enabled)
        did = set(machine.jobs) if machine.enabled else set()
        if body.name is not None:
            _name_free(ctrl, tenant_id, body.name, machine_id)
            machine.name = body.name
        if body.api_url is not None and _url(body.api_url) != machine.api_url:
            machine.api_url = _url(body.api_url)
            machine.api_key = ""  # a key never goes along to another host unless given again
        if body.api_key is not None:
            machine.api_key = body.api_key
        if body.jobs is not None:
            machine.jobs = body.jobs
        if body.at_once is not None:
            machine.at_once = body.at_once
        if body.enabled is not None:
            machine.enabled = body.enabled
        _ask_before_leaving_jobs(ctrl, tenant_id, machine, leave_jobs, did)
        # Asked again only for what needs it: where it is or its key changed,
        # a job it hadn't, or turned back on. A rename or a new limit doesn't.
        moved = (machine.api_url, machine.api_key) != was[:2]
        if machine.built_in:
            # Nothing to ask: the worker checks it. A new job's model must be one it knows.
            _check_jobs(ctrl.get(Tenant, tenant_id), list(set(machine.jobs) - was[2]), BUILT_IN_MODELS)
        elif machine.enabled and (moved or set(machine.jobs) - was[2] or not was[3]):
            models = _models_or_502(machine.api_url, machine.api_key)
            _check_jobs(ctrl.get(Tenant, tenant_id), machine.jobs, models)
            _record(machine, models, "")
        ctrl.add(machine)
        _commit_named(ctrl, body.name)
        return _settings(ctrl, tenant_id)


@router.delete("/machines/{machine_id}", response_model=AiSettings, dependencies=[Depends(require_tenant_admin)])
def remove_machine(machine_id: str, request: Request, leave_jobs: bool = False) -> AiSettings:
    """Remove a machine. The last machine doing a job asks first (409
    job_left_without_machine) unless leave_jobs. The built-in one can't be
    removed (409 built_in_machine): it can be turned off."""
    with _control() as ctrl:
        machine = _machine(ctrl, request, machine_id)
        if machine.built_in:
            raise _built_in_error("The built-in machine can't be removed: turn it off instead.")
        did = set(machine.jobs) if machine.enabled else set()
        _ask_before_leaving_jobs(ctrl, request.state.tenant_id, machine, leave_jobs, did, remove=True)
        ctrl.delete(machine)
        ctrl.commit()
        return _settings(ctrl, request.state.tenant_id)


def _stop_upgrades(session: Session, job: str, stop: bool) -> None:
    """A new model makes what upgrades under way would make stale: they stop,
    once the request says so (409 upgrades_stop with each one's count left)."""
    from src.server.repository import lineage
    from src.shared.producers import PRODUCERS

    artifacts = lineage.JOB_ARTIFACTS.get(job, ())
    running = [{"artifact": a, "title": PRODUCERS[a].title, "remaining": sum(u["remaining"] for u in ups)}
               for a in artifacts if (ups := lineage.upgrades(session, a))]
    if not running:
        return
    if not stop:
        what = " and ".join(f"{r['title'].lower()} ({r['remaining']:,} clips left)" for r in running)
        raise DecisionRequiredError(
            "upgrades_stop",
            f"Upgrades under way stop with a new model: {what}. What they haven't made yet stays stale.",
            {"upgrades": running},
        )
    session.execute(text("DELETE FROM producer_upgrades WHERE artifact = ANY(:a)"), {"a": list(artifacts)})
    session.commit()


@router.put("/jobs/{job}", response_model=AiSettings, dependencies=[Depends(require_tenant_admin)])
def set_job_model(job: str, body: JobModelIn, request: Request,
                  session: Annotated[Session, Depends(get_tenant_session)]) -> AiSettings:
    """Set a job's model; "" turns the job off. Every enabled machine doing
    the job is asked (what it says is its status): 409 model_not_offered,
    with each machine's models or why it couldn't say, when none offers it.
    The built-in machine isn't asked: it offers what faster-whisper knows.
    A faster-whisper model is kept by its one name ("small", not its repo).
    409 upgrades_stop when upgrades to the current model are under way,
    unless stop_upgrades says to stop them."""
    if job not in JOBS:
        raise HTTPException(status_code=404, detail="Unknown job")
    tenant_id = request.state.tenant_id
    model = canonical(body.model.strip())
    with _control() as ctrl:
        tenant = ctrl.get(Tenant, tenant_id)
        if model:
            asked = []
            for machine in _machines(ctrl, tenant_id):
                if machine.enabled and job in machine.jobs:
                    if machine.built_in:
                        models, error = BUILT_IN_MODELS, ""
                    else:
                        models, error = _ask(machine.api_url, machine.api_key)
                        _record(machine, models, error)
                        ctrl.add(machine)
                    asked.append({"machine_id": machine.machine_id, "name": machine.name, "models": models,
                                  "error": error})
            ctrl.commit()
            if not any(model in a["models"] for a in asked):
                raise ConflictError("model_not_offered",
                                    f"No machine doing {JOBS[job].lower()} offers {model}.",
                                    {"job": job, "model": model, "machines": asked})
        if model != _job_model(tenant, job):
            _stop_upgrades(session, job, body.stop_upgrades)
        _store_model(tenant, job, model)
        ctrl.add(tenant)
        ctrl.commit()
        return _settings(ctrl, tenant_id)


@router.get("/jobs/{job}", response_model=WorkerJob, dependencies=[Depends(require_editor)])
def job_machines(job: str, request: Request) -> WorkerJob:
    """For the worker: a job's model and the enabled machines doing it, with
    their keys, in order (built_in: the worker does it itself). It checks
    each before sending it work."""
    if job not in JOBS:
        raise HTTPException(status_code=404, detail="Unknown job")
    tenant_id = request.state.tenant_id
    with _control() as ctrl:
        tenant = ctrl.get(Tenant, tenant_id)
        return WorkerJob(job=job, model=_job_model(tenant, job), machines=[
            WorkerMachine(machine_id=m.machine_id, name=m.name, api_url=m.api_url, api_key=m.api_key,
                          at_once=m.at_once, built_in=m.built_in)
            for m in _machines(ctrl, tenant_id) if m.enabled and job in m.jobs])


@router.post("/machines/{machine_id}/status", status_code=204, dependencies=[Depends(require_editor)])
def report_status(machine_id: str, body: StatusIn, request: Request) -> Response:
    """The worker's check of a machine: online with its models, or why not.
    Not a failure of any clip."""
    with _control() as ctrl:
        machine = _machine(ctrl, request, machine_id)
        _record(machine, body.models, "" if body.online else (body.error or "Offline."))
        ctrl.add(machine)
        ctrl.commit()
    return Response(status_code=204)


__all__ = ["router", "first_vision_machine"]
