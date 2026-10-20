"""Search reads words in any language, accents aside, and a hyphenated word by its parts (ADR-019).

Revision ID: c7e1a9d3f5b2
Revises: a3c9e5f1b7d2
Create Date: 2026-10-20

- The text search configuration `lumiverb`: English's, with accents taken off
  words that have them (`unaccent`: "Zürich" is zurich, so either spelling
  finds the other) and a hyphenated word indexed as its parts alone (English's
  also indexes the whole word, a position ahead of its parts, so a quoted
  "John Wayne Airport" missed "John Wayne Airport-Orange County").
- Every searched column is computed with it: asset_search's three,
  video_scenes.words, transcript_segments.words, plugin_outputs.words. They're
  made again from the text kept beside them, in this transaction: nothing is
  synced or made again. src/server/search/text_search.py asks with it.

What's shown is the text as it is: only what's searched loses its accents.

Down: English's again, and the configuration and unaccent go.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7e1a9d3f5b2"
down_revision: str | Sequence[str] | None = "a3c9e5f1b7d2"
branch_labels = None
depends_on = None

# The searched columns: (table, column, its tsvector over the text beside it, with {c} the configuration).
COLUMNS = (
    ("asset_search", "everything",
     "setweight(to_tsvector('{c}', described), 'A') || setweight(to_tsvector('{c}', note), 'A') || "
     "setweight(to_tsvector('{c}', ocr_text), 'C') || setweight(to_tsvector('{c}', transcript_text), 'C') || "
     "setweight(to_tsvector('{c}', path_words), 'D')"),
    ("asset_search", "shown",
     "setweight(to_tsvector('{c}', described), 'A') || setweight(to_tsvector('{c}', ocr_text), 'C') || "
     "setweight(to_tsvector('{c}', transcript_text), 'C')"),
    ("asset_search", "shown_capped",
     "setweight(to_tsvector('{c}', described), 'A') || setweight(to_tsvector('{c}', ocr_text), 'C')"),
    ("video_scenes", "words",
     "setweight(to_tsvector('{c}', coalesce(description, '')), 'A') || "
     "setweight(jsonb_to_tsvector('{c}', coalesce(tags, '[]'::jsonb), '[\"string\"]'), 'B')"),
    ("transcript_segments", "words", "to_tsvector('{c}', text)"),
    ("plugin_outputs", "words", "setweight(to_tsvector('{c}', search_text), 'B')"),
)


def _index(table: str, column: str) -> str:
    return f"ix_{table}_{column}"


def _computed_with(config: str) -> None:
    for table, column, words in COLUMNS:
        op.execute(f"DROP INDEX IF EXISTS {_index(table, column)}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        op.execute(f"ALTER TABLE {table} ADD COLUMN {column} tsvector GENERATED ALWAYS AS "
                   f"({words.format(c=config)}) STORED")
        op.execute(f"CREATE INDEX {_index(table, column)} ON {table} USING gin ({column})")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE TEXT SEARCH CONFIGURATION lumiverb (COPY = english)")
    op.execute("ALTER TEXT SEARCH CONFIGURATION lumiverb ALTER MAPPING FOR word, hword_part WITH unaccent, english_stem")
    op.execute("ALTER TEXT SEARCH CONFIGURATION lumiverb DROP MAPPING FOR asciihword, hword")
    _computed_with("lumiverb")


def downgrade() -> None:
    _computed_with("english")
    op.execute("DROP TEXT SEARCH CONFIGURATION lumiverb")
    op.execute("DROP EXTENSION IF EXISTS unaccent")
