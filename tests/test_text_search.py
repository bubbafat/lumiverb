# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Text search in PostgreSQL (ADR-019), end to end.

Clips are described, read, noted and transcribed through the API as the
producers and people do; the search sync writes their rows (inline, and the
upkeep sweep); GET /v1/query finds them. The real database, the real search
(src/server/search/text_search.py) and the real sync (src/server/search
/sync.py): nothing here stands in for either.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.server.models.tenant import VideoScene
from tests.clips import make_clip
from tests.derived import describer
from tests.machine_lineage import made
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow


@contextmanager
def _db(env):
    engine = create_engine(env[5])
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _library(env) -> str:
    client, headers, *_ = env
    name = f"Search {uuid.uuid4().hex[:8]}"
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/Volumes/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


def _clip(env, lib: str, rel_path: str, media_type: str = "image") -> str:
    return make_clip(env[5], lib, rel_path, media_type=media_type)


def _describe(env, clip: str, description: str, tags: list[str] | None = None) -> None:
    """As the vision producer saves a description (its row is written then, inline)."""
    client, headers, *_ = env
    r = client.post("/v1/assets/batch-vision", headers=headers, json={
        "items": [{"asset_id": clip, "model_id": "qwen3-vl:8b", "description": description, "tags": tags or []}],
        "lineage": made(describer(client, clip, headers))})
    assert r.status_code == 200 and r.json()["updated"] == 1, r.text


def _read(env, clip: str, ocr_text: str) -> None:
    """As the OCR producer saves the text read in a picture (the sweep writes the row)."""
    client, headers, *_ = env
    r = client.post("/v1/assets/batch-ocr", headers=headers, json={
        "items": [{"asset_id": clip, "ocr_text": ocr_text}], "model_id": "qwen3-vl:8b", "lineage": made("lumiverb.image.text")})
    assert r.status_code == 200, r.text


def _note(env, clip: str, note: str) -> None:
    client, headers, *_ = env
    r = client.put(f"/v1/assets/{clip}/note", json={"text": note}, headers=headers)
    assert r.status_code in (200, 204), r.text


def _transcribe(env, clip: str, srt: str) -> None:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{clip}/transcript", json={"srt": srt, "source": "manual", "language": "en"},
                    headers=headers)
    assert r.status_code == 200, r.text


