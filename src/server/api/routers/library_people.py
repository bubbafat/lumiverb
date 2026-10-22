"""Who can see and edit a library (ADR-018 decision 8, roles per library):
its people, each with their role on it, and the role set for one of them.
The library's admin's, an account admin being every library's.

A person's role on a library is one rule (src/server/repository/library_roles.py):
an account admin's is admin; else the one set for them here; else none on a
private library or for someone who sees only what they're given; else their
account role.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_library_admin
from src.server.api.errors import ConflictError
from src.server.database import get_control_session
from src.server.repository.control_plane import UserRepository
from src.server.repository.libraries import LibraryRepository
from src.server.repository.library_roles import LibraryRoleRepository, Person
from src.shared.utils import utcnow

router = APIRouter(prefix="/v1/libraries", tags=["libraries"])


class LibraryPerson(BaseModel):
    user_id: str
    email: str
    account_role: str  # their role in the account
    every_library: bool  # false: they see only the libraries they're given a role on
    role: str  # their role on this library now: admin, editor, viewer or none
    # The role set for them on this library (None: the rule gives it) and by whom, when.
    set_role: str | None = None
    set_by: str | None = None
    set_at: datetime | None = None


class LibraryPeople(BaseModel):
    library_id: str
    private: bool  # only the account's admins and the people given a role on it see it
    people: list[LibraryPerson]


class RoleChoice(BaseModel):
    # Their role on the library; null: the rule's again (their account role, or none on a private library).
    role: Literal["admin", "editor", "viewer", "none"] | None


def _library_or_404(session: Session, library_id: str):
    library = LibraryRepository(session).get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    return library


def _people(request: Request) -> list:
    with get_control_session() as control:
        return UserRepository(control).list_by_tenant(request.state.tenant_id)


def _person(session: Session, library_id: str, user, set_rows: dict[str, dict]) -> LibraryPerson:
    row = set_rows.get(user.user_id) or {}
    role = LibraryRoleRepository(session).role_on(Person(user.user_id, user.role, user.every_library), library_id)
    return LibraryPerson(user_id=user.user_id, email=user.email, account_role=user.role,
                         every_library=user.every_library, role=role or "none", set_role=row.get("role"),
                         set_by=row.get("set_by"), set_at=row.get("set_at"))


@router.get("/{library_id}/people", response_model=LibraryPeople, dependencies=[Depends(require_library_admin)])
def library_people(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryPeople:
    """Everyone in the account, with their role on this library and the role
    set for them here, if one is."""
    library = _library_or_404(session, library_id)
    set_rows = {r["user_id"]: r for r in LibraryRoleRepository(session).set_for(library_id)}
    return LibraryPeople(library_id=library_id, private=library.private,
                         people=[_person(session, library_id, u, set_rows) for u in _people(request)])


@router.put("/{library_id}/people/{user_id}", response_model=LibraryPerson,
            dependencies=[Depends(require_library_admin)])
def set_library_role(
    library_id: str,
    user_id: str,
    body: RoleChoice,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> LibraryPerson:
    """Set a person's role on this library (null: the rule's again). 404 for
    someone not in the account; 409 account_admin for an account admin, who
    is every library's admin; 403 server_admin_user for a server admin, unless
    a server admin asks (as for changing them anywhere else, users.py)."""
    from src.server.api.routers.users import _within_reach

    _library_or_404(session, library_id)
    user = next((u for u in _people(request) if u.user_id == user_id), None)
    if user is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if user.role == "admin":
        raise ConflictError("account_admin", f"{user.email} is an account admin, every library's admin.",
                            {"user_id": user_id})
    _within_reach(request, user)
    by = getattr(request.state, "user_id", None) or getattr(request.state, "key_id", None)
    repo = LibraryRoleRepository(session)
    repo.set(library_id, user_id, body.role, by=by, now=utcnow())
    session.commit()
    set_rows = {r["user_id"]: r for r in repo.set_for(library_id)}
    return _person(session, library_id, user, set_rows)
