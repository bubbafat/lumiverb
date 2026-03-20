from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session


class SystemMetadataRepository:
    """Repository for system_metadata table (per-tenant)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_value(self, key: str) -> str | None:
        row = self._session.exec(
            text("SELECT value FROM system_metadata WHERE key = :key"),
            {"key": key},
        ).first()
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        # Upsert to avoid requiring an existing row.
        self._session.exec(
            text(
                """
                INSERT INTO system_metadata (key, value, updated_at)
                VALUES (:key, :value, NOW())
                ON CONFLICT (key) DO UPDATE
                  SET value = :value,
                      updated_at = NOW()
                """
            ),
            {"key": key, "value": value},
        )
        self._session.commit()

    def delete_key(self, key: str) -> None:
        self._session.exec(
            text("DELETE FROM system_metadata WHERE key = :key"),
            {"key": key},
        )
        self._session.commit()

