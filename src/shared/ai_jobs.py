"""The jobs the account's AI machines do (ADR-016 phase 3), as the producers
declare them (src/producers/contract.py AiJob; today's in src/producers/pools.py).

Each job has one model for the account, and a machine does a job only if
it offers that model, so an artifact is the same model's whatever machine
made it. Which machine made it isn't tracked (like the encoder).
"""

from __future__ import annotations

from src.producers import jobs as _jobs
from src.producers.contract import AiJob

AI_JOBS: dict[str, AiJob] = _jobs()
# job -> what Settings calls it.
JOBS: dict[str, str] = {name: job.label for name, job in AI_JOBS.items()}
# What the built-in machine (the scheduler's own computer, no URL) can do.
BUILT_IN_JOBS: frozenset[str] = frozenset(name for name, job in AI_JOBS.items() if job.built_in)
