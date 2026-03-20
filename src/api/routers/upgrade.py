from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session, require_tenant_admin
from src.upgrade.context import UpgradeContext
from src.upgrade.runner import TenantUpgradeRunner
from src.upgrade.runner import UpgradeStepNotReadyError
from src.repository.system_metadata import SystemMetadataRepository

router = APIRouter(prefix="/v1/tenant/upgrade", tags=["tenant-upgrade"])


class TenantUpgradeStatusResponse(BaseModel):
    has_work: bool
    steps_total: int
    done_steps: int
    completed_steps: int
    pending_steps: int
    skipped_steps: int
    failed_steps: int
    next_pending_step_id: str | None = None
    remaining_pending_step_ids: list[str]
    steps: list[dict]


class TenantUpgradeExecuteRequest(BaseModel):
    max_steps: int = 1
    step_id: str | None = None
    force: bool = False


class TenantUpgradeExecuteResponse(BaseModel):
    ran_steps: list[dict]
    steps_completed_now: int
    has_work_after: bool
    remaining_pending_step_ids: list[str]
    total_steps: int
    done_steps: int
    completed_steps: int
    failed_steps: int


@router.get("/status", response_model=TenantUpgradeStatusResponse)
def get_upgrade_status(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> TenantUpgradeStatusResponse:
    ctx = UpgradeContext(
        session=session,
        metadata=SystemMetadataRepository(session),
    )
    runner = TenantUpgradeRunner()
    return TenantUpgradeStatusResponse(**runner.get_status(ctx))


@router.post("/execute", response_model=TenantUpgradeExecuteResponse)
def execute_upgrade(
    body: TenantUpgradeExecuteRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> TenantUpgradeExecuteResponse:
    ctx = UpgradeContext(
        session=session,
        metadata=SystemMetadataRepository(session),
    )
    runner = TenantUpgradeRunner()
    try:
        result = runner.execute_with_options(
            ctx,
            max_steps=body.max_steps,
            step_id=body.step_id,
            force=body.force,
        )
    except UpgradeStepNotReadyError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upgrade_step_not_ready",
                "message": str(e),
                "details": {"step_id": e.step_id, "not_done_preceding_step_ids": e.not_done_preceding_step_ids},
            },
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Upgrade execution failed: {e}",
        ) from e

    return TenantUpgradeExecuteResponse(
        ran_steps=result.ran_steps,
        steps_completed_now=len(result.ran_steps),
        has_work_after=result.has_work_after,
        remaining_pending_step_ids=result.remaining_pending_step_ids,
        total_steps=result.total_steps,
        done_steps=result.done_steps,
        completed_steps=result.completed_steps,
        failed_steps=result.failed_steps,
    )

