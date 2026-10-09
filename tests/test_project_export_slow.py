"""Send to editor (ADR-016 phase 1): a project exports as a bin of master clips.

GET /v1/projects/{id}/export?format=fcp7|fcpxml returns the file. Clips
point at the originals as the libraries know them (Robert, Oct 9: just the
brain's paths), plus rel_path; photos are in it as stills.
"""

from __future__ import annotations

import io
import json
import os
import xml.etree.ElementTree as ET
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from src.server.storage.local import LocalStorage
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made

FACET = {
    "duration_sec": 7.07, "container": "mov", "video_codec": "h264", "width": 640,
    "height": 360, "rotation": 0, "frame_rate_num": 30000, "frame_rate_den": 1001,
    "start_timecode": None, "drop_frame": None, "audio_codec": "aac",
    "audio_channels": 2, "audio_sample_rate": 48000,
}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Yields (client, headers, library_id, tenant_url); the library root is /Volumes/DAS."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("export_storage")))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(
            make_url(control_url).set(database="{tenant_id}")
        )
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with (
            patch("src.server.api.routers.admin.provision_tenant_database"),
            TestClient(app) as bootstrap,
        ):
            r = bootstrap.post(
                "/v1/admin/tenants",
                json={"name": "ExportTenant", "plan": "free"},
                headers={"Authorization": "Bearer test-admin-secret"},
            )
            assert r.status_code == 200, r.text
            tenant_id = r.json()["tenant_id"]
            api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with (
                patch("src.server.api.routers.ingest.get_storage", return_value=storage),
                TestClient(app) as client,
            ):
                headers = {"Authorization": f"Bearer {api_key}"}
                r = client.post(
                    "/v1/libraries",
                    json={"name": "DAS", "root_path": "/Volumes/DAS"},
                    headers=headers,
                )
                assert r.status_code == 200, r.text
                yield client, headers, r.json()["library_id"], tenant_url

    _engines.clear()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), color=(30, 60, 90)).save(buf, format="JPEG")
    return buf.getvalue()


