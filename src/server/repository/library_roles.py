"""Roles per library (ADR-018 decision 8, phase 8): a person may edit one
library, only view another, and not see a third.

A person's role on a library, the one rule (roles_of, role_on):
- an account admin's is "admin" on every library;
- else the one set for them on it (library_roles), if there is one: "admin"
  (the library's admin: its settings, its reader, its people), "editor",
  "viewer", or "none" (they don't see it);
- else "none" on a private library (libraries.private), and on every
  library for someone who sees only the ones they're given
  (users.every_library false);
- else their account role.

A key a person made acts as them, never above its own role: its role on a
library is the lower of the two. A key no person made (the scheduler's, an
operator's) has its role on every library, and a device's the device
gate's (src/server/api/devices.py): neither is a Person.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session

# Least to most.
ROLES = ("none", "viewer", "editor", "admin")
RANK = {role: rank for rank, role in enumerate(ROLES)}
ACCOUNT_ROLES = ("viewer", "editor", "admin")


@dataclass(frozen=True)
class Person:
    """Whose roles a request has: a person signed in (cap None), or a key a
    person made (cap, its role)."""

    user_id: str
    account_role: str  # users.role
    every_library: bool = True
    cap: str | None = None

    @property
    def account_admin(self) -> bool:
        return self.account_role == "admin"


class MakerGoneError(Exception):
    """A key a person made whose person isn't in its account any more: it does nothing."""


def person_of_key(api_key, users) -> Person | None:
    """Whose roles a key has: the person who made it, never above its own
    role; None for a key no person made, or a device's. users: the control
    database's UserRepository. MakerGoneError when its person has left."""
    if api_key.created_by_user_id is None or api_key.device_id is not None:
        return None
    maker = users.get_by_id(api_key.created_by_user_id)
    if maker is None or maker.tenant_id != api_key.tenant_id:
        raise MakerGoneError(api_key.key_id)
    return Person(maker.user_id, maker.role, maker.every_library, cap=api_key.role)


def person_of_user(user) -> Person:
    """A person signed in, as they are now."""
    return Person(user.user_id, user.role, user.every_library)


def lower(a: str, b: str | None) -> str:
    return a if b is None or RANK[a] <= RANK[b] else b


# What a request names, and the library it's in.
_LIBRARY_OF = {
    "library": "SELECT library_id FROM libraries WHERE library_id = ANY(:ids)",
    "asset": "SELECT DISTINCT library_id FROM assets WHERE asset_id = ANY(:ids)",
    "scan": "SELECT DISTINCT library_id FROM scan_sessions WHERE scan_id = ANY(:ids)",
    "upload": "SELECT DISTINCT a.library_id FROM uploads u JOIN assets a ON a.asset_id = u.asset_id"
              " WHERE u.upload_id = ANY(:ids)",
    "chunk": "SELECT DISTINCT a.library_id FROM video_index_chunks c JOIN assets a ON a.asset_id = c.asset_id"
             " WHERE c.chunk_id = ANY(:ids)",
    "scene": "SELECT DISTINCT a.library_id FROM video_scenes s JOIN assets a ON a.asset_id = s.asset_id"
             " WHERE s.scene_id = ANY(:ids)",
    "face": "SELECT DISTINCT a.library_id FROM faces f JOIN assets a ON a.asset_id = f.asset_id"
            " WHERE f.face_id = ANY(:ids)",
}
NAMED = tuple(_LIBRARY_OF)


def libraries_of(session: Session, kind: str, ids: list[str]) -> set[str]:
    """The libraries these libraries, clips, scans, uploads, chunks, scenes
    or faces are in (in the trash too); one that isn't there is in none."""
    if not ids:
        return set()
    return set(session.execute(text(_LIBRARY_OF[kind]), {"ids": list(ids)}).scalars())


_ROLES = """
    SELECT l.library_id,
           CASE WHEN :admin THEN 'admin'
                ELSE COALESCE(r.role, CASE WHEN l.private OR NOT :every THEN 'none' ELSE :account END)
           END AS role
    FROM libraries l LEFT JOIN library_roles r ON r.library_id = l.library_id AND r.user_id = :user
"""


def _params(person: Person) -> dict:
    if person.account_role not in ACCOUNT_ROLES:
        raise ValueError(f"not an account role: {person.account_role!r}")
    return {"admin": person.account_admin, "every": person.every_library, "account": person.account_role,
            "user": person.user_id}


class LibraryRoleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def roles_of(self, person: Person) -> dict[str, str]:
        """Their role on every library the account has (in the trash too),
        by its id; "none" on the ones they don't see."""
        rows = self._session.execute(text(_ROLES), _params(person)).all()
        return {library_id: lower(role, person.cap) for library_id, role in rows}

    def role_on(self, person: Person, library_id: str) -> str | None:
        """Their role on one library; None when there's no such library."""
        row = self._session.execute(text(_ROLES + " WHERE l.library_id = :l"),
                                    {**_params(person), "l": library_id}).first()
        return None if row is None else lower(row.role, person.cap)

    def set_for(self, library_id: str) -> list[dict]:
        """The roles set on a library for its people, by when: {user_id, role,
        set_by, set_at}."""
        return [dict(r) for r in self._session.execute(text(
            "SELECT user_id, role, set_by, set_at FROM library_roles WHERE library_id = :l"
            " ORDER BY set_at, user_id"), {"l": library_id}).mappings()]

    def set(self, library_id: str, user_id: str, role: str | None, *, by: str | None, now: datetime) -> None:
        """Set a person's role on a library; None: the rule's again. No commit."""
        if role is None:
            self._session.execute(text("DELETE FROM library_roles WHERE library_id = :l AND user_id = :u"),
                                  {"l": library_id, "u": user_id})
            return
        if role not in RANK:
            raise ValueError(f"not a library role: {role!r}")
        self._session.execute(text(
            "INSERT INTO library_roles (library_id, user_id, role, set_by, set_at) VALUES (:l, :u, :r, :by, :now)"
            " ON CONFLICT (library_id, user_id) DO UPDATE SET role = :r, set_by = :by, set_at = :now"),
            {"l": library_id, "u": user_id, "r": role, "by": by, "now": now})

    def forget(self, user_id: str) -> None:
        """A person who left the account: their roles go. No commit."""
        self._session.execute(text("DELETE FROM library_roles WHERE user_id = :u"), {"u": user_id})
