"""Whisper model names (src/shared/whisper_models.py).

faster-whisper calls a model by a short name ("small") or by the Hugging
Face repo it loads it from ("Systran/faster-whisper-small"), which is what
a server built on it, such as speaches, lists. They're one model, so
Settings shows one name, and the worker asks each machine for it by the
name that machine uses.
"""

from __future__ import annotations

import pytest

from src.shared import whisper_models as W

pytestmark = pytest.mark.fast


@pytest.mark.parametrize(("given", "canonical"), [
    ("small", "small"),
    ("Systran/faster-whisper-small", "small"),
    ("large", "large-v3"),
    ("Systran/faster-whisper-large-v3", "large-v3"),
    ("turbo", "large-v3-turbo"),
    ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "large-v3-turbo"),
    ("small.en", "small.en"),
    # Not faster-whisper's: as given.
    ("whisper-1", "whisper-1"),
    ("deepdml/faster-whisper-large-v3-turbo-ct2", "deepdml/faster-whisper-large-v3-turbo-ct2"),
    ("qwen3-vl:8b-instruct", "qwen3-vl:8b-instruct"),
])
def test_one_name_per_model(given: str, canonical: str) -> None:
    assert W.canonical(given) == canonical


def test_a_machines_models_by_their_one_name() -> None:
    assert W.canonical_models(["Systran/faster-whisper-small", "small", "kokoro", "large"]) == [
        "kokoro", "large-v3", "small"]


def test_the_name_a_machine_knows_a_model_by() -> None:
    assert W.served_as(["kokoro", "Systran/faster-whisper-small"], "small") == "Systran/faster-whisper-small"
    assert W.served_as(["small"], "Systran/faster-whisper-small") == "small"
    assert W.served_as(["whisper-1"], "whisper-1") == "whisper-1"
    assert W.served_as(["whisper-1"], "small") is None
    assert W.served_as(["Systran/faster-whisper-small"], "") is None


def test_the_names_are_faster_whispers_own() -> None:
    from faster_whisper.utils import _MODELS

    assert W.REPOS == _MODELS
    # The built-in Whisper offers each model once, by its short name.
    assert "small" in W.BUILT_IN_MODELS and "large-v3" in W.BUILT_IN_MODELS
    assert "large" not in W.BUILT_IN_MODELS and "turbo" not in W.BUILT_IN_MODELS
    assert W.BUILT_IN_MODELS == sorted(set(W.BUILT_IN_MODELS))


@pytest.mark.parametrize(("said", "code"), [
    ("en", "en"), ("english", "en"), ("English", "en"), ("haitian creole", "ht"), ("yue", "yue"),
    ("", ""), ("klingon", "klingon"),
])
def test_a_language_is_its_code_whoever_says_it(said: str, code: str) -> None:
    """faster-whisper says "en"; OpenAI and whisper.cpp say "english"."""
    assert W.language_code(said) == code


def test_the_languages_are_whispers() -> None:
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    assert set(W.LANGUAGES) == set(_LANGUAGE_CODES)
