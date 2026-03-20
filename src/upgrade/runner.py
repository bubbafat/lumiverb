from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

from src.upgrade.context import UpgradeContext
from src.upgrade.registry import registered_upgrade_steps
from src.upgrade.step import UpgradeStep


StepStatus = Literal["completed", "pending", "skipped", "failed"]


@dataclass(frozen=True)
class UpgradeStepStatus:
    step_id: str
    version: str
    display_name: str
    status: StepStatus
    details: str | None = None


@dataclass(frozen=True)
class UpgradeRunResult:
    has_work_after: bool
    total_steps: int
    done_steps: int
    completed_steps: int
    failed_steps: int
    ran_steps: list[dict]
    remaining_pending_step_ids: list[str]


class TenantUpgradeRunner:
    """Ordered, idempotent upgrade runner for a single tenant.

    It uses `system_metadata` to mark step completion.
    """

    def __init__(self, *, steps: Sequence[UpgradeStep] | None = None) -> None:
        self._steps: Sequence[UpgradeStep] = steps if steps is not None else registered_upgrade_steps()

    def _completion_key(self, step_id: str) -> str:
        return f"upgrade.completed.{step_id}"

    def _failure_key(self, step_id: str) -> str:
        return f"upgrade.failed.{step_id}"

    def get_status(self, ctx: UpgradeContext) -> dict:
        total_steps = len(self._steps)
        completed_steps = 0
        pending_steps = 0
        skipped_steps = 0
        failed_steps = 0
        done_steps = 0
        steps_status: list[UpgradeStepStatus] = []
        next_pending_step_id: str | None = None
        remaining_pending_step_ids: list[str] = []

        for step in self._steps:
            step_id = step.info.step_id
            step_version = step.info.version
            completed_version = ctx.metadata.get_value(self._completion_key(step_id))
            failed_info = ctx.metadata.get_value(self._failure_key(step_id))

            if completed_version == step_version:
                completed_steps += 1
                done_steps += 1
                steps_status.append(
                    UpgradeStepStatus(
                        step_id=step_id,
                        version=step_version,
                        display_name=step.info.display_name,
                        status="completed",
                    )
                )
                continue

            # Not marked completed for this version; ask the step if it needs work.
            try:
                needs = step.needs_work(ctx)
            except Exception as e:  # pragma: no cover (defensive; step implementations decide)
                # Treat errors conservatively as needing work.
                needs = True
                details = f"needs_work error: {e!r}"
            else:
                details = None

            if failed_info is not None:
                failed_steps += 1
                # Preserve the last failure detail as the reason we still have work.
                if details is None:
                    details = failed_info
                else:
                    details = f"{failed_info} | {details}"

            if needs:
                pending_steps += 1
                remaining_pending_step_ids.append(step_id)
                if next_pending_step_id is None:
                    next_pending_step_id = step_id
                steps_status.append(
                    UpgradeStepStatus(
                        step_id=step_id,
                        version=step_version,
                        display_name=step.info.display_name,
                        status="pending",
                        details=details,
                    )
                )
            else:
                skipped_steps += 1
                done_steps += 1
                steps_status.append(
                    UpgradeStepStatus(
                        step_id=step_id,
                        version=step_version,
                        display_name=step.info.display_name,
                        status="skipped",
                        details=details,
                    )
                )

        has_work = pending_steps > 0
        return {
            "has_work": has_work,
            "steps_total": total_steps,
            "done_steps": done_steps,
            "completed_steps": completed_steps,
            "pending_steps": pending_steps,
            "skipped_steps": skipped_steps,
            "failed_steps": failed_steps,
            "next_pending_step_id": next_pending_step_id,
            "remaining_pending_step_ids": remaining_pending_step_ids,
            "steps": [s.__dict__ for s in steps_status],
        }

    def execute(self, ctx: UpgradeContext, *, max_steps: int = 1) -> UpgradeRunResult:
        return self.execute_with_options(ctx, max_steps=max_steps)

    def execute_with_options(
        self,
        ctx: UpgradeContext,
        *,
        max_steps: int = 1,
        step_id: str | None = None,
        force: bool = False,
    ) -> UpgradeRunResult:
        """Execute upgrade work.

        - When `step_id` is None: execute up to `max_steps` pending steps in order.
        - When `step_id` is provided: execute only that step if pending, with ordering checks.
        """
        if max_steps < 1:
            max_steps = 1

        status = self.get_status(ctx)
        pending_step_ids: list[str] = status["remaining_pending_step_ids"]
        all_step_ids_in_order = [s.info.step_id for s in self._steps]

        if step_id is not None and step_id not in all_step_ids_in_order:
            # Unknown step: treat as no-op (caller already validated).
            return UpgradeRunResult(
                has_work_after=bool(status["pending_steps"] > 0),
                total_steps=status["steps_total"],
                done_steps=status["done_steps"],
                completed_steps=status["completed_steps"],
                failed_steps=status["failed_steps"],
                ran_steps=[],
                remaining_pending_step_ids=status["remaining_pending_step_ids"],
            )

        if not pending_step_ids:
            return UpgradeRunResult(
                has_work_after=False,
                total_steps=status["steps_total"],
                done_steps=status["done_steps"],
                completed_steps=status["completed_steps"],
                failed_steps=status["failed_steps"],
                ran_steps=[],
                remaining_pending_step_ids=[],
            )

        def _step_status(step_id_val: str) -> StepStatus:
            for s in status["steps"]:
                if s["step_id"] == step_id_val:
                    return s["status"]  # type: ignore[return-value]
            # Fallback: treat unknown as pending.
            return "pending"

        ran_steps: list[dict] = []

        if step_id is not None:
            target_index = all_step_ids_in_order.index(step_id)
            preceding = all_step_ids_in_order[:target_index]
            # Ordering safety: refuse if any preceding step is not "completed enough"
            # unless force=True.
            if not force:
                not_done = [sid for sid in preceding if _step_status(sid) in ("pending", "failed")]
                if not_done:
                    raise UpgradeStepNotReadyError(
                        step_id=step_id,
                        not_done_preceding_step_ids=not_done,
                    )

            # Only run if target step is pending.
            if step_id not in pending_step_ids:
                status_after = self.get_status(ctx)
                return UpgradeRunResult(
                    has_work_after=bool(status_after["pending_steps"] > 0),
                    total_steps=status_after["steps_total"],
                    done_steps=status_after["done_steps"],
                    completed_steps=status_after["completed_steps"],
                    failed_steps=status_after["failed_steps"],
                    ran_steps=[],
                    remaining_pending_step_ids=status_after["remaining_pending_step_ids"],
                )

            try:
                result = self._steps[target_index].run(ctx)
            except Exception as e:
                ts = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
                ctx.metadata.set_value(
                    self._failure_key(step_id),
                    f"{ts} :: {e.__class__.__name__}: {e}",
                )
                raise

            # If run() succeeds, mark completed for this step version.
            ctx.metadata.set_value(self._completion_key(step_id), self._steps[target_index].info.version)
            ctx.metadata.delete_key(self._failure_key(step_id))
            ran_steps.append({"step_id": step_id, "result": result})

            status_after = self.get_status(ctx)
            return UpgradeRunResult(
                has_work_after=bool(status_after["pending_steps"] > 0),
                total_steps=status_after["steps_total"],
                done_steps=status_after["done_steps"],
                completed_steps=status_after["completed_steps"],
                failed_steps=status_after["failed_steps"],
                ran_steps=ran_steps,
                remaining_pending_step_ids=status_after["remaining_pending_step_ids"],
            )

        # step_id is None: run pending steps sequentially (up to max_steps).
        ran_count = 0
        for s in self._steps:
            if ran_count >= max_steps:
                break
            sid = s.info.step_id
            if sid not in pending_step_ids:
                continue

            try:
                result = s.run(ctx)
            except Exception as e:
                ts = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
                ctx.metadata.set_value(
                    self._failure_key(sid),
                    f"{ts} :: {e.__class__.__name__}: {e}",
                )
                raise

            ctx.metadata.set_value(self._completion_key(sid), s.info.version)
            ctx.metadata.delete_key(self._failure_key(sid))
            ran_steps.append({"step_id": sid, "result": result})
            ran_count += 1

        status_after = self.get_status(ctx)
        return UpgradeRunResult(
            has_work_after=bool(status_after["pending_steps"] > 0),
            total_steps=status_after["steps_total"],
            done_steps=status_after["done_steps"],
            completed_steps=status_after["completed_steps"],
            failed_steps=status_after["failed_steps"],
            ran_steps=ran_steps,
            remaining_pending_step_ids=status_after["remaining_pending_step_ids"],
        )


class UpgradeStepNotReadyError(Exception):
    def __init__(self, *, step_id: str, not_done_preceding_step_ids: list[str]) -> None:
        super().__init__(
            f"Upgrade step '{step_id}' not ready; preceding pending steps: {not_done_preceding_step_ids}"
        )
        self.step_id = step_id
        self.not_done_preceding_step_ids = not_done_preceding_step_ids

