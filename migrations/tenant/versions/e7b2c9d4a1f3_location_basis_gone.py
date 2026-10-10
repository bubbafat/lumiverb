"""Location guesses from fixes already out of sight are checked again (ADR-017 phase 3).

Revision ID: e7b2c9d4a1f3
Revises: c4e8a1d7b3f9
Create Date: 2026-10-10

asset_location.recheck takes 'outside_window' too: a guess made from a fix
that a narrower Inference window leaves outside, kept (marked) when nothing
else is found.

A guess made before rechecks from a fix that's since been trashed,
archived, gone missing or deleted for good is due to be checked again as
basis_gone, as if the fix had gone now: a new guess replaces it, or it's
kept, marked "source removed". Not one already marked so, and not one a
stronger reason (basis_changed) already waits on: running it again
changes nothing.

Down: 'outside_window' rechecks become 'basis_changed' and the constraint
goes back; the basis_gone marks stay (they were due anyway).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b2c9d4a1f3"
down_revision: Union[str, Sequence[str], None] = "c4e8a1d7b3f9"
branch_labels = None
depends_on = None

MARK_GONE = (
    "UPDATE asset_location loc SET recheck = 'basis_gone'"
    " WHERE loc.source = 'time'"
    "   AND (loc.recheck IS NULL OR loc.recheck IN ('new_fix', 'window_changed', 'outside_window'))"
    "   AND loc.basis -> 'basis_gone' IS NULL"
    "   AND jsonb_typeof(loc.basis -> 'fixes') = 'array'"
    "   AND EXISTS (SELECT 1 FROM jsonb_array_elements(loc.basis -> 'fixes') fx"
    "     WHERE NOT EXISTS (SELECT 1 FROM assets f WHERE f.asset_id = fx ->> 'asset_id' AND f.deleted_at IS NULL))"
)


def upgrade() -> None:
    op.drop_constraint("ck_asset_location_recheck", "asset_location", type_="check")
    op.create_check_constraint(
        "ck_asset_location_recheck", "asset_location",
        "recheck IS NULL OR recheck IN ('new_fix', 'window_changed', 'outside_window', 'basis_gone', 'basis_changed')")
    op.execute(sa.text(MARK_GONE))


def downgrade() -> None:
    op.execute(sa.text("UPDATE asset_location SET recheck = 'basis_changed' WHERE recheck = 'outside_window'"))
    op.drop_constraint("ck_asset_location_recheck", "asset_location", type_="check")
    op.create_check_constraint(
        "ck_asset_location_recheck", "asset_location",
        "recheck IS NULL OR recheck IN ('new_fix', 'window_changed', 'basis_gone', 'basis_changed')")
