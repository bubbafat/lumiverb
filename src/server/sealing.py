"""Secrets Lumiverb keeps for other services, sealed in its databases
(Robert, 2026-10-20: "Go for it", after how others do it): an AI machine's
key, and a plug-in's secret settings. Sealed with the install's own key,
LUMIVERB_SECRETS_KEY in its env file (made by init, or added by a Mac's run
and a Linux update when it isn't there), so a dump or a backup alone reads
none of them. Someone with the whole machine has the key too: this keeps
secrets out of what's copied off it, not away from its owner.

A sealed value is `lv1:<key id>:<Fernet token>` (AES-128-CBC with an
HMAC-SHA256, from `cryptography`): the key id is the first 8 hex digits of
the key's SHA-256, so a value sealed with another key (a backup restored on
another machine without its env file) is known as such: opened says None,
and the page asks for it again rather than failing. A value without the
prefix was stored before sealing: read as it is until upkeep seals it
(each repository's seal_stored). Named sealing, not secrets: Python's own secrets module is that.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "lv1:"
ENV_NAME = "LUMIVERB_SECRETS_KEY"


class SecretsKeyMissingError(RuntimeError):
    """The install has no LUMIVERB_SECRETS_KEY: nothing can be sealed."""

    def __init__(self) -> None:
        super().__init__(f"This install has no {ENV_NAME} in its env file: a Mac's service adds one when it starts, "
                         "a Linux update too (scripts/update.sh)")


def new_key() -> str:
    """A key for an install's env file."""
    return Fernet.generate_key().decode()


def _key() -> str:
    from src.server.config import get_settings

    return get_settings().lumiverb_secrets_key


def _key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def is_sealed(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(PREFIX)


def seal(plain: str) -> str:
    """The value to store: "" stays "" (none), one sealed already stays as it is. SecretsKeyMissingError when
    the install has no key."""
    if not plain or is_sealed(plain):
        return plain
    key = _key()
    if not key:
        raise SecretsKeyMissingError
    return f"{PREFIX}{_key_id(key)}:{Fernet(key.encode()).encrypt(plain.encode()).decode()}"


def opened(stored: str | None) -> str | None:
    """What a stored value is: "" for none, the value itself for one stored before sealing, the secret for one
    sealed with this install's key; None for one this install can't read (sealed with another key, or its key
    missing): it has to be given again."""
    if not stored:
        return ""
    if not is_sealed(stored):
        return stored
    key = _key()
    _, kid, token = stored.split(":", 2) if stored.count(":") >= 2 else ("", "", "")
    if not key or kid != _key_id(key):
        return None
    try:
        return Fernet(key.encode()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
