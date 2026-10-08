"""Whisper model and language names, for the transcripts job (Settings → AI).

faster-whisper calls a model by a short name ("small") or by the Hugging
Face repo it loads it from ("Systran/faster-whisper-small"), which is what
a server built on it, such as speaches, lists. Both are one model, so the
account's model is kept by its short name (the one transcripts have always
recorded), and the worker asks each machine for it by the name that
machine uses. Any other name is a model of its own.
"""

from __future__ import annotations

from collections.abc import Iterable

# faster-whisper's own table (faster_whisper.utils._MODELS; a test keeps them equal).
REPOS: dict[str, str] = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "tiny": "Systran/faster-whisper-tiny",
    "base.en": "Systran/faster-whisper-base.en",
    "base": "Systran/faster-whisper-base",
    "small.en": "Systran/faster-whisper-small.en",
    "small": "Systran/faster-whisper-small",
    "medium.en": "Systran/faster-whisper-medium.en",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "distil-large-v3.5": "distil-whisper/distil-large-v3.5-ct2",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Each repo's short name: the first in the table ("large-v3", not "large").
_SHORT: dict[str, str] = {}
for _name, _repo in REPOS.items():
    _SHORT.setdefault(_repo, _name)

# What the built-in Whisper (faster-whisper on the worker) offers: each model once.
BUILT_IN_MODELS: list[str] = sorted(set(_SHORT.values()))


def canonical(model: str) -> str:
    """The one name a model goes by: a faster-whisper model's short name, anything else as given."""
    return _SHORT.get(REPOS.get(model, model), model)


def canonical_models(models: Iterable[str]) -> list[str]:
    """A machine's models by their one names, each once, sorted."""
    return sorted({canonical(m) for m in models})


def served_as(models: Iterable[str], model: str) -> str | None:
    """The name a machine listing `models` knows `model` by, or None when it doesn't offer it."""
    if not model:
        return None
    want = canonical(model)
    return next((m for m in models if canonical(m) == want), None)


# Whisper's languages (faster_whisper.tokenizer's codes; OpenAI and whisper.cpp name them).
LANGUAGES: dict[str, str] = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish", "ru": "russian", "ko": "korean",
    "fr": "french", "ja": "japanese", "pt": "portuguese", "tr": "turkish", "pl": "polish", "ca": "catalan",
    "nl": "dutch", "ar": "arabic", "sv": "swedish", "it": "italian", "id": "indonesian", "hi": "hindi",
    "fi": "finnish", "vi": "vietnamese", "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay",
    "cs": "czech", "ro": "romanian", "da": "danish", "hu": "hungarian", "ta": "tamil", "no": "norwegian",
    "th": "thai", "ur": "urdu", "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin",
    "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak", "te": "telugu", "fa": "persian",
    "lv": "latvian", "bn": "bengali", "sr": "serbian", "az": "azerbaijani", "sl": "slovenian", "kn": "kannada",
    "et": "estonian", "mk": "macedonian", "br": "breton", "eu": "basque", "is": "icelandic", "hy": "armenian",
    "ne": "nepali", "mn": "mongolian", "bs": "bosnian", "kk": "kazakh", "sq": "albanian", "sw": "swahili",
    "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala", "km": "khmer", "sn": "shona",
    "yo": "yoruba", "so": "somali", "af": "afrikaans", "oc": "occitan", "ka": "georgian", "be": "belarusian",
    "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic", "yi": "yiddish", "lo": "lao",
    "uz": "uzbek", "fo": "faroese", "ht": "haitian creole", "ps": "pashto", "tk": "turkmen", "nn": "nynorsk",
    "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar", "bo": "tibetan",
    "tl": "tagalog", "mg": "malagasy", "as": "assamese", "tt": "tatar", "haw": "hawaiian", "ln": "lingala",
    "ha": "hausa", "ba": "bashkir", "jw": "javanese", "su": "sundanese", "yue": "cantonese",
}
_CODES = {name: code for code, name in LANGUAGES.items()}


def language_code(language: str) -> str:
    """The language's code ("en"), whether a server said "en" or "english"; anything else as said."""
    said = (language or "").strip()
    lower = said.lower()
    if lower in LANGUAGES:
        return lower
    return _CODES.get(lower, said)
