"""The jobs the account's AI machines do (ADR-016 phase 3).

Each job has one model for the account, and a machine does a job only if
it offers that model, so an artifact is the same model's whatever machine
made it. Which machine made it isn't tracked (like the encoder).
"""

from __future__ import annotations

# job -> what Settings calls it. Transcripts come next (phase 2).
JOBS: dict[str, str] = {
    "vision": "Descriptions & text",
}
