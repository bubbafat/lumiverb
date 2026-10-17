"""An install's first-run setup: its first admin.

Open while nobody can log in yet: no user on this server. It makes the first
admin in the account the installer made (deploy-api.sh makes one on Linux),
or, with none (a Mac), in a new one, and the install's server admin. Then
it's closed for good.

Two doors, one rule: the web app's setup page (POST /v1/setup, which only
answers the machine Lumiverb runs on) and `lumiverb-server setup` (which
reads the install's env file, so only its owner can run it).
"""

from __future__ import annotations

import bcrypt
from sqlalchemy import text
from sqlmodel import Session, select

from src.server.accounts import create_account
from src.server.api.routers.auth import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH  # as for any user
from src.server.database import get_control_engine
from src.server.models.control_plane import User
from src.server.repository.control_plane import TenantRepository, UserRepository

DEFAULT_ACCOUNT_NAME = "Lumiverb"  # renamed later in Settings
ALREADY_SET_UP = "This Lumiverb is set up already. Log in instead."
# One setup at a time: a second waits, then finds the first admin made.
_SETUP_LOCK = 0x4C56_5345  # "LVSE"


class SetupError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def setup_needed(session: Session) -> bool:
    """Nobody can log in yet: no user on this server."""
    return session.exec(select(User.user_id).limit(1)).first() is None


def _checked(email: str, password: str) -> str:
    email = email.strip()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise SetupError(400, "invalid_email", "Enter your email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SetupError(400, "password_too_short", f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise SetupError(400, "password_too_long", f"Use at most {MAX_PASSWORD_BYTES} characters.")
    return email


def create_first_admin(email: str, password: str, *, account_name: str | None = None) -> User:
    """The install's first admin, in its one account (made if there's none).
    Raises SetupError when it's set up already or the details won't do, and
    AccountError (src/server/accounts.py) when a new account can't be made."""
    email = _checked(email, password)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    engine = get_control_engine()
    with engine.connect() as lock:
        # A session-level lock, held on its own connection across the commits below.
        lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _SETUP_LOCK})
        try:
            with Session(engine) as session:
                if not setup_needed(session):
                    raise SetupError(409, "already_set_up", ALREADY_SET_UP)
                accounts = [t for t in TenantRepository(session).list_all() if t.status == "active"]
                if len(accounts) > 1:
                    raise SetupError(409, "several_accounts",
                                     "There's more than one account, so setup can't tell which is yours. "
                                     "Add its admin with `lumiverb user create`.")
                tenant_id = accounts[0].tenant_id if accounts else create_account(
                    session, (account_name or "").strip() or DEFAULT_ACCOUNT_NAME)
                # Who sets up the install is its server admin too (ADR-018 decision 8).
                return UserRepository(session).create(tenant_id, email, password_hash, role="admin",
                                                      server_admin=True)
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _SETUP_LOCK})
