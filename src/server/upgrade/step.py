from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UpgradeStepInfo:
    step_id: str
    version: str
    display_name: str


class UpgradeStep(Protocol):
    """A single idempotent upgrade step.

    The step must be safe to run multiple times.
    """

    info: UpgradeStepInfo

    def needs_work(self, ctx: object) -> bool:
        """Return True if this step still needs to be executed."""

    def run(self, ctx: object) -> dict:
        """Execute the step. Returns an opaque result dict."""

