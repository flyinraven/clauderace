"""Job status polling.

The frontend polls these endpoints while a job runs. That serves two purposes:
it drives the progress bar, and the traffic keeps the Render free instance from
sleeping mid-job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.constants import ROLE_ADMIN
from app.models import Job

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobStatus(BaseModel):
    id: int
    job_type: str
    status: str
    total_steps: int
    completed_steps: int
    progress: float
    message: str | None
    error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _to_status(job: Job) -> JobStatus:
    return JobStatus(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        total_steps=job.total_steps,
        completed_steps=job.completed_steps,
        progress=job.progress,
        message=job.message,
        error=job.error,
        result=job.result,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get("/{job_id}", response_model=JobStatus)
def get_job(job_id: int, user: CurrentUser, db: DbSession) -> JobStatus:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if user.role != ROLE_ADMIN and job.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Not your job")
    return _to_status(job)
