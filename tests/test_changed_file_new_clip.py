# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A different file at the same path is a new clip; the old one goes missing (Robert, Oct 9).

"A binary different file with the same name is a completely new file", and
"Old one goes missing": nothing made or written for the old file carries
over to the new one. The old clip is archived as missing, with everything
it had (its projects too), and comes back if its content does. A file
whose content isn't known (no hash) stays the clip it was.
"""

from __future__ import annotations

import pytest

from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _archive, _row, _sha, _unarchive

pytestmark = pytest.mark.slow


def _missing(env, *asset_ids: str) -> None:
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": list(asset_ids), "reason": "missing"},
                       headers=headers)
    assert r.status_code in (200, 204), r.text


def _note(env, asset_id: str, text: str) -> None:
    client, headers, *_ = env
    assert client.put(f"/v1/assets/{asset_id}/note", json={"text": text}, headers=headers).status_code in (200, 204)


def test_a_changed_file_is_a_new_clip_and_the_old_one_goes_missing(env):
    client, headers, *_ = env
    old = _ingest(env, "ncf/a.jpg", media_type="image", sha=_sha())
    _note(env, old, "the first take")
    r = client.post("/v1/projects", json={"name": "New clip", "asset_ids": [old]}, headers=headers)
    project_id = r.json()["project_id"]

    new = _ingest(env, "ncf/a.jpg", media_type="image", sha=_sha())
    assert new != old
    assert _row(env, old)[1] == "missing" and _row(env, old)[2] == "ncf/a.jpg"  # archived where it was
    assert _row(env, new)[0] is None
    detail = client.get(f"/v1/assets/{new}", headers=headers).json()
    assert detail["note"] is None  # nothing carries over
    project = client.get(f"/v1/projects/{project_id}", headers=headers).json()
    assert project["missing_asset_count"] == 1  # still in its project, as missing


def test_the_old_content_back_at_its_path_brings_its_clip_back(env):
    first_sha, second_sha = _sha(), _sha()
    first = _ingest(env, "ncf/back.jpg", media_type="image", sha=first_sha)
    second = _ingest(env, "ncf/back.jpg", media_type="image", sha=second_sha)
    again = _ingest(env, "ncf/back.jpg", media_type="image", sha=first_sha)
    assert again == first and _row(env, first)[0] is None
    assert _row(env, second)[1] == "missing"


def test_a_different_file_where_a_missing_clip_was_is_a_new_clip(env):
    gone = _ingest(env, "ncf/gone.jpg", media_type="image", sha=_sha())
    _missing(env, gone)
    new = _ingest(env, "ncf/gone.jpg", media_type="image", sha=_sha())
    assert new != gone
    assert _row(env, gone)[1] == "missing" and _row(env, new)[0] is None


def test_the_same_file_back_where_it_was_comes_back(env):
    sha = _sha()
    gone = _ingest(env, "ncf/same.jpg", media_type="image", sha=sha)
    _missing(env, gone)
    assert _ingest(env, "ncf/same.jpg", media_type="image", sha=sha) == gone
    assert _row(env, gone)[0] is None


def test_a_file_whose_content_isnt_known_stays_the_clip_it_was(env):
    clip = _ingest(env, "ncf/nohash.jpg", media_type="image")
    assert _ingest(env, "ncf/nohash.jpg", media_type="image") == clip
    known = _ingest(env, "ncf/nohash.jpg", media_type="image", sha=_sha())
    assert known == clip  # its first hash: the same file, now known


def test_unarchiving_onto_a_path_another_clip_holds_is_skipped(env):
    archived = _ingest(env, "ncf/held.jpg", media_type="image", sha=_sha())
    assert _archive(env, asset_ids=[archived]).status_code == 200
    holder = _ingest(env, "ncf/held.jpg", media_type="image", sha=_sha())  # another file there now
    assert holder != archived
    r = _unarchive(env, asset_ids=[archived])
    assert r.status_code == 200 and r.json()["skipped"] == [archived]
    assert _row(env, archived)[1] == "archived" and _row(env, holder)[0] is None


def test_restoring_from_the_trash_onto_a_path_another_clip_holds_is_skipped(env):
    client, headers, *_ = env
    trashed = _ingest(env, "ncf/binned.jpg", media_type="image", sha=_sha())
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [trashed], "reason": "user"}, headers=headers)
    assert r.status_code in (200, 204), r.text
    holder = _ingest(env, "ncf/binned.jpg", media_type="image", sha=_sha())
    r = client.post("/v1/assets/restore", json={"asset_ids": [trashed]}, headers=headers)
    assert r.status_code == 200 and r.json()["skipped"] == [trashed]
    assert _row(env, trashed)[1] == "user" and _row(env, holder)[0] is None


def test_restoring_one_clip_onto_a_held_path_says_why(env):
    client, headers, *_ = env
    trashed = _ingest(env, "ncf/one-binned.jpg", media_type="image", sha=_sha())
    client.request("DELETE", "/v1/assets", json={"asset_ids": [trashed], "reason": "user"}, headers=headers)
    _ingest(env, "ncf/one-binned.jpg", media_type="image", sha=_sha())
    r = client.post(f"/v1/assets/{trashed}/restore", headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "path_taken", r.text
    assert _row(env, trashed)[1] == "user"


# Review of PR 8: a trashed old version, follow-moves off, a copy made before
# the overwrite, two clips at one path restored together, the other ingest.


def _cli(env):
    from src.client.cli.client import LumiverbClient

    client, headers, *_ = env
    client.headers.update(headers)
    c = LumiverbClient(base_url="http://testserver", token="x")
    c._client = client
    return c


def _scan(env, tmp_path, rel_paths: list[str], prefix: str):
    """A scan of the library with these files on disk, unchanged for an hour."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock, patch

    from rich.console import Console

    from src.client.cli.scan import run_scan

    _, _, library_id, *_ = env
    root = tmp_path / "lib"
    for rp in rel_paths:
        (root / rp).parent.mkdir(parents=True, exist_ok=True)
        (root / rp).write_bytes(b"x")
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    local = [{"rel_path": rp, "file_size": 1000, "file_mtime": hour_ago, "media_type": "image", "ext": ".jpg"}
             for rp in rel_paths]
    with (
        patch("src.client.cli.scan.reachable_root", return_value=root),
        patch("src.client.cli.scan._walk_library", return_value=local),
        patch("src.client.cli.scan._scan_one"),
        patch("src.client.cli.scan.ProxyCache", MagicMock()),
        patch("src.client.cli.scan._populate_cache_for_unchanged"),
        patch("src.client.cli.scan.compute_sha256", return_value="f" * 64),
    ):
        return run_scan(_cli(env), {"library_id": library_id, "root_path": str(root)}, path_prefix=prefix,
                        console=Console(width=200))


