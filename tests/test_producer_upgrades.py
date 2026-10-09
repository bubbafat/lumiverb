# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Upgrades: an admin approves making stale artifacts again (ADR-016 phase 3, piece 5).

"NNN clips were made with the old model or settings. Upgrade them now?"
Yes approves exactly the clips stale in that scope then. The reconciler
hands them to the brain's worker (only a caller that asks: upgrades=true),
marked, after anything missing, until its producer makes each again.
Clips with a person's edits need a choice: keep the edits on top, replace
them (moved to history as each is made again) or skip those clips. Producers whose output must
come from one model (CLIP, faces) go all at once, with a confirmation that
names the count. A settings change before it finishes drops the rest, so
the new change is asked about on its own. The API requires every answer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _counts, _library
from tests.test_reconciler import _due as _due_for_others

OLD = "0ld5e771n65"  # a settings hash nothing makes now


def _made_old(env, clip: str, sha: str, artifact: str = "vision") -> None:
    """The artifact exists, made with settings that aren't current."""
    client, headers, *_ = env
    lineage = {"producer": artifact, "version": "1", "settings_hash": OLD, "source_sha256": sha}
    if artifact == "vision":
        r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "old words",
                                                            "lineage": lineage}, headers=headers)
    elif artifact == "ocr":
        r = client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "OLD SIGN", "lineage": lineage}, headers=headers)
    else:
        raise AssertionError(artifact)
    assert r.status_code == 200, r.text


def _made_now(env, clip: str, sha: str, artifact: str = "vision") -> None:
    """What the worker does with an item: makes it again with today's settings."""
    client, headers, *_ = env
    if artifact == "vision":
        r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "new words",
                                                            "lineage": _want(env, "vision", sha)}, headers=headers)
    else:
        r = client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "NEW SIGN", "lineage": _want(env, "ocr", sha)},
                        headers=headers)
    assert r.status_code == 200, r.text


def _stale_clip(lib, name: str, artifact: str = "vision") -> tuple[str, str]:
    sha = _sha()
    clip = _ingest_with(lib, name, sha, None)
    _made_old(lib, clip, sha, artifact)
    return clip, sha


def _page(env, flag: str) -> list[dict]:
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/page", params={"library_id": library_id, flag: "true", "upgrades": "true"},
                   headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _due(env, flag: str) -> list[str]:
    """What the brain's worker does for one step, in its order: what's missing, then an upgrade's."""
    return [i["asset_id"] for i in sorted(_page(env, flag), key=lambda i: i["upgrade"])]


def _summary(env, upgrades: bool = True) -> dict:
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/repair-summary", params={"library_id": library_id, "upgrades": str(upgrades).lower()},
                   headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _upgrade(env, artifact: str = "vision", headers: dict | None = None, **body):
    client, own, *_ = env
    return client.post(f"/v1/producers/{artifact}/upgrade", json=body, headers=headers or own)


def _producer(env, artifact: str = "vision", **params) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}[artifact]


def _sha_of(env, clip: str) -> str:
    with _db(env) as s:
        return s.execute(text("SELECT sha256 FROM assets WHERE asset_id = :a"), {"a": clip}).scalar()


def _mine(env, artifact: str = "vision") -> list[dict]:
    """The artifact's upgrades in this test's library (others' live in the same tenant)."""
    return [u for u in _producer(env, artifact)["upgrades"] if u["scope"].get("id") == env[2]]


def _error(r) -> dict:
    return r.json()["error"]


