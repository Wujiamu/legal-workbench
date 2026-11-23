"""Parse-run orchestration: per-unit checkpoints, coverage and active-run publish."""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .. import models
from ..config import load_settings
from ..db import begin_write
from ..errors import ErrorCode, WorkbenchError
from ..storage import Storage
from ..textnorm import normalize_with_map
from .base import ParseResult, ParsedUnit
from .dispatch import IMAGE_EXTENSIONS, parse_file
from .pdf import PDF_CONFIG, iter_pdf_units, pdf_page_count

PARSER_VERSION = "pipeline-1.0"
TERMINAL_UNIT_STATUSES = {"extracted", "ocr_done", "no_text_detected", "failed", "unsupported"}


def create_parse_run(session: Session, *, case_id: uuid.UUID, file_version_id: uuid.UUID,
                     force_ocr_pages: list[int] | None = None,
                     commit: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    """Queue a parse run for a file version.

    ``commit=False`` keeps the run and its job inside the caller's transaction.
    The default commits, which is what the HTTP and CLI entry points want; the
    registration path passes False so the revision bump, the new version and the
    job all become visible at one point — otherwise a later failure rolled back
    the version while the job and revision stayed committed.
    """
    version = session.get(models.FileVersion, file_version_id)
    if version is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "文件版本不存在",
                             {"file_version_id": str(file_version_id)})
    if version.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "文件版本不属于该案件",
                             {"file_version_id": str(file_version_id)})
    pages = sorted({int(page) for page in (force_ocr_pages or [])})
    if pages:
        stored = session.get(models.StoredObject, version.stored_object_id)
        if stored is None or not stored.relative_path.lower().endswith(".pdf"):
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "仅 PDF 支持指定页强制 OCR")
        if any(page < 1 for page in pages):
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "页码必须从 1 开始")
    max_sequence = session.execute(
        select(models.ParseRun.sequence)
        .where(models.ParseRun.file_version_id == file_version_id)
        .order_by(models.ParseRun.sequence.desc()).limit(1)
    ).scalar_one_or_none()
    sequence = 0 if max_sequence is None else max_sequence + 1
    config = {"parser_version": PARSER_VERSION, "quality": dict(PDF_CONFIG),
              "force_ocr_pages": pages}
    begin_write(session)
    run = models.ParseRun(
        id=uuid.uuid4(), case_id=case_id, file_version_id=file_version_id,
        parser_version=PARSER_VERSION, config=config, sequence=sequence, status="queued",
        coverage={},
    )
    session.add(run)
    session.flush()
    job = models.Job(
        id=uuid.uuid4(), case_id=case_id, kind="parse", target_id=run.id,
        status="queued", total_units=0,
    )
    session.add(job)
    if commit:
        session.commit()
    else:
        session.flush()
    return run.id, job.id


