"""Tenant migration b7d3f9a1c5e2: text read as "NONE" with why is no text, and its clip's search written again;
any other text kept, a sign reading "NONE SHALL PASS" too."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE
from tests.test_migration_lineage import _alembic
from tests.test_migration_scene_likeness import _database, _run

pytestmark = pytest.mark.slow

BEFORE, AFTER = "e9b3d7f1a5c8", "b7d3f9a1c5e2"
READ = {
    "a.jpg": "NONE. I don't see any legible text in this image. It shows a beach sunset with a pier.",
    "b.jpg": "None - the photo shows a dog.",
    "c.jpg": "NONE\n\nA mountain lake.",
    "d.jpg": "LES CATACOMBES DE PARIS",
    "e.jpg": "NONE SHALL PASS",
}


def test_none_with_why_is_no_text_and_its_search_written_again() -> None:
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    with PostgresContainer(PG_IMAGE) as pg:
        url = _database(pg)
        _alembic(url, "upgrade", BEFORE)
        _run(url, "INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                  " VALUES ('lib_a', 'a', '/a', 'active', now(), now())")
        engine = create_engine(url)
        with Session(engine) as session:
            clips = {name: make_clip(session, "lib_a", name) for name in READ}
            session.commit()
        engine.dispose()
        for name, said in READ.items():
            _run(url, "INSERT INTO asset_ocr (asset_id, text, has_text, model_id, generated_at)"
                      " VALUES (:a, :t, true, 'm', now() - interval '1 day')", a=clips[name], t=said)
        _run(url, "UPDATE assets SET search_synced_at = now()")

        _alembic(url, "upgrade", AFTER)
        got = {name: tuple(_run(url, "SELECT o.text, o.has_text, a.search_synced_at IS NULL FROM asset_ocr o"
                                     " JOIN assets a USING (asset_id) WHERE asset_id = :a", a=clips[name])[0])
               for name in READ}
        assert got == {"a.jpg": ("", False, True), "b.jpg": ("", False, True), "c.jpg": ("", False, True),
                       "d.jpg": (READ["d.jpg"], True, False), "e.jpg": (READ["e.jpg"], True, False)}
        # The same rule as the client's today.
        assert {n for n, said in READ.items() if OpenAICompatibleCaptionProvider._says_none(said)} == {
            n for n, (_, has_text, _) in got.items() if not has_text}

        _alembic(url, "downgrade", BEFORE)
        assert _run(url, "SELECT count(*) FROM asset_ocr WHERE has_text") == [(2,)], "down leaves it as it is"