def _correct(env, clip: str, **fields):
    client, headers, *_ = env
    r = client.patch(f"/v1/assets/{clip}/corrections", json=fields, headers=headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Approving hands the stale clips to the worker, until each is made again
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_approving_an_upgrade_hands_out_the_stale_clips_until_each_is_redone(env):
    lib = _library(env, "UpgBasic")
    _, library_id = lib[1], lib[2]
    a, sha_a = _stale_clip(lib, "a.jpg")
    b, sha_b = _stale_clip(lib, "b.jpg")
    current = _ingest_with(lib, "c.jpg", (sha_c := _sha()), None)
    _made_now(lib, current, sha_c)
    assert _counts(lib, "vision")["stale"] == 2
    assert _due(lib, "missing_vision") == []  # stale waits for approval

    r = _upgrade(lib, library_id=library_id)
    assert r.status_code == 200, r.text
    assert r.json()["upgrading"] == 2
    assert sorted(_due(lib, "missing_vision")) == sorted([a, b])
    assert _summary(lib)["missing_vision"] == 2

    assert [(u["scope"]["kind"], u["remaining"], u["total"]) for u in _mine(lib)] == [("library", 2, 2)]

    _made_now(lib, a, sha_a)
    assert _due(lib, "missing_vision") == [b]
    assert _mine(lib)[0]["remaining"] == 1
    _made_now(lib, b, sha_b)
    assert _due(lib, "missing_vision") == []
    assert _counts(lib, "vision")["stale"] == 0
    assert _mine(lib) == []  # finished: it goes away


@pytest.mark.slow
def test_missing_clips_are_handed_out_before_upgrades(env):
    lib = _library(env, "UpgMissingFirst")
    stale, _ = _stale_clip(lib, "old.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    fresh = _ingest_with(lib, "new.jpg", (sha := _sha()), None)  # nothing made yet

    assert {i["asset_id"]: i["upgrade"] for i in _page(lib, "missing_vision")} == {fresh: False, stale: True}
    assert _due(lib, "missing_vision") == [fresh, stale]
    assert _summary(lib)["missing_vision"] == 2  # both are work for the step
    _made_now(lib, fresh, sha)
    assert _due(lib, "missing_vision") == [stale]


@pytest.mark.slow
def test_only_a_caller_that_asks_gets_upgrade_work(env):
    """The macOS app pages missing_* too, and can't make an upgrade's artifact
    (it doesn't say how it made anything): it gets what's missing only."""
    lib = _library(env, "UpgOptIn")
    stale, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    assert _due_for_others(lib, "missing_vision") == []
    assert _summary(lib, upgrades=False)["missing_vision"] == 0
    assert _due(lib, "missing_vision") == [stale]
    assert _summary(lib)["missing_vision"] == 1


@pytest.mark.slow
def test_a_write_that_doesnt_say_how_it_was_made_doesnt_finish_an_upgrade(env):
    lib = _library(env, "UpgUnknownWrite")
    client, headers, *_ = lib
    clip, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "apple", "description": "from the Mac"},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "missing_vision") == [clip]
    assert _mine(lib)[0]["remaining"] == 1


@pytest.mark.slow
def test_made_again_with_other_settings_isnt_handed_out_again_but_is_still_stale(env):
    """A worker that read the settings before they changed: its write counts
    as made again (no loop), and the upgrade says it's still stale."""
    lib = _library(env, "UpgOtherHash")
    client, headers, *_ = lib
    a, sha_a = _stale_clip(lib, "a.jpg")
    b, _ = _stale_clip(lib, "b.jpg")
    assert _upgrade(lib, library_id=lib[2]).json()["upgrading"] == 2
    other = {"producer": "vision", "version": "1", "settings_hash": "n0t-th3-t4rg3t", "source_sha256": sha_a}
    r = client.post(f"/v1/assets/{a}/vision", json={"model_id": "m", "description": "x", "lineage": other},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "missing_vision") == [b]
    u = _mine(lib)[0]
    assert (u["remaining"], u["still_stale"]) == (1, 1)
    assert _counts(lib, "vision")["stale"] == 2


@pytest.mark.slow
def test_an_upgrade_is_the_clips_stale_when_it_was_approved(env):
    lib = _library(env, "UpgSnapshot")
    first, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).json()["upgrading"] == 1
    later, _ = _stale_clip(lib, "b.jpg")  # stale after the approval: asked about on its own
    assert _due(lib, "missing_vision") == [first]
    assert _counts(lib, "vision")["stale"] == 2


@pytest.mark.slow
def test_an_artifact_nobody_said_how_was_made_is_upgraded_too(env):
    lib = _library(env, "UpgUnknown")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "from the Mac"},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert _upgrade(lib, library_id=lib[2]).json()["upgrading"] == 1
    assert _due(lib, "missing_vision") == [clip]
    _made_now(lib, clip, sha)
    assert _due(lib, "missing_vision") == []


