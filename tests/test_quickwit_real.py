"""The server's search documents against a real Quickwit.

The rest of the suite never reaches Quickwit (tests/conftest.py turns it off),
and the server swallows Quickwit errors and falls back to Postgres, so nothing
else notices when Quickwit stops accepting what we send it. These tests do:
each builds a document with the real builder in src/server/search/sync.py,
ingests it through QuickwitClient into an index made from the real schema in
quickwit/, and looks for it through the search the API runs
(`_run_quickwit_search`) with the Postgres fallback off. Quickwit drops a
document that doesn't fit the schema without failing the ingest, so a document
it rejects is a document these tests can't find.

Opt-in: `uv run pytest -m quickwit`. They start a throwaway Quickwit in Docker
for the run (QUICKWIT_TEST_IMAGE, default the version docker-compose runs) and
skip if Docker isn't there.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from src.server.config import get_settings
from src.server.models.query_filter import SearchTerm
from src.server.models.tenant import Asset, AssetMetadata, VideoScene
from src.server.search.quickwit_client import QuickwitClient
from src.server.search.sync import (
    build_asset_document,
    build_scene_document,
    index_transcript_segments,
)

pytestmark = pytest.mark.quickwit

QUICKWIT_IMAGE = os.environ.get("QUICKWIT_TEST_IMAGE", "quickwit/quickwit:0.8.1")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def quickwit_url():
    """A Quickwit of the run's own, gone when the module is done."""
    try:
        import docker

        docker.from_env().ping()
    except Exception as e:
        pytest.skip(f"real Quickwit tests need Docker to start a throwaway Quickwit: {e}")
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(QUICKWIT_IMAGE)
        .with_command("run")
        .with_env("QW_DISABLE_TELEMETRY", "1")
        .with_exposed_ports(7280)
    )
    container.start()
    try:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(7280)}"
        deadline = time.monotonic() + 60
        while True:
            try:
                if requests.get(f"{url}/health/readyz", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"Quickwit at {url} not ready after 60 s:\n{container.get_logs()}")
            time.sleep(0.5)
        yield url
    finally:
        container.stop()


