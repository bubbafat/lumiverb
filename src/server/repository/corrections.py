"""A person's description, tags and OCR text (ADR-016; Robert, Oct 9: edits
keep only the latest).

What a person writes is the one copy. The machine's value of that field is
dropped when they write it, and machine output never replaces it (the
description and OCR stores leave out what a person wrote: without_persons).
A person's tag edit fixes the whole list: the machine no longer changes it.
Removing what a person wrote leaves none, and the machine makes it again
(the clip's description or OCR is missing again).

Everything that shows, searches or filters by these values uses what the
person sees: the detail, search documents, the Postgres search, tag filters
and facets.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.shared.utils import utcnow

# The fields a person can write, and the machine's artifact each belongs to.
FIELDS: dict[str, str] = {"description": "vision", "tags": "vision", "ocr_text": "ocr"}


def tags_join(alias: str = "m") -> str:
    """The tags a clip shows, as `{alias}.tags` (a jsonb array): a person's
    list when they wrote one, else its latest description's. Joined after
    `FROM active_assets a` wherever tags are filtered, counted or searched.
    Tags that aren't a list count as none."""
    md, c = f"{alias}_md", f"{alias}_c"
    machine = f"CASE WHEN jsonb_typeof({md}.tags) = 'array' THEN {md}.tags ELSE '[]'::jsonb END"
    return f"""
            LEFT JOIN LATERAL (
                SELECT data->'tags' AS tags FROM asset_metadata WHERE asset_id = a.asset_id
                ORDER BY generated_at DESC LIMIT 1
            ) {md} ON TRUE
            LEFT JOIN asset_corrections {c} ON {c}.asset_id = a.asset_id
            LEFT JOIN LATERAL (
                SELECT CASE WHEN jsonb_typeof({c}.tags) = 'array' THEN {c}.tags ELSE {machine} END AS tags
            ) {alias} ON TRUE
            """


TAGS_JOIN = tags_join("m")


def machine_tags(data: dict | None) -> list[str]:
    """The tags in a description's data; anything but a list counts as none."""
    tags = (data or {}).get("tags")
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for t in tags:
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def persons_fields(session: Session, asset_id: str) -> set[str]:
    """Which of description, tags and ocr_text a person wrote for the clip."""
    row = session.execute(text(
        "SELECT description, ocr_text, tags FROM asset_corrections WHERE asset_id = :a"
    ), {"a": asset_id}).mappings().first()
    return {f for f in FIELDS if row and row[f] is not None}


def without_persons(session: Session, asset_id: str, data: dict) -> dict:
    """A description's data less what a person wrote for the clip: the
    machine never replaces it, and keeps no copy under it."""
    owned = persons_fields(session, asset_id) & {"description", "tags"}
    return {k: v for k, v in data.items() if k not in owned} if owned else data


class CorrectionsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, asset_id: str) -> dict[str, Any] | None:
        row = self._session.execute(text(
            "SELECT description, ocr_text, tags, updated_by, updated_at"
            " FROM asset_corrections WHERE asset_id = :a"
        ), {"a": asset_id}).mappings().first()
        return dict(row) if row else None

    def update(self, asset_id: str, fields: dict[str, Any], updated_by: str | None) -> None:
        """Apply the fields given: `description` / `ocr_text` (a string) or
        `tags` (the whole list) is what the person wrote, the one copy: the
        machine's value is dropped. None removes what they wrote: none is
        left, and the machine makes it again. Fields not given stay as they
        are. An empty description or OCR, or an empty list, is what the
        person wrote too (there's none)."""
        from src.server.repository import lineage

        # One editor's save at a time per clip: each changes only its fields,
        # from what the other saved.
        self._session.execute(text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": asset_id})
        current = self.get(asset_id) or {"description": None, "ocr_text": None, "tags": None}
        values = {f: current[f] for f in FIELDS}
        removed: set[str] = set()
        for field in FIELDS.keys() & fields.keys():
            value = fields[field]
            if value is None:
                if values[field] is not None:
                    removed.add(field)
                values[field] = None
            else:
                values[field] = _clean_tags(value) if field == "tags" else value.strip()
        written = {f for f in FIELDS.keys() & fields.keys() if values[f] is not None}

        # The machine's copy of what the person wrote goes.
        for field in written & {"description", "tags"}:
            self._session.execute(text(f"UPDATE asset_metadata SET data = data - '{field}' WHERE asset_id = :a"),
                                  {"a": asset_id})
        if "ocr_text" in written:
            self._session.execute(text("UPDATE asset_ocr SET text = '', has_text = false WHERE asset_id = :a"),
                                  {"a": asset_id})
        # What they removed is missing again: the machine makes it.
        for artifact in {FIELDS[f] for f in removed}:
            if artifact == "vision":
                self._session.execute(text("DELETE FROM asset_metadata WHERE asset_id = :a"), {"a": asset_id})
            else:
                self._session.execute(text("DELETE FROM asset_ocr WHERE asset_id = :a"), {"a": asset_id})
            lineage.forget(self._session, [asset_id], artifact, commit=False)

        if all(v is None for v in values.values()):
            self._session.execute(text("DELETE FROM asset_corrections WHERE asset_id = :a"), {"a": asset_id})
        else:
            self._session.execute(text(
                "INSERT INTO asset_corrections (asset_id, description, ocr_text, tags, updated_by, updated_at)"
                " VALUES (:a, :d, :o, CAST(:t AS jsonb), :by, :now)"
                " ON CONFLICT (asset_id) DO UPDATE SET description = EXCLUDED.description,"
                "   ocr_text = EXCLUDED.ocr_text, tags = EXCLUDED.tags, updated_by = EXCLUDED.updated_by,"
                "   updated_at = EXCLUDED.updated_at"
            ), {"a": asset_id, "d": values["description"], "o": values["ocr_text"],
                "t": json.dumps(values["tags"]) if values["tags"] is not None else None,
                "by": updated_by, "now": utcnow()})
        self._session.commit()


def apply(corrections: dict[str, Any] | None, description: str | None, tags: list[str],
          ocr_text: str | None) -> tuple[str | None, list[str], str | None, list[str]]:
    """(description, tags, ocr_text) as a person sees them, and which a person wrote."""
    if not corrections:
        return description, tags, ocr_text, []
    corrected = []
    if corrections["description"] is not None:
        description = corrections["description"]
        corrected.append("description")
    if isinstance(corrections.get("tags"), list):
        tags = [str(t) for t in corrections["tags"]]
        corrected.append("tags")
    if corrections["ocr_text"] is not None:
        ocr_text = corrections["ocr_text"]
        corrected.append("ocr_text")
    return description, tags, ocr_text, corrected
