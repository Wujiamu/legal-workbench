"""Case search: literal/identifier/amount/date modes over immutable segments.

Amount and date hits come from ExactValue rows, never from plain text matching.
"""
from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from plainfolio_parse import exact_values as exact_values_core
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from . import models
from .coverage import coverage_for_version, latest_run, run_coverage
from .errors import ErrorCode, WorkbenchError
from .schemas import (Coverage, ExtractedSegment, ExtractedTextOutput, ParseUnitStatus,
                      ReadSourceOutput, SearchCaseOutput, SearchHit, SearchMode, SearchScope,
                      SourceLocation)
from .textnorm import normalize_with_map, original_index

CONTEXT_CHARS = 80
_WHITESPACE = re.compile(r"\s+")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_query(query: str) -> str:
    normalized, _runs = normalize_with_map(query)
    return normalized.strip()


def _active_runs_query(case_id: uuid.UUID, scope: SearchScope):
    query = (
        select(models.ParseRun)
        .join(models.FileVersion, models.ParseRun.file_version_id == models.FileVersion.id)
        .where(models.ParseRun.case_id == case_id,
               models.FileVersion.active_parse_run_id == models.ParseRun.id)
    )
    if scope == SearchScope.ORIGINALS:
        query = query.where(models.FileVersion.kind == "original")
    elif scope == SearchScope.WORKING:
        query = query.where(models.FileVersion.kind == "working")
    else:
        query = query.where(False)
    return query


def _active_runs(session: Session, case_id: uuid.UUID, scope: SearchScope) -> list[models.ParseRun]:
    if scope == SearchScope.ANALYSIS:
        return []
    return list(session.scalars(_active_runs_query(case_id, scope)))


def _coverage(session: Session, case_id: uuid.UUID, scope: SearchScope) -> Coverage:
    if scope == SearchScope.ANALYSIS:
        versions = session.execute(
            select(models.AnalysisVersion).join(
                models.Analysis, models.Analysis.current_version_id == models.AnalysisVersion.id)
            .where(models.AnalysisVersion.case_id == case_id)
        ).scalars().all()
        return Coverage(complete=len(versions), partial=0, failed=0, pending=0, unsupported=0)
    runs = _active_runs(session, case_id, scope)
    totals = {"complete": 0, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0}
    covered: set[uuid.UUID] = set()
    for run in runs:
        covered.add(run.file_version_id)
        _add_coverage(totals, run_coverage(session, run))
    # A material whose parse never published a run is still awaiting indexing; it
    # must be visible in coverage rather than silently absent from the totals.
    # Resolved in one query per table: per-material lookups dominated latency.
    materials = session.execute(
        select(models.Material.original_file_version_id)
        .where(models.Material.case_id == case_id)
    ).scalars().all()
    missing_versions = [vid for vid in materials if vid is not None and vid not in covered]
    pending_versions = [vid for vid in materials if vid is None]
    totals["pending"] += len(pending_versions)
    if missing_versions:
        latest_runs = session.execute(
            select(models.ParseRun)
            .where(models.ParseRun.file_version_id.in_(missing_versions))
            .order_by(models.ParseRun.file_version_id,
                      models.ParseRun.sequence.desc(), models.ParseRun.created_at.desc())
        ).scalars().all()
        newest: dict[uuid.UUID, models.ParseRun] = {}
        for run in latest_runs:
            newest.setdefault(run.file_version_id, run)
        for version_id in missing_versions:
            run = newest.get(version_id)
            if run is None:
                totals["pending"] += 1
            else:
                _add_coverage(totals, run_coverage(session, run))
    return Coverage(**totals)


def _add_coverage(totals: dict[str, int], coverage: Coverage) -> None:
    for key in totals:
        totals[key] += getattr(coverage, key)


def _eager_context(session: Session, segments: list[models.Segment]) -> dict:
    """Resolve run/version/unit for a batch in one query.

    Per-hit ``session.get`` calls made a 20-hit page cost 60 extra round trips,
    which dominated short-query latency on a 33k-segment corpus.
    """
    if not segments:
        return {}
    run_ids = {segment.parse_run_id for segment in segments}
    unit_ids = {segment.parse_unit_id for segment in segments}
    runs = {run.id: run for run in session.execute(
        select(models.ParseRun).where(models.ParseRun.id.in_(run_ids))).scalars()}
    versions = {version.id: version for version in session.execute(
        select(models.FileVersion).where(
            models.FileVersion.id.in_({run.file_version_id for run in runs.values()})
        )).scalars()}
    units = {unit.id: unit for unit in session.execute(
        select(models.ParseUnit).where(models.ParseUnit.id.in_(unit_ids))).scalars()}
    return {"runs": runs, "versions": versions, "units": units}


