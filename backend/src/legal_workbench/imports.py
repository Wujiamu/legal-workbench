"""Intake batches and bulk import from explicitly allowed local directories."""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import audit, cases, models
from .config import Settings, load_settings
from .db import begin_exclusive, begin_write
from .errors import ErrorCode, WorkbenchError
from .schemas import IntakeItem, IntakeStatus, ListIntakeItemsOutput
from .storage import StagedFile, Storage, safe_name

logger = logging.getLogger(__name__)

JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}
TERMINAL_ITEM_STATUSES = {"ready", "failed", "skipped"}


@dataclass(frozen=True)
class IntakeCreated:
    intake_id: uuid.UUID
    job_id: uuid.UUID
    created: bool
    item_count: int


@dataclass(frozen=True)
class ImportOutcome:
    job_id: uuid.UUID
    status: str
    ready: int
    failed: int
    skipped: int


def _settings(settings: Settings | None) -> Settings:
    return settings or load_settings()


def resolve_source(source_path: str, settings: Settings | None = None) -> tuple[Path, Path]:
    """Return (source, allowed_root); reject paths outside IMPORT_ROOTS or via symlinks."""
    settings = _settings(settings)
    if not settings.import_roots:
        raise WorkbenchError(ErrorCode.INVALID_PATH, "未配置允许导入的目录（LWB_IMPORT_ROOTS）")
    candidate = Path(source_path).expanduser()
    if not candidate.is_absolute():
        raise WorkbenchError(ErrorCode.INVALID_PATH, "导入来源必须是绝对路径")
    if candidate.is_symlink():
        raise WorkbenchError(ErrorCode.INVALID_PATH, "不接受符号链接来源", {"path": str(candidate)})
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "导入来源不存在", {"path": str(candidate)})
    for root in settings.import_roots:
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        return resolved, root_resolved
    raise WorkbenchError(ErrorCode.INVALID_PATH, "导入来源不在允许目录内", {"path": str(candidate)})


def _is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith(".") or name.startswith("~$")


def scan_source(source: Path, base_root: Path) -> list[tuple[str, Path | None, str | None]]:
    """List (relative_source, path, skip_reason) in deterministic order.

    ``relative_source`` is always relative to ``base_root`` because processing
    joins it back onto the intake's recorded ``source_root``; using the scanned
    subdirectory here would make a subdirectory import unresolvable later.
    """
    if source.is_file():
        return [(source.relative_to(base_root).as_posix(), source, None)]
    entries: list[tuple[str, Path | None, str | None]] = []
    for current, dirs, files in os.walk(source, followlinks=False):
        skipped_dirs = [d for d in dirs if _is_junk(d) or (Path(current) / d).is_symlink()]
        dirs[:] = sorted(d for d in dirs if d not in skipped_dirs)
        for name in sorted(skipped_dirs):
            path = Path(current) / name
            reason = "ignored_system_directory" if _is_junk(name) else "symlink_skipped"
            entries.append((path.relative_to(base_root).as_posix(), None, reason))
        for name in sorted(files):
            path = Path(current) / name
            relative = path.relative_to(base_root).as_posix()
            if _is_junk(name):
                entries.append((relative, None, "ignored_system_file"))
            elif path.is_symlink():
                entries.append((relative, None, "symlink_skipped"))
            elif not path.is_file():
                entries.append((relative, None, "not_a_regular_file"))
            else:
                entries.append((relative, path, None))
    return entries


def _existing_intake(session: Session, case_id: uuid.UUID,
                     request_key: str) -> IntakeCreated | None:
    intake_id = session.scalar(select(models.Intake.id).where(
        models.Intake.case_id == case_id, models.Intake.request_key == request_key))
    if intake_id is None:
        return None
    job_id = session.scalar(select(models.Job.id).where(
        models.Job.case_id == case_id, models.Job.target_id == intake_id,
        models.Job.kind == "import").order_by(models.Job.created_at.desc()).limit(1))
    if job_id is None:
        raise WorkbenchError(ErrorCode.CONFLICT, "既有接收批次缺少任务记录",
                             {"intake_id": str(intake_id)})
    count = session.scalar(select(func.count()).select_from(models.IntakeItem)
                           .where(models.IntakeItem.intake_id == intake_id))
    return IntakeCreated(intake_id, job_id, False, count)


def _claim_intake(session: Session, case_id: uuid.UUID, request_key: str,
                  source_label: str, source_root: str | None) -> uuid.UUID | IntakeCreated:
    begin_write(session)
    intake_id = session.scalar(
        insert(models.Intake).values(id=uuid.uuid4(), case_id=case_id, request_key=request_key,
                                     source_label=source_label, source_root=source_root)
        .on_conflict_do_nothing(constraint="uq_intakes_case_request_key")
        .returning(models.Intake.id)
    )
    if intake_id is not None:
        return intake_id
    existing = _existing_intake(session, case_id, request_key)
    assert existing is not None  # The conflicting transaction committed; intakes are immutable.
    session.rollback()
    return existing


