"""Text read as "NONE" with why is no text: what was saved as text forgotten.

Revision ID: b7d3f9a1c5e2
Revises: e9b3d7f1a5c8
Create Date: 2026-10-30

The OCR prompt says "If none, say NONE." Claude Haiku 5.5 says why too ("NONE. I don't see any legible text in this
image. It shows a beach sunset…"), and only a reply that was NONE alone counted as none, so the explanation was saved
as the image's text and searched. The caption client's _says_none counts it as none now; this does the same for what
was saved, with its own copy of the rule (a migration never changes), and marks those clips' search for the sweep to
write again. Going back down leaves them as they are now: no text.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3f9a1c5e2"
down_revision: str | Sequence[str] | None = "e9b3d7f1a5c8"
branch_labels = None
depends_on = None

# openai_caption.py _NONE on Oct 30, as PostgreSQL's case-insensitive regular expression.
NONE = r"^\s*<?none>?[ \t]*([.,:;!\-\u2013\u2014(\n]|$)"


def upgrade() -> None:
    op.execute(sa.text(
        "WITH said_none AS ("
        " UPDATE asset_ocr SET text = '', has_text = false WHERE has_text AND text ~* :none RETURNING asset_id)"
        " UPDATE assets SET search_synced_at = NULL WHERE asset_id IN (SELECT asset_id FROM said_none)"
    ).bindparams(none=NONE))


def downgrade() -> None:
    pass