def _segment_hit(session: Session, segment: models.Segment, norm_start: int, norm_end: int,
                 scope: SearchScope, amount: Decimal | None = None,
                 currency: str | None = None, preloaded: dict | None = None) -> SearchHit:
    if preloaded is None:
        preloaded = _eager_context(session, [segment])
    run = preloaded["runs"][segment.parse_run_id]
    version = preloaded["versions"][run.file_version_id]
    unit = preloaded["units"].get(segment.parse_unit_id)
    runs = segment.offset_map or []
    start = original_index(runs, norm_start)
    end_index = original_index(runs, max(norm_end - 1, norm_start))
    if start is None or end_index is None:
        start, end_index = 0, max(len(segment.text) - 1, 0)
    end = end_index + 1
    ctx_start = max(0, start - CONTEXT_CHARS)
    ctx_end = min(len(segment.text), end + CONTEXT_CHARS)
    context = segment.text[ctx_start:ctx_end]
    source = SourceLocation(
        source_ref=segment.id, material_id=version.material_id,
        file_version_id=version.id, parse_run_id=run.id, locator=segment.locator,
        text=segment.text, method=(unit.method if unit else "native") or "native",
    )
    return SearchHit(source=source, source_type=scope, context=context,
                     match_start=start - ctx_start, match_end=end - ctx_start,
                     amount=amount, currency=currency)


def _literal_hits(session: Session, *, case_id: uuid.UUID, query: str, scope: SearchScope,
                  offset: int, limit: int) -> tuple[list[SearchHit], int]:
    terms = [term for term in _normalize_query(query).split(" ") if term]
    if not terms:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "检索词不能为空")
    runs = _active_runs(session, case_id, scope)
    if not runs:
        return [], 0
    run_ids = [run.id for run in runs]
    conditions = [models.Segment.case_id == case_id, models.Segment.parse_run_id.in_(run_ids)]
    for term in terms:
        conditions.append(models.Segment.normalized_text.like(
            f"%{_escape_like(term)}%", escape="\\"))
    base = (
        select(models.Segment, models.Material.display_name)
        .join(models.ParseRun, models.Segment.parse_run_id == models.ParseRun.id)
        .join(models.FileVersion, models.ParseRun.file_version_id == models.FileVersion.id)
        .join(models.Material, models.FileVersion.material_id == models.Material.id)
        .where(and_(*conditions))
    )
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    normalized_query = _normalize_query(query)
    stem = func.regexp_replace(models.Material.display_name, r"\.[^.]+$", "")
    filename_rank = case((or_(func.lower(models.Material.display_name) == normalized_query.lower(),
                              func.lower(stem) == normalized_query.lower()), 0),
                         else_=2)
    segment_rank = case((models.Segment.normalized_text == normalized_query, 1), else_=2)
    rank = case((filename_rank == 0, 0), (segment_rank == 1, 1), else_=2)
    rows = session.execute(
        base.add_columns(rank.label("rank"))
        .order_by(rank, models.Segment.id)
        .offset(offset).limit(limit)
    ).all()
    segments = [row[0] for row in rows]
    preloaded = _eager_context(session, segments)
    hits = []
    for segment, _name, _rank in rows:
        norm_text = segment.normalized_text
        position = norm_text.find(normalized_query)
        if position < 0:
            position = min((norm_text.find(term) for term in terms
                            if norm_text.find(term) >= 0), default=0)
            length = len(terms[0]) if terms else 1
        else:
            length = len(normalized_query)
        hits.append(_segment_hit(session, segment, position, position + max(length, 1), scope,
                                 preloaded=preloaded))
    return hits, total


def _boundary_ok(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    allowed = lambda ch: ch == "" or not (ch.isalnum() or ch in "_-")
    return allowed(before) and allowed(after)


def _identifier_hits(session: Session, *, case_id: uuid.UUID, query: str, scope: SearchScope,
                     offset: int, limit: int) -> tuple[list[SearchHit], int]:
    normalized = _normalize_query(query)
    if not normalized:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "编号检索词不能为空")
    if scope == SearchScope.ANALYSIS:
        return _analysis_hits(session, case_id=case_id, query=normalized, mode=SearchMode.IDENTIFIER,
                              offset=offset, limit=limit)
    runs = _active_runs(session, case_id, scope)
    if not runs:
        return [], 0
    rows = session.execute(
        select(models.Segment)
        .where(models.Segment.case_id == case_id,
               models.Segment.parse_run_id.in_([run.id for run in runs]),
               models.Segment.normalized_text.like(f"%{_escape_like(normalized)}%", escape="\\"))
        .order_by(models.Segment.id)
    ).scalars().all()
    preloaded = _eager_context(session, rows)
    hits: list[SearchHit] = []
    for segment in rows:
        start = 0
        while True:
            position = segment.normalized_text.find(normalized, start)
            if position < 0:
                break
            end = position + len(normalized)
            if _boundary_ok(segment.normalized_text, position, end):
                hits.append(_segment_hit(session, segment, position, end, scope,
                                         preloaded=preloaded))
            start = position + 1
    total = len(hits)
    return hits[offset:offset + limit], total