def create_intake(
    session: Session,
    storage: Storage,
    *,
    case_id: uuid.UUID,
    source_path: str,
    source_label: str,
    request_key: str,
    settings: Settings | None = None,
) -> IntakeCreated:
    """Register a batch idempotently; the same request_key never creates a second batch."""
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
    cases.ensure_case_writable(session, case_id)
    existing = _existing_intake(session, case_id, request_key)
    if existing is not None:
        return existing

    source, root = resolve_source(source_path, settings)
    # 目录扫描可能很慢，先做只读预检；真正写批次/子表前拿案件行共享锁并复核归档状态。
    # 这里用 FOR SHARE：子表插入的隐式外键 KEY SHARE 与它相容，与归档的 FOR UPDATE 互斥。
    cases.lock_case_for_import(session, case_id)
    intake_id = _claim_intake(session, case_id, request_key, source_label, str(root))
    if isinstance(intake_id, IntakeCreated):
        return intake_id
    entries = scan_source(source, root)
    for relative, path, skip_reason in entries:
        item = models.IntakeItem(
            id=uuid.uuid4(), case_id=case_id, intake_id=intake_id,
            relative_source=relative,
            status="skipped" if skip_reason else "pending",
            error_code=skip_reason,
            error_message="系统垃圾文件或链接，已跳过" if skip_reason else None,
        )
        session.add(item)
    job = models.Job(
        id=uuid.uuid4(), case_id=case_id, kind="import", target_id=intake_id,
        status="queued", total_units=len(entries),
    )
    session.add(job)
    audit.record(session, operation="intake.import", target_type="intake",
                 target_id=intake_id, case_id=case_id)
    session.commit()
    return IntakeCreated(intake_id, job.id, True, len(entries))


def _mark_item(session: Session, item: models.IntakeItem, status: str,
               error_code: str | None = None, error_message: str | None = None) -> None:
    begin_write(session)
    item.status = status
    item.error_code = error_code
    item.error_message = error_message
    session.commit()


def _ensure_stored_object(session: Session, storage: Storage, case_id: uuid.UUID,
                          staged: StagedFile, published_paths: list[str]) -> models.StoredObject:
    obj = session.execute(
        select(models.StoredObject).where(models.StoredObject.case_id == case_id,
                                          models.StoredObject.sha256 == staged.sha256)
    ).scalar_one_or_none()
    if obj is None:
        # Publication can fail after linking the file but before returning.
        relative = storage.original_relative(case_id, staged.sha256, staged.extension)
        published_paths.append(relative)
        published = storage.publish_original(case_id, staged)
        obj = models.StoredObject(
            id=uuid.uuid4(), case_id=case_id, sha256=published.sha256,
            size=published.size, relative_path=relative,
        )
        try:
            with session.begin_nested():
                session.add(obj)
                session.flush()
        except IntegrityError:
            obj = session.execute(
                select(models.StoredObject).where(models.StoredObject.case_id == case_id,
                                                  models.StoredObject.sha256 == staged.sha256)
            ).scalar_one()
            if relative != obj.relative_path:
                storage.delete_unpublished(relative)
                published_paths.pop()
        else:
            storage.discard(staged)
            return obj
    if not storage.verify(obj.relative_path, obj.sha256, obj.size):
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "既有同内容对象校验失败",
                             {"stored_object_id": str(obj.id)})
    storage.discard(staged)
    return obj


def _associate_material(session: Session, item: models.IntakeItem, case_id: uuid.UUID,
                        obj: models.StoredObject, relative_source: str) -> models.Material:
    if item.material_id is not None:
        material = session.get(models.Material, item.material_id)
        if material is not None:
            return material
    material = models.Material(
        id=uuid.uuid4(), case_id=case_id, intake_item_id=item.id,
        display_name=safe_name(Path(relative_source).name), category="待分类",
    )
    begin_write(session)
    session.add(material)
    session.flush()
    version = models.FileVersion(
        id=uuid.uuid4(), case_id=case_id, material_id=material.id,
        stored_object_id=obj.id, kind="original",
    )
    session.add(version)
    session.flush()
    material.original_file_version_id = version.id
    item.material_id = material.id
    from .parsing.pipeline import create_parse_run

    create_parse_run(session, case_id=case_id, file_version_id=version.id, commit=False)
    session.commit()
    return material


