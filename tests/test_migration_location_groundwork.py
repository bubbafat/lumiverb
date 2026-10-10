"""Migration a7c3e9d1f2b4: location groundwork (ADR-017 phase 1).

Adds assets.taken_at_offset_min and gps_accuracy_m (active_assets rebuilt
around them), the asset_location table (gone with its clip), and clears
(0, 0) GPS, a device with no fix. Going back down drops them. Runs Alembic
against a throwaway testcontainers database only.
"""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_lineage import _alembic

BEFORE, AFTER = "f3c4d5e6a7b8", "a7c3e9d1f2b4"


@pytest.mark.migration
def test_location_columns_table_and_zero_zero() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib', 'lib', '/lib', 'active', now(), now())"))
            filler = {"text": "x", "character varying": "x", "integer": 0, "bigint": 0, "boolean": False,
                      "double precision": 0.0, "timestamp with time zone": "2026-01-01T00:00:00Z"}
            extra = {name: filler[dtype] for name, dtype in conn.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_name = 'assets' AND is_nullable = 'NO' AND column_default IS NULL"
                "   AND column_name NOT IN ('asset_id', 'library_id', 'rel_path', 'file_size', 'media_type')"
            )).all()}
            names = ", ".join(extra)
            for aid, lat, lon in (("zero", 0.0, 0.0), ("real", 48.8, 2.3), ("equator", 0.0, 10.0)):
                conn.execute(text(
                    f"INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, gps_lat, gps_lon,"
                    f" {names}) VALUES (:a, 'lib', :a, 1, 'image', :lat, :lon, {', '.join(':' + k for k in extra)})"),
                    {"a": aid, "lat": lat, "lon": lon, **extra})

        _alembic(url, "upgrade", AFTER)
        with engine.begin() as conn:
            gps = {r[0]: (r[1], r[2]) for r in conn.execute(text(
                "SELECT asset_id, gps_lat, gps_lon FROM active_assets"))}
            assert gps == {"zero": (None, None), "real": (48.8, 2.3), "equator": (0.0, 10.0)}
            assert conn.execute(text(
                "SELECT taken_at_offset_min, gps_accuracy_m FROM active_assets WHERE asset_id = 'real'")).one() \
                == (None, None)
            conn.execute(text("INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis,"
                              " set_at) VALUES ('real', 1, 2, 0, 'person', 'applied', '{}', now())"))
            with pytest.raises(Exception, match="ck_asset_location_source"):
                with conn.begin_nested():
                    conn.execute(text("INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status,"
                                      " basis, set_at) VALUES ('zero', 1, 2, 0, 'ocr', 'applied', '{}', now())"))
            conn.execute(text("DELETE FROM assets WHERE asset_id = 'real'"))
            assert conn.execute(text("SELECT count(*) FROM asset_location")).scalar() == 0  # gone with its clip

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'active_assets'"))}
            assert "taken_at_offset_min" not in cols and "gps_lat" in cols
            assert conn.execute(text("SELECT to_regclass('asset_location')")).scalar() is None
        engine.dispose()