def parse_amount_query(query: str) -> Decimal:
    cleaned = _normalize_query(query).replace(",", "").replace(" ", "")
    cleaned = cleaned.replace("人民币", "").replace("CNY", "").replace("RMB", "")
    cleaned = cleaned.replace("USD", "").replace("美元", "")
    multiplier = Decimal(1)
    for unit, factor in (("万元", Decimal(10000)), ("万", Decimal(10000)), ("元", Decimal(1))):
        if cleaned.endswith(unit):
            cleaned = cleaned[: -len(unit)]
            multiplier = factor
            break
    try:
        value = Decimal(cleaned) * multiplier
    except InvalidOperation as exc:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "金额检索词无法解析", {"query": query}) from exc
    if value < 0:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "金额不能为负数")
    return value


def _amount_hits(session: Session, *, case_id: uuid.UUID, query: str, currency: str,
                 scope: SearchScope, offset: int, limit: int) -> tuple[list[SearchHit], int]:
    if scope == SearchScope.ANALYSIS:
        return [], 0
    value = parse_amount_query(query)
    runs = _active_runs(session, case_id, scope)
    if not runs:
        return [], 0
    query_stmt = (
        select(models.ExactValue, models.Segment)
        .join(models.Segment, models.ExactValue.segment_id == models.Segment.id)
        .where(models.ExactValue.case_id == case_id,
               models.ExactValue.kind == "amount",
               models.ExactValue.currency == currency,
               models.ExactValue.amount_value == value,
               models.Segment.parse_run_id.in_([run.id for run in runs]))
        .order_by(models.Segment.id)
    )
    total = session.execute(select(func.count()).select_from(query_stmt.subquery())).scalar_one()
    rows = session.execute(query_stmt.offset(offset).limit(limit)).all()
    hits = []
    for exact, segment in rows:
        span = exact.span or {}
        start = int(span.get("start", 0))
        end = int(span.get("end", start + len(exact.raw_text)))
        # ExactValue spans are original-text offsets; build the hit from the original span.
        ctx_start = max(0, start - CONTEXT_CHARS)
        ctx_end = min(len(segment.text), end + CONTEXT_CHARS)
        run = session.get(models.ParseRun, segment.parse_run_id)
        version = session.get(models.FileVersion, run.file_version_id)
        unit = session.get(models.ParseUnit, segment.parse_unit_id)
        hits.append(SearchHit(
            source=SourceLocation(source_ref=segment.id, material_id=version.material_id,
                                  file_version_id=version.id, parse_run_id=run.id,
                                  locator=segment.locator, text=segment.text,
                                  method=(unit.method if unit else "native") or "native"),
            source_type=scope, context=segment.text[ctx_start:ctx_end],
            match_start=start - ctx_start, match_end=end - ctx_start,
            amount=exact.amount_value, currency=exact.currency,
        ))
    return hits, total


def _parse_date_query(query: str) -> str:
    normalized = _normalize_query(query)
    match = re.fullmatch(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", normalized)
    if not match:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "日期必须为完整年月日", {"query": query})
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "日期不存在", {"query": query}) from exc