@pytest.mark.slow
def test_a_settings_change_before_it_finishes_drops_the_rest(env):
    lib = _library(env, "UpgSettings")
    clip, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    with _db(env) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('producer.vision', :v, now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": '{"temperature": 0.4}'})
        s.commit()
    try:
        assert _due(lib, "missing_vision") == []
        assert _summary(lib)["missing_vision"] == 0
        assert _mine(lib) == []
        assert _counts(lib, "vision")["stale"] == 1  # still stale; the new change is asked about
    finally:
        with _db(env) as s:
            s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.vision'"))
            s.commit()


@pytest.mark.slow
def test_a_failed_redo_waits_its_turn_and_stays_in_the_upgrade(env):
    lib = _library(env, "UpgFail")
    client, headers, *_ = lib
    clip, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    r = client.post("/v1/producers/failures", json={"items": [{"asset_id": clip, "artifact": "vision",
                                                                "error": "the machine said no"}]}, headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "missing_vision") == []
    assert _mine(lib)[0]["remaining"] == 1
    with _db(env) as s:
        s.execute(text("UPDATE artifact_lineage SET retry_at = now() - interval '1 minute'"
                       " WHERE asset_id = :a AND artifact = 'vision'"), {"a": clip})
        s.commit()
    assert _due(lib, "missing_vision") == [clip]


@pytest.mark.slow
def test_a_clip_trashed_after_approval_isnt_handed_out(env):
    lib = _library(env, "UpgTrash")
    client, headers, *_ = lib
    clip, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "user"}, headers=headers)
    assert r.status_code < 400, r.text
    assert _due(lib, "missing_vision") == []


# ---------------------------------------------------------------------------
# Scope: everything, one library, one project
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_narrowed_to_one_library(env):
    a_lib = _library(env, "UpgLibA")
    b_lib = _library(env, "UpgLibB")
    a, _ = _stale_clip(a_lib, "a.jpg")
    b, _ = _stale_clip(b_lib, "b.jpg")
    assert _upgrade(a_lib, library_id=a_lib[2]).json()["upgrading"] == 1
    assert _due(a_lib, "missing_vision") == [a]
    assert _due(b_lib, "missing_vision") == []
    # Another library: a second upgrade beside the first.
    assert _upgrade(b_lib, library_id=b_lib[2]).json()["upgrading"] == 1
    assert _due(b_lib, "missing_vision") == [b]
    assert len(_producer(a_lib)["upgrades"]) >= 2


@pytest.mark.slow
def test_narrowed_to_one_project(env):
    lib = _library(env, "UpgProject")
    client, headers, *_ = lib
    inside, _ = _stale_clip(lib, "in.jpg")
    outside, _ = _stale_clip(lib, "out.jpg")
    r = client.post("/v1/projects", json={"name": "Upgrade me"}, headers=headers)
    assert r.status_code == 201, r.text
    project_id = r.json()["project_id"]
    r = client.post(f"/v1/projects/{project_id}/assets", json={"asset_ids": [inside]}, headers=headers)
    assert r.status_code == 200, r.text

    p = _producer(lib, project_id=project_id)
    assert p["counts"]["stale"] == 1
    r = _upgrade(lib, project_id=project_id)
    assert r.status_code == 200, r.text
    assert r.json()["upgrading"] == 1
    assert _due(lib, "missing_vision") == [inside]
    up = _producer(lib)["upgrades"]
    assert any(u["scope"] == {"kind": "project", "id": project_id, "name": "Upgrade me"} for u in up)


@pytest.mark.slow
def test_approving_the_same_scope_again_replaces_it(env):
    lib = _library(env, "UpgAgain")
    _stale_clip(lib, "a.jpg")
    first = _upgrade(lib, library_id=lib[2]).json()["upgrade_id"]
    _stale_clip(lib, "b.jpg")
    second = _upgrade(lib, library_id=lib[2]).json()
    assert second["upgrading"] == 2 and second["upgrade_id"] != first
    mine = [u for u in _producer(lib)["upgrades"] if u["scope"].get("id") == lib[2]]
    assert [u["upgrade_id"] for u in mine] == [second["upgrade_id"]]


