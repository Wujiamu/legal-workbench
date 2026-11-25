"""Case endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import cases
from ..schemas import (ArchiveCaseInput, CaseMetadataInput, CaseOverview, CaseScheduleInput,
                       CreateCaseInput, CreateCaseOutput, DashboardDeadlinesOutput,
                       DashboardOutput, ListCasesOutput)
from ..errors import ErrorCode, WorkbenchError
from .ids import uuid_of
from .deps import Guarded, get_db

router = APIRouter(prefix="/api/v1", tags=["cases"], dependencies=[Guarded])


@router.post("/cases", response_model=CreateCaseOutput)
def create_case(payload: CreateCaseInput,
                session: Session = Depends(get_db)) -> CreateCaseOutput:
    return cases.create_case(
        session,
        title=payload.title,
        description=payload.description,
        case_number=payload.case_number,
        plaintiff=payload.plaintiff,
        defendant=payload.defendant,
        work_status=payload.work_status,
    )


@router.get("/cases", response_model=ListCasesOutput)
def list_cases(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
               archived: bool | None = Query(None),
               q: str | None = Query(None, max_length=200),
               work_status: str | None = Query(None),
               deadline_filter: str | None = Query(None),
               sort_by: str = Query("created_at"),
               sort_dir: str = Query("asc"),
               session: Session = Depends(get_db)) -> ListCasesOutput:
    return cases.list_cases(
        session, offset=offset, limit=limit, archived=archived,
        q=q, work_status=work_status, deadline_filter=deadline_filter,
        sort_by=sort_by, sort_dir=sort_dir,
    )


@router.get("/dashboard", response_model=DashboardOutput)
def dashboard(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
              q: str | None = Query(None, max_length=200),
              work_status: str | None = Query(None),
              deadline_filter: str | None = Query(None),
              sort_by: str = Query("next_due"),
              sort_dir: str = Query("asc"),
              session: Session = Depends(get_db)) -> DashboardOutput:
    return cases.dashboard(
        session, offset=offset, limit=limit,
        q=q, work_status=work_status, deadline_filter=deadline_filter,
        sort_by=sort_by, sort_dir=sort_dir,
    )


@router.get("/dashboard/deadlines", response_model=DashboardDeadlinesOutput)
def dashboard_deadlines(session: Session = Depends(get_db)) -> DashboardDeadlinesOutput:
    """首页期限时间轴：全部未归档案件的期限，不分页（不受 /dashboard 50 条限制）。"""
    return cases.dashboard_deadlines(session)


@router.get("/cases/{case_id}", response_model=CaseOverview)
def get_case(case_id: str, session: Session = Depends(get_db)) -> CaseOverview:
    return cases.get_case(session, uuid_of(case_id, "case_id"))


@router.patch("/cases/{case_id}/metadata", response_model=CaseOverview)
def update_metadata(case_id: str, payload: CaseMetadataInput,
                    session: Session = Depends(get_db)) -> CaseOverview:
    if uuid_of(case_id, "case_id") != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")
    return cases.update_metadata(session, payload)


@router.patch("/cases/{case_id}/schedule", response_model=CaseOverview)
def update_schedule(case_id: str, payload: CaseScheduleInput,
                    session: Session = Depends(get_db)) -> CaseOverview:
    if uuid_of(case_id, "case_id") != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")
    return cases.update_schedule(session, payload)


@router.post("/cases/{case_id}/archive", response_model=CaseOverview)
def archive_case(case_id: str, payload: ArchiveCaseInput,
                 session: Session = Depends(get_db)) -> CaseOverview:
    if uuid_of(case_id, "case_id") != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")
    return cases.archive_case(session, payload)


@router.post("/cases/{case_id}/unarchive", response_model=CaseOverview)
def unarchive_case(case_id: str, payload: ArchiveCaseInput,
                   session: Session = Depends(get_db)) -> CaseOverview:
    if uuid_of(case_id, "case_id") != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")
    return cases.unarchive_case(session, payload)
