"""Tenant migration e6b2d8f4a1c3 (ADR-023 phase 3 part 3, the switch-over),
on an account as the built-in Place names left it: its chosen radius becomes
the GeoNames plug-in's, never over one chosen there; a pause of its producer
becomes a pause of the plug-in's work; what the old producer left goes (its
lineage, made, empty and failed, its crashes, its pauses, its settings),
nothing of another producer's and none of its names; every location set from
one of GeoNames' places keeps GeoNames' credit, its license too, nothing a
person chose changing. A downgrade takes the credits it added away again.

Marked slow, not migration: it's the release that touches Robert's own data,
so it runs with the suite. Alembic against a throwaway database only."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2

pytestmark = pytest.mark.slow
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "552e1e291a91", "e6b2d8f4a1c3"
CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_TENANT_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def _lineage(conn, asset_id: str, artifact: str, outcome: str) -> None:
    conn.execute(text(
        "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash, produced_at,"
        "   outcome, error, attempts) VALUES (:a, :art, :art, '2', 'h', now(), :o, :e, :n)"
    ), {"a": asset_id, "art": artifact, "o": outcome, "e": "it failed" if outcome == "failed" else None,
        "n": 3 if outcome == "failed" else 0})


def _located(conn, asset_id: str, basis: dict) -> None:
    conn.execute(text(
        "INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_by, set_at)"
        " VALUES (:a, 10.66668, -61.51889, 0, 'person', 'applied', CAST(:b AS jsonb), 'usr_robert', now())"
    ), {"a": asset_id, "b": json.dumps(basis)})


def _seeded(url: str, *, radius: object = 40, chosen: dict | None = None) -> dict[str, str]:
    """An account at BEFORE as Robert's is: Place names' radius chosen and its work paused, its lineage made,
    empty and failed, a crash, its names, and locations set from places."""
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                          " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
    with Session(engine) as session:
        clips = {name: make_clip(session, "lib_a", f"{name}.jpg") for name in
                 ("made", "empty", "failed", "picked", "theirs", "kept", "point")}
        session.commit()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('producer.place', :v, now())"),
                     {"v": json.dumps({"radius_km": radius})})
        if chosen is not None:
            conn.execute(text("INSERT INTO plugin_settings (plugin_id, chosen, changed_by, changed_at)"
                              " VALUES ('geonames', CAST(:c AS jsonb), 'robert', now())"), {"c": json.dumps(chosen)})
        for scope in ("work", "redo"):
            conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                              " VALUES ('place', :s, 'usr_robert', now())"), {"s": scope})
        conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                          " VALUES ('vision', 'work', 'usr_robert', now())"))
        for name, outcome in (("made", "ok"), ("empty", "empty"), ("failed", "failed")):
            _lineage(conn, clips[name], "place", outcome)
        _lineage(conn, clips["made"], "vision", "ok")
        conn.execute(text("INSERT INTO producer_crashes (asset_id, artifact, crashes, error, updated_at)"
                          " VALUES (:a, 'place', 2, 'gone', now()), (:a, 'vision', 1, 'gone', now())"),
                     {"a": clips["failed"]})
        conn.execute(text("INSERT INTO asset_place (asset_id, place_id, label, country_code, distance_m, lat, lon,"
                          " named_at) VALUES (:a, 'geonames:3573890', 'Port of Spain, Trinidad and Tobago', 'TT', 120,"
                          " 10.66, -61.51, now())"), {"a": clips["made"]})
        _located(conn, clips["picked"], {"place": {"id": "geonames:3573890", "label": "Port of Spain, Trinidad and"
                                                   " Tobago", "source": "geonames"}})
        _located(conn, clips["theirs"], {"place": {"id": "facts:harbour", "label": "Harbour Point", "source": "facts"}})
        _located(conn, clips["kept"], {"place": {"id": "geonames:1", "label": "Somewhere", "source": "geonames",
                                                 "credit": {"text": "Kept as it was"}}})
        _located(conn, clips["point"], {"summary": "by hand"})
    engine.dispose()
    return clips


def test_the_old_producers_choices_carry_over_and_what_it_left_goes() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        clips = _seeded(url)
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT chosen FROM plugin_settings WHERE plugin_id = 'geonames'")).scalar() == {
                "radius_km": 40}
            assert sorted(conn.execute(text("SELECT target, scope FROM producer_pauses")).all()) == [
                ("plugin_geonames", "redo"), ("plugin_geonames", "work"), ("vision", "work")]
            assert conn.execute(text("SELECT artifact FROM artifact_lineage")).scalars().all() == ["vision"]
            assert conn.execute(text("SELECT artifact FROM producer_crashes")).scalars().all() == ["vision"]
            assert conn.execute(text("SELECT count(*) FROM system_metadata WHERE key = 'producer.place'")).scalar() == 0
            assert conn.execute(text("SELECT asset_id FROM asset_place")).scalars().all() == [clips["made"]], \
                "its names stay until the plug-in's are saved"
            basis = dict(conn.execute(text("SELECT asset_id, basis FROM asset_location")).all())
        assert basis[clips["picked"]]["place"] == {"id": "geonames:3573890", "label": "Port of Spain, Trinidad and Tobago",
                                                   "source": "geonames", "credit": CREDIT}
        assert "credit" not in basis[clips["theirs"]]["place"], "another plug-in's: its own, kept from now on"
        assert basis[clips["kept"]]["place"]["credit"] == {"text": "Kept as it was"}
        assert basis[clips["point"]] == {"summary": "by hand"}

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            basis = dict(conn.execute(text("SELECT asset_id, basis FROM asset_location")).all())
        assert "credit" not in basis[clips["picked"]]["place"]
        assert basis[clips["kept"]]["place"]["credit"] == {"text": "Kept as it was"}
        engine.dispose()


@pytest.mark.parametrize(("radius", "chosen", "carried"), [
    (60, {"radius_km": 10}, {"radius_km": 10}),  # chosen for the plug-in already: kept
    (60, {"other": 1}, {"other": 1, "radius_km": 60}),
    (500, None, None), (2.5, None, None), ("far", None, None), (True, None, None),  # not one it takes
    (30.0, None, {"radius_km": 30}),
])
def test_a_radius_is_carried_over_only_as_the_plug_in_takes_it_and_never_over_its_own(radius, chosen, carried) -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        _seeded(url, radius=radius, chosen=chosen)
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT chosen FROM plugin_settings WHERE plugin_id = 'geonames'")).scalar() == carried
        engine.dispose()