@pytest.mark.slow
def test_a_scope_that_doesnt_exist(env):
    lib = _library(env, "UpgNowhere")
    _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id="lib_nope").status_code == 404
    assert _upgrade(lib, project_id="col_nope").status_code == 404
    r = _upgrade(lib, library_id=lib[2], project_id="col_nope")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# A person's edits: keep, replace (kept in history) or skip
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_clips_with_edits_need_a_choice(env):
    lib = _library(env, "UpgEdits")
    edited, _ = _stale_clip(lib, "edited.jpg")
    plain, _ = _stale_clip(lib, "plain.jpg")
    _correct(lib, edited, description="My words")
    assert _producer(lib, library_id=lib[2])["edited"] == 1

    r = _upgrade(lib, library_id=lib[2])
    assert r.status_code == 409
    err = _error(r)
    assert err["code"] == "edited_clips"
    assert err["details"]["stale"] == 2 and err["details"]["edited"] == 1
    assert err["details"]["choices"] == ["keep", "replace", "skip"]
    assert _due(lib, "missing_vision") == []  # nothing approved

    r = _upgrade(lib, library_id=lib[2], edits="skip")
    assert r.status_code == 200, r.text
    assert r.json()["upgrading"] == 1 and r.json()["skipped_edited"] == 1
    assert _due(lib, "missing_vision") == [plain]


@pytest.mark.slow
def test_keep_my_edits_redoes_underneath_them(env):
    lib = _library(env, "UpgKeep")
    client, headers, *_ = lib
    clip, sha = _stale_clip(lib, "a.jpg")
    _correct(lib, clip, description="My words", tags=["mine"])
    assert _upgrade(lib, library_id=lib[2], edits="keep").json()["upgrading"] == 1
    assert _due(lib, "missing_vision") == [clip]
    _made_now(lib, clip, sha)
    asset = client.get(f"/v1/assets/{clip}", headers=headers).json()
    assert asset["ai_description"] == "My words"
    assert "mine" in asset["ai_tags"]


@pytest.mark.slow
def _history(env, clip: str) -> list[tuple]:
    with _db(env) as s:
        return [tuple(r) for r in s.execute(text(
            "SELECT field, value, reason, edited_by IS NOT NULL FROM correction_history WHERE asset_id = :a"
            " ORDER BY field"), {"a": clip}).all()]


@pytest.mark.slow
def test_replace_my_edits_moves_them_to_history_as_each_is_made_again(env):
    lib = _library(env, "UpgReplace")
    client, headers, *_ = lib
    clip, sha = _stale_clip(lib, "a.jpg")
    _correct(lib, clip, description="My words", tags=["mine"], ocr_text="MY SIGN")
    r = _upgrade(lib, library_id=lib[2], edits="replace")
    assert r.status_code == 200, r.text
    assert r.json()["edits_to_replace"] == 1

    # Until it's made again, the edits show.
    asset = client.get(f"/v1/assets/{clip}", headers=headers).json()
    assert asset["ai_description"] == "My words" and "mine" in asset["ai_tags"]
    assert _history(lib, clip) == []

    assert _due(lib, "missing_vision") == [clip]
    _made_now(lib, clip, sha)
    asset = client.get(f"/v1/assets/{clip}", headers=headers).json()
    assert asset["ai_description"] == "new words"
    assert "mine" not in asset["ai_tags"]
    assert asset["ocr_text"] == "MY SIGN"  # another producer's edit: untouched
    assert _history(lib, clip) == [
        ("description", "My words", "upgrade:vision", True),
        ("tags", {"added": ["mine"], "removed": []}, "upgrade:vision", True),
    ]
    with _db(env) as s:
        who = s.execute(text("SELECT updated_by FROM asset_corrections WHERE asset_id = :a"), {"a": clip}).scalar()
    assert who is not None  # the OCR edit is still the person's

    # An edit made after it was made again stays, whatever is written next.
    _correct(lib, clip, description="Mine again")
    _made_now(lib, clip, sha)
    assert client.get(f"/v1/assets/{clip}", headers=headers).json()["ai_description"] == "Mine again"


@pytest.mark.slow
def test_stopping_an_upgrade_that_replaces_edits_leaves_them(env):
    lib = _library(env, "UpgReplaceStop")
    client, headers, *_ = lib
    clip, sha = _stale_clip(lib, "a.jpg")
    _correct(lib, clip, description="My words")
    _upgrade(lib, library_id=lib[2], edits="replace")
    assert client.delete("/v1/producers/vision/upgrade", headers=headers).status_code == 204
    _made_now(lib, clip, sha)  # made again some other way: not the upgrade's doing
    assert client.get(f"/v1/assets/{clip}", headers=headers).json()["ai_description"] == "My words"
    assert _history(lib, clip) == []


