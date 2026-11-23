"""Analysis endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import analysis
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (GetAnalysisOutput, GetAnalysisVersionOutput, ListAnalysesOutput,
                       ListAnalysisVersionsOutput, ReviewAnalysisInput, ReviewAnalysisOutput,
                       SaveAnalysisInput, SaveAnalysisOutput)
from ..storage import Storage
from .deps import Guarded, get_db, get_storage
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["analysis"], dependencies=[Guarded])


def _match(path_value: str, body_value, field: str) -> None:
    if str(body_value) != str(path_value):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "路径与请求体中的标识不一致",
                             {"field": field})


@router.post("/analyses", response_model=SaveAnalysisOutput)
def save_analysis(case_id: str, payload: SaveAnalysisInput,
                  session: Session = Depends(get_db),
                  storage: Storage = Depends(get_storage)) -> SaveAnalysisOutput:
    _match(case_id, payload.case_id, "case_id")
    return analysis.save_analysis(
        session, storage, case_id=payload.case_id, title=payload.title,
        content=payload.content, statement_kind=payload.statement_kind,
        source_refs=payload.source_refs, analysis_id=payload.analysis_id,
        expected_revision=payload.expected_revision,
    )


@router.get("/analyses", response_model=ListAnalysesOutput)
def list_analyses(case_id: str, offset: int = Query(0, ge=0),
                  limit: int = Query(50, ge=1, le=200),
                  session: Session = Depends(get_db)) -> ListAnalysesOutput:
    return analysis.list_analyses(session, case_id=uuid_of(case_id, "case_id"),
                                  offset=offset, limit=limit)


@router.get("/analyses/{analysis_id}", response_model=GetAnalysisOutput)
def get_analysis(case_id: str, analysis_id: str,
                 session: Session = Depends(get_db)) -> GetAnalysisOutput:
    summary = analysis.get_analysis(session, case_id=uuid_of(case_id, "case_id"),
                                    analysis_id=uuid_of(analysis_id, "analysis_id"))
    return GetAnalysisOutput(**summary.model_dump())


@router.get("/analyses/{analysis_id}/versions", response_model=ListAnalysisVersionsOutput)
def list_analysis_versions(case_id: str, analysis_id: str, offset: int = Query(0, ge=0),
                           limit: int = Query(50, ge=1, le=200),
                           session: Session = Depends(get_db)) -> ListAnalysisVersionsOutput:
    return analysis.list_analysis_versions(
        session, case_id=uuid_of(case_id, "case_id"),
        analysis_id=uuid_of(analysis_id, "analysis_id"), offset=offset, limit=limit,
    )


@router.get("/analyses/{analysis_id}/versions/{version_id}",
            response_model=GetAnalysisVersionOutput)
def get_analysis_version(case_id: str, analysis_id: str, version_id: str,
                         session: Session = Depends(get_db)) -> GetAnalysisVersionOutput:
    return analysis.get_analysis_version(
        session, case_id=uuid_of(case_id, "case_id"),
        analysis_id=uuid_of(analysis_id, "analysis_id"),
        version_id=uuid_of(version_id, "version_id"),
    )


@router.post("/analyses/{analysis_id}/versions/{version_id}/review",
             response_model=ReviewAnalysisOutput)
def review_analysis(case_id: str, analysis_id: str, version_id: str,
                    payload: ReviewAnalysisInput,
                    session: Session = Depends(get_db)) -> ReviewAnalysisOutput:
    _match(case_id, payload.case_id, "case_id")
    _match(analysis_id, payload.analysis_id, "analysis_id")
    _match(version_id, payload.version_id, "version_id")
    return analysis.review_analysis(
        session, case_id=payload.case_id, analysis_id=payload.analysis_id,
        version_id=payload.version_id, expected_revision=payload.expected_revision,
        review_status=payload.review_status,
    )