def test_trashing_the_old_version_leaves_the_clip_on_disk_alone(env, tmp_path):
    client, headers, library_id, *_ = env
    path = "ncr-trash/a.jpg"
    old = _ingest(env, path, media_type="image", sha=_sha())
    new = _ingest(env, path, media_type="image", sha=_sha())
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [old], "reason": "user"}, headers=headers)
    assert r.status_code == 200, r.text
    items = client.get(f"/v1/libraries/{library_id}/ignored-paths", headers=headers).json()["items"]
    assert path not in {i["rel_path"] for i in items}  # the file there is the new clip's
    _scan(env, tmp_path, [path], "ncr-trash")
    assert _row(env, new)[0] is None  # on disk, so not missing


def test_a_file_on_disk_at_an_ignored_path_is_never_missing(env, tmp_path):
    # Even if the ignore list names a path a clip in sight holds, the scan
    # counts the file there as on disk.
    from unittest.mock import patch

    path = "ncr-ignored/a.jpg"
    clip = _ingest(env, path, media_type="image", sha=_sha())
    with patch("src.client.cli.scan._fetch_ignored_paths", return_value={path: None}):
        _scan(env, tmp_path, [path], "ncr-ignored")
    assert _row(env, clip)[0] is None


def test_deleting_the_old_version_for_good_doesnt_stop_the_next_file(env):
    client, headers, *_ = env
    path = "ncr-emptied/a.jpg"
    old = _ingest(env, path, media_type="image", sha=_sha())
    new = _ingest(env, path, media_type="image", sha=_sha())
    client.request("DELETE", "/v1/assets", json={"asset_ids": [old], "reason": "user"}, headers=headers)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [old], "remove_from_projects": True},
                       headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    third = _ingest(env, path, media_type="image", sha=_sha())  # the file changed again
    assert third not in (old, new)
    assert _row(env, new)[1] == "missing" and _row(env, third)[0] is None


