"""Auth endpoints: login, forgot-password, reset-password, logout, refresh."""

from __future__ import annotations

import secrets
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText
from typing import Annotated

import bcrypt
import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session

from src.server.config import get_settings
from src.server.database import get_control_engine
from src.shared.utils import utcnow
from src.server.repository.control_plane import (
    PasswordResetTokenRepository,
    RevokedTokenRepository,
    TenantRepository,
    UserRepository,
)
from src.server.api.rate_limit import (
    demo_limiter,
    forgot_password_anywhere,
    forgot_password_here,
    forgot_password_limiter,
    login_failures,
    login_failures_here,
    login_limiter,
    name_cleared,
    name_counted,
    name_held,
    reset_password_limiter,
)
from ulid import ULID

router = APIRouter(prefix="/v1/auth", tags=["auth"])

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 3600  # 1 hour
REFRESH_WINDOW_SECONDS = 7 * 24 * 3600  # 7 days — how long a token can be refreshed
# bcrypt reads only the first 72 bytes (bcrypt 5 raises past them).
MAX_PASSWORD_BYTES = 72
# An unknown email's login checks against this, so it takes as long as a known one's.
_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12))


def _get_session() -> Session:
    engine = get_control_engine()
    return Session(engine)


def _get_db():
    with _get_session() as session:
        yield session


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = JWT_EXPIRY_SECONDS


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = JWT_EXPIRY_SECONDS


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


def _issue_jwt(user, jwt_secret: str, *, refresh_exp: int | None = None, demo: bool = False) -> str:
    """A signed JWT for the user as the database has them now: their role and
    token version ("tv"), with a unique jti for revocation. refresh_exp, the
    outer bound after which even refresh is denied, is set at login and kept
    by every refresh (no sliding). A demo session's says so (src/server/demo.py)."""
    now = utcnow()
    jti = str(ULID())
    payload = {
        "sub": user.user_id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "email": user.email,
        "tv": user.token_version,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + JWT_EXPIRY_SECONDS,
        "refresh_exp": refresh_exp if refresh_exp is not None else int(now.timestamp()) + REFRESH_WINDOW_SECONDS,
    }
    if demo:
        payload["demo"] = True
    return jwt.encode(payload, jwt_secret, algorithm=JWT_ALGORITHM)


def _account_live(session: Session, user) -> None:
    """403 account_deleted for someone whose account the server admin deleted: nobody signs in to it again."""
    if TenantRepository(session).gone(user.tenant_id):
        raise HTTPException(status_code=403, detail="This account was deleted")


def token_user_is_current(user, claims: dict) -> bool:
    """The token was issued to this user as they are now: same tenant and
    token version. A user deleted, given another role or with a reset
    password fails this (their version moved on, or they're gone)."""
    return (
        user is not None
        and user.tenant_id == claims.get("tenant_id")
        and isinstance(claims.get("tv"), int)
        and claims["tv"] == user.token_version
    )


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[Session, Depends(_get_db)],
) -> LoginResponse:
    # Every try by address; by email only failures, held before the password is checked (rate_limit.py says why).
    login_limiter.check(request)
    email = f"email:{body.email.strip().lower()}"
    name_held(request, email, login_failures_here, login_failures)

    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")

    def failed() -> HTTPException:
        name_counted(request, email, login_failures_here, login_failures)
        return HTTPException(status_code=401, detail="Invalid email or password")

    password = body.password.encode()
    if len(password) > MAX_PASSWORD_BYTES:
        raise failed()

    user_repo = UserRepository(session)
    user = user_repo.get_by_email(body.email)

    if user is None:
        bcrypt.checkpw(password, _DUMMY_HASH)  # as long as a known email's check
        raise failed()

    if not bcrypt.checkpw(password, user.password_hash.encode()):
        raise failed()
    name_cleared(request, email, login_failures_here)
    _account_live(session, user)  # after the password: said only to someone who knows it

    token = _issue_jwt(user, settings.jwt_secret)
    user_repo.update_last_login(user.user_id)

    return LoginResponse(access_token=token)


class DemoResponse(BaseModel):
    on: bool = True


