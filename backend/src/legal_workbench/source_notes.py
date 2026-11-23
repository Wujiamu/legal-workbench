"""Same-case notes tied to a fixed file version, with a one-time human review."""
from pathlib import PurePosixPath

import pypdfium2 as pdfium
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import cases, models
from .pdf_preview import pdf_lock
from .storage import Storage
from .db import update_with_revision
from .errors import ErrorCode, WorkbenchError
from .schemas import (
    AnnotationOutput, CreateAnnotationInput, ListAnnotationsInput,
    ListAnnotationsOutput, ReviewAnnotationInput,
)


def _output(row, name) -> AnnotationOutput:
    return AnnotationOutput(
        annotation_id=row.id, case_id=row.case_id, file_version_id=row.file_version_id,
        page=row.page, author=row.author, kind=row.kind, body=row.body,
        reviewed_text=row.reviewed_text, revision=row.revision,
        created_at=row.created_at, display_name=name)


def create_annotation(session: Session, payload: CreateAnnotationInput, *,
                      author: str, storage: Storage) -> AnnotationOutput:
    version = session.get(models.FileVersion, payload.file_version_id)
    if version is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "文件版本不存在")
    if version.case_id != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "文件版本不属于该案件")
    case_row = session.get(models.Case, payload.case_id)
    if case_row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    if case_row.archived_at is not None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "案件已归档，标注只读；如需修改请先取消归档")
    body = payload.body.strip()
    if not body:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "标注不能为空")
    if payload.page is not None:
        stored = session.get(models.StoredObject, version.stored_object_id)
        if PurePosixPath(stored.relative_path).suffix.lower() != ".pdf":
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "页码仅适用于 PDF")
        if stored.integrity_status != "ok" or not storage.verify(
                stored.relative_path, stored.sha256, stored.size):
            raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "无法核验标注所指的文件")
        try:
            # PDFium's native API requires serialized access within a process.
            with pdf_lock, pdfium.PdfDocument(storage.managed_path(stored.relative_path)) as document:
                if payload.page > len(document):
                    raise WorkbenchError(ErrorCode.INVALID_INPUT, "标注页码超出 PDF 范围")
        except (pdfium.PdfiumError, ValueError, FileNotFoundError) as exc:
            raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "无法读取 PDF 页码") from exc
    cases.lock_case_writable(
        session, payload.case_id, archived_message="案件已归档，标注只读；如需修改请先取消归档"
    )
    row = models.Annotation(case_id=payload.case_id, file_version_id=version.id,
                            author=author, kind=payload.kind, body=body, page=payload.page)
    session.add(row)
    session.flush()
    result = _output(row, session.get(models.Material, version.material_id).display_name)
    session.commit()
    return result


def list_annotations(session: Session, payload: ListAnnotationsInput) -> ListAnnotationsOutput:
    if session.get(models.Case, payload.case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    terms = [models.Annotation.case_id == payload.case_id]
    if payload.file_version_id:
        terms.append(models.Annotation.file_version_id == payload.file_version_id)
    if payload.query and payload.query.strip():
        query = payload.query.strip()
        terms.append(or_(models.Annotation.body.icontains(query, autoescape=True),
                         models.Annotation.reviewed_text.icontains(query, autoescape=True)))
    total = session.scalar(select(func.count()).select_from(models.Annotation).where(*terms))
    rows = session.execute(
        select(models.Annotation, models.Material.display_name)
        .join(models.FileVersion, models.Annotation.file_version_id == models.FileVersion.id)
        .join(models.Material, models.FileVersion.material_id == models.Material.id)
        .where(*terms).order_by(models.Annotation.created_at.desc(), models.Annotation.id)
        .offset(payload.offset).limit(payload.limit))
    return ListAnnotationsOutput(items=[_output(row, name) for row, name in rows],
                                 total=total, offset=payload.offset, limit=payload.limit)


def review_annotation(session: Session, payload: ReviewAnnotationInput) -> AnnotationOutput:
    row = session.get(models.Annotation, payload.annotation_id)
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "标注不存在")
    if row.case_id != payload.case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "标注不属于该案件")
    if row.author != "agent":
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "人工标注无需复核")
    reviewed = payload.reviewed_text.strip()
    if not reviewed:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "确认内容不能为空")
    cases.lock_case_writable(
        session, payload.case_id, archived_message="案件已归档，标注只读；如需修改请先取消归档"
    )
    if not update_with_revision(session, models.Annotation, row.id, 0, reviewed_text=reviewed):
        session.rollback()
        raise WorkbenchError(ErrorCode.CONFLICT, "该标注已经复核")
    session.refresh(row)
    version = session.get(models.FileVersion, row.file_version_id)
    result = _output(row, session.get(models.Material, version.material_id).display_name)
    session.commit()
    return result
