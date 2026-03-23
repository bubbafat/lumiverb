"""Fast tests for Phase 2 auth hardening fixes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routers import auth
from src.core.config import get_settings


def _fake_db():
    yield None


@pytest.mark.fast
def test_forgot_password_returns_same_501_for_any_email_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing SMTP config returns the same 501 regardless of user email."""
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SMTP_FROM", "")
    monkeypatch.setenv("APP_HOST", "")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    get_settings.cache_clear()

    app.dependency_overrides[auth._get_db] = _fake_db
    try:
        with TestClient(app) as client:
            r1 = client.post("/v1/auth/forgot-password", json={"email": "known@example.com"})
            r2 = client.post("/v1/auth/forgot-password", json={"email": "unknown@example.com"})
        assert r1.status_code == 501
        assert r2.status_code == 501
        assert r1.json() == r2.json()
    finally:
        app.dependency_overrides.pop(auth._get_db, None)
        get_settings.cache_clear()


@pytest.mark.fast
def test_reset_password_returns_400_when_atomic_consume_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset-password maps failed atomic consume to 400."""
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    get_settings.cache_clear()

    def _consume_false(self, plaintext_token: str, password_hash: str) -> bool:
        assert plaintext_token == "bad-token"
        assert password_hash
        return False

    monkeypatch.setattr(auth.PasswordResetTokenRepository, "consume_valid_and_update_password", _consume_false)

    app.dependency_overrides[auth._get_db] = _fake_db
    try:
        with TestClient(app) as client:
            r = client.post("/v1/auth/reset-password", json={"token": "bad-token", "password": "some-password-long-enough"})
        assert r.status_code == 400
    finally:
        app.dependency_overrides.pop(auth._get_db, None)

