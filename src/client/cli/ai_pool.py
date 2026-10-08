"""The account's AI machines doing a job, for the worker (Settings → AI).

Each machine is an OpenAI-compatible endpoint with its own limit of
requests at once; the job has one model (Robert's call, Oct 8). Before
sending a machine work, the worker checks it offers the model, and tells
the server what it found, per machine. Requests go to online machines, each
up to its limit, least busy for its size first. A machine that fails a
request (the endpoint's fault: unreachable, out of memory, the model gone)
is skipped and its item goes to another; it's checked again a minute
later. Only when no machine is left is it the endpoint's fault for the job,
which then waits without charging any clip.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.client.workers.captions.base import CaptionError, CaptionProvider
from src.shared.ai_jobs import JOBS
from src.shared.vision_endpoint import VisionEndpointError, list_models

logger = logging.getLogger(__name__)

# How long a machine that failed waits before it's checked again.
RECHECK_SEC = 60.0
# How long a request waits for a free slot before looking again (for a machine back online).
WAIT_SEC = 1.0


@dataclass(eq=False)
class Machine:
    machine_id: str
    name: str
    api_url: str
    api_key: str | None
    at_once: int
    online: bool = False
    error: str = ""
    checked_at: float | None = None
    busy: int = 0
    checking: bool = field(default=False, repr=False)


class MachinePool:
    def __init__(self, client: Any, job: str, *, clock: Callable[[], float] = time.monotonic,
                 recheck_sec: float = RECHECK_SEC) -> None:
        self._client = client
        self.job = job
        self.label = JOBS.get(job, job)
        self._clock = clock
        self._recheck_sec = recheck_sec
        self.model = ""
        self.machines: list[Machine] = []
        # Why the job can't go ahead; None while a machine is online.
        self.error: str | None = None
        self._cond = threading.Condition()

    def load(self) -> None:
        """The job's model and machines, from the server (raises when it can't say)."""
        data = self._client.get(f"/v1/ai/jobs/{self.job}").json()
        self.model = data.get("model") or ""
        self.machines = [Machine(machine_id=m["machine_id"], name=m["name"], api_url=m["api_url"],
                                 api_key=m.get("api_key") or None, at_once=max(1, int(m.get("at_once") or 1)))
                         for m in data.get("machines") or []]

    def check(self) -> bool:
        """Read the job's machines and check each. True when one is online.
        Between runs only: it replaces the machines requests hold."""
        self.load()
        return self.recheck()

    def recheck(self) -> bool:
        """Check each machine again (mid-run too). True when one is online."""
        if self.model:
            for machine in self.machines:
                self._check(machine)
        self._why()
        return not self.down

    @property
    def down(self) -> bool:
        return not self.model or not any(m.online for m in self.machines)

    def capacity(self) -> int:
        """Requests the online machines take at once, together."""
        with self._cond:
            return sum(m.at_once for m in self.machines if m.online)

    def describe(self) -> str:
        on = [f"{m.name} ({m.at_once} at once)" for m in self.machines if m.online]
        off = [f"{m.name}: {m.error}" for m in self.machines if not m.online]
        return f"{self.model} on {', '.join(on) or 'no machine'}" + (f"; offline: {'; '.join(off)}" if off else "")

    def acquire(self) -> Machine | None:
        """A slot on an online machine (waiting for one to free), or None when
        no machine is online. Release it when the request is done."""
        while True:
            self._recheck_due()
            with self._cond:
                online = [m for m in self.machines if m.online]
                if not online:
                    self._why()
                    return None
                free = [m for m in online if m.busy < m.at_once]
                if free:
                    machine = min(free, key=lambda m: m.busy / m.at_once)
                    machine.busy += 1
                    return machine
                self._cond.wait(WAIT_SEC)

    def release(self, machine: Machine) -> None:
        with self._cond:
            machine.busy = max(0, machine.busy - 1)
            self._cond.notify_all()

    def fault(self, machine: Machine, error: object) -> None:
        """A request failed for the machine's sake: skip it until it's checked again."""
        with self._cond:
            newly = machine.online
            if newly:
                machine.online = False
                machine.error = str(error)
                machine.checked_at = self._clock()
            self._cond.notify_all()
        if newly:
            logger.warning("%s: %s failed (%s); the others take its work", self.label, machine.name, error)
            self._report(machine, [])

    def _check(self, machine: Machine) -> None:
        try:
            models = list_models(machine.api_url, machine.api_key)
            error = "" if self.model in models else f"{machine.api_url} doesn't offer {self.model}."
        except VisionEndpointError as e:
            models, error = [], str(e)
        with self._cond:
            machine.online = not error
            machine.error = error
            machine.checked_at = self._clock()
            machine.checking = False
            self._cond.notify_all()
        if error:
            logger.warning("%s: %s can't be used: %s", self.label, machine.name, error)
        self._report(machine, models)

    def _recheck_due(self) -> None:
        now = self._clock()
        with self._cond:
            due = [m for m in self.machines if not m.online and not m.checking
                   and (m.checked_at is None or now - m.checked_at >= self._recheck_sec)]
            for m in due:
                m.checking = True
        for m in due:
            self._check(m)

    def _report(self, machine: Machine, models: list[str]) -> None:
        try:
            self._client.post(f"/v1/ai/machines/{machine.machine_id}/status",
                              json={"online": machine.online, "error": machine.error, "models": models})
        except Exception as e:  # noqa: BLE001 — never let reporting stop the work
            logger.warning("Couldn't tell the server what %s said: %s", machine.name, e)

    def _why(self) -> None:
        if not self.model:
            self.error = f"No model is chosen for {self.label.lower()}. An admin picks one in Settings → AI."
        elif not self.machines:
            self.error = f"No machine does {self.label.lower()}. An admin adds one in Settings → AI."
        elif not any(m.online for m in self.machines):
            why = "; ".join(f"{m.name}: {m.error}" for m in self.machines)
            self.error = f"No machine doing {self.label.lower()} is online ({why}). Settings → AI shows each."
        else:
            self.error = None


class PooledCaptionProvider(CaptionProvider):
    """Describes and reads images on whichever of the pool's machines is free;
    a machine's own failure moves the item to another."""

    def __init__(self, pool: MachinePool, make: Callable[[Machine], CaptionProvider]) -> None:
        self._pool = pool
        self._make = make
        self._providers: dict[str, CaptionProvider] = {}
        self._lock = threading.Lock()

    @property
    def provider_id(self) -> str:
        return "openai_compatible"

    def describe(self, proxy_path: Path) -> dict:
        return self._on_a_machine(lambda p: p.describe(proxy_path))

    def extract_text(self, proxy_path: Path) -> str:
        return self._on_a_machine(lambda p: p.extract_text(proxy_path))

    def _provider(self, machine: Machine) -> CaptionProvider:
        with self._lock:
            if machine.machine_id not in self._providers:
                self._providers[machine.machine_id] = self._make(machine)
            return self._providers[machine.machine_id]

    def _on_a_machine(self, call: Callable[[CaptionProvider], Any]) -> Any:
        while True:
            machine = self._pool.acquire()
            if machine is None:
                raise CaptionError(self._pool.error or "No machine is online.", endpoint_fault=True)
            try:
                return call(self._provider(machine))
            except CaptionError as e:
                if not e.endpoint_fault:
                    raise
                self._pool.fault(machine, e)
            finally:
                self._pool.release(machine)
