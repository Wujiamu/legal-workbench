"""Billable-hour HTTP endpoints (actor=human, entry=local-web).

Timer start/stop, manual entries, soft delete, day view + per-title summary.
Writes are rejected while the case is archived.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import time_entries
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (AddTimeEntryInput, RemoveTimeEntryInput, StartTimerInput,
                       StopTimerInput, TimeDayOutput, TimeEntryOut)
from .deps import Guarded, get_db
from .ids import uuid_of

router = APIRouter(prefix="/api/v1", tags=["time"], dependencies=[Guarded])

def _check_case(path_value: str, body_value) -> None:
    if uuid_of(path_value, "case_id") != body_value:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")


@router.get("/cases/{case_id}/time", response_model=TimeDayOutput)
def get_day(case_id: str, work_date: date = Query(...),
            session: Session = Depends(get_db)) -> TimeDayOutput:
    return time_entries.day(session, uuid_of(case_id, "case_id"), work_date)


@router.post("/cases/{case_id}/time/start", response_model=TimeEntryOut)
def start_timer(case_id: str, payload: StartTimerInput,
                session: Session = Depends(get_db)) -> TimeEntryOut:
    _check_case(case_id, payload.case_id)
    return time_entries.start_timer(session, payload)


@router.post("/cases/{case_id}/time/entries/{entry_id}/stop", response_model=TimeEntryOut)
def stop_timer(case_id: str, entry_id: str, payload: StopTimerInput,
               session: Session = Depends(get_db)) -> TimeEntryOut:
    _check_case(case_id, payload.case_id)
    if uuid_of(entry_id, "entry_id") != payload.entry_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "工时记录标识不一致")
    return time_entries.stop_timer(session, payload)


@router.post("/cases/{case_id}/time/entries", response_model=TimeEntryOut)
def add_manual(case_id: str, payload: AddTimeEntryInput,
               session: Session = Depends(get_db)) -> TimeEntryOut:
    _check_case(case_id, payload.case_id)
    return time_entries.add_manual(session, payload)


@router.post("/cases/{case_id}/time/entries/{entry_id}/remove", response_model=TimeEntryOut)
def remove_entry(case_id: str, entry_id: str, payload: RemoveTimeEntryInput,
                 session: Session = Depends(get_db)) -> TimeEntryOut:
    _check_case(case_id, payload.case_id)
    if uuid_of(entry_id, "entry_id") != payload.entry_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "工时记录标识不一致")
    return time_entries.remove_entry(session, payload)
