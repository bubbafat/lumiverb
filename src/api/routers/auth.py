"""Auth endpoints: login, forgot-password, reset-password, logout."""

from __future__ import annotations

import secrets
import smtplib
from datetime import timedelta
from email.mime.text import MIMEText
from typing import Annotated

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.core.config import get_settings
from src.core.database import get_control_engine
from src.core.utils import utcnow
from src.repository.control_plane import PasswordResetTokenRepository, UserRepository

router = APIRouter(prefix="/v1/auth", tags=["auth"])

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days


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


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, session: Annotated[Session, Depends(_get_db)]) -> LoginResponse:
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

    now = utcnow()
    payload = {
        "sub": user.user_id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "exp": int(now.timestamp()) + JWT_EXPIRY_SECONDS,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)

    user_repo.update_last_login(user.user_id)

    return LoginResponse(access_token=token)


@router.post("/forgot-password", status_code=204)
def forgot_password(body: ForgotPasswordRequest, session: Annotated[Session, Depends(_get_db)]) -> None:
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
def reset_password(body: ResetPasswordRequest, session: Annotated[Session, Depends(_get_db)]) -> None:
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    token_repo = PasswordResetTokenRepository(session)
    password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()
    ok = token_repo.consume_valid_and_update_password(body.token, password_hash)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid, expired, or already-used token")


@router.post("/logout", status_code=204)
def logout() -> None:
    """Stateless JWT logout — no server-side action needed.

    The client clears the token from localStorage. This endpoint exists so the
    web UI has a deterministic sign-out call and for future token revocation.
    """
    return None