def _demo_viewer(session: Session):
    from src.server.api.errors import ApiError
    from src.server.demo import demo_viewer

    viewer = demo_viewer(session)
    if viewer is None:
        raise ApiError(404, "not_found", "Not Found")  # as a server with no demo answers
    return viewer


@router.get("/demo", response_model=DemoResponse)
def demo_on(session: Annotated[Session, Depends(_get_db)]) -> DemoResponse:
    """Whether this server lets anyone try it as a viewer (src/server/demo.py): the sign-in page offers it."""
    _demo_viewer(session)
    return DemoResponse()


@router.post("/demo", response_model=LoginResponse)
def demo_sign_in(request: Request, session: Annotated[Session, Depends(_get_db)]) -> LoginResponse:
    """Try it as a viewer: signed in as the demo's viewer, with no password,
    for a session that reads only (src/server/demo.py)."""
    demo_limiter.check(request)
    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    return LoginResponse(access_token=_issue_jwt(_demo_viewer(session), settings.jwt_secret, demo=True))


@router.post("/refresh", response_model=RefreshResponse)
def refresh_token(request: Request, session: Annotated[Session, Depends(_get_db)]) -> RefreshResponse:
    """Issue a fresh JWT if the current token is valid and within its refresh window.

    The caller sends their current (possibly expired) JWT in the Authorization
    header. If the token is within its refresh_exp window and has not been
    revoked, a new JWT is issued and the old jti is revoked.
    """
    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    raw_token = auth[7:].strip()

    # Decode allowing expired tokens — we check refresh_exp manually.
    try:
        claims = jwt.decode(
            raw_token,
            settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Check refresh window: the one set at login, never extended.
    import time
    now = int(time.time())
    refresh_exp = claims.get("refresh_exp")
    if not isinstance(refresh_exp, int) or now > refresh_exp:
        raise HTTPException(status_code=401, detail="Refresh window expired — please log in again")

    # Check revocation
    old_jti = claims.get("jti")
    if not old_jti:
        raise HTTPException(status_code=401, detail="Invalid token")
    revoked_repo = RevokedTokenRepository(session)
    if revoked_repo.is_revoked(old_jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    # The user as they are now: gone, or with another role or password since, is no refresh.
    user = UserRepository(session).get_by_id(str(claims.get("sub", "")))
    if not token_user_is_current(user, claims):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    _account_live(session, user)
    demo = claims.get("demo") is True
    if demo:
        from src.server.demo import is_demo_viewer

        if not is_demo_viewer(session, user):
            raise HTTPException(status_code=401, detail="The demo is over")

    # Revoke the old token so it can't be refreshed again (only one refresh wins).
    if not revoked_repo.revoke(old_jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    new_token = _issue_jwt(user, settings.jwt_secret, refresh_exp=refresh_exp, demo=demo)
    return RefreshResponse(access_token=new_token)


RESET_ELSEWHERE = ("Password reset isn't set up here. On the computer Lumiverb is installed on, "
                   "use Forgot password there, or ask whoever looks after it.")
RESET_NOT_SET_UP = "Password reset isn't set up here. Ask whoever looks after Lumiverb."


@router.post("/forgot-password", status_code=204, response_model=None)
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
) -> JSONResponse | None:
    """Where the install says the person at its machine owns it (a Mac's,
    LUMIVERB_RESET_ON_THIS_MACHINE), from that machine: the reset link the
    email would carry, at once (a Mac has no email set up). Otherwise: the
    email, when SMTP is set up."""
    from src.server.api.client_address import on_this_machine
    from src.server.api.errors import ApiError

    # Every request by address; each link asked for by email, from here and from anywhere (rate_limit.py says why).
    forgot_password_limiter.check(request)
    email = f"email:{body.email.strip().lower()}"
    name_held(request, email, forgot_password_here, forgot_password_anywhere)
    name_counted(request, email, forgot_password_here, forgot_password_anywhere)

    settings = get_settings()
    if settings.lumiverb_reset_on_this_machine and on_this_machine(request):
        with _get_session() as session:
            user = UserRepository(session).get_by_email(body.email.strip())
            if user is None:
                raise ApiError(404, "no_such_account", "There's no account with that email here.")
            plaintext_token = secrets.token_urlsafe(32)
            PasswordResetTokenRepository(session).create(user.user_id, plaintext_token, utcnow() + timedelta(hours=1))
        return JSONResponse({"reset_url": f"/reset-password?token={plaintext_token}"})

    smtp_ready = all(
        [
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_user,
            settings.smtp_password,
            settings.smtp_from,
            settings.app_host,
        ]
    )
    if not smtp_ready:
        # The same for any email when reset is unavailable here.
        if settings.lumiverb_reset_on_this_machine:
            raise ApiError(501, "reset_elsewhere", RESET_ELSEWHERE)
        raise ApiError(501, "reset_not_set_up", RESET_NOT_SET_UP)

    # 204 always, at once: whether the email is known (and the mail's sent) happens after.
    background.add_task(_send_reset_email, body.email)


def _send_reset_email(email: str) -> None:
    settings = get_settings()
    with _get_session() as session:
        user = UserRepository(session).get_by_email(email)
        if user is None:
            return
        user_id, user_email = user.user_id, user.email
        plaintext_token = secrets.token_urlsafe(32)
        PasswordResetTokenRepository(session).create(user_id, plaintext_token, utcnow() + timedelta(hours=1))

    reset_url = f"{settings.app_host.rstrip('/')}/reset-password?token={plaintext_token}"
    msg = MIMEText(f"Click the link to reset your password (expires in 1 hour):\n\n{reset_url}")
    msg["Subject"] = "Reset your Lumiverb password"
    msg["From"] = settings.smtp_from
    msg["To"] = user_email

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


MIN_PASSWORD_LENGTH = 12


class ResetPasswordResponse(BaseModel):
    api_keys_revoked: int  # how many API keys the person made: they go with the old password


@router.post("/reset-password", response_model=ResetPasswordResponse)
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    session: Annotated[Session, Depends(_get_db)],
) -> ResetPasswordResponse:
    """A new password ends what the old one could have made: every session,
    the API keys the person made, and their other links not yet used."""
    reset_password_limiter.check(request)

    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(body.password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=400, detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes")
    token_repo = PasswordResetTokenRepository(session)
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()
    consumed = token_repo.consume_valid_and_update_password(body.token, password_hash)
    if not consumed:
        raise HTTPException(status_code=400, detail="Invalid, expired, or already-used token")
    _, api_keys_revoked = consumed
    return ResetPasswordResponse(api_keys_revoked=api_keys_revoked)


INVITE_NOT_VALID = "This link has been used or has expired. Ask whoever invited you for a new one."


@router.post("/join", response_model=LoginResponse)
def join(
    body: ResetPasswordRequest,
    request: Request,
    session: Annotated[Session, Depends(_get_db)],
) -> LoginResponse:
    """An invite's link (src/server/invites.py): the new person chooses their
    password, and is signed in."""
    from src.server.api.errors import ApiError

    reset_password_limiter.check(request)
    settings = get_settings()
    if not settings.jwt_secret:
        raise ApiError(500, "jwt_secret_missing", "JWT_SECRET isn't set in the install's env file.")
    if len(body.password) < MIN_PASSWORD_LENGTH or len(body.password.encode()) > MAX_PASSWORD_BYTES:
        raise ApiError(400, "password_length", f"Use {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_BYTES} characters.")
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()
    consumed = PasswordResetTokenRepository(session).consume_valid_and_update_password(body.token, password_hash)
    if consumed is None:
        raise ApiError(400, "invite_not_valid", INVITE_NOT_VALID)
    user, _ = consumed
    _account_live(session, user)
    UserRepository(session).update_last_login(user.user_id)
    return LoginResponse(access_token=_issue_jwt(user, settings.jwt_secret))


@router.post("/logout", status_code=204)
def logout(request: Request, session: Annotated[Session, Depends(_get_db)]) -> None:
    """Revoke the current JWT so it cannot be used or refreshed."""
    settings = get_settings()
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not settings.jwt_secret:
        return None

    try:
        claims = jwt.decode(
            auth[7:].strip(),
            settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        jti = claims.get("jti")
        if jti:
            RevokedTokenRepository(session).revoke(jti)
    except jwt.PyJWTError:
        pass  # Invalid token — nothing to revoke

    return None
