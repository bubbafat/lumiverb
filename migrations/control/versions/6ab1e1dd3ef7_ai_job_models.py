"""Each AI job's model kept by the job's name (ADR-016 phase 4).

Revision ID: 6ab1e1dd3ef7
Revises: 3f9c1d2e7a64
Create Date: 2026-10-09

tenants.ai_job_models ({job: model}) replaces a column per job
(vision_model_id, transcript_model_id), so a producer bringing a new AI job
needs no migration: its model is kept under its name, and a job not listed
has the model its producers declare (AiJob.default_model). Every tenant's
two models are carried over as they are ("" stays off).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "6ab1e1dd3ef7"
down_revision: Union[str, Sequence[str], None] = "3f9c1d2e7a64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("ai_job_models", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.execute(sa.text(
        "UPDATE tenants SET ai_job_models = jsonb_build_object('vision', vision_model_id,"
        " 'transcripts', transcript_model_id)"))
    op.drop_column("tenants", "vision_model_id")
    op.drop_column("tenants", "transcript_model_id")


def downgrade() -> None:
    op.add_column("tenants", sa.Column("vision_model_id", sa.String(), nullable=False, server_default=""))
    op.add_column("tenants", sa.Column("transcript_model_id", sa.String(), nullable=False, server_default="small"))
    op.execute(sa.text(
        "UPDATE tenants SET vision_model_id = COALESCE(ai_job_models->>'vision', ''),"
        " transcript_model_id = COALESCE(ai_job_models->>'transcripts', 'small')"))
    op.drop_column("tenants", "ai_job_models")
