"""GeoNames moved to its plug-in (ADR-023 phase 3, part 3: the switch-over).

Revision ID: e6b2d8f4a1c3
Revises: 552e1e291a91
Create Date: 2026-10-19

The built-in Place names producer (artifact place) is gone; the GeoNames
plug-in's work (artifact plugin_geonames) names clips now. Once each:

- its radius carries over: the account's chosen radius_km for place
  (system_metadata producer.place) becomes the plug-in's (plugin_settings,
  geonames), unless one is chosen there already, so nothing is named at
  25 km that was named at another;
- a pause of place (its work, or its redo) becomes a pause of
  plugin_geonames;
- then what place left goes: its lineage (made, empty and failed), its
  crashes, its pauses and its settings. asset_place stays: a clip shows its
  old name until the plug-in's is saved (part 4 drops it);
- every location set from one of GeoNames' places (basis.place.source
  geonames) keeps GeoNames' credit with it, with its license, beside what's
  there (nothing a person chose changes), as one set from a plug-in's place
  does from now on.

Down: the credits kept go again; what place left doesn't come back (its
names, in asset_place, stay either way).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b2d8f4a1c3"
down_revision: str | Sequence[str] | None = "552e1e291a91"
branch_labels = None
depends_on = None

CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


def _radius(raw: str | None) -> int | None:
    """The radius chosen for place, as the plug-in takes it (a whole number of km, 1 to 200), else None."""
    try:
        value = (json.loads(raw) if raw else {}).get("radius_km")
    except (ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return None
    return int(value) if 1 <= value <= 200 else None


def upgrade() -> None:
    bind = op.get_bind()
    radius = _radius(bind.execute(sa.text("SELECT value FROM system_metadata WHERE key = 'producer.place'")).scalar())
    if radius is not None:
        chosen = bind.execute(sa.text("SELECT chosen FROM plugin_settings WHERE plugin_id = 'geonames'")).scalar()
        if not (isinstance(chosen, dict) and "radius_km" in chosen):
            bind.execute(sa.text(
                "INSERT INTO plugin_settings (plugin_id, chosen, changed_by, changed_at)"
                " VALUES ('geonames', CAST(:c AS jsonb), 'lumiverb', now())"
                " ON CONFLICT (plugin_id) DO UPDATE SET chosen = plugin_settings.chosen || EXCLUDED.chosen,"
                "   changed_by = EXCLUDED.changed_by, changed_at = EXCLUDED.changed_at"
            ), {"c": json.dumps({"radius_km": radius})})
    op.execute("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
               " SELECT 'plugin_geonames', scope, paused_by, paused_at FROM producer_pauses WHERE target = 'place'"
               " ON CONFLICT (target, scope) DO NOTHING")
    op.execute("DELETE FROM producer_pauses WHERE target = 'place'")
    op.execute("DELETE FROM artifact_lineage WHERE artifact = 'place'")
    op.execute("DELETE FROM producer_crashes WHERE artifact = 'place'")
    op.execute("DELETE FROM system_metadata WHERE key = 'producer.place'")
    bind.execute(sa.text(
        "UPDATE asset_location SET basis = jsonb_set(basis, '{place,credit}', CAST(:c AS jsonb))"
        " WHERE jsonb_typeof(basis->'place') = 'object' AND basis->'place'->>'source' = 'geonames'"
        "   AND NOT (basis->'place' ? 'credit')"
    ), {"c": json.dumps(CREDIT)})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE asset_location SET basis = basis #- '{place,credit}'"
        " WHERE jsonb_typeof(basis->'place') = 'object' AND basis->'place'->>'source' = 'geonames'"
        "   AND basis->'place'->'credit' = CAST(:c AS jsonb)"
    ), {"c": json.dumps(CREDIT)})
