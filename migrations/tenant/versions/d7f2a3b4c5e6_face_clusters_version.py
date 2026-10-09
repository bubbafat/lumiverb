"""Face clusters: the database says when they change.

Revision ID: d7f2a3b4c5e6
Revises: c4e1d7a9b2f3
Create Date: 2026-10-09

The face cluster cache was meant to be served until something changed which
faces are clustered, but its dirty flag was the text 'false' (true in
Python), so it was never served; and trashing, restoring, archiving or
purging a clip, or trashing its library, never said the clusters changed.

Now the database keeps ``face_clusters_version`` in system_metadata: the id
of the last transaction that changed which faces are clustered, set by
triggers, so no path can forget. Those are: a clip with faces going out of
sight or back (assets.deleted_at), a face found, removed or re-embedded
(faces), a face named or un-named (face_person_matches). The cache records
the version it was computed at and is served while that's still the
version. A transaction writes the row once, however many rows it changes.
The old 'face_clusters_dirty' flag goes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d7f2a3b4c5e6"
down_revision: Union[str, Sequence[str], None] = "c4e1d7a9b2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION mark_face_clusters_changed() RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE system_metadata SET value = txid_current()::text, updated_at = now()
             WHERE key = 'face_clusters_version' AND value IS DISTINCT FROM txid_current()::text;
            IF NOT FOUND THEN
                INSERT INTO system_metadata (key, value, updated_at)
                VALUES ('face_clusters_version', txid_current()::text, now())
                ON CONFLICT (key) DO NOTHING;
            END IF;
        END $$;

        CREATE OR REPLACE FUNCTION face_clusters_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM mark_face_clusters_changed();
            RETURN NULL;
        END $$;

        CREATE OR REPLACE FUNCTION face_clusters_clip_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM faces WHERE asset_id = NEW.asset_id) THEN
                PERFORM mark_face_clusters_changed();
            END IF;
            RETURN NULL;
        END $$;

        CREATE TRIGGER face_clusters_clip_sight AFTER UPDATE OF deleted_at ON assets
            FOR EACH ROW WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
            EXECUTE FUNCTION face_clusters_clip_changed();
        CREATE TRIGGER face_clusters_faces AFTER INSERT OR DELETE
            OR UPDATE OF embedding_vector, embedding_model, asset_id ON faces
            FOR EACH ROW EXECUTE FUNCTION face_clusters_changed();
        CREATE TRIGGER face_clusters_names AFTER INSERT OR DELETE OR UPDATE OF face_id ON face_person_matches
            FOR EACH ROW EXECUTE FUNCTION face_clusters_changed();

        DELETE FROM system_metadata WHERE key = 'face_clusters_dirty';
        SELECT mark_face_clusters_changed();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS face_clusters_names ON face_person_matches;
        DROP TRIGGER IF EXISTS face_clusters_faces ON faces;
        DROP TRIGGER IF EXISTS face_clusters_clip_sight ON assets;
        DROP FUNCTION IF EXISTS face_clusters_clip_changed();
        DROP FUNCTION IF EXISTS face_clusters_changed();
        DROP FUNCTION IF EXISTS mark_face_clusters_changed();
        DELETE FROM system_metadata WHERE key = 'face_clusters_version';
        INSERT INTO system_metadata (key, value, updated_at) VALUES ('face_clusters_dirty', 'true', now())
        ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = now();
    """)