def _date_hits(session: Session, *, case_id: uuid.UUID, query: str, scope: SearchScope,
               offset: int, limit: int) -> tuple[list[SearchHit], int]:
    if scope == SearchScope.ANALYSIS:
        return [], 0
    iso = _parse_date_query(query)
    runs = _active_runs(session, case_id, scope)
    if not runs:
        return [], 0
    query_stmt = (
        select(models.ExactValue, models.Segment)
        .join(models.Segment, models.ExactValue.segment_id == models.Segment.id)
        .where(models.ExactValue.case_id == case_id,
               models.ExactValue.kind == "date",
               models.ExactValue.canonical_value["date"].astext == iso,
               models.Segment.parse_run_id.in_([run.id for run in runs]))
        .order_by(models.Segment.id)
    )
    total = session.execute(select(func.count()).select_from(query_stmt.subquery())).scalar_one()
    rows = session.execute(query_stmt.offset(offset).limit(limit)).all()
    hits = []
    for exact, segment in rows:
        span = exact.span or {}
        start = int(span.get("start", 0))
        end = int(span.get("end", start + len(exact.raw_text)))
        ctx_start = max(0, start - CONTEXT_CHARS)
        ctx_end = min(len(segment.text), end + CONTEXT_CHARS)
        run = session.get(models.ParseRun, segment.parse_run_id)
        version = session.get(models.FileVersion, run.file_version_id)
        unit = session.get(models.ParseUnit, segment.parse_unit_id)
        hits.append(SearchHit(
            source=SourceLocation(source_ref=segment.id, material_id=version.material_id,
                                  file_version_id=version.id, parse_run_id=run.id,
                                  locator=segment.locator, text=segment.text,
                                  method=(unit.method if unit else "native") or "native"),
            source_type=scope, context=segment.text[ctx_start:ctx_end],
            match_start=start - ctx_start, match_end=end - ctx_start,
        ))
    return hits, total


def _analysis_hits(session: Session, *, case_id: uuid.UUID, query: str, mode: SearchMode,
                   offset: int, limit: int) -> tuple[list[SearchHit], int]:
    versions = session.execute(
        select(models.AnalysisVersion, models.Analysis)
        .join(models.Analysis, models.Analysis.current_version_id == models.AnalysisVersion.id)
        .where(models.AnalysisVersion.case_id == case_id)
        .order_by(models.AnalysisVersion.id)
    ).all()
    hits: list[SearchHit] = []
    for version, analysis in versions:
        normalized, runs = normalize_with_map(version.content)
        start = 0
        while True:
            position = normalized.find(query, start)
            if position < 0:
                break
            end = position + len(query)
            if mode == SearchMode.IDENTIFIER and not _boundary_ok(normalized, position, end):
                start = position + 1
                continue
            original_start = original_index(runs, position) or 0
            original_end = (original_index(runs, end - 1) or original_start) + 1
            ctx_start = max(0, original_start - CONTEXT_CHARS)
            ctx_end = min(len(version.content), original_end + CONTEXT_CHARS)
            hits.append(SearchHit(
                source=None, analysis_id=analysis.id, analysis_version_id=version.id,
                source_type=SearchScope.ANALYSIS,
                context=version.content[ctx_start:ctx_end],
                match_start=original_start - ctx_start, match_end=original_end - ctx_start,
            ))
            start = position + 1
    total = len(hits)
    return hits[offset:offset + limit], total


def search_case(session: Session, *, case_id: uuid.UUID, query: str, mode: SearchMode,
                scope: SearchScope = SearchScope.ORIGINALS, currency: str | None = None,
                offset: int = 0, limit: int = 20) -> SearchCaseOutput:
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
    if mode == SearchMode.AMOUNT and not currency:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "金额检索必须提供币种")
    if mode == SearchMode.LITERAL:
        if scope == SearchScope.ANALYSIS:
            hits, total = _analysis_hits(session, case_id=case_id,
                                         query=_normalize_query(query),
                                         mode=mode, offset=offset, limit=limit)
        else:
            hits, total = _literal_hits(session, case_id=case_id, query=query, scope=scope,
                                        offset=offset, limit=limit)
    elif mode == SearchMode.IDENTIFIER:
        hits, total = _identifier_hits(session, case_id=case_id, query=query, scope=scope,
                                       offset=offset, limit=limit)
    elif mode == SearchMode.AMOUNT:
        hits, total = _amount_hits(session, case_id=case_id, query=query, currency=currency,
                                   scope=scope, offset=offset, limit=limit)
    elif mode == SearchMode.DATE:
        hits, total = _date_hits(session, case_id=case_id, query=query, scope=scope,
                                 offset=offset, limit=limit)
    else:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "未知检索模式")
    return SearchCaseOutput(hits=hits, coverage=_coverage(session, case_id, scope),
                            mode_used=mode, offset=offset, limit=limit, total=total)