@pytest.mark.slow
def test_a_write_with_other_settings_doesnt_replace_edits(env):
    lib = _library(env, "UpgReplaceOther")
    client, headers, *_ = lib
    clip, sha = _stale_clip(lib, "a.jpg")
    _correct(lib, clip, description="My words")
    _upgrade(lib, library_id=lib[2], edits="replace")
    _made_old(lib, clip, sha)
    assert client.get(f"/v1/assets/{clip}", headers=headers).json()["ai_description"] == "My words"


@pytest.mark.slow
def test_skipping_every_stale_clip_replaces_an_earlier_approval_with_nothing(env):
    lib = _library(env, "UpgSkipAll")
    clip, _ = _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200
    _correct(lib, clip, description="My words")
    r = _upgrade(lib, library_id=lib[2], edits="skip")
    assert r.status_code == 200, r.text
    assert (r.json()["upgrade_id"], r.json()["upgrading"], r.json()["skipped_edited"]) == (None, 0, 1)
    assert _mine(lib) == []
    assert _due(lib, "missing_vision") == []


@pytest.mark.slow
def test_ocr_edits_are_asked_about_for_ocr_only(env):
    lib = _library(env, "UpgOcrEdits")
    clip, _ = _stale_clip(lib, "a.jpg", artifact="ocr")
    _correct(lib, clip, ocr_text="MY SIGN")
    r = _upgrade(lib, "ocr", library_id=lib[2])
    assert r.status_code == 409 and _error(r)["code"] == "edited_clips"
    assert _upgrade(lib, "ocr", library_id=lib[2], edits="replace").status_code == 200
    assert _due(lib, "missing_ocr") == [clip]
    _made_now(lib, clip, _sha_of(lib, clip), "ocr")
    assert [(f, v) for f, v, *_ in _history(lib, clip)] == [("ocr_text", "MY SIGN")]


@pytest.mark.slow
def test_edits_that_dont_matter_are_ignored(env):
    """No clip in scope has edits: no question, whatever edits says."""
    lib = _library(env, "UpgNoEdits")
    _stale_clip(lib, "a.jpg")
    assert _upgrade(lib, library_id=lib[2]).status_code == 200


# ---------------------------------------------------------------------------
# CLIP and faces: all or nothing, confirmed with the count
# ---------------------------------------------------------------------------


def _embedded_old(env, clip: str, sha: str) -> None:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{clip}/embeddings", json={
        "model_id": "clip", "model_version": "ViT-B-32-openai", "vector": [0.1] * 512,
        "lineage": {"producer": "clip", "version": "1", "settings_hash": OLD, "source_sha256": sha}},
        headers=headers)
    assert r.status_code == 201, r.text


@pytest.mark.slow
def test_uniform_producers_go_all_at_once_with_a_confirmation(env):
    lib = _library(env, "UpgUniform")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _embedded_old(lib, clip, sha)
    p = _producer(lib, "clip")
    assert p["uniform"] is True and p["upgradable"] is True

    r = _upgrade(lib, "clip", library_id=lib[2])
    assert r.status_code == 422 and _error(r)["code"] == "all_or_nothing"

    r = _upgrade(lib, "clip")
    assert r.status_code == 409
    err = _error(r)
    assert err["code"] == "redo_everything"
    assert err["details"]["stale"] >= 1

    r = _upgrade(lib, "clip", confirm=True)
    assert r.status_code == 200, r.text
    assert clip in _due(lib, "missing_embeddings")
    assert _producer(lib, "clip")["upgrades"][0]["scope"] == {"kind": "all", "id": None, "name": None}


# ---------------------------------------------------------------------------
# What can't be upgraded, who may, and cancelling
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("artifact", ["scenes", "proxy", "video_preview"])
def test_some_producers_cant_be_made_again_in_place_yet(env, artifact):
    p = _producer(env, artifact)
    assert p["upgradable"] is False and p["why_not"]
    r = _upgrade(env, artifact)
    assert r.status_code == 409 and _error(r)["code"] == "cant_upgrade"


