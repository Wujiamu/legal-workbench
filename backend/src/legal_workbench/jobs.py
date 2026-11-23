"""Job records, retry and status reporting shared by worker and transports."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import begin_write
from .errors import ErrorCode, WorkbenchError
from .schemas import GetJobStatusOutput, JobItemError, JobStatus


def job_or_error(session: Session, case_id: uuid.UUID, job_id: uuid.UUID) -> models.Job:
    job = session.get(models.Job, job_id)
    if job is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "任务不存在", {"job_id": str(job_id)})
    if job.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "任务不属于该案件", {"job_id": str(job_id)})
    return job


def get_job_status(session: Session, *, case_id: uuid.UUID, job_id: uuid.UUID) -> GetJobStatusOutput:
    job = job_or_error(session, case_id, job_id)
    errors: list[JobItemError] = []
    if job.kind == "import":
        rows = session.execute(
            select(models.IntakeItem).where(models.IntakeItem.intake_id == job.target_id,
                                            models.IntakeItem.status == "failed")
            .order_by(models.IntakeItem.relative_source)
        ).scalars().all()
        errors = [JobItemError(item_id=row.id, error_code=row.error_code or "unknown",
                               message=row.error_message or "") for row in rows]
    elif job.kind == "parse":
        rows = session.execute(
            select(models.ParseUnit).where(models.ParseUnit.parse_run_id == job.target_id,
                                           models.ParseUnit.status == "failed")
            .order_by(models.ParseUnit.unit_key)
        ).scalars().all()
        errors = [JobItemError(item_id=row.id, error_code="parse_failed",
                               message=row.error or "") for row in rows]
    return GetJobStatusOutput(
        job_id=job.id, status=JobStatus(job.status),
        completed_units=job.completed_units, total_units=job.total_units, errors=errors,
    )


def retry_job(session: Session, *, case_id: uuid.UUID, job_id: uuid.UUID) -> models.Job:
    from . import imports

    job = job_or_error(session, case_id, job_id)
    begin_write(session)
    if job.status == "running":
        raise WorkbenchError(ErrorCode.CONFLICT, "任务正在运行，不能重试",
                             {"job_id": str(job_id)})
    if job.kind == "import":
        job = imports.reset_job_for_retry(session, job.id)
    else:
        job.status = "queued"
        job.attempt += 1
        job.error = None
        session.commit()
    return job


def update_progress(session: Session, job_id: uuid.UUID, *, completed: int | None = None,
                    total: int | None = None, status: str | None = None,
                    error: str | None = None) -> None:
    begin_write(session)
    job = session.get(models.Job, job_id)
    if job is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "任务不存在", {"job_id": str(job_id)})
    if completed is not None:
        job.completed_units = completed
    if total is not None:
        job.total_units = total
    if status is not None:
        job.status = status
    if error is not None:
        job.error = error
    session.commit()
