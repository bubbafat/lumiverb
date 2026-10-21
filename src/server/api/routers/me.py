"""GET /v1/me — return current user info; POST /v1/me/password — change your own; POST
/v1/me/failed-requests — what your browser couldn't load, for the log."""

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from src.server.database import get_control_session
from src.server.repository.control_plane import ApiKeyRepository, UserRepository

router = APIRouter(prefix="/v1/me", tags=["me"])
logger = logging.getLogger(__name__)


class CurrentUserResponse(BaseModel):
    user_id: str | None
    email: str | None
    role: str
    # The install's admin too (ADR-018 decision 8): only when signed in, never with a key.
    server_admin: bool = False
    # Signed in to try it as a viewer (src/server/demo.py): a session that reads only.
    demo: bool = False


@router.get("", response_model=CurrentUserResponse)
def get_current_user(request: Request) -> CurrentUserResponse:
    """Return the authenticated user's info, or just role for API key auth."""
    user_id: str | None = getattr(request.state, "user_id", None)
    role: str = getattr(request.state, "role", "viewer")

    if not user_id:
        return CurrentUserResponse(user_id=None, email=None, role=role)
    server_admin = getattr(request.state, "server_admin", False) is True

    email: str | None = None
    with get_control_session() as session:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is not None:
            email = user.email

    return CurrentUserResponse(user_id=user_id, email=email, role=role, server_admin=server_admin,
                               demo=getattr(request.state, "demo", False) is True)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
    # The API keys they made end with the old password (devices' too): said first, 409 keys_end_with_password.
    end_api_keys: bool = False


class PasswordChanged(BaseModel):
    access_token: str  # this browser's new session: every other one has ended
    token_type: str = "bearer"
    expires_in: int
    api_keys_revoked: int


@router.post("/password", response_model=PasswordChanged)
def change_password(body: PasswordChange, request: Request) -> PasswordChanged:
    """A person signed in changes their own password, knowing the one they have. As a reset does, it ends what
    the old one could have made (every session, the API keys they made, their links not yet used), and this
    browser gets a new session. Never with an API key: whoever holds a key mustn't take the account."""
    import bcrypt

    from src.server.api.errors import ApiError, DecisionRequiredError
    from src.server.api.rate_limit import change_password_limiter
    from src.server.api.routers.auth import (
        JWT_EXPIRY_SECONDS,
        MAX_PASSWORD_BYTES,
        MIN_PASSWORD_LENGTH,
        _issue_jwt,
    )
    from src.server.config import get_settings

    user_id: str | None = getattr(request.state, "user_id", None)
    if not user_id or getattr(request.state, "key_id", None):
        raise ApiError(403, "signed_in_only", "A password is changed signed in with it, not with an API key.")
    change_password_limiter.check(request)
    change_password_limiter.check(request, key=f"user:{user_id}")
    new = body.new_password
    if len(new) < MIN_PASSWORD_LENGTH or len(new.encode()) > MAX_PASSWORD_BYTES:
        raise ApiError(400, "password_length", f"Use {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_BYTES} characters.")
    settings = get_settings()
    if not settings.jwt_secret:
        raise ApiError(500, "jwt_secret_missing", "JWT_SECRET isn't set in the install's env file.")

    with get_control_session() as session:
        users = UserRepository(session)
        user = users.get_by_id(user_id)
        current = body.current_password.encode()
        # 400, not 401: the session is good, and a 401 would sign this browser out.
        if user is None or len(current) > MAX_PASSWORD_BYTES or not bcrypt.checkpw(current, user.password_hash.encode()):
            raise ApiError(400, "wrong_password", "That isn't your current password.")
        keys = ApiKeyRepository(session).live_made_by(user.tenant_id, user.user_id)
        if keys and not body.end_api_keys:
            raise DecisionRequiredError(
                "keys_end_with_password",
                f"A new password ends the {len(keys)} API key{'s' if len(keys) != 1 else ''} you made: whatever "
                "uses them stops until it's given a new one.",
                {"keys": [{"key_id": k.key_id, "label": k.label, "role": k.role, "device": k.device_id is not None,
                           "last_used_at": k.last_used_at} for k in keys]},
            )
        revoked = users.update_password(user.user_id, bcrypt.hashpw(new.encode(), bcrypt.gensalt(rounds=12)).decode())
        token = _issue_jwt(users.get_by_id(user_id), settings.jwt_secret)
    return PasswordChanged(access_token=token, expires_in=JWT_EXPIRY_SECONDS, api_keys_revoked=revoked)


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_SHORT = 200


def _plain(value: str) -> str:
    """Text a log line takes as it is: one line, nothing that isn't printed (no newline to forge a line with,
    no escape to color one)."""
    if not value.isprintable():
        raise ValueError("plain text only, on one line")
    return value


class FailedRequest(BaseModel):
    """A request the browser couldn't make at all: no answer came, so the server never saw it."""

    method: str
    path: str = Field(max_length=_SHORT)  # the API's path, /v1/..., its query left out
    error: str = Field(max_length=_SHORT)  # the browser's own words: "Load failed", "Failed to fetch"
    at: datetime  # when, by the browser's clock
    page: str = Field(default="", max_length=_SHORT)  # where in the app it was, its path

    @field_validator("method")
    @classmethod
    def _method(cls, value: str) -> str:
        if value not in _METHODS:
            raise ValueError(f"one of {', '.join(sorted(_METHODS))}")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/v1/") or "?" in value or " " in value:
            raise ValueError("the API's path, /v1/..., without its query")
        return _plain(value)

    @field_validator("error", "page")
    @classmethod
    def _text(cls, value: str) -> str:
        return _plain(value)


class FailedRequests(BaseModel):
    failed: list[FailedRequest] = Field(min_length=1, max_length=20)


@router.post("/failed-requests", status_code=204)
def report_failed_requests(body: FailedRequests, request: Request) -> None:
    """Requests this person's browser couldn't make at all, sent once one gets through (the web app's
    src/ui/web/src/api/failedRequests.ts): the connection dropped, so the server never saw them, and nothing
    else would say so. Each a line in the log, which Admin › Logs shows, with who and their browser. A person
    signed in, never a key; 20 at a time; 10 sends a minute, by address and by person (429)."""
    from src.server.api.errors import ApiError
    from src.server.api.rate_limit import failed_requests_limiter

    user_id: str | None = getattr(request.state, "user_id", None)
    if not user_id or getattr(request.state, "key_id", None):
        raise ApiError(403, "signed_in_only", "A browser signed in says what it couldn't load, not an API key.")
    failed_requests_limiter.check(request)
    failed_requests_limiter.check(request, key=f"user:{user_id}")
    with get_control_session() as session:
        user = UserRepository(session).get_by_id(user_id)
    who = user.email if user is not None else user_id
    browser = "".join(c for c in request.headers.get("user-agent", "") if c.isprintable())[:_SHORT] or "no browser said"
    for f in body.failed:
        logger.warning("web: %s's browser couldn't reach the server for %s %s at %s: %s (on %s; %s)",
                       who, f.method, f.path, f.at.isoformat(timespec="seconds"), f.error, f.page or "?", browser)
