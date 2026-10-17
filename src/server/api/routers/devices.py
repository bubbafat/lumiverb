"""An account's devices (ADR-018 decision 8): computers that scan the
libraries they read, not people. Account admins only.

An admin registers a device and gets its key, once; gives it a new key (the
old one stops at once: a lost or stolen computer); ends its key; renames
it; or removes it, once it reads no library. A device has one key at a
time, and like any key a person made, a new password of theirs ends it.
What a device's key may do: src/server/api/devices.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_tenant_admin
from src.server.api.errors import ConflictError
from src.server.api.routers.keys import caller_user_id
from src.server.database import get_control_session
from src.server.models.control_plane import ApiKey
from src.server.repository.control_plane import ApiKeyRepository
from src.server.repository.devices import DeviceRepository
from src.shared.path_filter import has_control_character
from src.shared.utils import utcnow

router = APIRouter(prefix="/v1/devices", tags=["devices"], dependencies=[Depends(require_tenant_admin)])


class DeviceName(BaseModel):
    # For people: "Robert's Mac Studio".
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _plain(cls, name: str) -> str:
        name = name.strip()
        if not name or has_control_character(name):
            raise ValueError("a name, with no control characters")
        return name


class Device(BaseModel):
    device_id: str
    name: str
    registered_at: datetime
    # Whether it has a key: none once ended, or once a new password of whoever made it ended it.
    has_key: bool
    key_last_used_at: datetime | None = None
    # The libraries it reads (their reader names it).
    libraries: list[str]


class DeviceList(BaseModel):
    devices: list[Device]


class DeviceWithKey(Device):
    # Its key, shown this once: the device sends it as Authorization: Bearer <key>.
    key: str


def _shown(device: dict, key: ApiKey | None) -> Device:
    return Device(device_id=device["device_id"], name=device["name"], registered_at=device["registered_at"],
                  has_key=key is not None, key_last_used_at=key.last_used_at if key is not None else None,
                  libraries=list(device["libraries"]))


def _keys(tenant_id: str) -> dict[str, ApiKey]:
    with get_control_session() as ctrl:
        return ApiKeyRepository(ctrl).device_keys(tenant_id)


def _held(session: Session, device_id: str) -> dict:
    device = DeviceRepository(session).get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _new_key(request: Request, device: dict) -> DeviceWithKey:
    with get_control_session() as ctrl:
        key, plaintext = ApiKeyRepository(ctrl).new_device_key(
            request.state.tenant_id, device["device_id"], caller_user_id(request, ctrl))
        return DeviceWithKey(**_shown(device, key).model_dump(), key=plaintext)


@router.get("", response_model=DeviceList)
def list_devices(request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> DeviceList:
    """The account's devices, by name, each with whether it has a key and the libraries it reads."""
    keys = _keys(request.state.tenant_id)
    return DeviceList(devices=[_shown(d, keys.get(d["device_id"])) for d in DeviceRepository(session).list()])


@router.post("", response_model=DeviceWithKey)
def register_device(body: DeviceName, request: Request,
                    session: Annotated[Session, Depends(get_tenant_session)]) -> DeviceWithKey:
    """Register a device: its key comes back this once. It reads no library
    until one names it as its reader."""
    with get_control_session() as ctrl:
        by = caller_user_id(request, ctrl)
    device_id = DeviceRepository(session).add(body.name, by, utcnow())
    session.commit()
    return _new_key(request, _held(session, device_id))


@router.patch("/{device_id}", response_model=Device)
def rename_device(device_id: str, body: DeviceName, request: Request,
                  session: Annotated[Session, Depends(get_tenant_session)]) -> Device:
    """Rename a device. 404 when there's none."""
    if not DeviceRepository(session).rename(device_id, body.name):
        raise HTTPException(status_code=404, detail="Device not found")
    session.commit()
    return _shown(_held(session, device_id), _keys(request.state.tenant_id).get(device_id))


@router.post("/{device_id}/key", response_model=DeviceWithKey)
def new_device_key(device_id: str, request: Request,
                   session: Annotated[Session, Depends(get_tenant_session)]) -> DeviceWithKey:
    """A new key for the device, back this once; its old one stops at once. 404 when there's no such device."""
    return _new_key(request, _held(session, device_id))


@router.delete("/{device_id}/key", status_code=204)
def end_device_key(device_id: str, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """End the device's key now: it does nothing until it gets a new one. 404 when there's no such device."""
    _held(session, device_id)
    with get_control_session() as ctrl:
        ApiKeyRepository(ctrl).end_device_key(request.state.tenant_id, device_id)


@router.delete("/{device_id}", status_code=204)
def remove_device(device_id: str, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Remove a device and end its key. 409 device_reads_libraries while a
    library names it as its reader (details.libraries): choose another
    reader for those first. 404 when there's none."""
    _held(session, device_id)
    reads = DeviceRepository(session).libraries_read_by(device_id)
    if reads:
        raise ConflictError("device_reads_libraries",
                            f"The device reads {len(reads)} {'library' if len(reads) == 1 else 'libraries'}:"
                            " choose another reader for them first.", {"libraries": reads})
    with get_control_session() as ctrl:  # its key first: a device without a key does nothing
        ApiKeyRepository(ctrl).end_device_key(request.state.tenant_id, device_id)
    DeviceRepository(session).remove(device_id)
    session.commit()
