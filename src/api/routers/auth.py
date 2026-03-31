"""Auth endpoints: login, forgot-password, reset-password, logout, refresh."""

from __future__ import annotations

import secrets
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText
from typing import Annotated

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.core.config import get_settings
from src.core.database import get_control_engine
from src.core.utils import utcnow
from src.repository.control_plane import PasswordResetTokenRepository, RevokedTokenRepository, UserRepository
from src.api.rate_limit import login_limiter, forgot_password_limiter, reset_password_limiter
from ulid import ULID

router = APIRouter(prefix="/v1/auth", tags=["auth"])

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 3600  # 1 hour
REFRESH_WINDOW_SECONDS = 7 * 24 * 3600  # 7 days — how long a token can be refreshed


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


def _issue_jwt(user_id: str, tenant_id: str, role: str, jwt_secret: str) -> str:
    """Create a signed JWT with a unique jti for revocation support."""
    now = utcnow()
    jti = str(ULID())
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + JWT_EXPIRY_SECONDS,
        # refresh_exp: the outer bound — after this, even refresh is denied.
        "refresh_exp": int(now.timestamp()) + REFRESH_WINDOW_SECONDS,
    }
    return jwt.encode(payload, jwt_secret, algorithm=JWT_ALGORITHM)


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[Session, Depends(_get_db)],
) -> LoginResponse:
    login_limiter.check(request)

    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")

    user_repo = UserRepository(session)
    user = user_repo.get_by_email(body.email)

    if user is None:
        # Dummy compare to mitigate timing attacks on unknown email.
        bcrypt.checkpw(body.password.encode(), bcrypt.hashpw(b"dummy", bcrypt.gensalt()))
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not bcrypt.checkpw(body.password.encode(), user.password_hash.encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = _issue_jwt(user.user_id, user.tenant_id, user.role, settings.jwt_secret)
    user_repo.update_last_login(user.user_id)

    return LoginResponse(access_token=token)


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

    # Check refresh window
    import time
    now = int(time.time())
    refresh_exp = claims.get("refresh_exp", 0)
    if now > refresh_exp:
        raise HTTPException(status_code=401, detail="Refresh window expired — please log in again")

    # Check revocation
    old_jti = claims.get("jti")
    if old_jti:
        revoked_repo = RevokedTokenRepository(session)
        if revoked_repo.is_revoked(old_jti):
            raise HTTPException(status_code=401, detail="Token has been revoked")
        # Revoke the old token so it can't be refreshed again
        revoked_repo.revoke(old_jti)

    new_token = _issue_jwt(claims["sub"], claims["tenant_id"], claims["role"], settings.jwt_secret)
    return RefreshResponse(access_token=new_token)


@router.post("/forgot-password", status_code=204)
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    session: Annotated[Session, Depends(_get_db)],
) -> None:
    forgot_password_limiter.check(request)

    settings = get_settings()
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
        # Return the same result for any email when reset is unavailable.
        raise HTTPException(status_code=501, detail="Password reset not configured")

    user_repo = UserRepository(session)
    user = user_repo.get_by_email(body.email)
    if user is None:
        return  # 204 always — prevents user enumeration

    plaintext_token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(hours=1)

    token_repo = PasswordResetTokenRepository(session)
    token_repo.create(user.user_id, plaintext_token, expires_at)

    reset_url = f"{settings.app_host.rstrip('/')}/reset-password?token={plaintext_token}"
    msg = MIMEText(f"Click the link to reset your password (expires in 1 hour):\n\n{reset_url}")
    msg["Subject"] = "Reset your Lumiverb password"
    msg["From"] = settings.smtp_from
    msg["To"] = user.email

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


MIN_PASSWORD_LENGTH = 12


@router.post("/reset-password", status_code=204)
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    session: Annotated[Session, Depends(_get_db)],
) -> None:
    reset_password_limiter.check(request)

    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    token_repo = PasswordResetTokenRepository(session)
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()
    ok = token_repo.consume_valid_and_update_password(body.token, password_hash)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid, expired, or already-used token")


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
