"""A clip's last modified time (assets.changed_at): the most recent of every time related to it.

Revision ID: d9a3f5c1e7b4
Revises: c7e1a9d3f5b2
Create Date: 2026-10-20

Its file changing (file_mtime), anything Lumiverb made for it (each producer's lineage, made, not a
failure), and what a person changed: their description, tags or text (asset_corrections), a note or a
transcript, a location (asset_location), a face named (faces.person_id, face_person_matches), a plug-in's
facts (plugin_outputs). Not ratings: each is one person's. The database keeps it, with triggers on each
of those, so it's always that most recent time, and the grid sorts by it with an index.

Filled in here from what's there now; a new clip's is when it's added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9a3f5c1e7b4"
down_revision: str | Sequence[str] | None = "c7e1a9d3f5b2"
branch_labels = None
depends_on = None

# active_assets is SELECT * FROM assets, expanded when the view was made: rebuilt around the column change.
_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"

# What each related row's change makes the clip's last modified time.
_FUNCTIONS = """
CREATE FUNCTION changed_now(asset text, at timestamptz) RETURNS void LANGUAGE sql AS $$
    UPDATE assets SET changed_at = GREATEST(changed_at, at) WHERE asset_id = asset
        AND changed_at IS DISTINCT FROM GREATEST(changed_at, at)
$$;

CREATE FUNCTION changed_by_row() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM changed_now(CASE WHEN TG_OP = 'DELETE' THEN OLD.asset_id ELSE NEW.asset_id END, now());
    RETURN NULL;
END $$;

CREATE FUNCTION changed_by_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM changed_now(NEW.asset_id, NEW.produced_at);
    RETURN NULL;
END $$;

CREATE FUNCTION changed_by_match() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM changed_now(f.asset_id, now()) FROM faces f
        WHERE f.face_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.face_id ELSE NEW.face_id END;
    RETURN NULL;
END $$;

CREATE FUNCTION changed_by_itself() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.file_mtime IS DISTINCT FROM OLD.file_mtime THEN
        NEW.changed_at := GREATEST(NEW.changed_at, NEW.file_mtime);
    END IF;
    IF NEW.note IS DISTINCT FROM OLD.note OR NEW.transcript_srt IS DISTINCT FROM OLD.transcript_srt THEN
        NEW.changed_at := GREATEST(NEW.changed_at, now());
    END IF;
    RETURN NEW;
END $$;
"""

_TRIGGERS = """
CREATE TRIGGER changed_by_itself BEFORE UPDATE OF file_mtime, note, transcript_srt ON assets
    FOR EACH ROW EXECUTE FUNCTION changed_by_itself();
CREATE TRIGGER changed_by_lineage AFTER INSERT OR UPDATE OF produced_at ON artifact_lineage
    FOR EACH ROW WHEN (NEW.outcome IS DISTINCT FROM 'failed') EXECUTE FUNCTION changed_by_lineage();
CREATE TRIGGER changed_by_corrections AFTER INSERT OR UPDATE OR DELETE ON asset_corrections
    FOR EACH ROW EXECUTE FUNCTION changed_by_row();
CREATE TRIGGER changed_by_location AFTER INSERT OR UPDATE OR DELETE ON asset_location
    FOR EACH ROW EXECUTE FUNCTION changed_by_row();
CREATE TRIGGER changed_by_facts AFTER INSERT OR UPDATE OF outputs OR DELETE ON plugin_outputs
    FOR EACH ROW EXECUTE FUNCTION changed_by_row();
CREATE TRIGGER changed_by_face_named AFTER UPDATE OF person_id ON faces
    FOR EACH ROW EXECUTE FUNCTION changed_by_row();
CREATE TRIGGER changed_by_face_match AFTER INSERT OR UPDATE OF confirmed OR DELETE ON face_person_matches
    FOR EACH ROW EXECUTE FUNCTION changed_by_match();
"""

_DROP = """
DROP TRIGGER IF EXISTS changed_by_face_match ON face_person_matches;
DROP TRIGGER IF EXISTS changed_by_face_named ON faces;
DROP TRIGGER IF EXISTS changed_by_facts ON plugin_outputs;
DROP TRIGGER IF EXISTS changed_by_location ON asset_location;
DROP TRIGGER IF EXISTS changed_by_corrections ON asset_corrections;
DROP TRIGGER IF EXISTS changed_by_lineage ON artifact_lineage;
DROP TRIGGER IF EXISTS changed_by_itself ON assets;
DROP FUNCTION IF EXISTS changed_by_itself();
DROP FUNCTION IF EXISTS changed_by_match();
DROP FUNCTION IF EXISTS changed_by_lineage();
DROP FUNCTION IF EXISTS changed_by_row();
DROP FUNCTION IF EXISTS changed_now(text, timestamptz);
"""

# Every clip's from what's there now: the most recent of its related times (GREATEST leaves out NULLs).
_FILL = """
UPDATE assets a SET changed_at = COALESCE(GREATEST(
    a.created_at, a.file_mtime, a.note_updated_at, a.transcribed_at,
    (SELECT max(l.produced_at) FROM artifact_lineage l
      WHERE l.asset_id = a.asset_id AND l.outcome IS DISTINCT FROM 'failed'),
    (SELECT c.updated_at FROM asset_corrections c WHERE c.asset_id = a.asset_id),
    (SELECT x.set_at FROM asset_location x WHERE x.asset_id = a.asset_id),
    (SELECT max(o.made_at) FROM plugin_outputs o WHERE o.asset_id = a.asset_id),
    (SELECT max(GREATEST(m.created_at, m.confirmed_at)) FROM face_person_matches m
       JOIN faces f ON f.face_id = m.face_id WHERE f.asset_id = a.asset_id)
), now())
"""


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text(_FILL))
    op.alter_column("assets", "changed_at", nullable=False, server_default=sa.text("now()"))
    op.create_index("ix_assets_changed_at", "assets", ["changed_at", "asset_id"])
    op.execute(sa.text(_VIEW_DDL))
    op.execute(sa.text(_FUNCTIONS))
    op.execute(sa.text(_TRIGGERS))


def downgrade() -> None:
    op.execute(sa.text(_DROP))
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_index("ix_assets_changed_at", table_name="assets")
    op.drop_column("assets", "changed_at")
    op.execute(sa.text(_VIEW_DDL))