def process_import_job(
    session_factory: sessionmaker[Session],
    storage: Storage,
    job_id: uuid.UUID,
    *,
    fault_inject: Callable[[str], None] | None = None,
) -> ImportOutcome:
    """Process pending/failed items of an import job; safe to retry after a crash."""
    session = session_factory()
    try:
        job = session.get(models.Job, job_id)
        if job is None or job.kind != "import":
            raise WorkbenchError(ErrorCode.NOT_FOUND, "导入任务不存在", {"job_id": str(job_id)})
        case_id = job.case_id
        intake_id = job.target_id
        begin_write(session)
        job.status = "running"
        session.commit()
        items = session.execute(
            select(models.IntakeItem)
            .where(models.IntakeItem.intake_id == intake_id)
            .order_by(models.IntakeItem.relative_source)
        ).scalars().all()
        source_root = session.get(models.Intake, intake_id).source_root
        ready = failed = skipped = 0
        for item in items:
            if item.status in ("ready", "skipped"):
                if item.status == "ready":
                    ready += 1
                else:
                    skipped += 1
                continue
            if item.status == "failed":
                failed += 1
                continue
            try:
                if item.status in ("pending", "copying"):
                    begin_write(session)
                    item.status = "copying"
                    session.commit()
                    if fault_inject:
                        fault_inject("copying")
                    source = Path(source_root) / item.relative_source
                    staged = storage.stage_path(source, original_name=source.name)
                    published_paths: list[str] = []
                    try:
                        begin_write(session)
                        obj = _ensure_stored_object(session, storage, case_id, staged, published_paths)
                        item.stored_object_id = obj.id
                        item.status = "published"
                        session.commit()
                    except BaseException as exc:
                        _rollback_import(session, storage, published_paths, staged, exc)
                        raise
                    if fault_inject:
                        fault_inject("published")
                else:  # published but association incomplete
                    obj = session.get(models.StoredObject, item.stored_object_id) \
                        if item.stored_object_id else None
                    if obj is None or obj.case_id != case_id:
                        raise WorkbenchError(ErrorCode.NOT_FOUND, "发布记录缺失",
                                             {"intake_item_id": str(item.id)})
                _associate_material(session, item, case_id, obj, item.relative_source)
                if fault_inject:
                    fault_inject("associated")
                begin_write(session)
                item.status = "ready"
                item.error_code = item.error_message = None
                session.commit()
                ready += 1
            except WorkbenchError as exc:
                session.rollback()
                item = session.get(models.IntakeItem, item.id)
                _mark_item(session, item, "failed", exc.code.value, exc.message)
                failed += 1
            except OSError as exc:
                session.rollback()
                item = session.get(models.IntakeItem, item.id)
                _mark_item(session, item, "failed", "invalid_path", f"无法读取来源: {exc.strerror}")
                failed += 1
            job = session.get(models.Job, job_id)
            begin_write(session)
            job.completed_units = ready + failed + skipped
            job.total_units = len(items)
            session.commit()
        job = session.get(models.Job, job_id)
        begin_write(session)
        job.completed_units = len(items)
        job.total_units = len(items)
        if failed and ready:
            job.status = "partial"
        elif failed:
            job.status = "failed"
        else:
            job.status = "succeeded"
        job.error = None if not failed else f"{failed} 个条目失败"
        session.commit()
        return ImportOutcome(job_id, job.status, ready, failed, skipped)
    finally:
        session.close()


def _rollback_import(session: Session, storage: Storage, published: list[str],
                     staged: StagedFile | None, exc: BaseException) -> None:
    try:
        session.rollback()
        if staged is not None:
            storage.discard(staged)
        damaged_id = exc.details.get("stored_object_id") if isinstance(exc, WorkbenchError) else None
        if not published and damaged_id is None:
            return
        # After rollback another writer can adopt the same path. The lock also
        # waits for an active backup snapshot before touching the filesystem.
        begin_exclusive(session)
        for relative in published:
            still_used = session.execute(
                select(models.StoredObject.id).where(
                    models.StoredObject.relative_path == relative)
            ).first()
            if still_used is None:
                storage.delete_unpublished(relative)
        if damaged_id is not None:
            obj = session.get(models.StoredObject, uuid.UUID(damaged_id))
            if obj is not None:
                obj.integrity_status = "integrity_error"
        session.commit()
    except Exception:
        logger.exception("导入失败后的清理未完成")
    finally:
        session.rollback()


