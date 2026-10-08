"""The jobs the account's AI machines do (ADR-016 phase 3).

Each job has one model for the account, and a machine does a job only if
it offers that model, so an artifact is the same model's whatever machine
made it. Which machine made it isn't tracked (like the encoder).

- vision: descriptions, OCR and scene descriptions, on OpenAI-compatible
  chat endpoints (Ollama).
- transcripts: Whisper, on the worker's own computer (the built-in machine)
  and on OpenAI-compatible transcription servers (speaches, LocalAI).
"""

from __future__ import annotations

# job -> what Settings calls it.
JOBS: dict[str, str] = {
    "vision": "Descriptions & text",
    "transcripts": "Transcripts",
}

# What the built-in machine (the worker's own computer, no URL) can do:
# Whisper is part of the worker; vision needs a server.
BUILT_IN_JOBS: frozenset[str] = frozenset({"transcripts"})