def _ingest(client, headers, library_id, rel_path, media_type, facet=None, exif=None) -> str:
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "5000",
            "media_type": media_type, "lineage": ingest_made()}
    if facet:
        data["video_facet"] = json.dumps(facet)
    if exif:
        data["exif"] = json.dumps(exif)
    r = client.post(
        "/v1/ingest", headers=headers,
        files={"proxy": ("p.jpg", io.BytesIO(_jpeg()), "image/jpeg")}, data=data,
    )
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _project(client, headers, name, asset_ids) -> str:
    r = client.post("/v1/projects", json={"name": name, "asset_ids": asset_ids}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["project_id"]


def _export(client, headers, project_id, **params):
    return client.get(f"/v1/projects/{project_id}/export", params=params, headers=headers)


@pytest.fixture(scope="module")
def job(env):
    """A project with two videos (one unprobed) and a still."""
    client, headers, library_id, _ = env
    probed = _ingest(client, headers, library_id, "shoot/A001 take.mov", "video", FACET)
    # Not probed, but its EXIF gives a duration: exported at the 30 fps fallback.
    unprobed = _ingest(client, headers, library_id, "shoot/A002.mov", "video",
                       exif={"duration_sec": 2.0})
    still = _ingest(client, headers, library_id, "shoot/still.jpg", "image")
    return _project(client, headers, "Customer Video 123", [probed, unprobed, still])


@pytest.mark.slow
def test_formats_endpoint(env) -> None:
    client, headers, _, _ = env

    r = client.get("/v1/export/formats", headers=headers)

    assert r.status_code == 200
    assert r.json()["items"] == [
        {"id": "fcp7", "label": "DaVinci Resolve / Premiere Pro", "file_extension": ".xml"},
        {"id": "fcpxml", "label": "Final Cut Pro", "file_extension": ".fcpxml"},
    ]


@pytest.mark.slow
def test_fcp7_export_points_at_originals(env, job) -> None:
    client, headers, _, _ = env

    r = _export(client, headers, job, format="fcp7")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/xml")
    assert 'filename="Customer Video 123.xml"' in r.headers["content-disposition"]
    assert r.headers["x-lumiverb-stills"] == "1"
    root = ET.fromstring(r.content)
    assert root.findtext("bin/name") == "Customer Video 123"
    urls = sorted(f.findtext("pathurl") for f in root.iter("file") if f.find("pathurl") is not None)
    assert urls == [
        "file://localhost/Volumes/DAS/shoot/A001%20take.mov",
        "file://localhost/Volumes/DAS/shoot/A002.mov",
        "file://localhost/Volumes/DAS/shoot/still.jpg",  # a photo, as a still (Robert, Oct 9)
    ]
    probed = next(c for c in root.findall("bin/children/clip") if c.findtext("name") == "A001 take.mov")
    assert probed.findtext("duration") == "212"
    assert probed.findtext("rate/ntsc") == "TRUE"
    still = next(c for c in root.findall("bin/children/clip") if c.findtext("name") == "still.jpg")
    assert still.findtext("duration") == "150"  # 5 s
    assert still.find("media/audio") is None


@pytest.mark.slow
def test_there_is_no_media_location_to_give(env, job) -> None:
    # Robert, Oct 9: just the brain's paths (the libraries'); the editor relinks.
    client, headers, _, _ = env

    r = _export(client, headers, job, format="fcp7", prefix="/Volumes/Travel SSD/")

    urls = sorted(f.findtext("pathurl") for f in ET.fromstring(r.content).iter("file")
                  if f.find("pathurl") is not None)
    assert urls[0] == "file://localhost/Volumes/DAS/shoot/A001%20take.mov"


@pytest.mark.slow
def test_fcpxml_export(env, job) -> None:
    client, headers, _, _ = env

    r = _export(client, headers, job, format="fcpxml")

    assert r.status_code == 200, r.text
    assert 'filename="Customer Video 123.fcpxml"' in r.headers["content-disposition"]
    root = ET.fromstring(r.content)
    assert root.find("library/event").get("name") == "Customer Video 123"
    assert len(root.findall("library/event/asset-clip")) == 3  # the photo too
    assert len(root.findall("library/event/project/sequence/spine/video")) == 1  # it, on the timeline


@pytest.mark.slow
def test_unknown_format_is_400(env, job) -> None:
    client, headers, _, _ = env

    assert _export(client, headers, job, format="edl").status_code == 400


@pytest.mark.slow
def test_archived_project_still_exports(env) -> None:
    client, headers, library_id, _ = env
    asset = _ingest(client, headers, library_id, "archive/clip.mov", "video", FACET)
    project_id = _project(client, headers, "Done job", [asset])
    client.patch(f"/v1/projects/{project_id}", json={"status": "archived"}, headers=headers)

    r = _export(client, headers, project_id, format="fcp7")

    assert r.status_code == 200, r.text
    assert len(ET.fromstring(r.content).findall("bin/children/clip")) == 1


@pytest.mark.slow
def test_trashed_clips_are_left_out(env) -> None:
    client, headers, library_id, _ = env
    keep = _ingest(client, headers, library_id, "trash/keep.mov", "video", FACET)
    gone = _ingest(client, headers, library_id, "trash/gone.mov", "video", FACET)
    project_id = _project(client, headers, "With trash", [keep, gone])
    # The project uses it, so trashing it asks first (409 in_projects).
    assert client.delete(f"/v1/assets/{gone}", headers=headers).status_code == 409
    assert client.delete(f"/v1/assets/{gone}", params={"remove_from_projects": True}, headers=headers).status_code == 204

    r = _export(client, headers, project_id, format="fcp7")

    names = [c.findtext("name") for c in ET.fromstring(r.content).findall("bin/children/clip")]
    assert names == ["keep.mov"]


@pytest.mark.slow
def test_filename_is_sanitized(env) -> None:
    client, headers, library_id, _ = env
    asset = _ingest(client, headers, library_id, "names/clip.mov", "video", FACET)
    project_id = _project(client, headers, 'Job: "A/B" test', [asset])

    r = _export(client, headers, project_id, format="fcp7")

    assert 'filename="Job_ _A_B_ test.xml"' in r.headers["content-disposition"]


@pytest.mark.slow
def test_unknown_project_is_404(env) -> None:
    client, headers, _, _ = env

    assert _export(client, headers, "prj_nope", format="fcp7").status_code == 404


@pytest.mark.slow
def test_a_project_over_1000_clips_exports_whole(env) -> None:
    """The phase 1 gate: no 1,000-clip cap anywhere between the project and the file."""
    from sqlalchemy import create_engine, text

    client, headers, _, tenant_url = env
    r = client.post("/v1/libraries", json={"name": "Big", "root_path": "/Volumes/Big"}, headers=headers)
    big = r.json()["library_id"]
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type,"
            " availability, status, duration_sec, created_at, updated_at)"
            " SELECT 'ast_big_' || n, :lib, 'clips/' || lpad(n::text, 5, '0') || '.mov', 1000,"
            "        'video', 'online', 'proxy_ready', 2.0, now(), now()"
            " FROM generate_series(1, 1050) AS n"
        ), {"lib": big})
    engine.dispose()
    project_id = client.post(
        "/v1/projects",
        json={"name": "Everything", "from_search": {"filters": [{"type": "library", "value": big}]}},
        headers=headers,
    ).json()["project_id"]

    r = _export(client, headers, project_id, format="fcp7")

    assert r.status_code == 200, r.text
    names = [c.findtext("name") for c in ET.fromstring(r.content).findall("bin/children/clip")]
    assert len(names) == len(set(names)) == 1050