def reparse(session: Session, *, case_id: uuid.UUID, file_version_id: uuid.UUID,
            force_ocr_pages: list[int] | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    return create_parse_run(session, case_id=case_id, file_version_id=file_version_id,
                            force_ocr_pages=force_ocr_pages)


def _unit_counts(units: list[models.ParseUnit]) -> dict[str, int]:
    counts = {"complete": 0, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0}
    for unit in units:
        if unit.status in ("extracted", "ocr_done", "no_text_detected"):
            counts["complete"] += 1
        elif unit.status == "failed":
            counts["failed"] += 1
        elif unit.status == "unsupported":
            counts["unsupported"] += 1
        else:
            counts["pending"] += 1
        if unit.warnings:
            counts["partial"] += 1
    return counts


def _iter_units(path: Path, settings, force_pages: set[int],
                sink: list[ParseResult] | None = None,
                skip_keys: set[str] | None = None) -> Iterator[ParsedUnit]:
    """Yield units, collecting the result-level outcome into ``sink``.

    Parsers report file-level gaps (page headers, embedded images, charts) on
    ``ParseResult``, not on individual units.  Yielding only ``result.units``
    silently dropped them, so a document with an unextracted header counted as
    fully complete.  The result object is handed back through ``sink`` so the
    caller can persist it.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        yield from iter_pdf_units(path, settings, force_pages or None, skip_keys)
        return
    result = parse_file(path)
    if sink is not None:
        sink.append(result)
    yield from result.units


def _page_of_key(unit_key: str) -> int | None:
    """``pdf:12`` -> 12; other formats have no page concept."""
    prefix, _, rest = unit_key.partition(":")
    if prefix == "pdf" and rest.isdigit():
        return int(rest)
    return None


def _copy_base_unit(session: Session, base_run: models.ParseRun,
                    unit_key: str) -> ParsedUnit | None:
    """Reuse a finished unit from the base run inside the new run.

    A page with no text still has to be carried over.  Returning None for it
    dropped the page from the new run entirely: forcing OCR on page 2 of a
    two-page scan produced a "complete" run that only covered page 2, and the
    job reported 1/2 while claiming success.
    """
    base = session.execute(
        select(models.ParseUnit).where(models.ParseUnit.parse_run_id == base_run.id,
                                       models.ParseUnit.unit_key == unit_key)
    ).scalar_one_or_none()
    if base is None or base.status not in TERMINAL_UNIT_STATUSES:
        return None
    segment = session.execute(
        select(models.Segment).where(models.Segment.parse_unit_id == base.id)
    ).scalars().first()
    if segment is None:
        # No text to reuse, but the unit itself must survive: keep its status so
        # a blank or failed page is visibly blank or failed, not missing.
        page = _page_of_key(unit_key)
        locator = {"page": page} if page is not None else {}
        return ParsedUnit(unit_key=unit_key, locator=locator, text="",
                          method=base.method or "ocr", status=base.status,
                          error=base.error, warnings=list(base.warnings or []))
    return ParsedUnit(unit_key=unit_key, locator=segment.locator, text=segment.text,
                      method=base.method or "native", status=base.status,
                      warnings=list(base.warnings or []))


def _persist_unit(session: Session, run: models.ParseRun, job_id: uuid.UUID,
                  unit: ParsedUnit, total_hint: int) -> None:
    begin_write(session)
    existing = session.execute(
        select(models.ParseUnit).where(models.ParseUnit.parse_run_id == run.id,
                                       models.ParseUnit.unit_key == unit.unit_key)
    ).scalar_one_or_none()
    if existing is not None:
        session.execute(models.ExactValue.__table__.delete().where(
            models.ExactValue.segment_id.in_(
                select(models.Segment.id).where(models.Segment.parse_unit_id == existing.id)
            )))
        session.execute(models.Segment.__table__.delete().where(
            models.Segment.parse_unit_id == existing.id))
        session.delete(existing)
        session.flush()
    row = models.ParseUnit(
        id=uuid.uuid4(), case_id=run.case_id, parse_run_id=run.id, unit_key=unit.unit_key,
        status=unit.status, method=unit.method, error=unit.error,
        warnings=list(unit.warnings),
    )
    session.add(row)
    session.flush()
    if unit.text and unit.status in ("extracted", "ocr_done"):
        normalized, runs = normalize_with_map(unit.text)
        segment = models.Segment(
            id=uuid.uuid4(), case_id=run.case_id, parse_run_id=run.id, parse_unit_id=row.id,
            locator=unit.locator, text=unit.text, normalized_text=normalized, offset_map=runs,
        )
        session.add(segment)
        session.flush()
        try:
            from ..search import extract_exact_values
            extract_exact_values(session, segment)
        except ImportError:  # search module arrives in T11
            pass
    job = session.get(models.Job, job_id)
    job.completed_units = session.query(models.ParseUnit).filter(
        models.ParseUnit.parse_run_id == run.id,
        models.ParseUnit.status.in_(list(TERMINAL_UNIT_STATUSES)),
    ).count()
    job.total_units = max(total_hint, job.completed_units)
    session.commit()


def _refresh_coverage(session: Session, run: models.ParseRun, job_id: uuid.UUID,
                      result: ParseResult | None = None, commit: bool = True) -> dict[str, int]:
    """Recompute coverage counts and progress for ``run``.

    ``commit=False`` leaves the transaction open so the caller can commit the
    terminal status and the active-version switch together.  Committing here
    used to split those two writes, and a crash in between left a succeeded job
    whose run was never published.
    """
    units = session.execute(
        select(models.ParseUnit).where(models.ParseUnit.parse_run_id == run.id)
    ).scalars().all()
    counts = _unit_counts(units)
    previous = run.coverage or {}
    if result is not None:
        warnings = list(result.warnings)
        parser_status = result.status
    else:
        # Keep whatever the parsing pass already recorded: _finish runs after
        # the units are stored and has no ParseResult to hand in.
        warnings = list(previous.get("warnings") or [])
        parser_status = previous.get("parser_status")
    if parser_status in ("partial", "unsupported", "failed"):
        # The parser's own verdict outranks the unit tally: a DOCX with an
        # unextracted header has one complete unit yet is not fully covered.
        counts["partial"] = max(counts["partial"], 1)
    run.coverage = dict(counts, total=len(units), warnings=warnings,
                        parser_status=parser_status)
    job = session.get(models.Job, job_id)
    job.completed_units = len(units)
    job.total_units = max(job.total_units, len(units))
    if commit:
        session.commit()
    else:
        session.flush()
    return dict(counts, total=len(units))


def _publish_active(session: Session, run: models.ParseRun) -> bool:
    """Point the file version at ``run`` if it is still the newest usable run.

    Assumes the caller already holds a write transaction (see ``_finish``).
    """
    version = session.get(models.FileVersion, run.file_version_id)
    active_id = version.active_parse_run_id
    if active_id is not None:
        active = session.get(models.ParseRun, active_id)
        if active is not None and active.sequence >= run.sequence:
            return False
        if run.status != "complete":
            return False
    elif run.status not in ("complete", "partial"):
        return False
    version.active_parse_run_id = run.id
    return True


def process_parse_job(session_factory: sessionmaker[Session], storage: Storage,
                      job_id: uuid.UUID,
                      fault_inject: Callable[[str], None] | None = None) -> str:
    session = session_factory()
    try:
        job = session.get(models.Job, job_id)
        if job is None or job.kind != "parse":
            raise WorkbenchError(ErrorCode.NOT_FOUND, "解析任务不存在", {"job_id": str(job_id)})
        run = session.get(models.ParseRun, job.target_id)
        if run is None:
            raise WorkbenchError(ErrorCode.NOT_FOUND, "解析版本不存在", {"job_id": str(job_id)})
        if run.status in ("complete", "partial", "unsupported"):
            # A previous attempt may have died between committing the terminal
            # status and switching the active run.  Publishing is idempotent, so
            # finish the job here rather than short-circuiting on the status.
            begin_write(session)
            _publish_active(session, run)
            job.status = "succeeded" if run.status == "complete" else "failed"
            session.commit()
            return run.status
        begin_write(session)
        run.status = "running"
        job.status = "running"
        job.error = None
        session.commit()
        version = session.get(models.FileVersion, run.file_version_id)
        stored = session.get(models.StoredObject, version.stored_object_id)
        path = storage.managed_path(stored.relative_path)
        if not storage.verify(stored.relative_path, stored.sha256, stored.size):
            stored.integrity_status = "integrity_error"
            _finish(session, run, job, "failed", "原件校验失败，拒绝解析")
            return "failed"
        settings = load_settings()
        force_pages = {int(page) for page in (run.config.get("force_ocr_pages") or [])}
        base_run = None
        if force_pages and version.active_parse_run_id:
            base_run = session.get(models.ParseRun, version.active_parse_run_id)
        existing = session.execute(
            select(models.ParseUnit).where(models.ParseUnit.parse_run_id == run.id)
        ).scalars().all()
        for unit in existing:
            if unit.status not in TERMINAL_UNIT_STATUSES:
                begin_write(session)
                session.execute(models.Segment.__table__.delete().where(
                    models.Segment.parse_unit_id == unit.id))
                session.delete(unit)
                session.commit()
        done_keys = {unit.unit_key for unit in existing
                     if unit.status in TERMINAL_UNIT_STATUSES}
        _refresh_coverage(session, run, job.id)
        total_hint = _total_hint(path)
        processed = 0
        results: list[ParseResult] = []
        # Pages that already have a usable result in the base run and were not
        # asked to be re-OCR'd are copied without parsing.  The set is computed
        # up front so the PDF iterator can skip them before rendering: deciding
        # after the fact still paid the OCR cost and let a missing model abort an
        # otherwise complete recovery.
        copy_keys: set[str] = set()
        if base_run is not None and force_pages:
            base_keys = session.execute(
                select(models.ParseUnit.unit_key).where(
                    models.ParseUnit.parse_run_id == base_run.id,
                    models.ParseUnit.status.in_(list(TERMINAL_UNIT_STATUSES)))
            ).scalars().all()
            for key in base_keys:
                page = _page_of_key(key)
                if page is None or page not in force_pages:
                    copy_keys.add(key)
        try:
            for unit in _iter_units(path, settings, force_pages, results,
                                    skip_keys=done_keys | copy_keys):
                if unit.unit_key in done_keys:
                    continue
                _persist_unit(session, run, job.id, unit, total_hint)
                processed += 1
                if fault_inject:
                    fault_inject(f"unit:{processed}")
            for key in sorted(copy_keys - done_keys):
                copied = _copy_base_unit(session, base_run, key)
                if copied is None:
                    continue
                _persist_unit(session, run, job.id, copied, total_hint)
                processed += 1
                if fault_inject:
                    fault_inject(f"unit:{processed}")
        except RuntimeError:
            raise
        except WorkbenchError as exc:
            _finish(session, run, job, "failed", exc.message, code=exc.code)
            return "failed"
        except Exception as exc:
            _finish(session, run, job, "failed", f"{type(exc).__name__}: {exc}")
            return "failed"
        result = results[0] if results else None
        # Injectable boundaries so recovery can be exercised at each stage:
        # "units_done" before the terminal status, "before_finish" right before
        # the terminal+publish commit, "after_finish" once it is durable.
        if fault_inject:
            fault_inject("units_done")
        counts = _refresh_coverage(session, run, job.id, result)
        status = "partial" if (counts["failed"] or counts["unsupported"] or counts["partial"]) else "complete"
        if counts["total"] == 0:
            status = "failed"
        # Terminal state and active-version switch commit together: a crash in
        # between used to leave a succeeded job whose output was never
        # published, and re-entry short-circuits on the terminal status.
        if fault_inject:
            fault_inject("before_finish")
        _finish(session, run, job, status, publish=True, fault_inject=fault_inject)
        if fault_inject:
            fault_inject("after_finish")
        return status
    finally:
        session.close()


def _total_hint(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_page_count(path)
    if suffix in IMAGE_EXTENSIONS:
        try:
            with Image.open(path) as image:
                return getattr(image, "n_frames", 1)
        except Exception:
            return 0
    return 0


def _finish(session: Session, run: models.ParseRun, job: models.Job, status: str,
            error: str | None = None, code: ErrorCode | None = None,
            publish: bool = False,
            fault_inject: Callable[[str], None] | None = None) -> None:
    begin_write(session)
    run.status = status
    if code == ErrorCode.UNSUPPORTED_FORMAT:
        run.status = "unsupported"
    job.status = {"complete": "succeeded", "partial": "partial"}.get(run.status, "failed")
    if error:
        job.error = f"{code.value}: {error}" if code else error
    # No commit inside: coverage, terminal status and the active-version switch
    # are one transaction.  Anything that commits in between reopens the crash
    # window this function exists to close.
    _refresh_coverage(session, run, job.id, commit=False)
    if publish:
        # Injectable at the exact point the old code had already committed the
        # terminal status.  Nothing here may be durable before session.commit().
        if fault_inject:
            fault_inject("before_publish")
        _publish_active(session, run)
    session.commit()


def repair_unpublished_runs(session: Session) -> int:
    """Publish the run that *should* be active, for versions that lost a publish.

    Two situations are repaired, and the second was missed by the first version of
    this function:

    1. The version has no active run at all.
    2. The version still points at an older run while a newer terminal run exists
       — the exact state left by a crash during a *reparse*.

    Picking the winner must follow the same rules as a live publish, so this
    reuses ``_publish_active`` per candidate instead of ordering the query and
    taking the first row: a version that is already correctly published must be
    left alone, and a partial run must not displace a newer complete one.
    """
    versions = session.execute(
        select(models.FileVersion.id)
        .join(models.ParseRun, models.ParseRun.file_version_id == models.FileVersion.id)
        .where(models.ParseRun.status.in_(("complete", "partial")))
        .distinct()
    ).scalars().all()
    repaired = 0
    for version_id in versions:
        # Order by status first, then newest.  Walking newest-first alone let a
        # newer partial win whenever there was no active pointer, and stopped at a
        # refused partial when there was one — in both cases a complete run that
        # should have been published was left behind.
        candidate = session.execute(
            select(models.ParseRun)
            .where(models.ParseRun.file_version_id == version_id,
                   models.ParseRun.status.in_(("complete", "partial")))
            .order_by((models.ParseRun.status == "complete").desc(),
                      models.ParseRun.sequence.desc())
        ).scalars().first()
        if candidate is None:
            continue
        begin_write(session)
        if _publish_active(session, candidate):
            session.commit()
            repaired += 1
        else:
            session.rollback()
    return repaired
