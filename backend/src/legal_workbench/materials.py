"""Materials: classification, display names, working copies and version registration."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, cases, models
from .coverage import coverage_for_version, latest_run, run_coverage
from .db import begin_write, update_with_revision
from .errors import ErrorCode, WorkbenchError
from .schemas import Coverage, IntakeStatus, MaterialSummary, UpdateAcquisitionInput
from .storage import Storage, safe_name

DEFAULT_CATEGORY = "待分类"
# A real extension: short, lowercase alphanumeric, no brackets or spaces.
_REAL_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def _material_or_error(session: Session, case_id: uuid.UUID, material_id: uuid.UUID) -> models.Material:
    material = session.get(models.Material, material_id)
    if material is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "材料不存在", {"material_id": str(material_id)})
    if material.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "材料不属于该案件",
                             {"material_id": str(material_id)})
    return material


def _file_version_or_error(session: Session, case_id: uuid.UUID,
                           file_version_id: uuid.UUID) -> models.FileVersion:
    version = session.get(models.FileVersion, file_version_id)
    if version is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "文件版本不存在",
                             {"file_version_id": str(file_version_id)})
    if version.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "文件版本不属于该案件",
                             {"file_version_id": str(file_version_id)})
    return version


def update_classification(session: Session, *, case_id: uuid.UUID, material_id: uuid.UUID,
                          category: str, expected_revision: int) -> int:
    cases.lock_case_writable(session, case_id)
    material = _material_or_error(session, case_id, material_id)
    category = category.strip() or DEFAULT_CATEGORY
    if not update_with_revision(session, models.Material, material.id, expected_revision,
                                category=category):
        session.rollback()
        raise WorkbenchError(ErrorCode.CONFLICT, "材料已被其他操作修改，请刷新后重试",
                             {"material_id": str(material_id)})
    audit.record(session, operation="material.classify", target_type="material",
                 target_id=material_id, case_id=case_id)
    session.commit()
    return session.get(models.Material, material_id).revision


def update_display_name(session: Session, *, case_id: uuid.UUID, material_id: uuid.UUID,
                        display_name: str, expected_revision: int) -> int:
    cases.lock_case_writable(session, case_id)
    material = _material_or_error(session, case_id, material_id)
    clean = safe_name(display_name.strip())
    if not clean:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "材料名称不能为空")
    if not update_with_revision(session, models.Material, material.id, expected_revision,
                                display_name=clean):
        session.rollback()
        raise WorkbenchError(ErrorCode.CONFLICT, "材料已被其他操作修改，请刷新后重试",
                             {"material_id": str(material_id)})
    audit.record(session, operation="material.rename", target_type="material",
                 target_id=material_id, case_id=case_id)
    session.commit()
    return session.get(models.Material, material_id).revision


def update_acquisition(session: Session, payload: UpdateAcquisitionInput) -> int:
    cases.lock_case_writable(session, payload.case_id)
    material = _material_or_error(session, payload.case_id, payload.material_id)
    acquisition = (payload.acquisition or "").strip() or None
    if not update_with_revision(session, models.Material, material.id, payload.expected_revision,
                                acquisition=acquisition):
        raise WorkbenchError(ErrorCode.CONFLICT, "材料已被其他操作修改，请刷新后重试")
    session.commit()
    return payload.expected_revision + 1


def _safe_suffix(name: str) -> str:
    """The suffix only if it is a real extension, not ``.docx（工作副本）``.

    ``Path("合同.docx（工作副本）").suffix`` is ``".docx（工作副本）"``: non-empty,
    so the old check skipped the fallback and ``safe_extension`` later turned the
    whole thing into ``.bin``.  A usable suffix is short, has no whitespace or
    brackets, and is made of word characters.
    """
    suffix = Path(name).suffix
    return suffix if _REAL_SUFFIX.match(suffix) else ""


def _stored_extension(stored_relative: str) -> str:
    """The extension of the stored object, used when the name carries none."""
    suffix = Path(stored_relative).suffix
    return suffix if _REAL_SUFFIX.match(suffix) else ".bin"


def working_copy_name(display_name: str, stored_relative: str) -> str:
    """Build a working-copy filename whose type comes from the stored object.

    Two earlier attempts were both wrong.  ``合同.docx（工作副本）`` left the marker
    after the extension, so ``Path.suffix`` returned ``.docx（工作副本）`` and the
    file became ``.bin``.  Then ``合同.v2.docx`` was fixed by reusing the display
    name's suffix, which broke the other direction: a legal rename to ``合同.v2``
    (a DOCX whose name ends in a version number) produced ``合同.v2（工作副本）.v2``
    and the copy could not be parsed.

    The display name may end in something that looks like an extension but is
    not the real type.  So a trailing suffix is stripped only when it *matches*
    the stored object's extension; anything else is part of the name.  That gives:

    ``合同.docx``      (DOCX) -> ``合同（工作副本）.docx``
    ``合同.v2.docx``   (DOCX) -> ``合同.v2（工作副本）.docx``
    ``合同.v2``        (DOCX) -> ``合同.v2（工作副本）.docx``
    ``改名后``          (DOCX) -> ``改名后（工作副本）.docx``
    """
    stem = safe_name(display_name.strip() or "工作副本")
    extension = _stored_extension(stored_relative)
    suffix = _safe_suffix(stem)
    if suffix and suffix.lower() == extension.lower():
        stem = stem[: -len(suffix)]
    return safe_name(f"{stem}（工作副本）{extension}")


def create_working_copy(session: Session, storage: Storage, *, case_id: uuid.UUID,
                        file_version_id: uuid.UUID,
                        display_name: str | None = None) -> tuple[uuid.UUID, str]:
    cases.lock_case_writable(session, case_id)
    version = _file_version_or_error(session, case_id, file_version_id)
    stored = session.get(models.StoredObject, version.stored_object_id)
    if stored is None or not storage.verify(stored.relative_path, stored.sha256, stored.size):
        if stored is not None:
            stored.integrity_status = "integrity_error"
            session.commit()
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "原件校验失败，禁止创建副本",
                             {"file_version_id": str(file_version_id)})
    if display_name is None:
        material = session.get(models.Material, version.material_id)
        display_name = material.display_name if material is not None else ""
    name = working_copy_name(display_name or "", stored.relative_path)
    working_id = uuid.uuid4()
    copied = storage.copy_to_working(case_id, working_id, stored.relative_path, name)
    begin_write(session)
    row = models.WorkingCopy(
        id=working_id, case_id=case_id, material_id=version.material_id,
        base_version_id=version.id, relative_path=copied.relative_path,
        display_name=name, revision=0,
    )
    session.add(row)
    audit.record(session, operation="material.working_copy", target_type="working_copy",
                 target_id=working_id, case_id=case_id)
    session.commit()
    return working_id, copied.relative_path


def _working_copy_or_error(session: Session, case_id: uuid.UUID,
                           working_copy_id: uuid.UUID) -> models.WorkingCopy:
    working = session.get(models.WorkingCopy, working_copy_id)
    if working is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "工作副本不存在",
                             {"working_copy_id": str(working_copy_id)})
    if working.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "工作副本不属于该案件",
                             {"working_copy_id": str(working_copy_id)})
    return working


def register_file_version(session: Session, storage: Storage, *, case_id: uuid.UUID,
                          working_copy_id: uuid.UUID, expected_revision: int) -> tuple[uuid.UUID, uuid.UUID]:
    cases.lock_case_writable(session, case_id)
    working = _working_copy_or_error(session, case_id, working_copy_id)
    if working.revision != expected_revision:
        raise WorkbenchError(ErrorCode.CONFLICT, "工作副本已变化，请刷新后重试",
                             {"working_copy_id": str(working_copy_id),
                              "expected_revision": expected_revision,
                              "actual_revision": working.revision})
    path = storage.managed_path(working.relative_path)
    if path.is_symlink() or not path.is_file():
        raise WorkbenchError(ErrorCode.INVALID_PATH, "工作副本文件缺失或被替换为链接",
                             {"working_copy_id": str(working_copy_id)})
    # Take the revision first, before anything with side effects.  Doing this after
    # publishing meant two parallel registrations could both publish, and the
    # loser then died on uq_stored_objects_case_sha — leaving an orphan file and
    # bypassing the conflict path entirely.
    begin_write(session)
    if not update_with_revision(session, models.WorkingCopy, working.id, expected_revision):
        session.rollback()
        raise WorkbenchError(ErrorCode.CONFLICT, "工作副本已变化，请刷新后重试",
                             {"working_copy_id": str(working_copy_id),
                              "expected_revision": expected_revision})
    staged = storage.stage_path(path, original_name=working.display_name)
    # One StoredObject per (case, sha256): identical bytes must reuse the existing
    # row instead of inserting a duplicate that the unique constraint rejects.
    existing_object = session.execute(
        select(models.StoredObject).where(models.StoredObject.case_id == case_id,
                                          models.StoredObject.sha256 == staged.sha256)
    ).scalar_one_or_none()
    version_id = uuid.uuid4()
    latest = session.execute(
        select(models.FileVersion)
        .where(models.FileVersion.case_id == case_id,
               models.FileVersion.material_id == working.material_id)
        .order_by(models.FileVersion.created_at.desc(), models.FileVersion.id)
    ).scalars().first()
    published = None
    if existing_object is not None:
        if not storage.verify(existing_object.relative_path, existing_object.sha256,
                              existing_object.size):
            # Roll back rather than commit: committing here kept the revision bump
            # from the CAS above, so a failed registration consumed a revision.
            # The integrity flag is re-applied in its own transaction below.
            session.rollback()
            storage.discard(staged)
            marker = session.get(models.StoredObject, existing_object.id)
            if marker is not None:
                begin_write(session)
                marker.integrity_status = "integrity_error"
                session.commit()
            raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "既有同内容对象校验失败",
                                 {"stored_object_id": str(existing_object.id)})
        storage.discard(staged)
        stored = existing_object
    else:
        published = storage.publish_version(case_id, version_id, staged)
        stored = models.StoredObject(
            id=uuid.uuid4(), case_id=case_id, sha256=published.sha256, size=published.size,
            relative_path=published.relative_path,
        )
        try:
            # Two working copies of the same material can be edited to identical
            # bytes.  Their CAS locks are different rows, so both reach this insert
            # and one hits uq_stored_objects_case_sha.  A savepoint keeps that from
            # poisoning the transaction; the loser reuses the winner's object and
            # still gets its own version and parse job.
            with session.begin_nested():
                session.add(stored)
                session.flush()
        except IntegrityError:
            stored = session.execute(
                select(models.StoredObject).where(
                    models.StoredObject.case_id == case_id,
                    models.StoredObject.sha256 == published.sha256)
            ).scalar_one()
            if published.relative_path != stored.relative_path:
                storage.delete_unpublished(published.relative_path)
            if not storage.verify(stored.relative_path, stored.sha256, stored.size):
                session.rollback()
                begin_write(session)
                marker = session.get(models.StoredObject, stored.id)
                if marker is not None:
                    marker.integrity_status = "integrity_error"
                    session.commit()
                raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "既有同内容对象校验失败",
                                     {"stored_object_id": str(stored.id)})
    version = models.FileVersion(
        id=version_id, case_id=case_id, material_id=working.material_id,
        stored_object_id=stored.id, kind="working",
        parent_version_id=latest.id if latest else working.base_version_id,
    )
    session.add(version)
    session.flush()
    try:
        from .parsing.pipeline import create_parse_run
        # commit=False: the run and its job must land in the same transaction as
        # the revision bump and the new version.  create_parse_run committing on
        # its own meant a later failure left a queued job and revision=1 behind a
        # rolled-back version.
        _run_id, job_id = create_parse_run(session, case_id=case_id,
                                           file_version_id=version.id, commit=False)
        audit.record(session, operation="file_version.register",
                     target_type="file_version", target_id=version.id, case_id=case_id)
        session.commit()
    except BaseException:
        # Any later failure must not leave the published file behind.
        session.rollback()
        if published is not None:
            storage.delete_unpublished(published.relative_path)
        raise
    return version.id, job_id


def _coverage_for(session: Session, version: models.FileVersion | None) -> Coverage:
    return coverage_for_version(session, version)


def list_materials(session: Session, *, case_id: uuid.UUID, category: str | None = None,
                   status: IntakeStatus | None = None, offset: int = 0,
                   limit: int = 50) -> tuple[list[MaterialSummary], int, int, list[str]]:
    """Return ``(items, total, case_total, categories)``.

    ``total`` is the filtered count for pagination; ``case_total`` and
    ``categories`` describe the whole case so the sidebar does not shrink to
    whatever happens to be on the current page.
    """
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
    query = (
        select(models.Material, models.IntakeItem, models.Intake)
        .join(models.IntakeItem, models.Material.intake_item_id == models.IntakeItem.id)
        .join(models.Intake, models.IntakeItem.intake_id == models.Intake.id)
        .where(models.Material.case_id == case_id)
    )
    if category is not None:
        query = query.where(models.Material.category == category)
    if status is not None:
        query = query.where(models.IntakeItem.status == status.value)
    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar_one()
    # Newest first, then by name length, then name, then id.
    #
    # The tiebreakers are not cosmetic: a batch imported in one transaction shares
    # a created_at (PostgreSQL now() is transaction-scoped), and without a stable
    # secondary key the rows shuffle between pages so an item can be skipped or
    # repeated.  Ordering by id alone is stable but random-looking, leaving the
    # user unable to tell which row is "the 51st".
    #
    # Length before name approximates a natural sort for numbered material names
    # (证据1..证据9, then 证据10..证据99, then 证据100..) with no schema change.
    # It is a heuristic: names with different prefixes and different lengths
    # (e.g. "附件 2" vs "证据10") still sort by length first.  A true natural
    # sort would need a persisted padded sort key.
    rows = session.execute(
        query.order_by(models.Material.created_at.desc(),
                       func.char_length(models.Material.display_name),
                       models.Material.display_name,
                       models.Material.id).offset(offset).limit(limit)
    ).all()
    items: list[MaterialSummary] = []
    for material, item, intake in rows:
        version = session.get(models.FileVersion, material.original_file_version_id) \
            if material.original_file_version_id else None
        items.append(MaterialSummary(
            material_id=material.id, display_name=material.display_name,
            category=material.category, revision=material.revision, intake_id=intake.id,
            acquisition=material.acquisition, source_label=intake.source_label, relative_source=item.relative_source,
            status=IntakeStatus(item.status), coverage=_coverage_for(session, version),
            created_at=material.created_at,
        ))
    case_total, categories = _case_wide_counts(session, case_id)
    return items, total, case_total, categories


def _case_wide_counts(session: Session, case_id: uuid.UUID) -> tuple[int, list[str]]:
    """Material count and distinct categories for the whole case, unfiltered."""
    case_total = session.execute(
        select(func.count()).select_from(models.Material)
        .where(models.Material.case_id == case_id)
    ).scalar_one()
    categories = session.execute(
        select(models.Material.category).where(models.Material.case_id == case_id)
        .distinct().order_by(models.Material.category)
    ).scalars().all()
    return case_total, list(categories)
