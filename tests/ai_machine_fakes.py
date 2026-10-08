"""One AI machine doing vision, for worker tests: no server, no endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from src.client.cli.ai_pool import Machine


@contextmanager
def one_machine(model: str = "m", offers: list[str] | BaseException | None = None, at_once: int = 2) -> Iterator[None]:
    """The account's vision model is `model`, done by one machine that offers
    `offers` (default: the model) or fails to say (an exception)."""
    def load(pool) -> None:
        pool.model = model
        pool.machines = [Machine("aim_gpu", "GPU", "http://vision", None, at_once)]

    def list_models(*_args, **_kwargs) -> list[str]:
        if isinstance(offers, BaseException):
            raise offers
        return [model] if offers is None else list(offers)

    with patch("src.client.cli.ai_pool.MachinePool.load", load), \
            patch("src.client.cli.ai_pool.list_models", side_effect=list_models):
        yield