@pytest.fixture
def qw(quickwit_url: str):
    """Quickwit on, at the throwaway one, with no Postgres fallback to hide a miss.
    Run from the repo root, where QuickwitClient looks for its schemas. No
    database: the URLs Settings wants come from conftest, nothing here opens them."""
    env = {
        "QUICKWIT_ENABLED": "true",
        "QUICKWIT_URL": quickwit_url,
        "QUICKWIT_FALLBACK_TO_POSTGRES": "false",
    }
    saved = {k: os.environ.get(k) for k in env}
    cwd = os.getcwd()
    os.environ.update(env)
    get_settings.cache_clear()
    os.chdir(_PROJECT_ROOT)
    try:
        client = QuickwitClient()
        assert client.enabled
        yield client
    finally:
        os.chdir(cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def _make_indexes(qw: QuickwitClient, tenant_id: str) -> None:
    """The tenant's three indexes, made as the server makes them. The client
    waits until each new index takes an ingest (QuickwitClient._wait_until_ready),
    so nothing here needs to."""
    qw.ensure_tenant_index(tenant_id)
    qw.ensure_tenant_scene_index(tenant_id)
    qw.ensure_tenant_transcript_index(tenant_id)


def _ids() -> tuple[str, str]:
    """A tenant of the test's own (so its own indexes) and a library."""
    return f"tqw_{uuid.uuid4().hex[:12]}", f"lib_{uuid.uuid4().hex[:12]}"


def _asset(library_id: str, media_type: str = "image", **kw: object) -> Asset:
    return Asset(
        asset_id=f"ast_{uuid.uuid4().hex[:12]}",
        library_id=library_id,
        rel_path=kw.pop("rel_path", "Trips/2024/beach_day.jpg"),
        file_size=1234,
        media_type=media_type,
        **kw,
    )


def _search(tenant_id: str, library_id: str, q: str):
    """What /v1/query asks Quickwit for this text, and what it got."""
    from src.server.api.routers.query import _run_quickwit_search

    return _run_quickwit_search(tenant_id, [SearchTerm(q=q)], [library_id], limit=20)


def test_asset_document_is_found(qw: QuickwitClient) -> None:
    tenant_id, library_id = _ids()
    asset = _asset(
        library_id,
        camera_make="FUJIFILM",
        camera_model="X-T5",
        taken_at=datetime(2024, 7, 4, 15, 30, tzinfo=UTC),
        gps_lat=41.5,
        gps_lon=-70.6,
        note="ask about the zephyrine print",
        transcript_text="",
    )
    meta = AssetMetadata(
        metadata_id="meta_1", asset_id=asset.asset_id, model_id="qwen", model_version="1",
        generated_at=datetime.now(UTC),
        data={"description": "a lighthouse on a rocky point", "tags": ["lighthouse", "coast"]},
    )
    # A person rewrote the description: the search should find their words.
    corrections = {"description": "a quoddy lighthouse in fog", "ocr_text": None, "tags": None}
    doc = build_asset_document(asset, meta, "WELCOME TO MARBLEHEAD", corrections)

    _make_indexes(qw, tenant_id)
    qw.ingest_tenant_documents(tenant_id, [doc])

    for q in ("quoddy", "marblehead", "zephyrine", "beach"):  # description, OCR, note, path
        scores, contexts, source = _search(tenant_id, library_id, q)
        assert source == "quickwit", q
        assert asset.asset_id in scores, f"{q!r} didn't find the asset document {doc}"
        assert contexts[asset.asset_id].hit_type == "asset"
        assert contexts[asset.asset_id].snippet == "a quoddy lighthouse in fog"


def test_scene_document_is_found(qw: QuickwitClient) -> None:
    tenant_id, library_id = _ids()
    asset = _asset(library_id, "video", rel_path="Clips/harbor.mov", duration_sec=93.5)
    scene = VideoScene(
        scene_id=f"scn_{uuid.uuid4().hex[:12]}", asset_id=asset.asset_id, scene_index=2,
        start_ms=41_000, end_ms=52_500, rep_frame_ms=46_000, thumbnail_key="t/scn.jpg",
        description="a trawler unloading lobster traps", tags=["boat", "harbor"],
        sharpness_score=0.82, keep_reason="scene_change",
    )
    doc = build_scene_document(scene, asset)

    _make_indexes(qw, tenant_id)
    qw.ingest_tenant_scene_documents(tenant_id, [doc])

    scores, contexts, source = _search(tenant_id, library_id, "trawler")
    assert source == "quickwit"
    assert asset.asset_id in scores, f"didn't find the scene document {doc}"
    ctx = contexts[asset.asset_id]
    assert (ctx.hit_type, ctx.start_ms, ctx.end_ms) == ("scene", 41_000, 52_500)
    assert ctx.snippet == "a trawler unloading lobster traps"


SRT = """1
00:00:01,000 --> 00:00:04,200
Welcome aboard the ferry to Nantucket.

2
00:01:05,500 --> 00:01:09,000
Watch for the cormorants off the jetty.
"""


def test_transcript_document_is_found(qw: QuickwitClient) -> None:
    tenant_id, library_id = _ids()
    asset = _asset(library_id, "video", rel_path="Clips/ferry.mov", transcript_language="en")

    _make_indexes(qw, tenant_id)
    index_transcript_segments(tenant_id, asset, SRT)  # builds and ingests

    scores, contexts, source = _search(tenant_id, library_id, "cormorants")
    assert source == "quickwit"
    assert asset.asset_id in scores, "didn't find the transcript segment"
    ctx = contexts[asset.asset_id]
    assert (ctx.hit_type, ctx.start_ms, ctx.end_ms) == ("transcript", 65_500, 69_000)
    assert ctx.snippet == "Watch for the cormorants off the jetty."


def test_a_document_quickwit_rejects_is_not_found(qw: QuickwitClient) -> None:
    """The check the tests above lean on: Quickwit takes the ingest of a
    document that breaks the schema, drops it, and the search misses it."""
    tenant_id, library_id = _ids()
    asset = _asset(library_id)
    good = build_asset_document(asset, None, "")
    good["description"] = "a pelican on a piling"
    bad = {**good, "id": "bad", "asset_id": "ast_bad", "capture_ts": "not a time"}

    _make_indexes(qw, tenant_id)
    qw.ingest_tenant_documents(tenant_id, [bad, good])  # no error for the bad one

    scores, _contexts, source = _search(tenant_id, library_id, "pelican")
    assert source == "quickwit"
    assert set(scores) == {asset.asset_id}
