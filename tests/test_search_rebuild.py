# ruff: noqa: F811 — pytest fixtures are named as parameters
"""An index made again beside the live one: search answers throughout.

A schema change, a restore or an admin's ask makes an account's index again
(src/server/search/indexes.py, sync.py rebuild / advance_rebuilds): never by
deleting it first. Every write goes to both while the sweep fills the new
one from the database; once full it's live, and the old one, written to for
one more sweep, goes. Here against Quickwit's HTTP API in memory (the real
client's requests), on a real account's database.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow


class _Answer:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code, self._body, self.text = status, body or {}, json.dumps(body or {})

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"Quickwit answered {self.status_code}")


class FakeQuickwit:
    """Quickwit's HTTP API as QuickwitClient uses it, in memory: indexes, each
    with its schema's fields and its documents."""

    def __init__(self) -> None:
        self.indexes: dict[str, dict] = {}

    def _path(self, url: str) -> str:
        return url.split("/api/v1/", 1)[1].split("?", 1)[0]

    def get(self, url, timeout=None):
        index_id = self._path(url).removeprefix("indexes/")
        if index_id not in self.indexes:
            return _Answer(404)
        fields = [{"name": f} for f in sorted(self.indexes[index_id]["fields"])]
        return _Answer(200, {"index_config": {"doc_mapping": {"field_mappings": fields}}})

    def delete(self, url, timeout=None):
        return _Answer(200 if self.indexes.pop(self._path(url).removeprefix("indexes/"), None) else 404)

    def post(self, url, json=None, data=None, headers=None, timeout=None):  # noqa: A002 — requests' names
        path = self._path(url)
        if path == "indexes":
            config = __import__("json").loads(data)
            self.indexes[config["index_id"]] = {
                "fields": {f["name"] for f in config["doc_mapping"].get("field_mappings", [])}, "docs": []}
            return _Answer(200)
        index_id, _, action = path.partition("/")
        index = self.indexes.get(index_id)
        if index is None:
            return _Answer(404)
        if action == "ingest":
            index["docs"] += [__import__("json").loads(line) for line in (data or "").splitlines() if line]
            return _Answer(200)
        if action == "delete-tasks":
            gone = set(re.findall(r'(\w+):"([^"]*)"', json["query"]))
            index["docs"] = [d for d in index["docs"] if not any(str(d.get(f)) == v for f, v in gone)]
            return _Answer(200)
        if action == "search":
            return _Answer(200, {"hits": [{"_source": d} for d in index["docs"]]})
        return _Answer(400)

    def asset_ids(self, index_id: str) -> list[str]:
        return sorted(d["asset_id"] for d in self.indexes[index_id]["docs"])


@pytest.fixture
def quickwit(env):
    """Quickwit on, in memory; the account's indexes read from its database."""
    from src.server.config import get_settings
    from src.server.search import indexes

    fake = FakeQuickwit()
    with (patch.object(get_settings(), "quickwit_enabled", True),
          patch("src.server.search.quickwit_client.requests.get", fake.get),
          patch("src.server.search.quickwit_client.requests.post", fake.post),
          patch("src.server.search.quickwit_client.requests.delete", fake.delete),
          patch("src.server.database.get_tenant_session", lambda tenant_id: _db(env))):
        indexes._cache.clear()
        yield fake
    indexes._cache.clear()
    with _db(env) as s:
        s.execute(text("DELETE FROM system_metadata WHERE key = :k OR key LIKE 'search.reindex.%'"),
                  {"k": indexes.KEY})
        s.commit()


def _sweep(env) -> None:
    from src.server.search.sync import run_search_sync_sweep

    with _db(env) as s:
        run_search_sync_sweep(s, tenant_id=env[4])


def _found(env) -> set[str]:
    """What a search finds: the clips in the live index."""
    from src.server.search.quickwit_client import QuickwitClient

    return {h["asset_id"] for h in QuickwitClient().search_tenant(env[4], "*", max_hits=10_000)}


def _where(env):
    from src.server.search import indexes

    with _db(env) as s:
        return indexes.read(s, env[4])["assets"]


def _active(env) -> set[str]:
    with _db(env) as s:
        return set(s.execute(text("SELECT asset_id FROM active_assets")).scalars())


def test_a_schema_change_rebuilds_beside_the_live_index_and_search_answers_throughout(env, quickwit):
    from src.server.search import indexes
    from src.server.search.sync import try_sync_asset

    lib = _library(env, "Rebuilt")
    clips = [_ingest_with(lib, f"r{i}.jpg", _sha(), None) for i in range(3)]
    tenant_id = env[4]
    old, new = indexes.base_id(tenant_id, "assets"), f"{indexes.base_id(tenant_id, 'assets')}_g1"
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = NULL"))
        s.commit()
    _sweep(env)
    assert set(clips) <= _found(env) and _where(env).live == old

    # A new field in the schema now: the live index lacks it.
    quickwit.indexes[old]["fields"].discard("description")
    _sweep(env)  # made again beside it, empty, filled from the next sweep; search still answers from the old one
    where = _where(env)
    assert (where.live, where.building, where.cursor) == (old, new, "") and quickwit.asset_ids(new) == []
    assert set(clips) <= _found(env)

    # A write meanwhile goes to both.
    with _db(env) as s:
        from src.server.models.tenant import Asset

        try_sync_asset(s, s.get(Asset, clips[0]), tenant_id=tenant_id)
    assert clips[0] in quickwit.asset_ids(new) and set(clips) <= _found(env)

    _sweep(env)  # filled, then live; the old one takes writes one more sweep
    where = _where(env)
    assert (where.live, where.building, where.retired) == (new, None, old)
    assert quickwit.asset_ids(new) == sorted(_active(env)), "every clip, once"
    assert set(clips) <= _found(env)

    # A clip trashed now leaves both.
    from src.server.search.quickwit_client import QuickwitClient

    QuickwitClient().delete_tenant_documents_by_asset_ids(tenant_id, [clips[1]])
    assert clips[1] not in quickwit.asset_ids(new) and clips[1] not in quickwit.asset_ids(old)

    _sweep(env)  # the old one goes
    assert old not in quickwit.indexes and _where(env).retired is None
    assert {clips[0], clips[2]} <= _found(env)


def test_a_restore_rebuilds_every_kind_beside_and_drops_nothing_first(env, quickwit):
    from src.server.search import indexes
    from src.server.search.sync import mark_recreate

    lib = _library(env, "RestoredBeside")
    clip = _ingest_with(lib, "restored.jpg", _sha(), None)
    _sweep(env)
    assert clip in _found(env)
    with _db(env) as s:
        mark_recreate(s)
        s.commit()
    before = set(quickwit.indexes)
    _sweep(env)
    with _db(env) as s:
        where = indexes.read(s, env[4])
    assert before <= set(quickwit.indexes), "nothing deleted"
    assert all(w.building == f"{w.live}_g1" for w in where.values()) and clip in _found(env)
    for _ in range(3):  # begun; filled and live; the old ones go
        _sweep(env)
    with _db(env) as s:
        where = indexes.read(s, env[4])
    assert all(w.live.endswith("_g1") and w.building is None and w.retired is None for w in where.values()), where
    assert not before & set(quickwit.indexes) and clip in _found(env)