def test_a_trashed_file_back_at_its_path_stays_in_the_trash(env):
    # The clip there goes missing all the same: its file isn't there now.
    import io
    import json

    from PIL import Image

    from tests.machine_lineage import ingest_made

    client, headers, library_id, *_ = env
    path = "ncr-refused/a.jpg"
    trashed_sha = _sha()
    trashed = _ingest(env, path, media_type="image", sha=trashed_sha)
    client.request("DELETE", "/v1/assets", json={"asset_ids": [trashed], "reason": "user"}, headers=headers)
    live = _ingest(env, path, media_type="image", sha=_sha())  # a different file there since
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post("/v1/ingest", headers=headers, files={"proxy": ("p.jpg", buf, "image/jpeg")},
                    data={"library_id": library_id, "rel_path": path, "file_size": "1000", "media_type": "image",
                          "exif": json.dumps({"sha256": trashed_sha}), "lineage": ingest_made(trashed_sha)})
    assert r.status_code == 409  # a person's trash stands
    assert _row(env, trashed)[1] == "user" and _row(env, live)[1] == "missing"
    r = client.post(f"/v1/assets/{trashed}/restore", headers=headers)  # and the path is free for it
    assert r.status_code in (200, 204), r.text
    assert _row(env, trashed)[0] is None


def test_without_following_moves_the_old_content_back_at_its_path_is_its_clip(env):
    client, headers, *_ = env
    assert client.patch("/v1/tenant/settings", json={"follow_moves": False}, headers=headers).status_code == 200
    try:
        first_sha = _sha()
        first = _ingest(env, "ncr-nofollow/a.jpg", media_type="image", sha=first_sha)
        second = _ingest(env, "ncr-nofollow/a.jpg", media_type="image", sha=_sha())
        assert _ingest(env, "ncr-nofollow/a.jpg", media_type="image", sha=first_sha) == first
        assert _row(env, first)[0] is None and _row(env, second)[1] == "missing"
    finally:
        client.patch("/v1/tenant/settings", json={"follow_moves": True}, headers=headers)


def test_a_copy_made_before_the_overwrite_takes_the_old_clip(env):
    sha = _sha()
    original = _ingest(env, "ncr-copy/a.jpg", media_type="image", sha=sha)
    _note(env, original, "keep")
    copy = _ingest(env, "ncr-copy/copy-of-a.jpg", media_type="image", sha=sha)  # a scan sends new files first
    _ingest(env, "ncr-copy/a.jpg", media_type="image", sha=_sha())  # then the overwritten original
    assert _row(env, original)[0] is None and _row(env, original)[2] == "ncr-copy/copy-of-a.jpg"
    from tests.test_delete_missing import _exists

    assert not _exists(env, copy)  # the copy went: the clip took its place


def test_unarchiving_two_clips_at_one_path_brings_one_back(env):
    a = _ingest(env, "ncr-two/a.jpg", media_type="image", sha=_sha())
    assert _archive(env, asset_ids=[a]).status_code == 200
    b = _ingest(env, "ncr-two/a.jpg", media_type="image", sha=_sha())
    assert b != a and _archive(env, asset_ids=[b]).status_code == 200
    r = _unarchive(env, asset_ids=[a, b])
    assert r.status_code == 200, r.text
    assert len(r.json()["skipped"]) == 1
    assert sorted(_row(env, x)[0] is None for x in (a, b)) == [False, True]


