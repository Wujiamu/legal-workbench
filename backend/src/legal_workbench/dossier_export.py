"""自动装卷（证据卷宗 ZIP）—— 工作台侧的薄适配层。

排版内核已迁到零数据库独立包 :mod:`plainfolio_dossier`（`packages/plainfolio-dossier`），
本模块只做三件事：

1. **在共享锁内取快照**：案件元数据、已确认的证据组/条目/绑定、材料与存储对象
   校验，出任何问题都在释放锁之前报业务错误；
2. **释放锁**：``session.rollback()`` 之后才读盘、渲染、打包，磁盘 I/O 不占着
   数据库行锁；
3. **注入路径解析**：把 ``Storage.verify`` + ``managed_path`` 作为
   ``path_resolver`` 交给渲染内核，因此工作台的"受管目录 + SHA-256"校验一字未减。

``DossierBundleResult`` / ``export_evidence_bundle`` / 错误码与拆分前完全一致：
API 路由、CLI 与既有测试都不需要改动。同名私有符号（``_sanitize_xml_text``、
``_place_pdf_page_onto_a4``、``_rasterize_pdf_page_to_a4``）继续导出，且通过
渲染原语接缝真正作用于渲染路径（故障注入测试因此仍然有效）。
"""
from __future__ import annotations

import importlib.metadata
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from plainfolio_dossier import builder as _builder
from plainfolio_dossier.builder import (  # noqa: F401 - 兼容旧引用与故障注入
    A4_LANDSCAPE,
    A4_PORTRAIT,
    BOTTOM_MARGIN,
    FOOTER_Y,
    IMAGE_SUFFIXES,
    LEFT_BIND_MARGIN,
    MAX_IMAGE_FRAMES,
    ONLINE_MAX_BYTES,
    ONLINE_WARN_BYTES,
    PDF_SUFFIXES,
    RIGHT_MARGIN,
    SOFTWARE_NAME,
    TOP_MARGIN,
    TZ_SHANGHAI,
    _place_pdf_page_onto_a4,
    _rasterize_pdf_page_to_a4,
    _sanitize_xml_text,
)
from plainfolio_dossier.errors import DossierError
from plainfolio_dossier.models import BindingSpec, CaseMeta, DossierSpec, GroupSpec, ItemSpec
from sqlalchemy.orm import Session

from . import cases, evidence_catalog, models
from .errors import ErrorCode, WorkbenchError
from .storage import Storage

#: 独立包的固定错误码 → 工作台错误码。缺一个就会把业务错误退化成 500。
_ERROR_CODE_MAP = {
    "invalid_input": ErrorCode.INVALID_INPUT,
    "not_found": ErrorCode.NOT_FOUND,
    "forbidden": ErrorCode.FORBIDDEN,
    "integrity_error": ErrorCode.INTEGRITY_ERROR,
    "internal_error": ErrorCode.INTERNAL_ERROR,
}

EMPTY_CATALOG_MESSAGE = "暂无已确认的证据条目，请先确认证据目录后再装卷"


@dataclass(frozen=True)
class DossierBundleResult:
    filename: str
    data: bytes
    summary: dict[str, Any]


@dataclass(frozen=True)
class _CaseSnapshot:
    id: uuid.UUID
    title: str
    case_number: str | None
    plaintiff: str | None
    defendant: str | None


@dataclass(frozen=True)
class _BindingSnapshot:
    page_start: int | None
    page_end: int | None
    material_name: str
    acquisition: str | None
    relative_path: str
    sha256: str
    size: int
    ext: str


def _render_primitives():
    """Read the module attributes at call time so monkeypatching still bites."""
    module = sys.modules[__name__]
    return module._place_pdf_page_onto_a4, module._rasterize_pdf_page_to_a4


_builder.install_render_hook_loader(_render_primitives)


def _workbench_version() -> str:
    try:
        return importlib.metadata.version("legal-workbench")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _snapshot_binding(
    session: Session,
    case_id: uuid.UUID,
    binding: models.EvidenceItemBinding,
    material: models.Material | None,
) -> _BindingSnapshot:
    """Resolve binding metadata inside the shared-lock transaction before releasing DB locks."""
    if material is None or material.case_id != case_id:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "证据绑定的材料不存在或不属于本案")
    if material.original_file_version_id is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, f"材料「{material.display_name}」缺少原件版本")
    version = session.get(models.FileVersion, material.original_file_version_id)
    if version is None or version.case_id != case_id:
        raise WorkbenchError(ErrorCode.NOT_FOUND, f"材料「{material.display_name}」文件版本缺失")
    stored = session.get(models.StoredObject, version.stored_object_id)
    if stored is None or stored.case_id != case_id:
        raise WorkbenchError(ErrorCode.NOT_FOUND, f"材料「{material.display_name}」存储对象缺失")
    if stored.integrity_status != "ok":
        raise WorkbenchError(
            ErrorCode.INTEGRITY_ERROR,
            f"材料「{material.display_name}」完整性校验失败，已阻止装卷",
            {"stored_object_id": str(stored.id)},
        )
    return _BindingSnapshot(
        page_start=binding.page_start,
        page_end=binding.page_end,
        material_name=material.display_name,
        acquisition=material.acquisition,
        relative_path=stored.relative_path,
        sha256=stored.sha256,
        size=stored.size,
        ext=PurePosixPath(stored.relative_path).suffix.lower(),
    )


