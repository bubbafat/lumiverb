"""Add indexes for person clustering and face-person lookups.

Revision ID: g7h8i9j0k1l3
Revises: f6g7h8i9j0k1
Create Date: 2026-03-30

Adds unique constraint on face_person_matches(face_id) to enforce one person per face.
Adds index on face_person_matches(person_id) for listing faces by person.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = "g7h8i9j0k1l3"
down_revision: Union[str, None] = "f6g7h8i9j0k1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One person per face — enforced at DB level
    op.create_unique_constraint(
        "uq_face_person_matches_face_id",
        "face_person_matches",
        ["face_id"],
    )
    # Fast lookup: all faces for a person
    op.create_index(
        "ix_face_person_matches_person_id",
        "face_person_matches",
        ["person_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_face_person_matches_person_id", table_name="face_person_matches")
    op.drop_constraint("uq_face_person_matches_face_id", "face_person_matches", type_="unique")
