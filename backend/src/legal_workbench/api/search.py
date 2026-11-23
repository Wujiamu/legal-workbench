"""Search, source reading and index status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import search
from ..schemas import (Coverage, ExtractedTextOutput, ReadSourceOutput, SearchCaseInput,
                       SearchCaseOutput, SearchMode, SearchScope)
from .deps import Guarded, get_db
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["search"], dependencies=[Guarded])


@router.post("/search", response_model=SearchCaseOutput)
def search_case(case_id: str, payload: SearchCaseInput,
                session: Session = Depends(get_db)) -> SearchCaseOutput:
    if str(payload.case_id) != str(case_id):
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "路径与请求体中的案件 ID 不一致")
    return search.search_case(
        session, case_id=payload.case_id, query=payload.query, mode=payload.mode,
        scope=payload.scope, currency=payload.currency,
        offset=payload.offset, limit=payload.limit,
    )


@router.get("/sources/{source_ref}", response_model=ReadSourceOutput)
def read_source(case_id: str, source_ref: str,
                context_chars: int = Query(200, ge=0, le=2000),
                session: Session = Depends(get_db)) -> ReadSourceOutput:
    return search.read_source(session, case_id=uuid_of(case_id, "case_id"),
                              source_ref=uuid_of(source_ref, "source_ref"),
                              context_chars=context_chars)


@router.get("/file-versions/{file_version_id}/text", response_model=ExtractedTextOutput)
def extracted_text(case_id: str, file_version_id: str,
                   session: Session = Depends(get_db)) -> ExtractedTextOutput:
    return search.extracted_text(session, case_id=uuid_of(case_id, "case_id"),
                                 file_version_id=uuid_of(file_version_id, "file_version_id"))


@router.get("/index-status", response_model=Coverage)
def index_status(case_id: str, session: Session = Depends(get_db)) -> Coverage:
    return search.get_index_status(session, case_id=uuid_of(case_id, "case_id"))
