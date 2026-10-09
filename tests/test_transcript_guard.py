"""Transcription only while a machine doing transcripts can (Settings → AI).

The transcripts job's machines are the built-in Whisper (the worker's own
computer) and transcription servers. Before transcribing, the worker checks
each and tells the server what it found. Clips go to the online machines,
as many at once as they take together. When none can be used, transcription
doesn't start, or stops, and no clip is charged a failure: it's the
machines' problem. A clip is charged only when a machine heard it and
couldn't make sense of it, and a check afterwards finds that machine fine.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.config import CLIConfig, save_config
from src.client.cli.repair import run_repair
from src.client.cli.transcript_guard import TranscriptGuard
from src.client.video.audio import AudioTrack
from src.client.workers.transcripts.base import TranscriptError
from src.shared import producers as P
from src.shared.vision_endpoint import VisionEndpointError
from tests.test_transcribers import _Server

pytestmark = pytest.mark.fast

SHA = "ab" * 32
BUILT_IN = {"machine_id": "aim_self", "name": "Built in", "api_url": "", "api_key": "", "at_once": 1, "built_in": True}


def _speaches(url: str = "http://speaches/v1", at_once: int = 2) -> dict:
    return {"machine_id": "aim_sp", "name": "Speaches", "api_url": url, "api_key": "", "at_once": at_once,
            "built_in": False}


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    save_config(CLIConfig(api_url="http://test", api_key="k"))
    for asset_id in ("ast_a", "ast_b", "ast_c", "ast_d"):
        proxy = home / ".cache" / "lumiverb" / "analysis" / f"{asset_id}.mp4"
        proxy.parent.mkdir(parents=True, exist_ok=True)
        proxy.write_bytes(b"proxy")
    return {"library_id": "lib_1", "name": "Footage", "root_path": str(tmp_path / "nowhere")}


def _client(machines: list[dict], model: str = "small") -> MagicMock:
    listing = {"producers": [{"artifact": a, "settings": P.effective_settings(a)} for a in P.ARTIFACTS]}
    job = {"job": "transcripts", "model": model, "machines": machines}
    client = MagicMock()

    def get(path, **_kwargs):
        resp = MagicMock()
        resp.json.return_value = {"/v1/producers": listing, "/v1/ai/jobs/transcripts": job}.get(path, {"items": []})
        return resp

    client.get.side_effect = get
    return client


def _clips(*ids: str) -> list[dict]:
    return [{"asset_id": x, "rel_path": f"{x}.mov", "duration_sec": 4.0, "sha256": SHA, "has_analysis_proxy": True}
            for x in ids]


def _run(client: MagicMock, library: dict, clips: list[dict]) -> None:
    with (
        patch("src.client.cli.repair.get_repair_summary",
              return_value={"total_assets": len(clips), "missing_transcription": len(clips)}),
        patch("src.client.cli.repair._page_missing", return_value=clips),
    ):
        run_repair(client, library, job_type="transcribe", console=Console(quiet=True))


def _charged(client: MagicMock) -> list[dict]:
    return [item for c in client.post.call_args_list if c.args and c.args[0] == "/v1/producers/failures"
            for item in c.kwargs["json"]["items"]]


def _transcripts(client: MagicMock) -> dict[str, dict]:
    return {c.args[0].split("/")[3]: c.kwargs["json"] for c in client.post.call_args_list
            if c.args and c.args[0].endswith("/transcript")}


def _statuses(client: MagicMock, machine_id: str) -> list[dict]:
    return [c.kwargs["json"] for c in client.post.call_args_list if c.args[0] == f"/v1/ai/machines/{machine_id}/status"]


def test_no_usable_machine_means_no_transcription_and_no_clip_charged(library):
    client = _client([_speaches()])
    with patch("src.client.cli.ai_pool.list_models", side_effect=VisionEndpointError("Couldn't reach it.")), \
            patch("src.client.cli.repair._transcribe_one") as one:
        _run(client, library, _clips("ast_a"))
    one.assert_not_called()
    assert _charged(client) == [] and _transcripts(client) == {}
    assert _statuses(client, "aim_sp")[-1] == {"online": False, "error": "Couldn't reach it.", "models": []}


def test_transcription_is_off_without_a_model(library):
    guard = TranscriptGuard(_client([BUILT_IN], model=""))
    assert guard.check() is False and "No model is chosen for transcripts" in guard.error


def test_without_faster_whisper_here_the_speech_cant_be_found_so_nothing_starts(library):
    client = _client([_speaches()])
    with patch("src.client.workers.transcripts.local.unavailable", return_value="faster-whisper isn't installed."), \
            patch("src.client.cli.ai_pool.list_models", return_value=["Systran/faster-whisper-small"]), \
            patch("src.client.cli.repair._transcribe_one") as one:
        _run(client, library, _clips("ast_a"))
        guard = TranscriptGuard(client)
        assert guard.check() is False and "can't find the speech" in guard.error
    one.assert_not_called()


def test_clips_go_to_every_online_machine_as_many_at_once_as_they_take(library):
    """Each machine up to its own limit; meanwhile more clips get their speech ready."""
    from src.client.workers.transcripts.base import Heard
    from src.client.workers.transcripts.local import BuiltInWhisper
    from src.client.workers.transcripts.openai_compatible import OpenAITranscriber

    client = _client([BUILT_IN, _speaches(at_once=2)])
    lock = threading.Lock()
    busy = {"all": 0, BuiltInWhisper: 0, OpenAITranscriber: 0}
    most = dict.fromkeys(busy, 0)
    preparing = [0]

    def hear(self, wav):
        with lock:
            for k in ("all", type(self)):
                busy[k] += 1
                most[k] = max(most[k], busy[k])
        time.sleep(0.15)
        with lock:
            for k in ("all", type(self)):
                busy[k] -= 1
        return Heard()

    def transcribe_one(source, transcriber, vad_ms):
        with lock:
            preparing[0] += 1
        return ("", "") if transcriber.transcribe(source) == Heard() else None

    clips = [f"ast_{c}" for c in "abcdefgh"]
    with patch("src.client.cli.ai_pool.list_models", return_value=["Systran/faster-whisper-small"]), \
            patch.object(BuiltInWhisper, "transcribe", hear), patch.object(OpenAITranscriber, "transcribe", hear), \
            patch("src.client.cli.repair._transcribe_one", side_effect=transcribe_one):
        _run(client, library, _clips(*clips))
    assert most["all"] == 3  # the built-in's one and the server's two
    assert most[BuiltInWhisper] == 1 and most[OpenAITranscriber] == 2
    assert set(_transcripts(client)) == set(clips) and preparing[0] == len(clips)


def test_no_machine_left_stops_transcription_without_charging_a_clip(library):
    client = _client([BUILT_IN])
    calls = []

    def transcribe_one(source, transcriber, vad_ms):
        calls.append(source.name)
        raise TranscriptError("No machine doing transcripts is online (Built in: Couldn't load small).",
                              endpoint_fault=True)

    with patch("src.client.cli.repair._transcribe_one", side_effect=transcribe_one):
        _run(client, library, _clips("ast_a", "ast_b", "ast_c", "ast_d", "ast_e"))
    # Those already under way end too; the rest wait.
    assert 1 <= len(calls) <= 2 and "ast_e.mp4" not in calls
    assert _charged(client) == [] and _transcripts(client) == {}


def test_a_surprise_on_one_clip_charges_it_and_the_rest_go_on(library):
    client = _client([BUILT_IN])

    def transcribe_one(source, transcriber, vad_ms):
        if source.name == "ast_a.mp4":
            raise OSError(28, "No space left on device")
        return ("1\n00:00:00,000 --> 00:00:01,000\nhi\n", "en")

    with patch("src.client.cli.repair._transcribe_one", side_effect=transcribe_one):
        _run(client, library, _clips("ast_a", "ast_b"))
    assert [(c["asset_id"], c["artifact"]) for c in _charged(client)] == [("ast_a", "transcript")]
    assert set(_transcripts(client)) == {"ast_b"}


def test_without_faster_whisper_here_settings_says_so(library):
    client = _client([BUILT_IN])
    with patch("src.client.workers.transcripts.local.unavailable", return_value="faster-whisper isn't installed."):
        assert TranscriptGuard(client).check() is False
    [status] = _statuses(client, "aim_self")
    assert status["online"] is False and "faster-whisper isn't installed" in status["error"]


def test_a_clip_the_machine_couldnt_make_sense_of_is_charged_once_the_machine_checks_out(library):
    client = _client([BUILT_IN])

    def transcribe_one(source, transcriber, vad_ms):
        if source.name == "ast_a.mp4":
            raise TranscriptError("Invalid data found when processing input", endpoint_fault=False)
        return ("1\n00:00:00,000 --> 00:00:01,000\nhi\n", "en")

    with patch("src.client.cli.repair._transcribe_one", side_effect=transcribe_one):
        _run(client, library, _clips("ast_a", "ast_b"))
    assert [(c["asset_id"], c["artifact"]) for c in _charged(client)] == [("ast_a", "transcript")]
    assert set(_transcripts(client)) == {"ast_b"}


def test_the_built_in_failing_to_load_moves_the_clip_to_a_server_timed_onto_the_clip(library, tmp_path):
    """End to end on the worker: the speech is found here (500 ms silences),
    the built-in Whisper can't load its model, so the server hears the
    speech; its times come back onto the clip, and the lineage says small."""
    server = _Server((200, {"language": "english", "text": " hi",
                            "segments": [{"start": 0.25, "end": 0.75, "text": " hi"}]}))
    try:
        client = _client([BUILT_IN, _speaches(server.url, at_once=1)])

        def ffmpeg(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"\x00" * 10_000)
            return MagicMock(returncode=0, stderr=b"")

        found = []

        def find_speech(wav, speech, min_silence_ms):
            found.append(min_silence_ms)
            speech.write_bytes(b"RIFF speech")
            return [{"start": 2 * 16_000, "end": 3 * 16_000}]  # one second of speech at 2 s

        with patch("src.client.cli.ai_pool.list_models", return_value=["Systran/faster-whisper-small", "kokoro"]), \
                patch("src.client.cli.repair.audio_tracks", return_value=[AudioTrack(0, 2, "aac")]), \
                patch("subprocess.run", side_effect=ffmpeg), \
                patch("src.client.workers.transcripts.speech.find_speech", side_effect=find_speech), \
                patch("src.client.workers.transcripts.local.BuiltInWhisper.transcribe",
                      side_effect=TranscriptError("Couldn't load small: no space left", endpoint_fault=True)):
            _run(client, library, _clips("ast_a"))
        [req] = server.requests
    finally:
        server.close()
    assert found == [500]
    assert req["fields"]["model"][2] == b"Systran/faster-whisper-small"
    assert req["fields"]["file"][2] == b"RIFF speech"
    sent = _transcripts(client)["ast_a"]
    assert sent["srt"] == "1\n00:00:02,250 --> 00:00:02,750\nhi\n" and sent["language"] == "en"
    assert sent["lineage"] == P.lineage("transcript", {"model": "small", "vad_min_silence_ms": 500}, SHA)
    assert _statuses(client, "aim_self")[-1]["error"] == "Couldn't load small: no space left"
    assert _charged(client) == []
