# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A scan's plan grows with the files found, not with their square.

v0.1.73's plan joined every clip's lineage and ranked its remakes in one
statement: with the statistics a live install has (the scan's listing is
filled and emptied by every scan, so the planner takes it for empty), it
went nested-loop, quadratic in the files found, and a 14,000-clip library's
plan ran for minutes. The plan is planned twice here, 4x the files the
second time, the listing's statistics as a live install's, and the time
must grow about 4x, never anything like 16x.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.server import scans
from src.server.repository import lineage
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow

SMALL, LARGE = 3000, 12000
ARTIFACTS = ("lumiverb.preview", "lumiverb.video.preview", "lumiverb.video.format", "lumiverb.capture_time", "lumiverb.video.analysis_proxy", "lumiverb.image.likeness", "lumiverb.image.faces", "lumiverb.image.description", "lumiverb.image.text",
             "lumiverb.video.scenes", "lumiverb.video.transcript", "lumiverb.video.faces")


def _library(session: Session, library_id: str, n: int) -> None:
    """n clips, a quarter of them videos, each with every artifact made as now but its preview (made another way:
    each is to be remade)."""
    session.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                         " VALUES (:l, :l, '/' || :l, 'active', now(), now())"), {"l": library_id})
    session.execute(text(
        "INSERT INTO assets (asset_id, library_id, rel_path, content_hash, file_size, file_mtime, media_type, status,"
        " proxy_key, thumbnail_key, video_preview_key, availability, created_at, updated_at)"
        " SELECT :l || '_' || lpad(i::text, 6, '0'), :l, 'p/' || lpad(i::text, 6, '0') || '.jpg',"
        "        'sha256:' || md5(:l || i) || md5(i::text), 1000 + i, '2026-01-01T00:00:00Z',"
        "        CASE WHEN i % 4 = 0 THEN 'video' ELSE 'image' END, 'proxy_ready', 'k/p' || i, 'k/t' || i,"
        "        CASE WHEN i % 4 = 0 THEN 'k/v' || i END, 'online', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'"
        " FROM generate_series(1, :n) i"), {"l": library_id, "n": n})
    for artifact in ARTIFACTS:
        want = lineage.desired(session, artifact)
        session.execute(text(
            "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash, source_hash,"
            " produced_at) SELECT asset_id, :a, :p, :v, :h, content_hash, now() FROM assets WHERE library_id = :l"),
            {"a": artifact, "p": want["producer"], "v": want["version"], "l": library_id,
             "h": "0000000000000000" if artifact == "lumiverb.preview" else want["settings_hash"]})
    session.commit()


def _planned(session: Session, library_id: str) -> tuple[float, dict]:
    """How long its plan takes, the listing's statistics a live install's (taken while it's empty), and its counts."""
    session.execute(text("ANALYZE scan_found"))  # as autovacuum last saw it: between scans
    session.commit()
    scan = scans.start(session, library_id, scope="", media_type="all", thorough=False, force=False, moves="apply",
                       dry_run=False)
    session.execute(text("INSERT INTO scan_found (scan_id, rel_path, file_size, file_mtime, media_type, settling)"
                         " SELECT :s, rel_path, file_size, file_mtime, media_type, false FROM assets"
                         " WHERE library_id = :l"), {"s": scan.scan_id, "l": library_id})
    session.commit()
    started = time.monotonic()
    scans.plan(session, scan, [], follow_moves=True)
    took = time.monotonic() - started
    counts = scans.counts(session, scan)
    scans.drop(session, scan.scan_id)
    session.commit()
    return took, counts


def test_a_plan_grows_with_the_files_found_not_their_square(env):
    engine = create_engine(env[5])
    try:
        with Session(engine) as session:
            _library(session, "lib_scale_s", SMALL)
            _library(session, "lib_scale_l", LARGE)
            session.execute(text("ANALYZE assets"))
            session.execute(text("ANALYZE artifact_lineage"))
            session.commit()
            small = min(_planned(session, "lib_scale_s")[0] for _ in range(2))
            runs = [_planned(session, "lib_scale_l") for _ in range(2)]
            large = min(took for took, _ in runs)
            counts = runs[0][1]
    finally:
        engine.dispose()
    # Every preview is to be remade: REMAKES_PER_SCAN of them in this scan.
    assert counts["upload"] == scans.REMAKES_PER_SCAN and counts["found"] == LARGE
    assert large < 8 * max(small, 0.02), f"{SMALL:,} files: {small:.2f}s; {LARGE:,}: {large:.2f}s"
