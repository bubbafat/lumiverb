"""The removal's migrations (ADR-023 phase 3, part 4): the control
database's d7f3b9e1c5a2 drops the built-in Place names' list (geonames_*)
and plugin_data, each account's f8c4a2e6d0b5 drops asset_place, with what
they held; nothing a person made goes (their locations, their places and
their places' credits). Down, each is made again, empty, as part 3's code
reads it.

Marked slow, not migration: it's the release that touches Robert's own data,
so it runs with the suite. Alembic against a throwaway database only."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2

pytestmark = pytest.mark.slow
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL = ("alembic-control.ini", "ALEMBIC_CONTROL_URL", "c4e8a2f6b1d9", "d7f3b9e1c5a2")
TENANT = ("alembic-tenant.ini", "ALEMBIC_TENANT_URL", "e6b2d8f4a1c3", "f8c4a2e6d0b5")
LIST = {"geonames_places", "geonames_landmarks", "geonames_names", "geonames_admin1", "geonames_countries",
        "plugin_data"}
CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


def _alembic(tree: tuple[str, str, str, str], url: str, *args: str) -> None:
    ini, var, *_ = tree
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", ini, *args], cwd=PROJECT_ROOT,
                            env={**os.environ, var: url}, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_the_control_databases_old_list_goes_and_comes_back_empty() -> None:
    _, _, before, after = CONTROL
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(CONTROL, url, "upgrade", before)
        with engine.begin() as conn:  # a list fetched, as Robert's control database has one
            conn.execute(text("INSERT INTO geonames_places (geonameid, name, ascii_name, folded, lat, lon,"
                              " feature_code, country_code, admin1_code, population, timezone) VALUES"
                              " (2988507, 'Paris', 'Paris', 'paris', 48.85, 2.35, 'PPLC', 'FR', '11', 2138551,"
                              " 'Europe/Paris')"))
            conn.execute(text("INSERT INTO geonames_landmarks (geonameid, name, ascii_name, folded, lat, lon,"
                              " feature_code, country_code, admin1_code, known) VALUES (6254976, 'Tour Eiffel',"
                              " 'Tour Eiffel', 'tour eiffel', 48.858, 2.294, 'MNMT', 'FR', '11', 40)"))
            conn.execute(text("INSERT INTO geonames_names VALUES (6254976, 'eiffel tower', 'Eiffel Tower', 'en')"))
            conn.execute(text("INSERT INTO plugin_data (plugin_id, state, version) VALUES ('geonames', 'ready', 3)"))
        _alembic(CONTROL, url, "upgrade", after)
        assert not _tables(engine) & LIST
        assert {"installed_plugins", "plugin_install_log", "plugin_server_settings"} <= _tables(engine)

        _alembic(CONTROL, url, "downgrade", before)
        assert _tables(engine) >= LIST
        with engine.connect() as conn:
            for table in LIST:
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
            # As part 3's code reads them: the columns it asks for, and the indexes it searches by.
            conn.execute(text("SELECT known FROM geonames_landmarks"))
            conn.execute(text("SELECT version, count, ready_at FROM plugin_data"))
            indexes = {i["name"] for t in ("geonames_places", "geonames_landmarks", "geonames_names")
                       for i in inspect(engine).get_indexes(t)}
        assert {"ix_geonames_places_lat", "ix_geonames_places_folded", "ix_geonames_landmarks_folded",
                "ix_geonames_names_folded"} <= indexes
        _alembic(CONTROL, url, "upgrade", "head")
        engine.dispose()


def test_each_accounts_old_names_go_and_what_a_person_made_stays() -> None:
    _, _, before, after = TENANT
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(TENANT, url, "upgrade", before)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
        with Session(engine) as session:
            named, picked = make_clip(session, "lib_a", "named.jpg"), make_clip(session, "lib_a", "picked.jpg")
            session.commit()
        basis = {"place": {"id": "geonames:3573890", "label": "Port of Spain, Trinidad and Tobago",
                           "source": "geonames", "credit": CREDIT}}
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO asset_place (asset_id, place_id, label, country_code, distance_m, lat, lon,"
                              " landmark_id, landmark_name) VALUES (:a, 'geonames:3573890', 'Port of Spain', 'TT', 120,"
                              " 10.66, -61.51, 'geonames:1', 'Queen''s Park')"), {"a": named})
            conn.execute(text("INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_by,"
                              " set_at) VALUES (:a, 10.66668, -61.51889, 0, 'person', 'applied', CAST(:b AS jsonb),"
                              " 'usr_robert', now())"), {"a": picked, "b": json.dumps(basis)})
        _alembic(TENANT, url, "upgrade", after)
        assert "asset_place" not in _tables(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT asset_id, basis FROM asset_location")).all() == [(picked, basis)], \
                "a person's place, its credit too, stays"
            assert conn.execute(text("SELECT count(*) FROM assets")).scalar() == 2

        _alembic(TENANT, url, "downgrade", before)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM asset_place")).scalar() == 0
            conn.execute(text("SELECT landmark_id, landmark_name, landmark_label, landmark_distance_m, named_at"
                              " FROM asset_place"))
        _alembic(TENANT, url, "upgrade", "head")
        engine.dispose()


def test_an_account_from_before_the_switch_over_upgrades_straight_to_head() -> None:
    """Robert's question 1 (plain migrations): an install that jumps from before part 3 to the removal or later,
    its account seeded as Robert's was (the switch-over test's), upgraded to head in one go: the radius and
    pauses carried over, GeoNames' credit kept with every place a person picked, what the old producer left gone,
    and its names gone too (the plug-in names the clips again)."""
    from tests.test_migration_geonames_switch_over import BEFORE, _seeded

    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(TENANT, url, "upgrade", BEFORE)
        clips = _seeded(url)
        _alembic(TENANT, url, "upgrade", "head")
        assert "asset_place" not in _tables(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT chosen FROM plugin_settings WHERE plugin_id = 'geonames'")).scalar() == {
                "radius_km": 40}
            assert sorted(conn.execute(text("SELECT target, scope FROM producer_pauses")).all()) == [
                ("plugin_geonames", "redo"), ("plugin_geonames", "work"), ("vision", "work")]
            assert conn.execute(text("SELECT artifact FROM artifact_lineage")).scalars().all() == ["vision"]
            basis = dict(conn.execute(text("SELECT asset_id, basis FROM asset_location")).all())
        assert basis[clips["picked"]]["place"] == {"id": "geonames:3573890", "label": "Port of Spain, Trinidad and Tobago",
                                                   "source": "geonames", "credit": CREDIT}
        assert basis[clips["kept"]]["place"]["credit"] == {"text": "Kept as it was"}
        assert basis[clips["point"]] == {"summary": "by hand"}
        engine.dispose()