def _sweep(env) -> dict:
    """The upkeep's search sync, as the timers run it."""
    client, headers, *_ = env
    r = client.post("/v1/upkeep/search-sync", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _search(env, lib: str, *terms: str, raw: bool = False):
    """What GET /v1/query finds for search terms in a library: the clips in
    order (raw: the whole response)."""
    client, headers, *_ = env
    f = "&".join(f"f=query:{quote(t)}" for t in terms)
    r = client.get(f"/v1/query?{f}&f=library:{lib}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json() if raw else [item["asset_id"] for item in r.json()["items"]]


def _scene(env, clip: str, start_ms: int, end_ms: int, description: str, tags: list[str]) -> str:
    """A scene a clip shows (as the scenes producer makes them), then described
    as the scene producer describes it (PATCH /v1/video/scenes/{id})."""
    client, headers, *_ = env
    scene_id = f"scn_{uuid.uuid4().hex[:12]}"
    with _db(env) as s:
        s.add(VideoScene(scene_id=scene_id, asset_id=clip, scene_index=start_ms // 1000, start_ms=start_ms,
                         end_ms=end_ms, rep_frame_ms=start_ms))
        s.commit()
    r = client.patch(f"/v1/video/scenes/{scene_id}", headers=headers, json={
        "model_id": "qwen3-vl:8b", "model_version": "1", "description": description, "tags": tags,
        "lineage": made("lumiverb.video.scene_descriptions")})
    assert r.status_code == 200, r.text
    return scene_id


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def test_a_described_clip_is_found_by_its_words_and_their_forms(env):
    lib = _library(env)
    dogs = _clip(env, lib, "2024/park/IMG_0001.jpg")
    cat = _clip(env, lib, "2024/park/IMG_0002.jpg")
    _describe(env, dogs, "Two dogs running on a sandy beach at sunset", ["dog", "beach"])
    _describe(env, cat, "A cat asleep on a sofa", ["cat"])

    assert _search(env, lib, "dog") == [dogs]  # "dogs" in the description, "dog" a tag: stemmed alike
    assert _search(env, lib, "runs") == [dogs]  # running, runs: one word
    assert _search(env, lib, "sofa") == [cat]
    assert _search(env, lib, "giraffe") == []
    result = _search(env, lib, "beach", raw=True)
    assert result["search_source"] == "postgres"
    assert result["items"][0]["search_context"]["snippet"] == "Two dogs running on a sandy beach at sunset"


def test_any_unquoted_word_matches_and_more_of_them_rank_higher(env):
    lib = _library(env)
    both = _clip(env, lib, "a.jpg")
    one = _clip(env, lib, "b.jpg")
    _describe(env, both, "A red barn in a snowy field")
    _describe(env, one, "A red car on a road")

    assert _search(env, lib, "red barn") == [both, one]


def test_a_quoted_phrase_must_match_in_order(env):
    lib = _library(env)
    phrase = _clip(env, lib, "a.jpg")
    apart = _clip(env, lib, "b.jpg")
    _describe(env, phrase, "Lots of negative space around a single tree")
    _describe(env, apart, "A space heater with a negative review sticker")

    assert _search(env, lib, '"negative space"') == [phrase]
    assert set(_search(env, lib, "negative space")) == {phrase, apart}


def test_accents_are_set_aside_both_ways_and_shown_as_written(env):
    # Robert, 2026-10-20: a landmark named with macrons couldn't be found by its own name.
    lib = _library(env)
    zurich = _clip(env, lib, "a.jpg")
    chateau = _clip(env, lib, "b.jpg")
    pasha = _clip(env, lib, "c.jpg")
    _describe(env, zurich, "The old town of Zürich at dusk")
    _describe(env, chateau, "Château Frontenac above the river")
    _read(env, pasha, "Kānī Pāshā")

    for typed, found in (("Zürich", [zurich]), ("zurich", [zurich]), ("ZÜRICH", [zurich]), ("château", [chateau]),
                         ("chateau", [chateau]), ("chât", [chateau]), ('"Kānī Pāshā"', [pasha]),
                         ("kani pasha", [pasha])):
        assert _search(env, lib, typed) == found, typed
    result = _search(env, lib, "zurich", raw=True)
    assert result["items"][0]["search_context"]["snippet"] == "The old town of Zürich at dusk", "shown as written"


def test_any_languages_letters_are_searched(env):
    lib = _library(env)
    moscow = _clip(env, lib, "a.jpg")
    athens = _clip(env, lib, "b.jpg")
    _describe(env, moscow, "Красная площадь, Москва")
    _note(env, athens, "Ακρόπολη της Αθήνας")

    assert _search(env, lib, "Москва") == [moscow]
    assert _search(env, lib, "Ακρόπολη") == [athens]


def test_a_hyphenated_word_is_its_parts_in_a_phrase_and_as_typed(env):
    lib = _library(env)
    airport = _clip(env, lib, "a.jpg")
    raleigh = _clip(env, lib, "b.jpg")
    _describe(env, airport, "John Wayne Airport-Orange County terminal")
    _describe(env, raleigh, "Raleigh-Durham International at night")

    assert _search(env, lib, '"John Wayne Airport"') == [airport], "a phrase ending at the hyphen"
    assert _search(env, lib, '"Airport-Orange County"') == [airport]
    assert _search(env, lib, '"Orange County"') == [airport]
    assert _search(env, lib, "Raleigh-Durham") == [raleigh]
    assert _search(env, lib, "durham") == [raleigh]


def test_a_word_matches_as_a_prefix_too_ranked_below_whole_words(env):
    lib = _library(env)
    exact = _clip(env, lib, "a.jpg")
    longer = _clip(env, lib, "b.jpg")
    _describe(env, exact, "The Disney castle at night")
    _describe(env, longer, "Fireworks over Disneyland")

    assert _search(env, lib, "disney") == [exact, longer]
    assert set(_search(env, lib, "dis")) == {exact, longer}  # three letters or more: a prefix too
    assert _search(env, lib, "di") == []  # fewer: whole words only


def test_descriptions_and_notes_weigh_more_than_text_read_or_paths(env):
    lib = _library(env)
    described = _clip(env, lib, "x/1.jpg")
    noted = _clip(env, lib, "x/2.jpg")
    read = _clip(env, lib, "x/3.jpg")
    named = _clip(env, lib, "card/4.jpg")
    _describe(env, described, "A christmas card on a table")
    _note(env, noted, "the card mum sent")
    _read(env, read, "CARD SALE 50% OFF")
    _sweep(env)

    found = _search(env, lib, "card")
    assert set(found[:2]) == {described, noted}
    assert found[2:] == [read, named]


def test_every_search_term_must_match_each_anywhere(env):
    lib = _library(env)
    both = _clip(env, lib, "trips/paris/a.jpg")
    one = _clip(env, lib, "trips/rome/b.jpg")
    _describe(env, both, "A bakery window")
    _describe(env, one, "A bakery window")

    assert _search(env, lib, "bakery", "paris") == [both]  # one in the description, one in the path


def test_a_clip_is_found_by_its_path_note_text_read_and_transcript(env):
    lib = _library(env)
    clip = _clip(env, lib, "Trips/Kyoto/DJI_20250809143945_0008_D.MP4", media_type="video")
    _note(env, clip, "ask Anna about the permit")
    _read(env, clip, "Fushimi Inari Taisha")
    _transcribe(env, clip, "1\n00:00:01,000 --> 00:00:03,000\nThe gates go on for miles\n")
    _sweep(env)

    for word in ("kyoto", "DJI_20250809143945", "permit", "inari", "miles"):
        assert _search(env, lib, word) == [clip], word


def test_a_paths_words_are_found_as_typed_whatever_their_stem(env):
    """A path is read as typed words are: "family" (stem "famili") finds a
    folder named Family, and "trips" a folder named trips."""
    lib = _library(env)
    clip = _clip(env, lib, "Family/City Trips/Happy days.jpg")
    _sweep(env)

    for word in ("family", "city", "trips", "trip", "happy", "days"):
        assert _search(env, lib, word) == [clip], word
    assert _search(env, lib, '"city trips"') == [clip]


def test_what_a_person_wrote_replaces_the_machines_words(env):
    client, headers, *_ = env
    lib = _library(env)
    clip = _clip(env, lib, "a.jpg")
    _describe(env, clip, "A horse in a field", ["horse"])
    r = client.patch(f"/v1/assets/{clip}/corrections", json={"description": "Our pony Biscuit", "tags": ["pony"]},
                     headers=headers)
    assert r.status_code == 200, r.text
    _sweep(env)

    assert _search(env, lib, "biscuit") == [clip]
    assert _search(env, lib, "horse") == []


def test_only_a_libraries_clips_are_searched_when_it_names_one(env):
    here, there = _library(env), _library(env)
    a = _clip(env, here, "a.jpg")
    b = _clip(env, there, "b.jpg")
    _describe(env, a, "A lighthouse")
    _describe(env, b, "A lighthouse")

    assert _search(env, here, "lighthouse") == [a]
    assert _search(env, there, "lighthouse") == [b]


# ---------------------------------------------------------------------------
# What's in sight
# ---------------------------------------------------------------------------


def test_a_clip_out_of_sight_isnt_found_and_is_found_again_when_restored(env):
    client, headers, *_ = env
    lib = _library(env)
    clip = _clip(env, lib, "a.jpg")
    _describe(env, clip, "A windmill")
    assert client.delete(f"/v1/assets/{clip}", headers=headers).status_code == 204
    assert _search(env, lib, "windmill") == []

    r = client.post(f"/v1/assets/{clip}/restore", headers=headers)
    assert r.status_code == 204, r.text
    assert _search(env, lib, "windmill") == [clip]  # its words were kept: nothing to write again first

    r = client.post("/v1/assets/archive", json={"asset_ids": [clip]}, headers=headers)
    assert r.status_code == 200, r.text
    assert _search(env, lib, "windmill") == []


def test_a_clip_deleted_for_good_takes_its_words_with_it(env):
    client, headers, *_ = env
    lib = _library(env)
    clip = _clip(env, lib, "a.mov", media_type="video")
    _describe(env, clip, "A waterfall")
    _transcribe(env, clip, "1\n00:00:01,000 --> 00:00:02,000\nlisten to the waterfall\n")
    _scene(env, clip, 0, 4000, "Water over rocks", ["waterfall"])
    assert client.delete(f"/v1/assets/{clip}", headers=headers).status_code == 204
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [clip]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text

    with _db(env) as s:
        for table in ("asset_search", "transcript_segments", "video_scenes"):
            n = s.execute(text(f"SELECT count(*) FROM {table} WHERE asset_id = :a"), {"a": clip}).scalar()
            assert n == 0, table


# ---------------------------------------------------------------------------
# Scenes and transcripts: where in a video
# ---------------------------------------------------------------------------


def test_a_scene_is_searchable_as_soon_as_its_description_is_saved(env):
    lib = _library(env)
    clip = _clip(env, lib, "a.mov", media_type="video")
    _scene(env, clip, 0, 5000, "A kitchen", ["kitchen"])
    _scene(env, clip, 5000, 9000, "A dog chasing a ball in a garden", ["dog", "ball"])

    result = _search(env, lib, "garden", raw=True)  # no sync ran
    assert [i["asset_id"] for i in result["items"]] == [clip]
    context = result["items"][0]["search_context"]
    assert context["hit_type"] == "scene"
    assert (context["start_ms"], context["end_ms"]) == (5000, 9000)
    assert context["snippet"] == "A dog chasing a ball in a garden"


def test_a_redos_scenes_are_searchable_only_once_swapped_in(env):
    lib = _library(env)
    clip = _clip(env, lib, "a.mov", media_type="video")
    scene = _scene(env, clip, 0, 5000, "A lighthouse at dawn", [])
    with _db(env) as s:
        s.execute(text("UPDATE video_scenes SET staged = 'found' WHERE scene_id = :s"), {"s": scene})
        s.commit()
    assert _search(env, lib, "lighthouse") == []


def test_a_transcript_is_searched_a_segment_at_a_time_with_its_times(env):
    lib = _library(env)
    clip = _clip(env, lib, "a.mov", media_type="video")
    _transcribe(env, clip, "1\n00:00:01,000 --> 00:00:03,000\nWelcome to the island\n\n"
                           "2\n00:01:10,500 --> 00:01:14,000\nThe volcano last erupted in 1986\n")

    result = _search(env, lib, "volcano", raw=True)  # written with the transcript: no sync ran
    assert [i["asset_id"] for i in result["items"]] == [clip]
    context = result["items"][0]["search_context"]
    assert context["hit_type"] == "transcript"
    assert (context["start_ms"], context["end_ms"]) == (70500, 74000)
    assert context["snippet"] == "The volcano last erupted in 1986"


def test_a_new_transcript_replaces_the_old_ones_segments(env):
    lib = _library(env)
    clip = _clip(env, lib, "a.mov", media_type="video")
    _transcribe(env, clip, "1\n00:00:01,000 --> 00:00:02,000\nold words here\n")
    _transcribe(env, clip, "1\n00:00:01,000 --> 00:00:02,000\nnew words here\n")

    assert _search(env, lib, "old") == []
    assert _search(env, lib, "new") == [clip]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_the_sweep_writes_every_stale_clip_and_transcript_once(env):
    lib = _library(env)
    clip = _clip(env, lib, "a.mov", media_type="video")
    _describe(env, clip, "A harbour full of boats")
    _transcribe(env, clip, "1\n00:00:01,000 --> 00:00:02,000\nthe ferry leaves at noon\n")
    with _db(env) as s:  # as the migration leaves every account: nothing written yet
        s.execute(text("DELETE FROM asset_search WHERE asset_id = :a"), {"a": clip})
        s.execute(text("DELETE FROM transcript_segments WHERE asset_id = :a"), {"a": clip})
        s.execute(text("UPDATE assets SET search_synced_at = NULL, transcript_synced_at = NULL WHERE asset_id = :a"),
                  {"a": clip})
        s.commit()
    assert _search(env, lib, "harbour") == []

    first = _sweep(env)
    assert first["synced"] >= 1 and first["transcripts_synced"] >= 1 and first["failed"] == 0
    assert _search(env, lib, "harbour") == [clip]
    assert _search(env, lib, "ferry") == [clip]
    with _db(env) as s:
        synced = s.execute(text("SELECT search_synced_at, transcript_synced_at FROM assets WHERE asset_id = :a"),
                           {"a": clip}).one()
    assert synced[0] is not None and synced[1] is not None
    again = _sweep(env)
    assert again["synced"] == 0 and again["transcripts_synced"] == 0  # nothing stale: nothing written
