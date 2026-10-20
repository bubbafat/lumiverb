"""An email address is one person's, whatever its case.

Revision ID: b5d9e3a7f1c4
Revises: d7f3b9e1c5a2
Create Date: 2026-10-20

Users were unique by their address as typed, so `Ana@example.com` and
`ana@example.com` could be two people, and signing in as one with the
other's case failed. Sign-in, invites, a forgotten password and naming a
server admin now find an address whatever its case
(repository/control_plane.py `UserRepository.get_by_email`), and this index
keeps any two from differing only in case.

An install that already has two such users keeps them, without the index
(said in the log; the first made is the one found), as dropping either would
lose someone's account: the code refuses a new one either way.

Down: the index goes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d9e3a7f1c4"
down_revision: str | Sequence[str] | None = "d7f3b9e1c5a2"
branch_labels = None
depends_on = None

INDEX = "users_email_any_case"

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    shared = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM (SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*) > 1) d"
    )).scalar_one()
    if shared:
        logger.warning("%d email address(es) belong to more than one user, differing only in case: they're kept, "
                       "the first made is the one who signs in with it, and %s isn't made", shared, INDEX)
        return
    op.execute(f"CREATE UNIQUE INDEX {INDEX} ON users (lower(email))")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