# ---------------------------------------------------------------------------
# Hardening from the phase 1 validation review
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_names_outside_latin1_export(env) -> None:
    client, headers, library_id, _ = env
    asset = _ingest(client, headers, library_id, "names/u.mov", "video", FACET)
    name = "Mike’s wedding — 東京"
    project_id = _project(client, headers, name, [asset])

    r = _export(client, headers, project_id, format="fcp7")

    assert r.status_code == 200, r.text
    disposition = r.headers["content-disposition"]
    assert disposition.isascii()
    from urllib.parse import unquote
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == name + ".xml"


@pytest.mark.slow
def test_each_library_keeps_its_own_folder(env) -> None:
    client, headers, _, _ = env
    libs = [
        client.post("/v1/libraries", json={"name": f"Y{y}", "root_path": f"/mnt/das/{y}"},
                    headers=headers).json()["library_id"]
        for y in (2024, 2025)
    ]
    assets = [_ingest(client, headers, lib, "a/clip.mov", "video", FACET) for lib in libs]
    project_id = _project(client, headers, "Two years", assets)

    r = _export(client, headers, project_id, format="fcp7")

    urls = sorted(f.findtext("pathurl") for f in ET.fromstring(r.content).iter("file")
                  if f.find("pathurl") is not None)
    assert urls == [
        "file://localhost/mnt/das/2024/a/clip.mov",
        "file://localhost/mnt/das/2025/a/clip.mov",
    ]


@pytest.mark.slow
def test_unprobed_and_durationless_clips_are_reported(env) -> None:
    """An unprobed clip still exports (the editor reads the real rate); a
    clip with no known duration at all is left out, and both are counted."""
    client, headers, library_id, _ = env
    probed = _ingest(client, headers, library_id, "report/probed.mov", "video", FACET)
    unprobed = _ingest(client, headers, library_id, "report/unprobed.mov", "video",
                       exif={"duration_sec": 3.0})
    no_length = _ingest(client, headers, library_id, "report/no-length.mov", "video")
    project_id = _project(client, headers, "Report", [probed, unprobed, no_length])

    r = _export(client, headers, project_id, format="fcpxml")

    assert r.status_code == 200, r.text
    assert r.headers["x-lumiverb-unprobed"] == "1"
    assert r.headers["x-lumiverb-skipped-no-duration"] == "1"
    names = sorted(c.get("name") for c in ET.fromstring(r.content).findall("library/event/asset-clip"))
    assert names == ["probed.mov", "unprobed.mov"]


@pytest.mark.slow
def test_name_without_latin_letters_gets_a_usable_ascii_fallback(env) -> None:
    client, headers, library_id, _ = env
    asset = _ingest(client, headers, library_id, "names/tokyo.mov", "video", FACET)
    project_id = _project(client, headers, "東京", [asset])

    r = _export(client, headers, project_id, format="fcp7")

    assert 'filename="project-export.xml"' in r.headers["content-disposition"]
    assert "filename*=UTF-8''%E6%9D%B1%E4%BA%AC.xml" in r.headers["content-disposition"]
