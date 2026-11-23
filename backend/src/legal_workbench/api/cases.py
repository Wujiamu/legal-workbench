"""Case endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import cases
from ..schemas import (ArchiveCaseInput, CaseOverview, CaseScheduleInput, CreateCaseInput,
                       CreateCaseOutput, DashboardDeadlinesOutput, DashboardOutput,
                       ListCasesOutput)
from ..errors import ErrorCode, WorkbenchError
from .ids import uuid_of
from .deps import Guarded, get_db

router = APIRouter(prefix="/api/v1", tags=["cases"], dependencies=[Guarded])


@router.post("/cases", response_model=CreateCaseOutput)
def create_case(payload: CreateCaseInput,
                session: Session = Depends(get_db)) -> CreateCaseOutput:
    return cases.create_case(session, title=payload.title, description=payload.description)


@router.get("/cases", response_model=ListCasesOutput)
def list_cases(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
               archived: bool | None = Query(None),
               session: Session = Depends(get_db)) -> ListCasesOutput:
    return cases.list_cases(session, offset=offset, limit=limit, archived=archived)


@router.get("/dashboard", response_model=DashboardOutput)
def dashboard(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
              session: Session = Depends(get_db)) -> DashboardOutput:
    return cases.dashboard(session, offset=offset, limit=limit)


@router.get("/dashboard/deadlines", response_model=DashboardDeadlinesOutput)
def dashboard_deadlines(session: Session = Depends(get_db)) -> DashboardDeadlinesOutput:
    """首页期限时间轴：全部未归档案件的期限，不分页（不受 /dashboard 50 条限制）。"""
    return cases.dashboard_deadlines(session)


@router.get("/cases/{case_id}", response_model=CaseOverview)
def get_case(case_id: str, session: Session = Depends(get_db)) -> CaseOverview:
    return cases.get_case(session, uuid_of(case_id, "case_id"))


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