@pytest.mark.slow
def test_nothing_stale_nothing_to_approve(env):
    lib = _library(env, "UpgNothing")
    r = _upgrade(lib, library_id=lib[2])
    assert r.status_code == 409 and _error(r)["code"] == "nothing_stale"
    assert _upgrade(lib, "nope").status_code == 404


@pytest.mark.slow
def test_only_admins_approve_or_cancel(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "UpgRoles")
    client, *_ = lib
    _stale_clip(lib, "a.jpg")
    editor = _key_with_role(env, "editor")
    viewer = _key_with_role(env, "viewer")
    assert _upgrade(lib, library_id=lib[2], headers=editor).status_code == 403
    assert client.delete("/v1/producers/vision/upgrade", headers=editor).status_code == 403
    r = client.get("/v1/producers", params={"library_id": lib[2]}, headers=viewer)
    assert r.status_code == 200  # everyone signed in sees the counts


@pytest.mark.slow
def test_cancelling_stops_handing_them_out(env):
    lib = _library(env, "UpgCancel")
    client, headers, *_ = lib
    clip, _ = _stale_clip(lib, "a.jpg")
    up = _upgrade(lib, library_id=lib[2]).json()
    assert _due(lib, "missing_vision") == [clip]
    r = client.delete("/v1/producers/vision/upgrade", params={"upgrade_id": up["upgrade_id"]}, headers=headers)
    assert r.status_code == 204, r.text
    assert _due(lib, "missing_vision") == []
    assert _counts(lib, "vision")["stale"] == 1
    assert client.delete("/v1/producers/vision/upgrade", params={"upgrade_id": "upg_nope"},
                         headers=headers).status_code == 404


@pytest.mark.slow
def test_the_listing_says_how_many_stale_clips_have_edits(env):
    lib = _library(env, "UpgListing")
    clip, _ = _stale_clip(lib, "a.jpg")
    _correct(lib, clip, tags=["mine"])
    p = _producer(lib, library_id=lib[2])
    assert p["counts"]["stale"] == 1 and p["edited"] == 1
    assert p["upgradable"] is True and p["why_not"] is None
    assert _producer(lib, "transcript", library_id=lib[2])["edited"] == 0


@pytest.mark.slow
def test_approval_is_recorded_with_who_and_when(env):
    lib = _library(env, "UpgWho")
    _stale_clip(lib, "a.jpg")
    _upgrade(lib, library_id=lib[2], edits="keep")
    u = [u for u in _producer(lib)["upgrades"] if u["scope"].get("id") == lib[2]][0]
    assert u["edits"] == "keep" and u["approved_at"]
    with _db(env) as s:
        who = s.execute(text("SELECT approved_by FROM producer_upgrades WHERE upgrade_id = :u"),
                        {"u": u["upgrade_id"]}).scalar()
    assert who  # the admin's user (or key) id


# ---------------------------------------------------------------------------
# Scene descriptions: a video is done once every scene is
# ---------------------------------------------------------------------------


def _video_with_scenes(lib, n: int = 2) -> tuple[str, str, list[str]]:
    """A found video whose scenes were described with old settings."""
    sha = _sha()
    vid = _ingest_with(lib, "v.mov", sha, None, media_type="video")
    old = {"producer": "scene-vision", "version": "1", "settings_hash": OLD}
    scenes = [f"scn_{vid[-10:]}_{i}" for i in range(n)]
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, video_indexed = true WHERE asset_id = :a"), {"a": vid})
        for i, scene_id in enumerate(scenes):
            s.execute(text(
                "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                " description, tags, lineage, created_at) VALUES (:s, :a, :i, :t, :t + 1000, :t, 'old words',"
                " '[]'::jsonb, CAST(:l AS jsonb), now())"), {"s": scene_id, "a": vid, "i": i, "t": i * 1000,
                                                            "l": __import__("json").dumps(old)})
        s.execute(text(
            "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
            " source_sha256, produced_at, outcome, attempts) VALUES (:a, 'scene_vision', 'scene-vision', '1', :h,"
            " :sha, now(), 'ok', 0)"), {"a": vid, "h": OLD, "sha": sha})
        s.commit()
    return vid, sha, scenes


