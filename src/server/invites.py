"""Invites: an admin adds someone by email and role (routers/users.py, `lumiverb
user create`) and gets a link to send them, instead of choosing their
password. Opening it, they choose their own and are signed in
(routers/auth.py join). Until then their password is one nobody knows.

The link is a one-time token (password_reset_tokens), good for 7 days, at
the address other people open Lumiverb at: a Mac's network address once
Settings › Network lets other devices use it (lifecycle/network.py),
otherwise APP_HOST (a server's public URL; a Mac's own localhost). A new
link for someone who hasn't joined yet voids the old one.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
from sqlmodel import Session

from src.server.config import get_settings
from src.server.lifecycle import network
from src.server.repository.control_plane import PasswordResetTokenRepository
from src.shared.utils import utcnow

INVITE_FOR = timedelta(days=7)


@dataclass(frozen=True)
class Invite:
    url: str
    expires_at: datetime


def unusable_password_hash() -> str:
    """A password nobody knows, hashed as any other (a login's check takes as long)."""
    return bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt(rounds=12)).decode()


def where_people_open_it() -> str:
    settings = get_settings()
    data = Path(settings.data_dir)
    if settings.web_port > 0 and network.managed(data, settings.nginx_config) and network.other_devices(data):
        return network.addresses(settings.web_port)[0]
    return settings.app_host.rstrip("/")


def new_invite(session: Session, user_id: str) -> Invite:
    """The user's link; any older one they haven't used stops working."""
    tokens = PasswordResetTokenRepository(session)
    tokens.void_unused(user_id)
    plaintext = secrets.token_urlsafe(32)
    expires_at = utcnow() + INVITE_FOR
    tokens.create(user_id, plaintext, expires_at)
    return Invite(f"{where_people_open_it()}/join?token={plaintext}", expires_at)
