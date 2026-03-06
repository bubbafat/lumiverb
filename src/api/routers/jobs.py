"""Jobs API: enqueue worker jobs."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.workers.enqueue import enqueue_proxy_jobs

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


class EnqueueRequest(BaseModel):
    library_id: str
    job_type: str


class EnqueueResponse(BaseModel):
    enqueued: int


@router.post("/enqueue", response_model=EnqueueResponse)
def enqueue_jobs(
    body: EnqueueRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> EnqueueResponse:
    if body.job_type != "proxy":
        raise HTTPException(status_code=400, detail=f"Unsupported job_type: {body.job_type}")
    enqueued = enqueue_proxy_jobs(session, body.library_id)
    return EnqueueResponse(enqueued=enqueued)