def _storage_resolver(storage: Storage):
    """``path_resolver`` for the renderer: hash-verify then hand back the managed path."""

    def resolve(binding: BindingSpec) -> Path:
        if not storage.verify(binding.file_path, binding.sha256 or "", binding.size or -1):
            raise DossierError(
                "integrity_error",
                f"材料「{binding.material_name}」完整性校验失败，已阻止装卷",
                {"relative_path": binding.file_path},
            )
        return storage.managed_path(binding.file_path)

    return resolve


def export_evidence_bundle(session: Session, storage: Storage,
                           case_id: uuid.UUID) -> DossierBundleResult:
    """Assemble confirmed evidence items into a Chinese-court-ready ZIP dossier."""
    case_row = cases.lock_case_shared(session, case_id)
    case = _CaseSnapshot(
        id=case_row.id,
        title=case_row.title,
        case_number=case_row.case_number,
        plaintiff=case_row.plaintiff,
        defendant=case_row.defendant,
    )
    groups, items, bindings = evidence_catalog.confirmed_catalog_rows(session, case_id)
    _numbers, group_numbers = evidence_catalog.compute_numbers(groups, items)

    materials = {
        row.id: row
        for row in session.query(models.Material).filter(models.Material.case_id == case_id).all()
    }

    active_groups = sorted((g for g in groups if g.removed_at is None),
                           key=lambda g: (g.position, str(g.id)))

    raw_bindings_by_item: dict[uuid.UUID, list[models.EvidenceItemBinding]] = {}
    for b in bindings:
        if b.removed_at is None and b.review_state == "confirmed":
            raw_bindings_by_item.setdefault(b.item_id, []).append(b)
    confirmed_bindings_by_item: dict[uuid.UUID, list[_BindingSnapshot]] = {}
    for item_id, b_list in raw_bindings_by_item.items():
        b_list.sort(key=lambda b: (b.created_at, str(b.id)))
        confirmed_bindings_by_item[item_id] = [
            _snapshot_binding(session, case_id, b, materials.get(b.material_id))
            for b in b_list
        ]

    spec_groups: list[GroupSpec] = []
    for grp in active_groups:
        members = sorted(
            (it for it in items
             if it.group_id == grp.id and it.removed_at is None and it.review_state == "confirmed"),
            key=lambda it: (it.position, str(it.id)),
        )
        # 空分组也要保留：组号由 `compute_numbers` 按全部未删除分组连续发放，
        # 丢掉空组会让后面的组号整体前移，与证据编号对不上。
        spec_items: list[ItemSpec] = []
        for it in members:
            spec_items.append(ItemSpec(
                title=it.title,
                purpose=it.purpose,
                bindings=[
                    BindingSpec(
                        file_path=snapshot.relative_path,
                        material_name=snapshot.material_name,
                        page_start=snapshot.page_start,
                        page_end=snapshot.page_end,
                        acquisition=snapshot.acquisition,
                        sha256=snapshot.sha256,
                        size=snapshot.size,
                        ext=snapshot.ext,
                    )
                    for snapshot in confirmed_bindings_by_item.get(it.id, [])
                ],
            ))
        spec_groups.append(GroupSpec(title=grp.title, items=spec_items))

    # Release FOR SHARE row lock & read-only transaction before disk I/O and rendering.
    session.rollback()

    if not any(group.items for group in spec_groups):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, EMPTY_CATALOG_MESSAGE)

    spec = DossierSpec(
        case=CaseMeta(
            title=case.title,
            case_number=case.case_number,
            plaintiff=case.plaintiff,
            defendant=case.defendant,
        ),
        groups=spec_groups,
    )

    try:
        result = _builder.build_dossier(
            spec,
            path_resolver=_storage_resolver(storage),
            software_version=_workbench_version(),
        )
    except DossierError as exc:
        raise WorkbenchError(_ERROR_CODE_MAP[exc.code], exc.message, exc.details) from exc

    return DossierBundleResult(
        filename=result.filename,
        data=result.zip_bytes,
        summary={
            "case_id": str(case.id),
            "group_count": result.group_count,
            "item_count": result.item_count,
            "body_page_count": result.total_body_pages,
            "total_pdf_pages": result.total_pages,
            "standalone_file_count": result.standalone_file_count,
            "warnings": result.warnings,
            "reminders": result.reminders,
        },
    )
