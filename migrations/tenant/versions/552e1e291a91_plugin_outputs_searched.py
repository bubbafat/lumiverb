"""A plug-in's facts are searched (ADR-023 phase 2c).

Revision ID: 552e1e291a91
Revises: 87cedeb63a82
Create Date: 2026-10-18

- plugin_outputs.search_text: the text of its facts its manifest says are
  searchable (a clip's, from that plug-in), and words, its words, computed
  with the row and indexed: a search finds a clip by a fact of a plug-in
  that's on in the account (src/server/search/text_search.py), weighed below
  a description and above the text read in a picture. Rows made before keep
  '' until made again.

Down: they go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "552e1e291a91"
down_revision: str | Sequence[str] | None = "87cedeb63a82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plugin_outputs", sa.Column("search_text", sa.Text(), nullable=False, server_default=""))
    op.execute("ALTER TABLE plugin_outputs ADD COLUMN words tsvector GENERATED ALWAYS AS"
               " (setweight(to_tsvector('english', search_text), 'B')) STORED")
    op.execute("CREATE INDEX ix_plugin_outputs_words ON plugin_outputs USING gin (words)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_plugin_outputs_words")
    op.drop_column("plugin_outputs", "words")
    op.drop_column("plugin_outputs", "search_text")
