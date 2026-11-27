"""Evidence-catalog HTTP endpoints (actor=human, entry=local-web).

The catalog is the single source of evidence numbers (D2); this module only
fixes the actor, enforces path/body identifier agreement and delegates.  Import
preview parses an uploaded DOCX/XLSX/CSV into draft rows without touching the
database; committing a catalog always goes through ``import_catalog`` so drafts
stay ``pending`` until a human confirms them.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import dossier_export, evidence_catalog
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    EvidenceAcceptAllInput, EvidenceBindingRevisionInput, EvidenceCatalogOut,
    EvidenceChangeIdInput, EvidenceGroupRevisionInput, EvidenceItemRevisionInput,
    ImportDraftRow, ImportEvidenceCatalogInput, ImportPreviewOutput,
    MoveEvidenceItemInput, RemoveEvidenceBindingInput, ReorderEvidenceGroupsInput,
    UpdateEvidenceGroupInput, UpdateEvidenceItemInput,
)
from ..storage import Storage
from .deps import Guarded, get_db, get_storage, require_module
from .files import _content_disposition
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["evidence"],
                   dependencies=[Guarded, Depends(require_module("evidence"))])

ACTOR = "human"
ENTRY = "local-web"


def _same(*pairs: tuple[str, uuid.UUID]) -> None:
    first_label, first = pairs[0]
    for label, value in pairs[1:]:
        if value != first:
            raise WorkbenchError(ErrorCode.CASE_MISMATCH, f"{label}与{first_label}不一致")


def _check_case(path_case_id: str, payload) -> uuid.UUID:
    case_id = uuid_of(path_case_id, "case_id")
    _same(("路径案件", case_id), ("请求案件", payload.case_id))
    return case_id


@router.get("/evidence-catalog", response_model=EvidenceCatalogOut)
def get_catalog(case_id: str, session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    return evidence_catalog.get_catalog(session, uuid_of(case_id, "case_id"))


@router.get("/evidence-catalog/export")
def export_catalog(case_id: str, session: Session = Depends(get_db)) -> Response:
    text = evidence_catalog.export_catalog_csv(session, uuid_of(case_id, "case_id"))
    # UTF-8 BOM so Excel opens Chinese headers correctly; the file is an export,
    # never an import round-trip (imports are re-parsed by the preview endpoint).
    body = ("\ufeff" + text).encode("utf-8")
    name = quote("证据目录.csv")
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"catalog.csv\"; filename*=UTF-8''{name}",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/evidence-catalog/bundle")
def export_evidence_bundle(
    case_id: str,
    session: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    """自动装卷：导出合订卷宗 PDF、可编辑 Word 目录、CSV 与按编号重命名的分项文件 ZIP。"""
    result = dossier_export.export_evidence_bundle(session, storage, uuid_of(case_id, "case_id"))
    return Response(
        result.data,
        media_type="application/zip",
        headers={
            "Content-Disposition": _content_disposition("attachment", result.filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/evidence-catalog/import/preview", response_model=ImportPreviewOutput)
async def preview_import(case_id: str, file: UploadFile = File(...),
                         session: Session = Depends(get_db)) -> ImportPreviewOutput:
    """Parse an external catalog into draft rows; nothing is written."""
    uuid_of(case_id, "case_id")  # reject malformed ids before reading the body
    data = await file.read()
    rows = evidence_catalog.preview_catalog_bytes(data, file.filename or "")
    drafts = [ImportDraftRow(**row) for row in rows]
    warnings = [
        f"{len(drafts)} 条草稿未绑定材料：请先匹配材料，再提交导入（确认前不参与编号）"
    ]
    return ImportPreviewOutput(rows=drafts, warnings=warnings)


@router.post("/evidence-catalog/import", response_model=EvidenceCatalogOut)
def import_catalog(case_id: str, payload: ImportEvidenceCatalogInput,
                   session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    return evidence_catalog.import_catalog(session, payload, actor=ACTOR, entry=ENTRY)


# --- groups -----------------------------------------------------------------

@router.post("/evidence-catalog/groups", response_model=EvidenceCatalogOut)
def add_group(case_id: str, payload: AddEvidenceGroupInput,
              session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    return evidence_catalog.add_group(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/evidence-catalog/groups/{group_id}", response_model=EvidenceCatalogOut)
def update_group(case_id: str, group_id: str, payload: UpdateEvidenceGroupInput,
                 session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径分组", uuid_of(group_id, "group_id")),
          ("请求分组", payload.group_id))
    return evidence_catalog.update_group(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/groups/reorder", response_model=EvidenceCatalogOut)
def reorder_groups(case_id: str, payload: ReorderEvidenceGroupsInput,
                   session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    return evidence_catalog.reorder_groups(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/groups/{group_id}/remove", response_model=EvidenceCatalogOut)
def remove_group(case_id: str, group_id: str, payload: EvidenceGroupRevisionInput,
                 session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径分组", uuid_of(group_id, "group_id")),
          ("请求分组", payload.group_id))
    return evidence_catalog.remove_group(session, payload, actor=ACTOR, entry=ENTRY)


# --- items ------------------------------------------------------------------

@router.post("/evidence-catalog/items", response_model=EvidenceCatalogOut)
def add_item(case_id: str, payload: AddEvidenceItemInput,
             session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    return evidence_catalog.add_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/evidence-catalog/items/{item_id}", response_model=EvidenceCatalogOut)
def update_item(case_id: str, item_id: str, payload: UpdateEvidenceItemInput,
                session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径条目", uuid_of(item_id, "item_id")),
          ("请求条目", payload.item_id))
    return evidence_catalog.update_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/items/{item_id}/move", response_model=EvidenceCatalogOut)
def move_item(case_id: str, item_id: str, payload: MoveEvidenceItemInput,
              session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径条目", uuid_of(item_id, "item_id")),
          ("请求条目", payload.item_id))
    return evidence_catalog.move_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/items/{item_id}/remove", response_model=EvidenceCatalogOut)
def remove_item(case_id: str, item_id: str, payload: EvidenceItemRevisionInput,
                session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径条目", uuid_of(item_id, "item_id")),
          ("请求条目", payload.item_id))
    return evidence_catalog.remove_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/items/{item_id}/confirm", response_model=EvidenceCatalogOut)
def confirm_item(case_id: str, item_id: str, payload: EvidenceItemRevisionInput,
                 session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径条目", uuid_of(item_id, "item_id")),
          ("请求条目", payload.item_id))
    return evidence_catalog.confirm_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/items/{item_id}/reject", response_model=EvidenceCatalogOut)
def reject_item(case_id: str, item_id: str, payload: EvidenceItemRevisionInput,
                session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径条目", uuid_of(item_id, "item_id")),
          ("请求条目", payload.item_id))
    return evidence_catalog.reject_item(session, payload, actor=ACTOR, entry=ENTRY)


# --- bindings ---------------------------------------------------------------

@router.post("/evidence-catalog/bindings", response_model=EvidenceCatalogOut)
def add_binding(case_id: str, payload: AddEvidenceBindingInput,
                session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    return evidence_catalog.add_binding(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/bindings/{binding_id}/remove",
             response_model=EvidenceCatalogOut)
def remove_binding(case_id: str, binding_id: str, payload: RemoveEvidenceBindingInput,
                   session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径绑定", uuid_of(binding_id, "binding_id")),
          ("请求绑定", payload.binding_id))
    return evidence_catalog.remove_binding(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/bindings/{binding_id}/confirm",
             response_model=EvidenceCatalogOut)
def confirm_binding(case_id: str, binding_id: str, payload: EvidenceBindingRevisionInput,
                    session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径绑定", uuid_of(binding_id, "binding_id")),
          ("请求绑定", payload.binding_id))
    return evidence_catalog.confirm_binding(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/bindings/{binding_id}/reject",
             response_model=EvidenceCatalogOut)
def reject_binding(case_id: str, binding_id: str, payload: EvidenceBindingRevisionInput,
                   session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径绑定", uuid_of(binding_id, "binding_id")),
          ("请求绑定", payload.binding_id))
    return evidence_catalog.reject_binding(session, payload, actor=ACTOR, entry=ENTRY)


# --- review of agent changes (human only) -----------------------------------

@router.post("/evidence-catalog/changes/{change_id}/accept",
             response_model=EvidenceCatalogOut)
def accept_change(case_id: str, change_id: str, payload: EvidenceChangeIdInput,
                  session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径变更", uuid_of(change_id, "change_id")),
          ("请求变更", payload.change_id))
    return evidence_catalog.accept_change(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/changes/accept-all", response_model=EvidenceCatalogOut)
def accept_all(case_id: str, payload: EvidenceAcceptAllInput,
               session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    return evidence_catalog.accept_all(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/evidence-catalog/changes/{change_id}/revert",
             response_model=EvidenceCatalogOut)
def revert_change(case_id: str, change_id: str, payload: EvidenceChangeIdInput,
                  session: Session = Depends(get_db)) -> EvidenceCatalogOut:
    _check_case(case_id, payload)
    _same(("路径变更", uuid_of(change_id, "change_id")),
          ("请求变更", payload.change_id))
    return evidence_catalog.revert_change(session, payload, actor=ACTOR, entry=ENTRY)
