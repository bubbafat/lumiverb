"""artifact_lineage: how each clip's artifacts were made (ADR-016 phase 3).

Revision ID: d9f3a6b1c2e8
Revises: c4d8e1f2a7b6
Create Date: 2026-10-08

One row per clip and artifact kind: producer, producer version, a hash of
the settings that change its output, and the source file's SHA-256 it was
made from. When any differs from what's registered now, the artifact is
stale. Failures are kept here too (outcome "failed", attempts, retry_at).

Backfill, from what the artifacts' own columns already say (Robert's call,
Oct 8: nothing re-runs just because lineage arrived):
- made the way today's CLI makes them: current, as version 1 with the v1
  settings (frozen below: this migration never changes with the registry);
- made by the macOS app (Apple Vision faces, FeaturePrint, the
  "openai-compatible" vision rows, whisper.cpp): an unknown producer, so stale;
- a transcript a person wrote or pasted: a person's, current.
The vision model the CLI used most becomes the account's vision model.
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9f3a6b1c2e8"
down_revision: Union[str, Sequence[str], None] = "c4d8e1f2a7b6"
branch_labels = None
depends_on = None

# The v1 settings hashes (src/shared/producers.py on Oct 8), frozen.
V1 = {
    "probe": ("ffprobe", "44136fa355b3678a"),
    "proxy": ("proxy", "8baa5eb52e86ea80"),
    "video_preview": ("preview", "4f627804650c7f23"),
    "analysis_proxy": ("analysis-proxy", "b63de287be1aa99f"),
    "scenes": ("scene-detect", "27e5da9cd68d56d9"),
    "clip": ("clip", "c34fd145303f354e"),
    "faces": ("insightface", "8a9e204f61973fa2"),
    "transcript": ("whisper", "82c28198e6c0a481"),
}
_VISION_PROMPT = (
    "Describe this image in 2-3 sentences, being specific about "
    "the subject, setting, and mood. Then provide 5-10 descriptive "
    "tags. Respond only with valid JSON in this exact format:\n"
    '{"description": "...", "tags": ["tag1", "tag2", ...]}'
)
_OCR_PROMPT = (
    "What text is visible in this image? "
    "Include text from signs, labels, products, screens, documents, or watermarks. "
    "If none, say NONE."
)


def _hash(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _vision_hash(model: str, prompt: str = _VISION_PROMPT) -> str:
    return _hash({"model": model, "prompt": prompt, "max_edge": 1280, "temperature": 0.2, "max_tokens": 500})


def upgrade() -> None:
    op.create_table(
        "artifact_lineage",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("artifact", sa.String(), primary_key=True),
        sa.Column("producer", sa.String(), nullable=False),
        sa.Column("producer_version", sa.String(), nullable=False),
        sa.Column("settings_hash", sa.String(), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=True),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("outcome", sa.String(), nullable=False, server_default="ok"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_artifact_lineage_artifact", "artifact_lineage", ["artifact", "outcome"])

    conn = op.get_bind()

    def put(artifact: str, select_sql: str, params: dict | None = None) -> None:
        """Insert lineage rows from SELECT asset_id, producer, version, hash, source, produced_at, outcome."""
        conn.execute(sa.text(
            "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
            " source_sha256, produced_at, outcome) "
            f"SELECT s.asset_id, '{artifact}', s.producer, s.version, s.hash, s.source, s.produced_at, s.outcome"
            f" FROM ({select_sql}) s ON CONFLICT DO NOTHING"
        ), params or {})

    def current(artifact: str, where: str, produced_at: str = "now()", outcome: str = "'ok'") -> None:
        producer, h = V1[artifact]
        put(artifact, f"SELECT a.asset_id, '{producer}' AS producer, '1' AS version, '{h}' AS hash,"
                      f" a.sha256 AS source, COALESCE({produced_at}, now()) AS produced_at, {outcome} AS outcome"
                      f" FROM assets a WHERE {where}")

    current("probe", "EXISTS (SELECT 1 FROM video_facets f WHERE f.asset_id = a.asset_id)",
            "(SELECT f.probed_at FROM video_facets f WHERE f.asset_id = a.asset_id)")
    current("proxy", "a.proxy_key IS NOT NULL")
    current("video_preview", "a.video_preview_key IS NOT NULL", "a.video_preview_generated_at")
    current("analysis_proxy", "a.analysis_proxy_key IS NOT NULL", "a.analysis_proxy_generated_at")
    current("scenes", "a.video_indexed")

    # Faces: Apple Vision's are another producer's; none found is still a result.
    apple = "EXISTS (SELECT 1 FROM faces f WHERE f.asset_id = a.asset_id AND f.detection_model <> 'insightface')"
    current("faces", f"a.face_count IS NOT NULL AND NOT {apple}",
            outcome="CASE WHEN a.face_count = 0 THEN 'empty' ELSE 'ok' END")
    put("faces", "SELECT a.asset_id, 'unknown' AS producer, '' AS version, '' AS hash, a.sha256 AS source,"
                 f" now() AS produced_at, 'ok' AS outcome FROM assets a WHERE a.face_count IS NOT NULL AND {apple}")

    # CLIP: the CLI's open_clip key (the macOS CoreML CLIP shares it); FeaturePrint is another producer's.
    current("clip", "EXISTS (SELECT 1 FROM asset_embeddings e WHERE e.asset_id = a.asset_id"
                    " AND e.model_id = 'clip' AND e.model_version = 'ViT-B-32-openai')")
    put("clip", "SELECT a.asset_id, 'unknown' AS producer, '' AS version, '' AS hash, a.sha256 AS source,"
                " now() AS produced_at, 'ok' AS outcome FROM assets a"
                " WHERE EXISTS (SELECT 1 FROM asset_embeddings e WHERE e.asset_id = a.asset_id)")

    # Transcripts: a person's; Whisper (the CLI, default "small"); anything else another producer's.
    current("transcript", "a.has_transcript IS NOT NULL AND a.transcript_source = 'whisper'", "a.transcribed_at",
            outcome="CASE WHEN a.has_transcript THEN 'ok' ELSE 'empty' END")
    put("transcript", "SELECT a.asset_id, 'person' AS producer, '' AS version, '' AS hash, a.sha256 AS source,"
                      " COALESCE(a.transcribed_at, now()) AS produced_at, 'ok' AS outcome FROM assets a"
                      " WHERE a.transcript_source = 'manual'")
    put("transcript", "SELECT a.asset_id, 'unknown' AS producer, '' AS version, '' AS hash, a.sha256 AS source,"
                      " COALESCE(a.transcribed_at, now()) AS produced_at,"
                      " CASE WHEN a.has_transcript THEN 'ok' ELSE 'empty' END AS outcome FROM assets a"
                      " WHERE a.has_transcript IS NOT NULL")

    # Vision and OCR: the CLI writes (model, "1"); the macOS app ("openai-compatible", model).
    latest = ("SELECT DISTINCT ON (m.asset_id) m.asset_id, m.model_id, m.model_version, m.generated_at, m.data"
              " FROM asset_metadata m ORDER BY m.asset_id, m.generated_at DESC")
    models = [r[0] for r in conn.execute(sa.text(
        f"SELECT l.model_id FROM ({latest}) l WHERE l.model_version = '1' GROUP BY l.model_id ORDER BY count(*) DESC"
    ))]
    for model in models:
        for artifact, producer, prompt, extra in (
            ("vision", "vision", _VISION_PROMPT, ""),
            ("ocr", "ocr", _OCR_PROMPT, " AND l.data ? 'has_text'"),
        ):
            put(artifact, "SELECT l.asset_id, :producer AS producer, '1' AS version, :hash AS hash,"
                          " a.sha256 AS source, l.generated_at AS produced_at,"
                          " CASE WHEN :artifact = 'ocr' AND NOT COALESCE((l.data->>'has_text')::boolean, false)"
                          "   THEN 'empty' ELSE 'ok' END AS outcome"
                          f" FROM ({latest}) l JOIN assets a ON a.asset_id = l.asset_id"
                          f" WHERE l.model_version = '1' AND l.model_id = :model{extra}",
                {"producer": producer, "hash": _vision_hash(model, prompt), "model": model, "artifact": artifact})
    for artifact, extra in (("vision", ""), ("ocr", " AND l.data ? 'has_text'")):
        put(artifact, "SELECT l.asset_id, 'unknown' AS producer, '' AS version, '' AS hash, a.sha256 AS source,"
                      " l.generated_at AS produced_at, 'ok' AS outcome"
                      f" FROM ({latest}) l JOIN assets a ON a.asset_id = l.asset_id WHERE true{extra}")
    if models:
        # The model the CLI used most is the account's: what vision is current against.
        conn.execute(sa.text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES ('vision_model', :m, now())"
            " ON CONFLICT (key) DO NOTHING"
        ), {"m": models[0]})
        # Scene descriptions came from the same model; which one isn't recorded.
        put("scene_vision", "SELECT a.asset_id, 'scene-vision' AS producer, '1' AS version, :hash AS hash,"
                            " a.sha256 AS source, now() AS produced_at, 'ok' AS outcome FROM assets a"
                            " WHERE a.video_indexed AND EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id)"
                            "   AND NOT EXISTS (SELECT 1 FROM video_scenes s WHERE s.asset_id = a.asset_id"
                            "                   AND s.description IS NULL)",
            {"hash": _vision_hash(models[0])})


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM system_metadata WHERE key = 'vision_model'"))
    op.drop_index("ix_artifact_lineage_artifact", table_name="artifact_lineage")
    op.drop_table("artifact_lineage")