def test_the_other_ingest_refuses_a_different_file(env):
    import io
    import json

    from PIL import Image

    from tests.machine_lineage import ingest_made

    client, headers, *_ = env
    clip = _ingest(env, "ncr-other/a.jpg", media_type="image", sha=_sha())
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    buf.seek(0)
    other = _sha()
    r = client.post(f"/v1/assets/{clip}/ingest", headers=headers, files={"proxy": ("p.jpg", buf, "image/jpeg")},
                    data={"exif": json.dumps({"sha256": other}), "lineage": ingest_made(other)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "different_file", r.text


def test_a_path_remembers_each_file_deleted_there_for_good(env):
    client, headers, library_id, *_ = env
    path = "ncr-versions/a.jpg"
    first_sha, second_sha = _sha(), _sha()
    first = _ingest(env, path, media_type="image", sha=first_sha)
    second = _ingest(env, path, media_type="image", sha=second_sha)  # first goes missing
    for clip in (first, second):
        client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "user"}, headers=headers)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [first, second]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 2, r.text
    items = client.get(f"/v1/libraries/{library_id}/ignored-paths", headers=headers).json()["items"]
    item = next(i for i in items if i["rel_path"] == path)
    assert item["reason"] == "emptied"
    assert sorted(f["sha256"] for f in item["files"]) == sorted([first_sha, second_sha])
    assert all(f["file_size"] == 1000 for f in item["files"])
    for sha in (first_sha, second_sha):
        with pytest.raises(AssertionError, match="409"):
            _ingest(env, path, media_type="image", sha=sha)
    third = _ingest(env, path, media_type="image", sha=_sha())  # another file: a new clip
    assert _row(env, third)[0] is None



# Review round 2: a copy and the overwritten original ingested at once; a
# handed-over clip's search; races on a path; a library back after a path was taken.


def _post(client, env, rel_path: str, sha: str):
    import io
    import json

    from PIL import Image

    from tests.machine_lineage import ingest_made

    _, headers, library_id, *_ = env
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    buf.seek(0)
    return client.post("/v1/ingest", headers=headers, files={"proxy": ("p.jpg", buf, "image/jpeg")},
                       data={"library_id": library_id, "rel_path": rel_path, "file_size": "1000",
                             "media_type": "image", "exif": json.dumps({"sha256": sha}), "lineage": ingest_made(sha)})


def test_a_copy_and_the_overwrite_ingested_at_once_keep_the_clip(env):
    # The copy's ingest looks for a missing clip with its content before the
    # original is archived; the original's handover looks for the copy before
    # the copy has its content. Whichever commits second moves the clip.
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from src.server.api.main import app
    from src.server.repository.tenant import AssetRepository

    client, *_ = env
    sha = _sha()
    original = _ingest(env, "ncr-race/a.jpg", media_type="image", sha=sha)
    _note(env, original, "keep me")
    real = AssetRepository.find_missing_by_sha
    fired = []

    def find_missing_by_sha(self, library_id, s):
        found = real(self, library_id, s)  # the original is still in sight: nothing
        if s == sha and not fired:
            fired.append(1)
            r = _post(TestClient(app), env, "ncr-race/a.jpg", _sha())  # meanwhile the overwrite runs to the end
            assert r.status_code == 200, r.text
        return found

    with patch.object(AssetRepository, "find_missing_by_sha", find_missing_by_sha):
        r = _post(client, env, "ncr-race/copy-of-a.jpg", sha)
    assert fired and r.status_code == 200, r.text
    assert r.json()["asset_id"] == original and r.json()["created"] is False
    assert _row(env, original)[0] is None and _row(env, original)[2] == "ncr-race/copy-of-a.jpg"


