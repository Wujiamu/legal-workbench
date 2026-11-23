"""Analyses with immutable Markdown versions, source references and review status."""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, cases, models
from .db import update_with_revision
from .errors import ErrorCode, WorkbenchError
from .schemas import (AnalysisSummary, AnalysisVersionSummary, GetAnalysisVersionOutput,
                      ListAnalysesOutput, ListAnalysisVersionsOutput, ReviewAnalysisOutput,
                      ReviewStatus, SaveAnalysisOutput, StatementKind)
from .storage import Storage


def _analysis_or_error(session: Session, case_id: uuid.UUID,
                       analysis_id: uuid.UUID) -> models.Analysis:
    analysis = session.get(models.Analysis, analysis_id)
    if analysis is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "分析不存在", {"analysis_id": str(analysis_id)})
    if analysis.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "分析不属于该案件",
                             {"analysis_id": str(analysis_id)})
    return analysis


def _validate_source_refs(session: Session, case_id: uuid.UUID,
                          source_refs: list[uuid.UUID]) -> None:
    for source_ref in source_refs:
        segment = session.get(models.Segment, source_ref)
        if segment is None:
            raise WorkbenchError(ErrorCode.NOT_FOUND, "来源引用不存在",
                                 {"source_ref": str(source_ref)})
        if segment.case_id != case_id:
            raise WorkbenchError(ErrorCode.CASE_MISMATCH, "来源引用不属于该案件",
                                 {"source_ref": str(source_ref)})


def _summary(session: Session, analysis: models.Analysis) -> AnalysisSummary:
    version = (session.get(models.AnalysisVersion, analysis.current_version_id)
               if analysis.current_version_id else None)
    if version is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "分析缺少当前版本",
                             {"analysis_id": str(analysis.id)})
    return AnalysisSummary(
        analysis_id=analysis.id, title=analysis.title, revision=analysis.revision,
        current_version_id=version.id, review_status=ReviewStatus(version.review_status),
        created_at=analysis.created_at,
    )


def save_analysis(session: Session, storage: Storage, *, case_id: uuid.UUID, title: str,
                  content: str, statement_kind: StatementKind,
                  source_refs: list[uuid.UUID], analysis_id: uuid.UUID | None = None,
                  expected_revision: int | None = None) -> SaveAnalysisOutput:
    if (analysis_id is None) != (expected_revision is None):
        raise WorkbenchError(ErrorCode.INVALID_INPUT,
                             "更新分析必须同时提供 analysis_id 与 expected_revision")
    _validate_source_refs(session, case_id, source_refs)
    cases.lock_case_writable(
        session, case_id, archived_message="案件已归档，分析只读；如需修改请先取消归档"
    )
    version_id = uuid.uuid4()
    relative = storage.analysis_relative(case_id, version_id)
    data = content.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if analysis_id is None:
        analysis = models.Analysis(id=uuid.uuid4(), case_id=case_id, title=title, revision=0)
        session.add(analysis)
        session.flush()
        revision = 0
    else:
        try:
            analysis = _analysis_or_error(session, case_id, analysis_id)
        except WorkbenchError:
            session.rollback()
            raise
        if analysis.revision != expected_revision:
            session.rollback()
            raise WorkbenchError(ErrorCode.CONFLICT, "分析已被修改，请刷新后重试",
                                 {"analysis_id": str(analysis_id),
                                  "expected_revision": expected_revision,
                                  "actual_revision": analysis.revision})
        revision = expected_revision + 1
    published = storage.write_immutable(relative, data)
    version = models.AnalysisVersion(
        id=version_id, case_id=case_id, analysis_id=analysis.id,
        relative_path=published.relative_path, sha256=digest,
        statement_kind=statement_kind.value, source_refs=[str(ref) for ref in source_refs],
        review_status="pending", content=content,
    )
    session.add(version)
    session.flush()
    if analysis_id is None:
        analysis.current_version_id = version.id
        analysis.title = title
        analysis.revision = 0
    else:
        if not update_with_revision(session, models.Analysis, analysis.id, expected_revision,
                                    title=title, current_version_id=version.id):
            session.rollback()
            raise WorkbenchError(ErrorCode.CONFLICT, "分析已被修改，请刷新后重试",
                                 {"analysis_id": str(analysis_id)})
        revision = expected_revision + 1
    audit.record(session, operation="analysis.save", target_type="analysis_version",
                 target_id=version.id, case_id=case_id)
    session.commit()
    return SaveAnalysisOutput(analysis_id=analysis.id, version_id=version.id, revision=revision)


