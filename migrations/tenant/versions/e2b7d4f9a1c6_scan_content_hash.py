"""A scan's files can carry the whole file's hash beside its fp1 (src/shared/content_hash.py).

Revision ID: e2b7d4f9a1c6
Revises: d5f9b3a7c2e1
Create Date: 2026-10-15

Files over 64 MiB are known by "fp1:" and a hash of 3 MiB of them; every
hash made before is the whole file's. Until a scan meets each such file and
swaps its hash (scans.py rekey), a file of the same size is compared with
both:

- scan_found.legacy: the plan asks for the whole file's SHA-256 too (a clip
  or file deleted for good of its size in the library has a hash made
  before); false for every scan before this;
- scan_found.legacy_sha256: what was sent, so what the scan lists as skipped
  (GET /v1/scans/{id}/skipped) finds the removal by either.

Down: drops both columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7d4f9a1c6"
down_revision: str | Sequence[str] | None = "d5f9b3a7c2e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scan_found", sa.Column("legacy", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("scan_found", sa.Column("legacy_sha256", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scan_found", "legacy_sha256")
    op.drop_column("scan_found", "legacy")
