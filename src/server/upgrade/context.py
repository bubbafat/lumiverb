from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from src.server.repository.system_metadata import SystemMetadataRepository


@dataclass(frozen=True)
class UpgradeContext:
    """Per-tenant context passed to upgrade steps.

    Note: Steps should use repositories/services provided here rather than
    reaching into the SQL session directly.
    """

    session: Session
    metadata: SystemMetadataRepository
    tenant_id: str | None = None

    # Optional scratch space for step implementations.
    extra: dict[str, Any] | None = None

