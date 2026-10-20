"""Try it as a viewer: a demo server (try.lumiverb.io, docs/demo-server.md)
lets anyone in, with no password, as the one viewer its env file names
(LUMIVERB_DEMO_VIEWER, an email). Off unless that names a viewer who isn't a
server admin, in an account that isn't deleted.

A demo session reads only: its token says it's one (the "demo" claim), and
the middleware refuses it anything but a read, whatever the viewer's roles
on each library. Visitors share the one person, so nothing one of them does
is seen by the next. The setting removed, or that person no longer the
demo's viewer, ends every demo session at its next request."""

from __future__ import annotations

import logging

from sqlmodel import Session

from src.server.config import get_settings
from src.server.repository.control_plane import TenantRepository, UserRepository

logger = logging.getLogger(__name__)

# What a demo session may send: reads. Nothing else, a sign-out included (routers/auth.py, which the
# middleware doesn't see), is let through.
READS = frozenset({"GET", "HEAD"})


def demo_viewer(session: Session):
    """The person demo sessions are, or None while the demo is off."""
    email = get_settings().lumiverb_demo_viewer.strip()
    if not email:
        return None
    user = UserRepository(session).get_by_email(email)
    if user is None or user.role != "viewer" or user.server_admin or TenantRepository(session).gone(user.tenant_id):
        logger.warning("LUMIVERB_DEMO_VIEWER names %s, who isn't a viewer here (one who isn't a server admin, "
                       "in an account that isn't deleted): the demo is off", email)
        return None
    return user


def is_demo_viewer(session: Session, user) -> bool:
    """A demo session's person is still the demo's viewer."""
    viewer = demo_viewer(session)
    return viewer is not None and user is not None and viewer.user_id == user.user_id
