"""The account's AI machines, for worker tests: no server, no endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from src.processing.ai_pool import Machine, MachinePool


@contextmanager
def one_machine(model: str = "m", offers: list[str] | BaseException | None = None, at_once: int = 2) -> Iterator[None]:
    """The account's vision model is `model`, done by one machine that offers
    `offers` (default: the model) or fails to say (an exception)."""
    prior = MachinePool.load

    def load(pool) -> None:
        if pool.job != "vision":
            return prior(pool)
        pool.model = model
        pool.machines = [Machine("aim_gpu", "GPU", "http://vision", None, at_once)]

    def list_models(*_args, **_kwargs) -> list[str]:
        if isinstance(offers, BaseException):
            raise offers
        return [model] if offers is None else list(offers)

    with patch("src.processing.ai_pool.MachinePool.load", load), \
            patch("src.processing.ai_pool.list_models", side_effect=list_models):
        yield


@contextmanager
def built_in_whisper(model: str = "small", at_once: int = 1) -> Iterator[None]:
    """The account's transcripts model is `model`, done by the built-in Whisper alone."""
    prior = MachinePool.load

    def load(pool) -> None:
        if pool.job != "transcripts":
            return prior(pool)
        pool.model = model
        pool.machines = [Machine("aim_self", "Built in", "", None, at_once, built_in=True)]

    with patch("src.processing.ai_pool.MachinePool.load", load):
        yield
