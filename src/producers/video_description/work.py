"""A video described as a whole by the account's vision machines: its scenes' descriptions, in order and
with when each starts, sent as text (no picture), and the answer saved as its description and tags."""

from __future__ import annotations

import re

from src.producers.names import VIDEO_DESCRIPTION
from src.producers.runner import Waits, Work

# The scenes sent at most: about 2,000 tokens, so they, the prompt and the 900-token answer fit in the 4,096
# tokens Ollama gives a model unless told otherwise. A request longer than a machine's context is cut from its
# start, unseen: the model then answers the scenes alone (a summary of "your 55 prompts"), never the JSON asked.
MOST_CHARS = 8000
# A first sentence kept at most (one with no full stop in a long description).
MOST_SENTENCE = 300

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(description: str) -> str:
    sentence = _SENTENCE_END.split(description, maxsplit=1)[0]
    if len(sentence) <= MOST_SENTENCE:
        return sentence
    return sentence[:MOST_SENTENCE].rsplit(" ", 1)[0] + "…"


def _lines(scenes: list[dict], *, tags: bool, first_sentence: bool) -> list[str]:
    lines = []
    for n, scene in enumerate(scenes, 1):
        seconds = int(scene["start_ms"]) // 1000
        description = scene["description"].strip()
        line = f"{n}. ({seconds // 60}:{seconds % 60:02d}) "
        line += _first_sentence(description) if first_sentence else description
        its_tags = [t for t in scene.get("tags") or [] if isinstance(t, str) and t.strip()]
        if tags and its_tags:
            line += f" [tags: {', '.join(its_tags)}]"
        lines.append(line)
    return lines


def _evenly(scenes: list[dict], k: int) -> list[dict]:
    """k of the scenes, evenly spaced, the first and the last among them."""
    if k == 1:
        return scenes[:1]
    return [scenes[round(i * (len(scenes) - 1) / (k - 1))] for i in range(k)]


def scenes_text(scenes: list[dict]) -> str:
    """The scenes as the model reads them: one line each, in order, with when it starts; at most MOST_CHARS.
    A longer list is sent without its tags, then with each scene's first sentence alone, then as evenly spaced
    scenes, as many as fit (the first and last always)."""
    for tags, first_sentence in ((True, False), (False, False), (False, True)):
        text = "\n".join(_lines(scenes, tags=tags, first_sentence=first_sentence))
        if len(text) <= MOST_CHARS:
            return text
    for k in range(len(scenes) - 1, 0, -1):
        text = "\n".join(_lines(_evenly(scenes, k), tags=False, first_sentence=True))
        if len(text) <= MOST_CHARS:
            return text
    return text


class VideoDescription(Work):
    artifact = VIDEO_DESCRIPTION

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        guard = acct.guard("vision")
        self.model = guard.model
        self.used = acct.producers.with_model(self.artifact, self.model)
        self.provider = guard.provider(settings=self.used)

    def make(self, clip: dict) -> dict:
        from src.processing.workers.captions.base import CaptionError

        # The scenes it shows, in order (a redo's being found aren't shown yet).
        scenes = self.client.get(f"/v1/video/{clip['asset_id']}/scenes").json().get("scenes", [])
        described = [s for s in scenes if (s.get("description") or "").strip()]
        if not described or len(described) < len(scenes):
            raise Waits("its scenes aren't all described yet")  # made once they are (it needs them)
        try:
            result = self.provider.describe_text(scenes_text(described))
        except CaptionError as e:
            raise self.machines_or_clip(e) from e
        description = (result.get("description") or "").strip() if result else ""
        if not description:
            raise self.machines_or_clip("the model said nothing")
        return {"asset_id": clip["asset_id"], "model_id": self.model, "model_version": "1",
                "description": description,
                "tags": [t.strip() for t in result.get("tags") or [] if isinstance(t, str) and t.strip()]}

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, result in made:
            client.post("/v1/assets/batch-vision", json={
                "items": [{**result, "source_hash": clip.get("content_hash")}],
                "lineage": self.lineage(None, used=self.used)})  # each item says its file
