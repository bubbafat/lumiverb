"""An https AI machine can be added with its certificate's fingerprint.

Revision ID: c5e9a3f7b1d4
Revises: b3f7d1a9c5e8
Create Date: 2026-10-22

ai_machines.fingerprint: the SHA-256 of the certificate an https machine was
added with (Lumiverb's AI worker makes its own), as `openssl x509
-fingerprint -sha256` shows it: its requests take that certificate and no
other (src/shared/ai_requests.py). "", every machine's after this, is none:
an http machine stays as it is, and an https one is checked as any https site
is, as before. A fingerprint is only ever an https machine's, in that one form.

Down: the column goes, and every https machine is checked as any https site
is again (an AI worker's own certificate then isn't taken).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e9a3f7b1d4"
down_revision: str | Sequence[str] | None = "b3f7d1a9c5e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_machines", sa.Column("fingerprint", sa.String(), nullable=False, server_default=""))
    op.create_check_constraint(
        "ai_machines_fingerprint", "ai_machines",
        "fingerprint = '' OR (fingerprint ~ '^([0-9A-F]{2}:){31}[0-9A-F]{2}$' AND api_url LIKE 'https://%')")


def downgrade() -> None:
    op.drop_constraint("ai_machines_fingerprint", "ai_machines", type_="check")
    op.drop_column("ai_machines", "fingerprint")