def review_analysis(session: Session, *, case_id: uuid.UUID, analysis_id: uuid.UUID,
                    version_id: uuid.UUID, expected_revision: int,
                    review_status: ReviewStatus) -> ReviewAnalysisOutput:
    analysis = _analysis_or_error(session, case_id, analysis_id)
    version = session.get(models.AnalysisVersion, version_id)
    if version is None or version.analysis_id != analysis_id or version.case_id != case_id:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "分析版本不存在或不属于该分析",
                             {"version_id": str(version_id)})
    cases.lock_case_writable(
        session, case_id, archived_message="案件已归档，分析只读；如需修改请先取消归档"
    )
    if not update_with_revision(session, models.Analysis, analysis.id, expected_revision):
        session.rollback()
        raise WorkbenchError(ErrorCode.CONFLICT, "分析已被修改，请刷新后重试",
                             {"analysis_id": str(analysis_id)})
    version.review_status = review_status.value
    audit.record(session, operation="analysis.review", target_type="analysis_version",
                 target_id=version.id, case_id=case_id)
    session.commit()
    updated = session.get(models.Analysis, analysis_id)
    return ReviewAnalysisOutput(revision=updated.revision, review_status=review_status)


def list_analyses(session: Session, *, case_id: uuid.UUID, offset: int = 0,
                  limit: int = 50) -> ListAnalysesOutput:
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
    total = session.execute(
        select(func.count()).select_from(models.Analysis)
        .where(models.Analysis.case_id == case_id)
    ).scalar_one()
    rows = session.execute(
        select(models.Analysis).where(models.Analysis.case_id == case_id)
        .order_by(models.Analysis.created_at, models.Analysis.id)
        .offset(offset).limit(limit)
    ).scalars().all()
    return ListAnalysesOutput(items=[_summary(session, row) for row in rows],
                              offset=offset, limit=limit, total=total)


def get_analysis(session: Session, *, case_id: uuid.UUID,
                 analysis_id: uuid.UUID) -> AnalysisSummary:
    return _summary(session, _analysis_or_error(session, case_id, analysis_id))


def list_analysis_versions(session: Session, *, case_id: uuid.UUID, analysis_id: uuid.UUID,
                           offset: int = 0, limit: int = 50) -> ListAnalysisVersionsOutput:
    _analysis_or_error(session, case_id, analysis_id)
    total = session.execute(
        select(func.count()).select_from(models.AnalysisVersion)
        .where(models.AnalysisVersion.analysis_id == analysis_id)
    ).scalar_one()
    rows = session.execute(
        select(models.AnalysisVersion)
        .where(models.AnalysisVersion.analysis_id == analysis_id)
        .order_by(models.AnalysisVersion.created_at, models.AnalysisVersion.id)
        .offset(offset).limit(limit)
    ).scalars().all()
    return ListAnalysisVersionsOutput(
        items=[AnalysisVersionSummary(version_id=row.id,
                                      statement_kind=StatementKind(row.statement_kind),
                                      review_status=ReviewStatus(row.review_status),
                                      created_at=row.created_at)
               for row in rows],
        offset=offset, limit=limit, total=total,
    )


def get_analysis_version(session: Session, *, case_id: uuid.UUID, analysis_id: uuid.UUID,
                         version_id: uuid.UUID) -> GetAnalysisVersionOutput:
    analysis = _analysis_or_error(session, case_id, analysis_id)
    version = session.get(models.AnalysisVersion, version_id)
    if version is None or version.analysis_id != analysis_id or version.case_id != case_id:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "分析版本不存在或不属于该分析",
                             {"version_id": str(version_id)})
    return GetAnalysisVersionOutput(
        analysis_id=analysis.id, version_id=version.id, title=analysis.title,
        content=version.content, statement_kind=StatementKind(version.statement_kind),
        source_refs=[uuid.UUID(ref) for ref in (version.source_refs or [])],
        review_status=ReviewStatus(version.review_status),
    )
