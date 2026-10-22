"""Tenant migration c7e1a9d3f5b2: search reads any language's words, accents
aside, and a hyphenated word by its parts.

Run as an account's own role, as Lumiverb's app role is (an owner, never a
superuser): unaccent is a trusted extension, so the owner may create it.
Every searched column is made again from the text kept beside it, up and
down, nothing synced. Runs Alembic against throwaway databases only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.app_role import make_app_role
from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

pytestmark = pytest.mark.slow

BEFORE, AFTER = "a3c9e5f1b7d2", "c7e1a9d3f5b2"
# Each searched column, and SQL finding the row by a word typed without its accent, and by a phrase that ends at
# a hyphen.
FOUND = {
    "asset_search.everything": "SELECT count(*) FROM asset_search WHERE everything @@ {q}",
    "asset_search.shown": "SELECT count(*) FROM asset_search WHERE shown @@ {q}",
    "asset_search.shown_capped": "SELECT count(*) FROM asset_search WHERE shown_capped @@ {q}",
    "video_scenes.words": "SELECT count(*) FROM video_scenes WHERE words @@ {q}",
    "transcript_segments.words": "SELECT count(*) FROM transcript_segments WHERE words @@ {q}",
    "plugin_outputs.words": "SELECT count(*) FROM plugin_outputs WHERE words @@ {q}",
}
SAID = "Zürich: John Wayne Airport-Orange County"


def _owners_database(pg, tmp: Path) -> str:
    """A database owned by an ordinary role (made by deploy-api.sh's own lines), vector made in it by the superuser
    (as Lumiverb's install does)."""
    make_app_role(_ensure_psycopg2(pg.get_connection_url()), "acct_owner", "owner", tmp)
    admin = create_engine(_ensure_psycopg2(pg.get_connection_url()), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("CREATE DATABASE acct OWNER acct_owner"))
    admin.dispose()
    url = make_url(_ensure_psycopg2(pg.get_connection_url()))
    in_acct = create_engine(url.set(database="acct"))
    with in_acct.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    in_acct.dispose()
    return url.set(database="acct", username="acct_owner", password="owner").render_as_string(hide_password=False)


def _seed(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                          " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
    with Session(engine) as session:
        clip = make_clip(session, "lib_a", "a.mov", media_type="video")
        session.commit()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO asset_search (asset_id, described, ocr_text) VALUES (:a, :t, :t)"),
                     {"a": clip, "t": SAID})
        conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                          " description, created_at) VALUES ('scn_a', :a, 0, 0, 1000, 0, :t, now())"),
                     {"a": clip, "t": SAID})
        conn.execute(text("INSERT INTO transcript_segments (asset_id, seq, start_ms, end_ms, text)"
                          " VALUES (:a, 0, 0, 1000, :t)"), {"a": clip, "t": SAID})
        conn.execute(text("INSERT INTO plugin_outputs (asset_id, plugin_id, outputs, inputs_hash, made_at, search_text)"
                          " VALUES (:a, 'geonames', '{}', 'h', now(), :t)"), {"a": clip, "t": SAID})


def _found(url: str, config: str) -> dict[str, tuple[int, int]]:
    """Each column's rows found by "zurich" and by the phrase "John Wayne Airport", asked in config."""
    with create_engine(url).connect() as conn:
        return {column: tuple(conn.execute(text(sql.format(q=f"{query}('{config}', :q)")), {"q": q}).scalar_one()
                              for query, q in (("plainto_tsquery", "zurich"),
                                               ("phraseto_tsquery", "john wayne airport")))
                for column, sql in FOUND.items()}


def test_an_accounts_own_role_searches_any_language_after_and_english_before(tmp_path: Path) -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _owners_database(pg, tmp_path)
        _alembic(url, "upgrade", BEFORE)
        _seed(url)
        assert set(_found(url, "english").values()) == {(0, 0)}, "before: neither, as English reads them"

        _alembic(url, "upgrade", AFTER)
        assert set(_found(url, "lumiverb").values()) == {(1, 1)}, "every column, made again from its text"
        with create_engine(url).connect() as conn:
            assert conn.execute(text("SELECT described FROM asset_search")).scalar_one() == SAID, "shown as written"

        _alembic(url, "downgrade", BEFORE)
        assert set(_found(url, "english").values()) == {(0, 0)}
        with create_engine(url).connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM pg_ts_config WHERE cfgname = 'lumiverb'")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'unaccent'")).scalar_one() == 0
        _alembic(url, "upgrade", AFTER)
        assert set(_found(url, "lumiverb").values()) == {(1, 1)}, "and up again"
