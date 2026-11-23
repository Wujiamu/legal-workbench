"""Local human annotation and review endpoints."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import source_notes
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (
    AnnotationOutput, CreateAnnotationInput, ListAnnotationsInput,
    ListAnnotationsOutput, ReviewAnnotationInput,
)
from ..storage import Storage
from .deps import Guarded, get_db, get_storage
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}/annotations",
                   tags=["annotations"], dependencies=[Guarded])


@router.get("", response_model=ListAnnotationsOutput)
def list_annotations(case_id: str, query: str | None = Query(None, max_length=500),
                     file_version_id: UUID | None = None,
                     offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                     session: Session = Depends(get_db)) -> ListAnnotationsOutput:
    return source_notes.list_annotations(session, ListAnnotationsInput(
        case_id=uuid_of(case_id), query=query, file_version_id=file_version_id,
        offset=offset, limit=limit))


@router.post("", response_model=AnnotationOutput)
def create_annotation(case_id: str, payload: CreateAnnotationInput,
                      session: Session = Depends(get_db),
                      storage: Storage = Depends(get_storage)) -> AnnotationOutput:
    if uuid_of(case_id) != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")
    return source_notes.create_annotation(session, payload, author="human", storage=storage)


@router.post("/{annotation_id}/review", response_model=AnnotationOutput)
def review_annotation(case_id: str, annotation_id: str, payload: ReviewAnnotationInput,
                      session: Session = Depends(get_db)) -> AnnotationOutput:
    if uuid_of(case_id) != payload.case_id or uuid_of(annotation_id) != payload.annotation_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件或标注标识不一致")
    return source_notes.review_annotation(session, payload)