def create_upload_intake(
    session: Session,
    storage: Storage,
    *,
    case_id: uuid.UUID,
    source_label: str,
    request_key: str,
    uploads: list[tuple[str, "BinaryIO"]],
) -> IntakeCreated:
    """Register a browser upload batch.

    Browser relative paths are validated as relative names, never as server
    paths.  Each file is published before its row is written, so a crash leaves
    either an orphan file (recoverable) or a ``published`` item that the same
    job can finish on retry.
    """
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
    # 归档守卫与其它同案写入同口径：先只读预检（快速失败），真正写文件/批次行之前
    # 再拿案件行锁复核，避免「预检通过、归档随后提交」仍写入已归档案件。
    cases.ensure_case_writable(session, case_id)
    existing = _existing_intake(session, case_id, request_key)
    if existing is not None:
        return existing
    # Validate the whole batch before publishing anything.  Checking duplicate
    # paths inside the publish loop rejected the request after earlier files were
    # already on disk, and nothing removed them.
    validated: dict[str, BinaryIO] = {}
    for relative_path, stream in uploads:
        relative = validate_upload_path(relative_path)
        if relative in validated:
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "上传批次内存在重复相对路径",
                                 {"relative_path": relative})
        validated[relative] = stream

    cases.lock_case_for_import(session, case_id)
    intake_id = _claim_intake(session, case_id, request_key, source_label, None)
    if isinstance(intake_id, IntakeCreated):
        return intake_id
    items: list[models.IntakeItem] = []
    published_paths: list[str] = []
    staged = None
    try:
        for relative, stream in validated.items():
            staged = storage.stage_stream(stream, original_name=PurePosixPath(relative).name)
            try:
                obj = _ensure_stored_object(session, storage, case_id, staged, published_paths)
            except WorkbenchError as exc:
                exc.details.setdefault("relative_path", relative)
                raise
            staged = None
            item = models.IntakeItem(
                id=uuid.uuid4(), case_id=case_id, intake_id=intake_id,
                relative_source=relative, status="published", stored_object_id=obj.id,
            )
            session.add(item)
            items.append(item)
        job = models.Job(
            id=uuid.uuid4(), case_id=case_id, kind="import", target_id=intake_id,
            status="queued", total_units=len(items),
        )
        session.add(job)
        audit.record(session, operation="intake.upload", target_type="intake",
                     target_id=intake_id, case_id=case_id)
        session.commit()
    except BaseException as exc:
        _rollback_import(session, storage, published_paths, staged, exc)
        raise
    return IntakeCreated(intake_id, job.id, True, len(items))


def validate_upload_path(relative_path: str) -> str:
    """Accept only plain relative POSIX names supplied by a browser."""
    value = (relative_path or "").replace("\\", "/").strip()
    if not value:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "上传文件缺少相对路径")
    if value.startswith("/") or PurePosixPath(value).is_absolute():
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "上传路径必须是相对路径",
                             {"relative_path": relative_path})
    parts = PurePosixPath(value).parts
    if any(part in ("..", ".", "") for part in parts) or ":" in parts[0]:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "上传路径包含非法片段",
                             {"relative_path": relative_path})
    if _is_junk(parts[-1]):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "不接受系统垃圾文件",
                             {"relative_path": relative_path})
    return PurePosixPath(*parts).as_posix()


def list_intake_items(session: Session, *, case_id: uuid.UUID, intake_id: uuid.UUID,
                      status: IntakeStatus | None = None, offset: int = 0,
                      limit: int = 50) -> ListIntakeItemsOutput:
    intake = session.get(models.Intake, intake_id)
    if intake is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "接收批次不存在", {"intake_id": str(intake_id)})
    if intake.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "接收批次不属于该案件",
                             {"intake_id": str(intake_id)})
    query = select(models.IntakeItem).where(models.IntakeItem.intake_id == intake_id)
    if status is not None:
        query = query.where(models.IntakeItem.status == status.value)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = session.execute(
        query.order_by(models.IntakeItem.relative_source).offset(offset).limit(limit)
    ).scalars().all()
    return ListIntakeItemsOutput(
        items=[IntakeItem(intake_item_id=row.id, relative_source=row.relative_source,
                          status=IntakeStatus(row.status), material_id=row.material_id,
                          error_code=row.error_code, error_message=row.error_message)
               for row in rows],
        offset=offset, limit=limit, total=total,
    )


def reset_job_for_retry(session: Session, job_id: uuid.UUID) -> models.Job:
    """Requeue failed items of the same job; never creates a new intake or batch."""
    job = session.get(models.Job, job_id)
    if job is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "任务不存在", {"job_id": str(job_id)})
    begin_write(session)
    if job.kind == "import":
        items = session.execute(
            select(models.IntakeItem).where(models.IntakeItem.intake_id == job.target_id,
                                            models.IntakeItem.status == "failed")
        ).scalars().all()
        for item in items:
            item.status = "pending"
            item.error_code = item.error_message = None
    job.status = "queued"
    job.attempt += 1
    job.error = None
    session.commit()
    return job
