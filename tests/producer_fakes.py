"""Running one producer's job against the real API, without the scheduler:
an account that has only what the runner and a Work use (its client, the
server's settings, its storage), and the failures it was charged."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from src.server.scheduler.dispatch import Job


def scheduler_client(tenant_id: str):
    """The scheduler's API client (its own key), talking to the app in-process."""
    from fastapi.testclient import TestClient

    from src.client.cli.client import LumiverbClient
    from src.server.api.main import app
    from src.server.scheduler.service import scheduler_key

    key = scheduler_key(tenant_id)
    client = LumiverbClient(base_url="http://testserver", token=key)
    client._client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return client


class Failures:
    def __init__(self) -> None:
        self.charged: list[tuple[str, str, str]] = []

    def add(self, artifact: str, asset_id: str, error: object) -> None:
        self.charged.append((artifact, asset_id, str(error)))


class FakeAccount:
    """What a Work and the runner read off the scheduler's Account."""

    def __init__(self, client: Any, roots: dict[str, Path | None] | None = None) -> None:
        from src.processing.producer_settings import ProducerSettings

        self.client = client
        self.producers = ProducerSettings(client)
        self.stopping = threading.Event()
        self.failures = Failures()
        self.roots = dict(roots or {})
        self.away: list[str] = []

    def root(self, library_id: str) -> Path | None:
        return self.roots.get(library_id)

    def unreachable(self, library_id: str) -> None:
        self.away.append(library_id)

    def storage_gone(self, library_id: str) -> bool:
        return self.roots.get(library_id) is None


def run_job(work_class: type, acct: FakeAccount, tenant_id: str, kind: str, items: list[dict]) -> Any:
    """The runner's outcome for one job of these clips (as the queue lists them)."""
    from src.producers.runner import run

    return run(work_class, acct, Job(tenant_id=tenant_id, kind=kind, tier=3, items=tuple(items)))


def due(tenant_url: str, kind: str, library_ids: list[str]) -> list[dict]:
    """The kind's due clips, as the scheduler's queue lists them."""
    from sqlalchemy import create_engine
    from sqlmodel import Session

    from src.server.repository import lineage
    from src.server.scheduler.kinds import KINDS
    from src.server.scheduler.queue import candidates

    engine = create_engine(tenant_url)
    try:
        with Session(engine) as session:
            k = KINDS[kind]
            want = lineage.desired(session, k.artifact) if k.redo else None
            return candidates(session, k, library_ids, want=want)
    finally:
        engine.dispose()
