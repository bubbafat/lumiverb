"""A person's corrections, beside the machine's values (ADR-016 phase 3, piece 4).

A corrected description or OCR wins; the machine's goes on being made
underneath and comes back when the correction is removed. Tag edits are
adds and removes on top of the machine's list, so a new list from the
machine keeps them. Everything that shows, searches or filters by these
values uses what the person sees: the detail, search documents, the
Postgres search, tag filters and facets.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.shared.utils import utcnow

def tags_join(alias: str = "m") -> str:
    """The tags a clip shows, as `{alias}.tags` (a jsonb array): its latest
    description's tags, less the ones a person removed, plus the ones they
    added. Joined after `FROM active_assets a` wherever tags are filtered,
    counted or searched."""
    return f"""
            LEFT JOIN LATERAL (
                SELECT COALESCE(jsonb_agg(DISTINCT s.t), '[]'::jsonb) AS tags
                FROM (
                    SELECT jsonb_array_elements_text(COALESCE(md.data->'tags', '[]'::jsonb)) AS t
                    FROM (SELECT data FROM asset_metadata WHERE asset_id = a.asset_id
                          ORDER BY generated_at DESC LIMIT 1) md
                    UNION
                    SELECT jsonb_array_elements_text(ca.tags_added)
                    FROM asset_corrections ca WHERE ca.asset_id = a.asset_id
                ) s
                WHERE NOT EXISTS (SELECT 1 FROM asset_corrections cr
                                  WHERE cr.asset_id = a.asset_id AND cr.tags_removed ? s.t)
            ) {alias} ON TRUE
            """


TAGS_JOIN = tags_join("m")


def shown_tags(machine: list[str], added: list[str], removed: list[str]) -> list[str]:
    """The tags a clip shows: the machine's, less those removed, plus those added."""
    out = [t for t in machine if t not in set(removed)]
    out += [t for t in added if t not in set(out)]
    return out


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for t in tags:
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


class CorrectionsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, asset_id: str) -> dict[str, Any] | None:
        row = self._session.execute(text(
            "SELECT description, ocr_text, tags_added, tags_removed, updated_by, updated_at"
            " FROM asset_corrections WHERE asset_id = :a"
        ), {"a": asset_id}).mappings().first()
        return dict(row) if row else None

    def update(self, asset_id: str, fields: dict[str, Any], machine_tags: list[str],
               updated_by: str | None) -> None:
        """Apply the fields given: `description` / `ocr_text` (a string sets
        the correction, None removes it), `tags` (the list the person wants
        shown, kept as adds and removes against the machine's; None removes
        the tag edits). Fields not given stay as they are."""
        current = self.get(asset_id) or {"description": None, "ocr_text": None,
                                          "tags_added": [], "tags_removed": []}
        description = current["description"]
        ocr_text = current["ocr_text"]
        added, removed = list(current["tags_added"]), list(current["tags_removed"])
        if "description" in fields:
            description = fields["description"].strip() if fields["description"] is not None else None
        if "ocr_text" in fields:
            ocr_text = fields["ocr_text"].strip() if fields["ocr_text"] is not None else None
        if "tags" in fields:
            if fields["tags"] is None:
                added, removed = [], []
            else:
                wanted = _clean_tags(fields["tags"])
                added = [t for t in wanted if t not in machine_tags]
                removed = [t for t in machine_tags if t not in wanted]
        if description is None and ocr_text is None and not added and not removed:
            self._session.execute(text("DELETE FROM asset_corrections WHERE asset_id = :a"), {"a": asset_id})
        else:
            self._session.execute(text(
                "INSERT INTO asset_corrections (asset_id, description, ocr_text, tags_added, tags_removed,"
                "   updated_by, updated_at)"
                " VALUES (:a, :d, :o, CAST(:ta AS jsonb), CAST(:tr AS jsonb), :by, :now)"
                " ON CONFLICT (asset_id) DO UPDATE SET description = EXCLUDED.description,"
                "   ocr_text = EXCLUDED.ocr_text, tags_added = EXCLUDED.tags_added,"
                "   tags_removed = EXCLUDED.tags_removed, updated_by = EXCLUDED.updated_by,"
                "   updated_at = EXCLUDED.updated_at"
            ), {"a": asset_id, "d": description, "o": ocr_text, "ta": json.dumps(added), "tr": json.dumps(removed),
                "by": updated_by, "now": utcnow()})
        self._session.commit()


def apply(corrections: dict[str, Any] | None, description: str | None, tags: list[str],
          ocr_text: str | None) -> tuple[str | None, list[str], str | None, list[str]]:
    """(description, tags, ocr_text) as a person sees them, and which they corrected."""
    if not corrections:
        return description, tags, ocr_text, []
    corrected = []
    if corrections["description"] is not None:
        description = corrections["description"]
        corrected.append("description")
    if corrections["tags_added"] or corrections["tags_removed"]:
        tags = shown_tags(tags, corrections["tags_added"], corrections["tags_removed"])
        corrected.append("tags")
    if corrections["ocr_text"] is not None:
        ocr_text = corrections["ocr_text"]
        corrected.append("ocr_text")
    return description, tags, ocr_text, corrected