def test_a_handed_over_clip_keeps_its_transcript_in_search(env):
    from unittest.mock import patch

    from sqlalchemy import text

    from src.server.search.quickwit_client import QuickwitClient
    from tests.test_archive_by_hand import _db

    sha = _sha()
    original = _ingest(env, "ncr-ts/a.mp4", media_type="video", sha=sha)
    with _db(env) as s:
        s.execute(text("UPDATE assets SET transcript_srt = :t WHERE asset_id = :a"),
                  {"t": "1\n00:00:00,000 --> 00:00:01,000\nhello\n", "a": original})
        s.commit()
    _ingest(env, "ncr-ts/copy.mp4", media_type="video", sha=sha)
    calls: list[tuple[str, tuple]] = []

    def index(tenant_id, asset):
        calls.append(("index", (asset.asset_id,)))

    def delete(self, tenant_id, asset_ids):
        calls.append(("delete", tuple(asset_ids)))

    with (patch("src.server.search.sync.index_transcript_segments", index),
          patch.object(QuickwitClient, "delete_tenant_documents_by_asset_ids", delete)):
        _ingest(env, "ncr-ts/a.mp4", media_type="video", sha=_sha())  # overwritten
    assert _row(env, original)[2] == "ncr-ts/copy.mp4"  # handed over
    about = [c[0] for c in calls if original in c[1]]
    assert about and about[-1] == "index", about


def _someone_else_takes(env, rel_path: str):
    from unittest.mock import patch

    from src.server.models.tenant import Asset
    from src.server.repository.tenant import AssetRepository
    from tests.test_archive_by_hand import _db

    real = AssetRepository.is_ignored

    def is_ignored(self, library_id, path, sha=None):
        with _db(env) as s:  # another ingest commits a clip at this path meanwhile
            s.add(Asset(asset_id="ast_other_" + _sha()[:8], library_id=library_id, rel_path=path, file_size=1,
                        media_type="image", status="pending", availability="online"))
            s.commit()
        return real(self, library_id, path, sha)

    return patch.object(AssetRepository, "is_ignored", is_ignored)


def test_a_new_clip_racing_another_at_its_path_is_409(env):
    client, *_ = env
    with _someone_else_takes(env, "ncr-race2/new.jpg"):
        r = _post(client, env, "ncr-race2/new.jpg", _sha())
    assert r.status_code == 409, r.text


def test_a_missing_clip_moving_onto_a_path_just_taken_is_409(env):
    client, *_ = env
    sha = _sha()
    gone = _ingest(env, "ncr-race2/old.jpg", media_type="image", sha=sha)
    _missing(env, gone)
    with _someone_else_takes(env, "ncr-race2/moved.jpg"):
        r = _post(client, env, "ncr-race2/moved.jpg", sha)
    assert r.status_code == 409, r.text
    assert _row(env, gone)[1] == "missing"


def test_a_library_back_from_the_trash_onto_a_taken_path_archives_that_clip(env):
    from sqlalchemy import text

    from tests.test_archive_by_hand import _db

    client, headers, *rest = env
    lib = client.post("/v1/libraries", json={"name": "NcrBack", "root_path": "/tmp/ncr-back"},
                      headers=headers).json()["library_id"]
    clip = _ingest((client, headers, lib, *rest[1:]), "a.jpg", media_type="image", sha=_sha())
    assert client.request("DELETE", f"/v1/libraries/{lib}", json={}, headers=headers).status_code == 204
    with _db(env) as s:  # an ingest that got past the library check as it went
        s.execute(text("INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, status,"
                       " availability, created_at, updated_at) VALUES ('ast_ncr_taken', :lib, 'a.jpg', 1, 'image', 'pending',"
                       " 'online', now(), now())"), {"lib": lib})
        s.commit()
    assert client.post(f"/v1/libraries/{lib}/restore", headers=headers).status_code == 200
    assert _row(env, clip)[1] == "missing" and _row(env, "ast_ncr_taken")[0] is None
