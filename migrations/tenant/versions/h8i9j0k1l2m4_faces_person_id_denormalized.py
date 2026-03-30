"""Add denormalized faces.person_id for person–face assignments.

Revision ID: h8i9j0k1l2m4
Revises: g7h8i9j0k1l3
Create Date: 2026-03-30

Nullable FK to people.person_id, kept in sync with face_person_matches by the API.
Backfills from face_person_matches for existing rows.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m4"
down_revision: Union[str, Sequence[str], None] = "g7h8i9j0k1l3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "faces",
        sa.Column(
            "person_id",
            sa.String(),
            sa.ForeignKey("people.person_id"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE faces AS f SET person_id = m.person_id "
            "FROM face_person_matches AS m WHERE m.face_id = f.face_id"
        )
    )
    op.create_index("ix_faces_person_id", "faces", ["person_id"])


def downgrade() -> None:
    op.drop_index("ix_faces_person_id", table_name="faces")
    op.drop_column("faces", "person_id")