def _describe_scene(env, scene_id: str, sha: str):
    client, headers, *_ = env
    r = client.patch(f"/v1/video/scenes/{scene_id}", json={
        "model_id": "m", "model_version": "1", "description": "new words", "tags": [],
        "lineage": _want(env, "scene_vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text


@pytest.mark.slow
def test_a_video_is_upgraded_once_every_scene_is_described_again(env):
    lib = _library(env, "UpgScenes")
    client, headers, *_ = lib
    vid, sha, (first, second) = _video_with_scenes(lib)
    assert _counts(lib, "scene_vision")["stale"] == 1
    assert _upgrade(lib, "scene_vision", library_id=lib[2]).json()["upgrading"] == 1
    assert _due(lib, "missing_scene_vision") == [vid]

    _describe_scene(lib, first, sha)
    # Half new, half old: not current, and still the upgrade's.
    assert _counts(lib, "scene_vision")["stale"] == 1
    assert _due(lib, "missing_scene_vision") == [vid]
    scenes = client.get(f"/v1/video/{vid}/scenes", headers=headers).json()["scenes"]
    assert [s["lineage"]["settings_hash"] for s in scenes] == [
        _want(lib, "scene_vision", sha)["settings_hash"], OLD]

    _describe_scene(lib, second, sha)
    assert _counts(lib, "scene_vision") == {"applicable": 1, "current": 1, "stale": 0, "missing": 0, "failing": 0}
    assert _due(lib, "missing_scene_vision") == []


# ---------------------------------------------------------------------------
# A new model stops upgrades to the old one, once asked
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_changing_the_model_asks_before_it_stops_upgrades(env):
    from tests.test_ai_machines import BRAIN, _machines

    lib = _library(env, "UpgModel")
    client, headers, *_ = lib
    for artifact in ("vision", "ocr", "scene_vision"):  # other tests' upgrades in this tenant
        client.delete(f"/v1/producers/{artifact}/upgrade", headers=headers)
    fake, _ = _machines({BRAIN: ("m1", "m2")})
    try:
        with fake:
            r = client.post("/v1/ai/machines", json={"name": "Brain", "api_url": BRAIN, "jobs": ["vision"],
                                                     "at_once": 2}, headers=headers)
            assert r.status_code == 201, r.text
            assert client.put("/v1/ai/jobs/vision", json={"model": "m1"}, headers=headers).status_code == 200
            _stale_clip(lib, "a.jpg")
            assert _upgrade(lib, library_id=lib[2]).status_code == 200

            r = client.put("/v1/ai/jobs/vision", json={"model": "m2"}, headers=headers)
            assert r.status_code == 409
            err = _error(r)
            assert err["code"] == "upgrades_stop"
            assert err["details"]["upgrades"] == [{"artifact": "vision", "title": "Descriptions and tags",
                                                   "remaining": 1}]
            assert client.get("/v1/ai", headers=headers).json()["jobs"][0]["model"] == "m1"  # nothing changed

            # The same model again stops nothing.
            assert client.put("/v1/ai/jobs/vision", json={"model": "m1"}, headers=headers).status_code == 200
            r = client.put("/v1/ai/jobs/vision", json={"model": "m2", "stop_upgrades": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert _mine(lib) == []
    finally:
        assert client.put("/v1/ai/jobs/vision", json={"model": ""}, headers=headers).status_code == 200
        for m in client.get("/v1/ai", headers=headers).json()["machines"]:
            client.delete(f"/v1/ai/machines/{m['machine_id']}?leave_jobs=true", headers=headers)


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_counts_for_a_project_need_an_editor_and_only_admins_see_who_approved(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "UpgWhoSees")
    client, headers, *_ = lib
    _stale_clip(lib, "a.jpg")
    _upgrade(lib, library_id=lib[2])
    project_id = client.post("/v1/projects", json={"name": "Seen", "visibility": "shared"},
                             headers=headers).json()["project_id"]
    viewer = _key_with_role(env, "viewer")
    editor = _key_with_role(env, "editor")
    assert client.get("/v1/producers", params={"project_id": project_id}, headers=viewer).status_code == 403
    assert client.get("/v1/producers", params={"project_id": project_id}, headers=editor).status_code == 200
    seen = client.get("/v1/producers", headers=viewer).json()["producers"]
    assert all(u["approved_by"] is None for p in seen for u in p["upgrades"])
    assert any(u["approved_by"] for u in _producer(lib)["upgrades"])
