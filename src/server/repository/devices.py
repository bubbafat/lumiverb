"""The account's devices (ADR-018 decision 8): computers that scan the
libraries they read, each registered by an account admin. A device's key is
in the control database (ApiKeyRepository.new_device_key); a library a
device reads names it as its reader (libraries.reader).

reader_of says who reads the library a call is about, by what names it (a
library, a scan, a clip, an upload), for the API's device gate
(src/server/api/devices.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session
from ulid import ULID

# What a call names its library by, and the library's reader from it.
_READER_OF = {
    "library": "SELECT reader FROM libraries WHERE library_id = :id",
    "scan": "SELECT l.reader FROM scan_sessions s JOIN libraries l ON l.library_id = s.library_id"
            " WHERE s.scan_id = :id",
    "asset": "SELECT l.reader FROM assets a JOIN libraries l ON l.library_id = a.library_id WHERE a.asset_id = :id",
    "upload": "SELECT l.reader FROM uploads u JOIN assets a ON a.asset_id = u.asset_id"
              " JOIN libraries l ON l.library_id = a.library_id WHERE u.upload_id = :id",
}
NAMED_BY = tuple(_READER_OF)


def reader_of(session: Session, named_by: str, id_: str) -> str | None:
    """The reader of the library this library, scan, clip or upload is in;
    None when there's none."""
    return session.execute(text(_READER_OF[named_by]), {"id": id_}).scalar()


class DeviceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    _SELECT = """
        SELECT d.device_id, d.name, d.registered_at, d.registered_by,
               COALESCE(array_agg(l.library_id ORDER BY l.name, l.library_id)
                        FILTER (WHERE l.library_id IS NOT NULL), '{}') AS libraries
        FROM devices d LEFT JOIN libraries l ON l.reader = d.device_id
    """

    def list(self) -> list[dict[str, Any]]:
        """Every device, by name: {device_id, name, registered_at,
        registered_by, libraries}, libraries the ids of those it reads (in
        the trash too)."""
        return [dict(r) for r in self._session.execute(text(
            self._SELECT + " GROUP BY d.device_id ORDER BY d.name, d.device_id")).mappings()]

    def get(self, device_id: str) -> dict[str, Any] | None:
        row = self._session.execute(text(self._SELECT + " WHERE d.device_id = :d GROUP BY d.device_id"),
                                    {"d": device_id}).mappings().first()
        return dict(row) if row is not None else None

    def add(self, name: str, registered_by: str | None, now: datetime) -> str:
        """Register a device (no commit). Its id."""
        device_id = "dev_" + str(ULID())
        self._session.execute(text("INSERT INTO devices (device_id, name, registered_at, registered_by)"
                                   " VALUES (:d, :n, :now, :by)"),
                              {"d": device_id, "n": name, "now": now, "by": registered_by})
        return device_id

    def rename(self, device_id: str, name: str) -> bool:
        """No commit. False when there's no such device."""
        return bool(self._session.execute(text("UPDATE devices SET name = :n WHERE device_id = :d"),
                                          {"n": name, "d": device_id}).rowcount)

    def remove(self, device_id: str) -> bool:
        """No commit. False when there's no such device."""
        return bool(self._session.execute(text("DELETE FROM devices WHERE device_id = :d"),
                                          {"d": device_id}).rowcount)

    def libraries_read_by(self, device_id: str) -> list[dict[str, str]]:
        """The libraries the device reads (in the trash too): {library_id, name}, by name."""
        return [dict(r) for r in self._session.execute(text(
            "SELECT library_id, name FROM libraries WHERE reader = :d ORDER BY name, library_id"),
            {"d": device_id}).mappings()]
