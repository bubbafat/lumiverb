"""Infer location is each library's (ADR-017, Robert 2026-10-18).

Revision ID: b5d9e3f7a1c4
Revises: a3c7e1f5b9d2
Create Date: 2026-10-18

- libraries.infer_location: whether the location producer guesses where
  this library's clips were. Off by default, as the account's setting was.
- The account's setting goes (the location producer's override,
  system_metadata producer.location): where it was on, every library is on,
  so nothing changes for anyone. The Inference window stays the account's.
- The location producer's lineage hash no longer has infer_location in it
  (its settings are now none that remake): every guess made so far gets
  the hash it has now, so nothing is redone.

Down: the account's setting is on again if any library's was, the hashes
go back, the column goes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d9e3f7a1c4"
down_revision: str | Sequence[str] | None = "a3c7e1f5b9d2"
branch_labels = None
depends_on = None

_KEY = "producer.location"


def _hash(settings: dict) -> str:
    """src/shared/producers.py settings_hash."""
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _overrides(bind) -> dict | None:
    raw = bind.execute(sa.text("SELECT value FROM system_metadata WHERE key = :k"), {"k": _KEY}).scalar()
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _set_overrides(bind, values: dict) -> None:
    if values:
        bind.execute(sa.text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": _KEY, "v": json.dumps(values, sort_keys=True)})
    else:
        bind.execute(sa.text("DELETE FROM system_metadata WHERE key = :k"), {"k": _KEY})


def _rehash(bind, settings: dict) -> None:
    bind.execute(sa.text(
        "UPDATE artifact_lineage SET settings_hash = :h"
        " WHERE artifact = 'location' AND producer = 'location' AND settings_hash <> ''"
    ), {"h": _hash(settings)})


def upgrade() -> None:
    op.add_column("libraries", sa.Column("infer_location", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))
    bind = op.get_bind()
    values = _overrides(bind)
    if values is not None and "infer_location" in values:
        if values.pop("infer_location") is True:
            bind.execute(sa.text("UPDATE libraries SET infer_location = true"))
        _set_overrides(bind, values)
    _rehash(bind, {})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM libraries WHERE infer_location)")).scalar():
        _set_overrides(bind, {**(_overrides(bind) or {}), "infer_location": True})
    _rehash(bind, {"infer_location": True})
    op.drop_column("libraries", "infer_location")
