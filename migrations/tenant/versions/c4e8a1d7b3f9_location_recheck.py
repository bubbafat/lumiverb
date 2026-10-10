"""Location guesses are checked again, not made again (ADR-017 phase 3).

Revision ID: c4e8a1d7b3f9
Revises: b8d4f0a2c6e1
Create Date: 2026-10-10

asset_location.recheck: why a guess is due to be checked again (a fix it
was made from changed or went, a new fix near it, the window changed), or
NULL. The guess stays until the location producer saves what it finds now
(src/server/repository/locations.py save_guess).

The Inference window no longer goes into the location producer's lineage
hash (a change rechecks guesses instead of making them all again): the
hash of every guess made so far is what it is now, so nothing is redone.
Guesses exist only while Infer location is on, so it's that hash.

Down: the column goes; the hashes stay (they show as stale, and are redone
after approval as any settings change).
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1d7b3f9"
down_revision: Union[str, Sequence[str], None] = "b8d4f0a2c6e1"
branch_labels = None
depends_on = None

# src/shared/producers.py settings_hash of the location producer's settings now.
_HASH = hashlib.sha256(json.dumps({"infer_location": True}, sort_keys=True, separators=(",", ":")).encode()
                       ).hexdigest()[:16]


def upgrade() -> None:
    op.add_column("asset_location", sa.Column("recheck", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_asset_location_recheck", "asset_location",
        "recheck IS NULL OR recheck IN ('new_fix', 'window_changed', 'basis_gone', 'basis_changed')")
    op.execute(sa.text(
        "UPDATE artifact_lineage SET settings_hash = :h"
        " WHERE artifact = 'location' AND producer = 'location' AND settings_hash <> ''"
    ).bindparams(h=_HASH))


def downgrade() -> None:
    op.drop_constraint("ck_asset_location_recheck", "asset_location", type_="check")
    op.drop_column("asset_location", "recheck")