def read_source(session: Session, *, case_id: uuid.UUID, source_ref: uuid.UUID,
                context_chars: int = 200) -> ReadSourceOutput:
    segment = session.get(models.Segment, source_ref)
    if segment is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "来源引用不存在", {"source_ref": str(source_ref)})
    run = session.get(models.ParseRun, segment.parse_run_id)
    if run.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "来源引用不属于该案件",
                             {"source_ref": str(source_ref)})
    version = session.get(models.FileVersion, run.file_version_id)
    unit = session.get(models.ParseUnit, segment.parse_unit_id)
    unit_rows = session.execute(
        select(models.ParseUnit.id, models.ParseUnit.unit_key)
        .where(models.ParseUnit.parse_run_id == run.id)
    ).all()
    unit_rows.sort(key=lambda row: [int(part) if part.isdigit() else part
                                    for part in re.split(r"(\d+)", row[1])])
    unit_ids = [row[0] for row in unit_rows]
    position = unit_ids.index(segment.parse_unit_id) if segment.parse_unit_id in unit_ids else 0
    before = ""
    after = ""
    if position > 0:
        previous = session.execute(
            select(models.Segment).where(models.Segment.parse_unit_id == unit_ids[position - 1])
        ).scalars().first()
        if previous is not None:
            before = previous.text[-context_chars:] if context_chars else ""
    if position + 1 < len(unit_ids):
        following = session.execute(
            select(models.Segment).where(models.Segment.parse_unit_id == unit_ids[position + 1])
        ).scalars().first()
        if following is not None:
            after = following.text[:context_chars] if context_chars else ""
    material = session.get(models.Material, version.material_id)
    intake_item = session.get(models.IntakeItem, material.intake_item_id) if material else None
    intake = session.get(models.Intake, intake_item.intake_id) if intake_item else None
    return ReadSourceOutput(
        acquisition=material.acquisition if material else None,
        display_name=material.display_name if material else None,
        source_label=intake.source_label if intake else None,
        relative_source=intake_item.relative_source if intake_item else None,
        is_original=bool(material and material.original_file_version_id == version.id),
        source_ref=segment.id, material_id=version.material_id, file_version_id=version.id,
        parse_run_id=run.id, locator=segment.locator, text=segment.text,
        method=(unit.method if unit else "native") or "native",
        context_before=before, context_after=after,
    )


def get_index_status(session: Session, *, case_id: uuid.UUID) -> Coverage:
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
    return _coverage(session, case_id, SearchScope.ORIGINALS)


def extracted_text(session: Session, *, case_id: uuid.UUID,
                   file_version_id: uuid.UUID) -> ExtractedTextOutput:
    """Return the active run's segments in document order for side-by-side viewing."""
    version = session.get(models.FileVersion, file_version_id)
    if version is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "文件版本不存在",
                             {"file_version_id": str(file_version_id)})
    if version.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "文件版本不属于该案件",
                             {"file_version_id": str(file_version_id)})
    run = latest_run(session, version)
    if run is None:
        return ExtractedTextOutput(file_version_id=version.id, parse_run_id=None,
                                   coverage=coverage_for_version(session, version), segments=[])
    rows = session.execute(
        select(models.Segment, models.ParseUnit)
        .join(models.ParseUnit, models.Segment.parse_unit_id == models.ParseUnit.id)
        .where(models.Segment.parse_run_id == run.id)
    ).all()
    rows.sort(key=lambda row: _unit_sort_key(row[1].unit_key))
    segments = [
        ExtractedSegment(source_ref=segment.id, locator=segment.locator, text=segment.text,
                         method=unit.method or "native",
                         status=ParseUnitStatus(unit.status))
        for segment, unit in rows
    ]
    return ExtractedTextOutput(file_version_id=version.id, parse_run_id=run.id,
                               coverage=_coverage_for_version(session, version), segments=segments)


def _unit_sort_key(unit_key: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", unit_key)]


def _coverage_for_version(session: Session, version: models.FileVersion) -> Coverage:
    return coverage_for_version(session, version)


# ---- exact value extraction -------------------------------------------------
#
# 提取规则（日期 / 分币种金额 / 表格单元格配对 / 编号）整体在零数据库独立包
# ``plainfolio_parse.exact_values``；本模块只负责把命中结果落成 ``ExactValue`` 行，
# 以保证检索与"精确值"面板在独立包与工作台之间口径完全一致。

def extract_exact_values(session: Session, segment: models.Segment) -> None:
    """Extract dates, currency amounts and identifiers with original-text spans."""
    hits = exact_values_core.extract_exact_values(segment.text)
    values = [
        models.ExactValue(
            id=uuid.uuid4(), case_id=segment.case_id, segment_id=segment.id, kind=hit.kind,
            raw_text=hit.raw_text, canonical_value=hit.canonical,
            amount_value=hit.amount, currency=hit.currency,
            span={"start": hit.start, "end": hit.end},
        )
        for hit in hits
    ]
    for value in values:
        session.add(value)
    if values:
        session.flush()


# 保留原有私有名字：既有测试直接引用 ``search._parse_tabular_currency``。
_parse_tabular_currency = exact_values_core.parse_tabular_currency
_extract_tabular_cell_amounts = exact_values_core.extract_tabular_cell_amounts
_overlaps = exact_values_core._overlaps
