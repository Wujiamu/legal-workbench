"""Evidence catalog service: the single source of evidence numbers (D2).

Numbers are never stored on graph rows.  A source points at an immutable
``segment_id`` (``source_ref``), a material or an annotation; the label shown in
the graph is resolved here against the *confirmed* catalog:

    evidence{group}-{item}[ 第N页]

Only groups/items/bindings with ``removed_at IS NULL`` and
``review_state = 'confirmed'`` take part.  Drafts produced by an external
Word/Excel import stay ``pending`` and therefore never shift a working-draft
number until a human confirms them.

Every write follows the same order as the progress service: permission guard ->
maintenance gate -> case row lock (FOR UPDATE, archive re-check inside the lock)
-> domain sequence lock -> rule and same-case checks -> CAS -> change row ->
seq + 1 -> audit -> commit.  Reads take the case row FOR SHARE first so catalog
data and seq come from one committed state.
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, cases, domain_changes, graph_rules, models
from .errors import ErrorCode, WorkbenchError
from .schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    CatalogMaterial, EvidenceAcceptAllInput, EvidenceBindingOut, EvidenceCatalogOut,
    EvidenceChangeIdInput, EvidenceGroupChanges, EvidenceGroupOut, EvidenceGroupRevisionInput,
    EvidenceItemChanges, EvidenceItemOut, EvidenceItemRevisionInput, EvidenceBindingRevisionInput,
    GraphChangeOut, GraphDiffEntry,
    ImportEvidenceCatalogInput, MoveEvidenceItemInput, RemoveEvidenceBindingInput,
    ReorderEvidenceGroupsInput, ResolvedLabelOut, UpdateEvidenceGroupInput,
    UpdateEvidenceItemInput,
)

DOMAIN = "evidence"

_DRAFT_MODELS = {
    "evidence_item": models.EvidenceItem,
    "evidence_binding": models.EvidenceItemBinding,
}
_DRAFT_REVIEW_COMMANDS = ("confirm_item", "reject_item", "confirm_binding", "reject_binding")

ARCHIVED_MESSAGE = "案件已归档，证据目录只读；如需修改请先取消归档"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _err(code: ErrorCode, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details or None)


# --- serialization ----------------------------------------------------------

def _group_dict(row: models.EvidenceGroup) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "position": row.position,
        "title": row.title, "purpose": row.purpose, "created_by": row.created_by,
        "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _item_dict(row: models.EvidenceItem) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "group_id": str(row.group_id),
        "position": row.position, "title": row.title, "purpose": row.purpose,
        "source_kind": row.source_kind,
        "import_material_id": str(row.import_material_id) if row.import_material_id else None,
        "review_state": row.review_state, "created_by": row.created_by,
        "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _binding_dict(row: models.EvidenceItemBinding) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "item_id": str(row.item_id),
        "material_id": str(row.material_id), "page_start": row.page_start,
        "page_end": row.page_end, "review_state": row.review_state,
        "created_by": row.created_by,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _group_out(row: models.EvidenceGroup) -> EvidenceGroupOut:
    return EvidenceGroupOut(
        id=row.id, case_id=row.case_id, position=row.position, title=row.title,
        purpose=row.purpose, created_by=row.created_by, request_key=row.request_key,
        removed_at=row.removed_at, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _item_out(row: models.EvidenceItem) -> EvidenceItemOut:
    return EvidenceItemOut(
        id=row.id, case_id=row.case_id, group_id=row.group_id, position=row.position,
        title=row.title, purpose=row.purpose, source_kind=row.source_kind,
        import_material_id=row.import_material_id, review_state=row.review_state,
        created_by=row.created_by, request_key=row.request_key, removed_at=row.removed_at,
        revision=row.revision, created_at=row.created_at, updated_at=row.updated_at,
    )


def _binding_out(row: models.EvidenceItemBinding) -> EvidenceBindingOut:
    return EvidenceBindingOut(
        id=row.id, case_id=row.case_id, item_id=row.item_id, material_id=row.material_id,
        page_start=row.page_start, page_end=row.page_end, review_state=row.review_state,
        created_by=row.created_by, removed_at=row.removed_at, created_at=row.created_at,
    )


# --- reads ------------------------------------------------------------------

def _load(session: Session, case_id: uuid.UUID) -> tuple[
        list[models.EvidenceGroup], list[models.EvidenceItem],
        list[models.EvidenceItemBinding], list[models.Material]]:
    groups = session.execute(
        select(models.EvidenceGroup).where(models.EvidenceGroup.case_id == case_id)
        .order_by(models.EvidenceGroup.position, models.EvidenceGroup.id)
    ).scalars().all()
    items = session.execute(
        select(models.EvidenceItem).where(models.EvidenceItem.case_id == case_id)
        .order_by(models.EvidenceItem.position, models.EvidenceItem.id)
    ).scalars().all()
    bindings = session.execute(
        select(models.EvidenceItemBinding)
        .where(models.EvidenceItemBinding.case_id == case_id)
    ).scalars().all()
    materials = session.execute(
        select(models.Material).where(models.Material.case_id == case_id)
        .order_by(models.Material.display_name, models.Material.id)
    ).scalars().all()
    return groups, items, bindings, materials


def compute_numbers(groups: Iterable[models.EvidenceGroup],
                    items: Iterable[models.EvidenceItem]) -> tuple[dict[str, str], dict[str, int]]:
    """``item_id -> 证据{组}-{序}`` and ``group_id -> 组号`` for confirmed rows only.

    Group order is ``(position, id)``; item order inside a group is
    ``(position, id)``.  Removed groups are skipped entirely, so deleting a group
    renumbers the ones after it.  A group created by an agent stays out of the
    numbering until its change is accepted (its items/bindings are pending too);
    the group row itself carries no review_state, so the caller filters pending
    additions via :func:`confirmed_catalog_rows`.
    """
    active_groups = sorted((g for g in groups if g.removed_at is None),
                           key=lambda g: (g.position, str(g.id)))
    numbers: dict[str, str] = {}
    group_numbers: dict[str, int] = {}
    for index, group in enumerate(active_groups, start=1):
        group_numbers[str(group.id)] = index
        members = sorted(
            (item for item in items
             if item.group_id == group.id and item.removed_at is None
             and item.review_state == "confirmed"),
            key=lambda item: (item.position, str(item.id)))
        for item_index, item in enumerate(members, start=1):
            numbers[str(item.id)] = f"证据{index}-{item_index}"
    return numbers, group_numbers


def _pending_addition_ids(session: Session, case_id: uuid.UUID) -> set[tuple[str, str]]:
    return domain_changes.pending_addition_ids(
        domain_changes.pending_changes(session, models.EvidenceChange, case_id))


def confirmed_catalog_rows(session: Session, case_id: uuid.UUID) -> tuple[
        list[models.EvidenceGroup], list[models.EvidenceItem],
        list[models.EvidenceItemBinding]]:
    """条目与绑定按自身的复核状态生效。导入分组有已确认条目后才编号。"""
    groups, items, bindings, _materials = _load(session, case_id)
    change_model = models.EvidenceChange
    proposals = session.scalars(select(change_model).where(
        change_model.case_id == case_id,
        (change_model.review_state == "pending") | (change_model.command == "import_catalog"),
    ))
    imported_groups, pending_groups = set(), set()
    for change in proposals:
        group_ids = {uuid.UUID(entry["id"]) for entry in change.diff
                     if entry["target"] == "evidence_group" and entry["before"] is None}
        if change.command == "import_catalog":
            imported_groups.update(group_ids)
        else:
            pending_groups.update(group_ids)
    confirmed_groups = {item.group_id for item in items
                        if item.removed_at is None and item.review_state == "confirmed"}
    groups = [group for group in groups if group.id not in pending_groups
              and (group.id not in imported_groups or group.id in confirmed_groups)]
    return groups, items, bindings


def _seq(session: Session, case_id: uuid.UUID) -> int:
    # Plain read: the caller holds the case row lock; a seq-row lock here would
    # invert the fixed case -> domain order.
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id,
               models.CaseChangeSeq.domain == DOMAIN)
    ).scalar_one_or_none()
    return row.seq if row else 0


def get_catalog(session: Session, case_id: uuid.UUID) -> EvidenceCatalogOut:
    cases.lock_case_shared(session, case_id)
    groups, items, bindings, materials = _load(session, case_id)
    confirmed_groups, confirmed_items, _confirmed_bindings = confirmed_catalog_rows(
        session, case_id)
    numbers, group_numbers = compute_numbers(confirmed_groups, confirmed_items)
    changes = domain_changes.list_changes(session, models.EvidenceChange, case_id)
    return EvidenceCatalogOut(
        case_id=case_id, seq=_seq(session, case_id),
        groups=[_group_out(row) for row in groups],
        items=[_item_out(row) for row in items],
        bindings=[_binding_out(row) for row in bindings],
        materials=[CatalogMaterial(id=row.id, display_name=row.display_name) for row in materials],
        numbers=numbers, group_numbers=group_numbers,
        pending_changes=[_change_out(row) for row in changes if row.review_state == "pending"],
        changes=[_change_out(row) for row in changes],
    )


def _change_out(row: models.EvidenceChange) -> GraphChangeOut:
    return GraphChangeOut(
        id=row.id, case_id=row.case_id, seq=row.seq, command=row.command, actor=row.actor,
        entry=row.entry, diff=[GraphDiffEntry(**item) for item in row.diff],
        review_state=row.review_state, reviewed_at=row.reviewed_at,
        reverted_by=row.reverted_by, created_at=row.created_at,
    )


def _locate_source(session: Session, source: models.GraphSource) -> tuple[
        uuid.UUID | None, int | None, str | None, uuid.UUID | None, uuid.UUID | None]:
    """Resolve a source reference to (material_id, page, excerpt, version, ref).

    ``version`` is the immutable file version the reference resolves to;
    ``ref`` is the segment UUID when the source points at an immutable segment.
    Both are what P4 needs to open the source panel without guessing.

    ``ParseUnit`` has no ``locator`` column: the page comes from the segment's
    own locator, or from the unit key (``pdf:12``) when the locator omits it.
    """
    if source.segment_id is not None:
        segment = session.get(models.Segment, source.segment_id)
        if segment is None:
            return None, None, None, None, None
        unit = session.get(models.ParseUnit, segment.parse_unit_id)
        run = session.get(models.ParseRun, segment.parse_run_id)
        version = session.get(models.FileVersion, run.file_version_id) if run else None
        material_id = version.material_id if version else None
        page = None
        value = (segment.locator or {}).get("page")
        if isinstance(value, int) and value >= 1:
            page = value
        elif unit is not None:
            page = _page_of_unit_key(unit.unit_key)
        return material_id, page, segment.text, version.id if version else None, segment.id
    if source.annotation_id is not None:
        annotation = session.get(models.Annotation, source.annotation_id)
        if annotation is None:
            return None, None, None, None, None
        version = session.get(models.FileVersion, annotation.file_version_id)
        material_id = version.material_id if version else None
        excerpt = annotation.reviewed_text or annotation.body
        return (material_id, annotation.page, excerpt,
                version.id if version else None, None)
    if source.material_id is not None:
        material = session.get(models.Material, source.material_id)
        version_id = material.original_file_version_id if material else None
        return source.material_id, None, None, version_id, None
    return None, None, None, None, None


def _page_of_unit_key(unit_key: str) -> int | None:
    """``pdf:12`` -> 12; other formats have no page concept (same rule as parsing)."""
    prefix, _, rest = (unit_key or "").partition(":")
    if prefix == "pdf" and rest.isdigit():
        value = int(rest)
        return value if value >= 1 else None
    return None


def _covers(binding: models.EvidenceItemBinding, page: int | None) -> bool:
    if page is None or binding.page_start is None:
        return True
    return binding.page_start <= page <= (binding.page_end or binding.page_start)


def resolve_labels(session: Session, case_id: uuid.UUID,
                   sources: Iterable[models.GraphSource]) -> dict[str, ResolvedLabelOut]:
    """Batch-resolve active sources to current catalog labels.

    Unmatched sources show the material name and 「未编号」; a source whose
    reference no longer exists shows 「（出处已失效）」 — never a guessed number.
    Callers hold the case row lock (shared read or exclusive write); this
    function itself takes no locks so it can run inside either.
    """
    groups, items, bindings, materials = _load(session, case_id)
    confirmed_groups, confirmed_items, confirmed_bindings = confirmed_catalog_rows(
        session, case_id)
    numbers, group_numbers = compute_numbers(confirmed_groups, confirmed_items)
    by_material = {str(row.id): row for row in materials}
    confirmed_bindings = [b for b in confirmed_bindings
                          if b.removed_at is None and b.review_state == "confirmed"]
    items_by_id = {str(item.id): item for item in items}
    result: dict[str, ResolvedLabelOut] = {}
    for source in sources:
        material_id, page, excerpt, file_version_id, source_ref = _locate_source(session, source)
        if material_id is None:
            result[str(source.id)] = ResolvedLabelOut(
                labels=[], text="（出处已失效）", material_id=None,
                material_title="", page=page, text_excerpt=excerpt,
                source_ref=source_ref, file_version_id=file_version_id)
            continue
        material = by_material.get(str(material_id))
        if material is None:
            result[str(source.id)] = ResolvedLabelOut(
                labels=[], text="（材料已不存在）", material_id=material_id,
                material_title="", page=page, text_excerpt=excerpt,
                source_ref=source_ref, file_version_id=file_version_id)
            continue
        item_ids = {
            str(binding.item_id) for binding in confirmed_bindings
            if binding.material_id == material_id and _covers(binding, page)
        }
        hits = [items_by_id[item_id] for item_id in item_ids
                if item_id in items_by_id and item_id in numbers]
        hits.sort(key=lambda item: (group_numbers.get(str(item.group_id), 10 ** 6),
                                    item.position, str(item.id)))
        suffix = f" 第{page}页" if page is not None else ""
        labels = [f"{numbers[str(item.id)]}{suffix}" for item in hits]
        result[str(source.id)] = ResolvedLabelOut(
            labels=labels,
            text="、".join(labels) if labels else f"{material.display_name}（未编号）",
            material_id=material_id, material_title=material.display_name,
            page=page, text_excerpt=excerpt,
            source_ref=source_ref, file_version_id=file_version_id)
    return result


def export_catalog_csv(session: Session, case_id: uuid.UUID) -> str:
    """CSV for the working draft; numbering is resolved, never copied from rows."""
    catalog = get_catalog(session, case_id)
    materials = {str(row.id): row.display_name for row in catalog.materials}
    bindings_by_item: dict[str, list[EvidenceBindingOut]] = {}
    for binding in catalog.bindings:
        if binding.removed_at is None:
            bindings_by_item.setdefault(str(binding.item_id), []).append(binding)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["组号", "组名", "序号", "编号", "证据名称", "证明目的",
                     "对应材料", "页码范围", "状态", "来源"])
    for group in catalog.groups:
        if group.removed_at is not None:
            continue
        group_no = catalog.group_numbers.get(str(group.id))
        members = [item for item in catalog.items
                   if str(item.group_id) == str(group.id) and item.removed_at is None]
        members.sort(key=lambda item: (item.position, str(item.id)))
        for index, item in enumerate(members, start=1):
            bound = bindings_by_item.get(str(item.id), [])
            names = "；".join(materials.get(str(b.material_id), "（材料已不存在）") for b in bound)
            pages = "；".join(
                f"{b.page_start}-{b.page_end}" if b.page_start is not None
                else "全部" for b in bound)
            writer.writerow([
                group_no, group.title, index, catalog.numbers.get(str(item.id), ""),
                item.title, item.purpose or "", names, pages, item.review_state,
                item.source_kind,
            ])
    return buffer.getvalue()


# --- import parsing helper ---------------------------------------------------

_NUMBER_RE = re.compile(r"^\s*证据\s*(\d+)\s*[-－—]\s*(\d+)")


def _parse_pages(value: str) -> tuple[int | None, int | None]:
    text = (value or "").strip()
    if not text:
        return None, None
    match = re.search(r"(\d+)\s*[-－—~至]\s*(\d+)", text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        return (start, end) if start >= 1 and end >= start else (None, None)
    match = re.search(r"(\d+)", text)
    if match:
        page = int(match.group(1))
        return (page, page) if page >= 1 else (None, None)
    return None, None


_HEADER_ALIASES = {
    "group_title": ("组名", "证据组", "组", "分组"),
    "item_title": ("证据名称", "名称", "证据", "证据名称/内容", "材料名称"),
    "purpose": ("证明目的", "证明对象", "证明事项", "用途"),
    "material": ("对应材料", "材料", "材料名称", "原件/复印件"),
    "pages": ("页码", "页数", "所在页"),
}


def table_to_draft_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Turn a DOCX/XLSX evidence-catalog table into import draft rows.

    Tolerant by design: the header row is located by keyword within the first
    five rows; unknown columns are ignored; rows without an evidence name are
    skipped.  Material *names* are returned, not ids — the caller (agent or UI)
    matches them to case materials and the service only stores confirmed ids.
    """
    header_index = -1
    mapping: dict[str, int] = {}
    for index, row in enumerate(rows[:5]):
        cells = [str(cell or "").strip() for cell in row]
        candidate = {}
        for field, aliases in _HEADER_ALIASES.items():
            for position, cell in enumerate(cells):
                if cell in aliases:
                    candidate[field] = position
                    break
        if "item_title" in candidate:
            header_index, mapping = index, candidate
            break
    if header_index < 0:
        raise _err(ErrorCode.UNSUPPORTED_FORMAT,
                   "未找到表头：需要包含「证据名称」列（可含组名/证明目的/材料/页码）")

    def cell(row: list[str], field: str) -> str:
        position = mapping.get(field)
        if position is None or position >= len(row):
            return ""
        return str(row[position] or "").strip()

    drafts: list[dict[str, Any]] = []
    last_group = ""
    for row in rows[header_index + 1:]:
        title = cell(row, "item_title")
        if not title:
            continue
        number_match = _NUMBER_RE.match(title)
        if number_match:
            title = title[number_match.end():].strip(" ：:、") or title
        group_title = cell(row, "group_title") or last_group or "导入证据"
        last_group = group_title
        page_start, page_end = _parse_pages(cell(row, "pages"))
        drafts.append({
            "group_title": group_title,
            "item_title": title,
            "item_purpose": cell(row, "purpose") or None,
            "material_title": cell(row, "material") or None,
            "page_start": page_start,
            "page_end": page_end,
        })
    if not drafts:
        raise _err(ErrorCode.INVALID_INPUT, "目录表格没有可导入的证据条目")
    return drafts


MAX_IMPORT_BYTES = 10 * 1024 * 1024
IMPORT_SUFFIXES = {".docx", ".xlsx", ".csv"}


def rows_from_catalog_bytes(data: bytes, filename: str) -> list[list[str]]:
    """Read the first usable table from an uploaded DOCX/XLSX/CSV catalog.

    This only turns the file into rows; header detection, numbering prefixes and
    page ranges stay in :func:`table_to_draft_rows`.  The result never touches the
    database: the caller shows a draft and commits explicit material ids.
    """
    if len(data) > MAX_IMPORT_BYTES:
        raise _err(ErrorCode.INVALID_INPUT, "目录文件超过 10 MB，请先裁剪或另存为 CSV",
                   size=len(data))
    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix not in IMPORT_SUFFIXES:
        raise _err(ErrorCode.UNSUPPORTED_FORMAT,
                   "仅支持 .docx / .xlsx / .csv 证据目录", suffix=suffix)
    if suffix == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030", errors="replace")
        return [row for row in csv.reader(io.StringIO(text))]
    if suffix == ".docx":
        from docx import Document

        try:
            document = Document(io.BytesIO(data))
        except Exception as exc:  # python-docx raises several unrelated types
            raise _err(ErrorCode.PARSE_FAILED, "无法读取 DOCX 文件") from exc
        rows: list[list[str]] = []
        for table in document.tables:
            for row in table.rows:
                rows.append([cell.text.strip() for cell in row.cells])
            if rows:
                break  # first table with content is the catalog
        if not rows:
            raise _err(ErrorCode.UNSUPPORTED_FORMAT, "DOCX 中没有可读取的表格")
        return rows
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise _err(ErrorCode.PARSE_FAILED, "无法读取 XLSX 文件") from exc
    try:
        for sheet in workbook.worksheets:
            rows = [["" if value is None else str(value).strip() for value in row]
                    for row in sheet.iter_rows(values_only=True)]
            if any(any(cell for cell in row) for row in rows):
                return rows
        raise _err(ErrorCode.UNSUPPORTED_FORMAT, "XLSX 中没有可读取的表格")
    finally:
        workbook.close()


def preview_catalog_bytes(data: bytes, filename: str) -> list[dict[str, Any]]:
    return table_to_draft_rows(rows_from_catalog_bytes(data, filename))


# --- write preamble ---------------------------------------------------------

def _begin(session: Session, case_id: uuid.UUID, actor: str, entry: str,
           command: str) -> None:
    rules = graph_rules.get_rules()
    if actor == "agent" and not graph_rules.evidence_agent_allowed(rules, command):
        raise _err(ErrorCode.FORBIDDEN, f"Agent 无权执行命令：{command}")
    # 维护门禁 → 案件行锁（锁内复核归档）→ 域序号锁；与 graph/进度同一串行点。
    cases.lock_case_writable(session, case_id, archived_message=ARCHIVED_MESSAGE)
    domain_changes.lock_seq(session, case_id, DOMAIN)


def _begin_review(session: Session, case_id: uuid.UUID, actor: str,
                  message: str) -> None:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, message)
    cases.lock_case_writable(session, case_id, archived_message=ARCHIVED_MESSAGE)
    domain_changes.lock_seq(session, case_id, DOMAIN)


def _finish(session: Session, case_id: uuid.UUID, change_id: uuid.UUID | None = None,
            notice: str | None = None) -> EvidenceCatalogOut:
    session.commit()
    catalog = get_catalog(session, case_id)
    catalog.last_change_id = change_id
    catalog.notice = notice
    return catalog


def _record(session: Session, case_id: uuid.UUID, command: str, actor: str, entry: str,
            diff: list[dict[str, Any]]) -> models.EvidenceChange:
    return domain_changes.record(
        session, models.EvidenceChange, case_id=case_id, domain=DOMAIN,
        command=command, actor=actor, entry=entry, diff=diff)


def _audit(session: Session, command: str, case_id: uuid.UUID, target_id: uuid.UUID) -> None:
    audit.record(session, operation=f"evidence.{command}", target_type="evidence",
                 target_id=target_id, case_id=case_id)


def _diff(target: str, row_id: uuid.UUID, before: dict[str, Any] | None,
          after: dict[str, Any] | None, before_rev: int | None = None,
          after_rev: int | None = None) -> dict[str, Any]:
    return {"target": target, "id": str(row_id), "before": before, "after": after,
            "before_rev": before_rev, "after_rev": after_rev}


def _group_or_error(session: Session, case_id: uuid.UUID,
                    group_id: uuid.UUID) -> models.EvidenceGroup:
    row = session.get(models.EvidenceGroup, group_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "证据分组不存在", group_id=str(group_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "证据分组不属于本案", group_id=str(group_id))
    return row


def _item_or_error(session: Session, case_id: uuid.UUID,
                   item_id: uuid.UUID) -> models.EvidenceItem:
    row = session.get(models.EvidenceItem, item_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "证据条目不存在", item_id=str(item_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "证据条目不属于本案", item_id=str(item_id))
    return row


def _binding_or_error(session: Session, case_id: uuid.UUID,
                      binding_id: uuid.UUID) -> models.EvidenceItemBinding:
    row = session.get(models.EvidenceItemBinding, binding_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "证据绑定不存在", binding_id=str(binding_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "证据绑定不属于本案", binding_id=str(binding_id))
    return row


def _cas(row: Any, expected_revision: int, label: str) -> None:
    if row.revision != expected_revision:
        raise _err(ErrorCode.CONFLICT, f"{label}已被修改，请刷新后重试",
                   expected_revision=expected_revision, actual_revision=row.revision)


def _active_groups(session: Session, case_id: uuid.UUID) -> list[models.EvidenceGroup]:
    return [row for row in _load(session, case_id)[0] if row.removed_at is None]


def _active_items(session: Session, case_id: uuid.UUID,
                  group_id: uuid.UUID | None = None) -> list[models.EvidenceItem]:
    return [row for row in _load(session, case_id)[1]
            if row.removed_at is None and (group_id is None or row.group_id == group_id)]


# --- group commands ---------------------------------------------------------

def add_group(session: Session, payload: AddEvidenceGroupInput, *, actor: str,
              entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "add_group")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新建证据分组必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.EvidenceGroup).where(
                models.EvidenceGroup.case_id == payload.case_id,
                models.EvidenceGroup.request_key == payload.request_key)
        ).scalar_one_or_none()
        if existing is not None:
            return _finish(session, payload.case_id)
    title = payload.title.strip()
    if not title:
        raise _err(ErrorCode.INVALID_INPUT, "分组名称不能为空")
    groups = _active_groups(session, payload.case_id)
    position = len(groups) if payload.position is None else max(0, min(payload.position, len(groups)))
    # 先按稳定序号归一化（软删除留下的间隙不产生 tie），再按序号插入。
    shifted: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        new_position = index if index < position else index + 1
        if group.position != new_position:
            before = _group_dict(group)
            group.position = new_position
            group.revision += 1
            group.updated_at = _now()
            shifted.append(_diff("evidence_group", group.id, before, _group_dict(group),
                                 before["revision"], group.revision))
    row = models.EvidenceGroup(
        id=uuid.uuid4(), case_id=payload.case_id, position=position, title=title,
        purpose=payload.purpose, created_by=actor, request_key=payload.request_key,
        removed_at=None, revision=0, created_at=_now(), updated_at=_now(),
    )
    session.add(row)
    session.flush()
    diff = [_diff("evidence_group", row.id, None, _group_dict(row), None, row.revision)]
    diff.extend(shifted)
    change = _record(session, payload.case_id, "add_group", actor, entry, diff)
    _audit(session, "add_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def update_group(session: Session, payload: UpdateEvidenceGroupInput, *, actor: str,
                 entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "update_group")
    row = _group_or_error(session, payload.case_id, payload.group_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    _cas(row, payload.expected_revision, "分组")
    changes: EvidenceGroupChanges = payload.changes
    provided = changes.model_fields_set
    before = _group_dict(row)
    before_rev = row.revision
    if "title" in provided:
        title = (changes.title or "").strip()
        if not title:
            raise _err(ErrorCode.INVALID_INPUT, "分组名称不能为空")
        row.title = title
    if "purpose" in provided:
        row.purpose = changes.purpose
    row.revision += 1
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "update_group", actor, entry,
                     [_diff("evidence_group", row.id, before, _group_dict(row),
                            before_rev, row.revision)])
    _audit(session, "update_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def reorder_groups(session: Session, payload: ReorderEvidenceGroupsInput, *, actor: str,
                   entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "reorder_groups")
    groups = _active_groups(session, payload.case_id)
    by_id = {row.id: row for row in groups}
    if len(set(payload.group_ids)) != len(payload.group_ids):
        raise _err(ErrorCode.INVALID_INPUT, "分组顺序包含重复项")
    if set(payload.group_ids) != set(by_id):
        raise _err(ErrorCode.INVALID_INPUT, "分组顺序必须覆盖全部有效分组",
                   expected=len(by_id), actual=len(payload.group_ids))
    diff: list[dict[str, Any]] = []
    for index, group_id in enumerate(payload.group_ids):
        row = by_id[group_id]
        if row.position == index:
            continue
        before = _group_dict(row)
        before_rev = row.revision
        row.position = index
        row.revision += 1
        row.updated_at = _now()
        diff.append(_diff("evidence_group", row.id, before, _group_dict(row),
                          before_rev, row.revision))
    if not diff:
        return _finish(session, payload.case_id)
    session.flush()
    change = _record(session, payload.case_id, "reorder_groups", actor, entry, diff)
    _audit(session, "reorder_groups", payload.case_id, payload.group_ids[0])
    return _finish(session, payload.case_id, change.id)


def remove_group(session: Session, payload: EvidenceGroupRevisionInput, *, actor: str,
                 entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "remove_group")
    row = _group_or_error(session, payload.case_id, payload.group_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    _cas(row, payload.expected_revision, "分组")
    if _active_items(session, payload.case_id, row.id):
        raise _err(ErrorCode.INVALID_INPUT, "分组内仍有证据条目：请先删除或移动条目")
    before = _group_dict(row)
    before_rev = row.revision
    row.removed_at = _now()
    row.revision += 1
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "remove_group", actor, entry,
                     [_diff("evidence_group", row.id, before, _group_dict(row),
                            before_rev, row.revision)])
    _audit(session, "remove_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


# --- item commands ----------------------------------------------------------

def _insert_item_position(session: Session, case_id: uuid.UUID, group_id: uuid.UUID,
                          position: int | None) -> tuple[int, list[dict[str, Any]]]:
    members = _active_items(session, case_id, group_id)
    # 稳定排序（position, id）后按序号插入并整体归一化：删除中间条目留下的
    # 间隙不会让 len() 插入与既有 position 出 tie。
    target = len(members) if position is None else max(0, min(position, len(members)))
    diff: list[dict[str, Any]] = []
    for index, item in enumerate(members):
        new_position = index if index < target else index + 1
        if item.position != new_position:
            before = _item_dict(item)
            before_rev = item.revision
            item.position = new_position
            item.revision += 1
            item.updated_at = _now()
            diff.append(_diff("evidence_item", item.id, before, _item_dict(item),
                              before_rev, item.revision))
    session.flush()
    return target, diff


def add_item(session: Session, payload: AddEvidenceItemInput, *, actor: str,
             entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "add_item")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新建证据条目必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.EvidenceItem).where(
                models.EvidenceItem.case_id == payload.case_id,
                models.EvidenceItem.request_key == payload.request_key)
        ).scalar_one_or_none()
        if existing is not None:
            return _finish(session, payload.case_id)
    group = _group_or_error(session, payload.case_id, payload.group_id)
    if group.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    if actor == "human" and ("evidence_group", str(group.id)) in _pending_addition_ids(
            session, payload.case_id):
        raise _err(ErrorCode.CONFLICT,
                   "该分组是 Agent 待确认新增：请先在待确认列表中确认后再添加条目")
    title = payload.title.strip()
    if not title:
        raise _err(ErrorCode.INVALID_INPUT, "证据名称不能为空")
    position, diff = _insert_item_position(session, payload.case_id, group.id, payload.position)
    row = models.EvidenceItem(
        id=uuid.uuid4(), case_id=payload.case_id, group_id=group.id, position=position,
        title=title, purpose=payload.purpose, source_kind="manual",
        import_material_id=None,
        # Agent 条目是草稿：确认前不参与编号（D2 / 检查单 4）。
        review_state="pending" if actor == "agent" else "confirmed", created_by=actor,
        request_key=payload.request_key, removed_at=None, revision=0,
        created_at=_now(), updated_at=_now(),
    )
    session.add(row)
    session.flush()
    diff.insert(0, _diff("evidence_item", row.id, None, _item_dict(row), None, row.revision))
    change = _record(session, payload.case_id, "add_item", actor, entry, diff)
    _audit(session, "add_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def update_item(session: Session, payload: UpdateEvidenceItemInput, *, actor: str,
                entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "update_item")
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "证据条目已删除")
    _cas(row, payload.expected_revision, "证据条目")
    changes: EvidenceItemChanges = payload.changes
    provided = changes.model_fields_set
    before = _item_dict(row)
    before_rev = row.revision
    if "title" in provided:
        title = (changes.title or "").strip()
        if not title:
            raise _err(ErrorCode.INVALID_INPUT, "证据名称不能为空")
        row.title = title
    if "purpose" in provided:
        row.purpose = changes.purpose
    # Agent 的修改仍待复核。条目先前的复核状态和编号继续有效。
    row.revision += 1
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "update_item", actor, entry,
                     [_diff("evidence_item", row.id, before, _item_dict(row),
                            before_rev, row.revision)])
    _audit(session, "update_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def move_item(session: Session, payload: MoveEvidenceItemInput, *, actor: str,
              entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "move_item")
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "证据条目已删除")
    target_group = _group_or_error(session, payload.case_id, payload.to_group_id)
    if target_group.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "目标分组已删除")
    if payload.before_item_id is not None:
        anchor = _item_or_error(session, payload.case_id, payload.before_item_id)
        if (anchor.removed_at is not None or anchor.group_id != target_group.id):
            raise _err(ErrorCode.INVALID_INPUT, "参照条目不在目标分组")
        if anchor.id == row.id:
            return _finish(session, payload.case_id)
    old_group = row.group_id
    # 记录受影响条目（原组 + 目标组）的移动前状态，按最终顺序整体归一化，
    # 避免 position 间隙导致的 tie 与半截重排。
    affected: dict[uuid.UUID, dict[str, Any]] = {}
    for item in session.execute(select(models.EvidenceItem).where(
            models.EvidenceItem.case_id == payload.case_id,
            models.EvidenceItem.removed_at.is_(None),
            models.EvidenceItem.group_id.in_([old_group, target_group.id]))).scalars().all():
        affected[item.id] = _item_dict(item)
    ordered = [item for item in _active_items(session, payload.case_id, target_group.id)
               if item.id != row.id]
    index = len(ordered)
    if payload.before_item_id is not None:
        index = next((i for i, item in enumerate(ordered)
                      if item.id == payload.before_item_id), len(ordered))
    ordered.insert(index, row)
    row.group_id = target_group.id
    for position, item in enumerate(ordered):
        item.position = position
    if old_group != target_group.id:
        for position, item in enumerate(_active_items(session, payload.case_id, old_group)):
            item.position = position
    diff: list[dict[str, Any]] = []
    for item_id, before in affected.items():
        item = session.get(models.EvidenceItem, item_id)
        if item is None:
            continue
        if item.group_id == uuid.UUID(before["group_id"]) and item.position == before["position"]:
            continue
        item.revision += 1
        item.updated_at = _now()
        diff.append(_diff("evidence_item", item.id, before, _item_dict(item),
                          before["revision"], item.revision))
    session.flush()
    change = _record(session, payload.case_id, "move_item", actor, entry, diff)
    _audit(session, "move_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def _active_bindings(session: Session, case_id: uuid.UUID,
                     item_id: uuid.UUID) -> list[models.EvidenceItemBinding]:
    binding = models.EvidenceItemBinding
    return list(session.scalars(select(binding).where(
        binding.case_id == case_id,
        binding.item_id == item_id,
        binding.removed_at.is_(None),
    )))


def remove_item(session: Session, payload: EvidenceItemRevisionInput, *, actor: str,
                entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "remove_item")
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "证据条目已删除")
    _cas(row, payload.expected_revision, "证据条目")
    before = _item_dict(row)
    before_rev = row.revision
    timestamp = _now()
    row.removed_at = timestamp
    row.revision += 1
    row.updated_at = timestamp
    diff = [_diff("evidence_item", row.id, before, _item_dict(row), before_rev, row.revision)]
    # Bindings of a removed item disappear with it; numbering stops either way.
    for binding in _active_bindings(session, payload.case_id, row.id):
        b_before = _binding_dict(binding)
        binding.removed_at = timestamp
        diff.append(_diff("evidence_binding", binding.id, b_before, _binding_dict(binding)))
    # 归一化同组剩余条目，避免 position 间隙。
    for position, sibling in enumerate(_active_items(session, payload.case_id, row.group_id)):
        if sibling.position != position:
            sib_before = _item_dict(sibling)
            sibling.position = position
            sibling.revision += 1
            sibling.updated_at = timestamp
            diff.append(_diff("evidence_item", sibling.id, sib_before, _item_dict(sibling),
                              sib_before["revision"], sibling.revision))
    session.flush()
    change = _record(session, payload.case_id, "remove_item", actor, entry, diff)
    _audit(session, "remove_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


# --- binding commands -------------------------------------------------------

def add_binding(session: Session, payload: AddEvidenceBindingInput, *, actor: str,
                entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "add_binding")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新增证据绑定必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.EvidenceItemBinding.id).where(
                models.EvidenceItemBinding.case_id == payload.case_id,
                models.EvidenceItemBinding.item_id == payload.item_id,
                models.EvidenceItemBinding.material_id == payload.material_id,
                models.EvidenceItemBinding.page_start == payload.page_start,
                models.EvidenceItemBinding.page_end == payload.page_end,
                models.EvidenceItemBinding.removed_at.is_(None))
        ).scalar_one_or_none()
        if existing is not None:
            return _finish(session, payload.case_id)
    item = _item_or_error(session, payload.case_id, payload.item_id)
    if item.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "证据条目已删除")
    if actor == "human" and item.review_state == "pending":
        raise _err(ErrorCode.CONFLICT,
                   "该条目是 Agent 待确认新增：请先在待确认列表中确认后再修改")
    material = session.get(models.Material, payload.material_id)
    if material is None:
        raise _err(ErrorCode.NOT_FOUND, "材料不存在", material_id=str(payload.material_id))
    if material.case_id != payload.case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "材料不属于本案", material_id=str(payload.material_id))
    duplicate = session.execute(
        select(models.EvidenceItemBinding).where(
            models.EvidenceItemBinding.case_id == payload.case_id,
            models.EvidenceItemBinding.item_id == item.id,
            models.EvidenceItemBinding.material_id == material.id,
            models.EvidenceItemBinding.page_start == payload.page_start,
            models.EvidenceItemBinding.page_end == payload.page_end,
            models.EvidenceItemBinding.removed_at.is_(None))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _err(ErrorCode.INVALID_INPUT, "该材料与页码已绑定到此条目")
    row = models.EvidenceItemBinding(
        id=uuid.uuid4(), case_id=payload.case_id, item_id=item.id, material_id=material.id,
        page_start=payload.page_start, page_end=payload.page_end,
        # Agent proposals wait for human confirmation and never enter numbering.
        review_state="pending" if actor == "agent" else "confirmed",
        created_by=actor, removed_at=None, created_at=_now(),
    )
    session.add(row)
    session.flush()
    change = _record(session, payload.case_id, "add_binding", actor, entry,
                     [_diff("evidence_binding", row.id, None, _binding_dict(row))])
    _audit(session, "add_binding", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_binding(session: Session, payload: RemoveEvidenceBindingInput, *, actor: str,
                   entry: str) -> EvidenceCatalogOut:
    _begin(session, payload.case_id, actor, entry, "remove_binding")
    row = _binding_or_error(session, payload.case_id, payload.binding_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "绑定已解除")
    before = _binding_dict(row)
    row.removed_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "remove_binding", actor, entry,
                     [_diff("evidence_binding", row.id, before, _binding_dict(row))])
    _audit(session, "remove_binding", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def set_binding_review(session: Session, payload: EvidenceBindingRevisionInput, *, actor: str,
                       entry: str, review_state: str) -> EvidenceCatalogOut:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "证据复核仅限人工入口")
    _begin(session, payload.case_id, actor, entry,
           "confirm_binding" if review_state == "confirmed" else "reject_binding")
    row = _binding_or_error(session, payload.case_id, payload.binding_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "绑定已解除")
    if row.review_state == review_state:
        return _finish(session, payload.case_id)
    if row.review_state != "pending":
        raise _err(ErrorCode.CONFLICT, "该绑定已复核，请刷新后核对")
    item = _item_or_error(session, payload.case_id, row.item_id)
    if item.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "证据条目已删除")
    if review_state == "confirmed" and item.review_state != "confirmed":
        raise _err(ErrorCode.INVALID_INPUT,
                   "所属证据条目尚未确认：请先确认条目，再确认绑定")
    before = _binding_dict(row)
    row.review_state = review_state
    session.flush()
    command = "confirm_binding" if review_state == "confirmed" else "reject_binding"
    change = _record(session, payload.case_id, command, actor, entry,
                     [_diff("evidence_binding", row.id, before, _binding_dict(row))])
    _settle_draft_proposals(session, change)
    _audit(session, command, payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def confirm_binding(session: Session, payload: EvidenceBindingRevisionInput, *, actor: str,
                    entry: str) -> EvidenceCatalogOut:
    return set_binding_review(session, payload, actor=actor, entry=entry,
                              review_state="confirmed")


def reject_binding(session: Session, payload: EvidenceBindingRevisionInput, *, actor: str,
                   entry: str) -> EvidenceCatalogOut:
    return set_binding_review(session, payload, actor=actor, entry=entry,
                              review_state="rejected")


def set_item_review(session: Session, payload: EvidenceItemRevisionInput, *, actor: str,
                    entry: str, review_state: str) -> EvidenceCatalogOut:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "证据复核仅限人工入口")
    command = "confirm_item" if review_state == "confirmed" else "reject_item"
    _begin(session, payload.case_id, actor, entry, command)
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "证据条目已删除")
    _cas(row, payload.expected_revision, "证据条目")
    if review_state == "confirmed":
        _require_group_reviewed(session, row)
    timestamp = _now()
    diff = [_review_draft(row, "evidence_item", review_state, timestamp)]
    if review_state == "rejected":
        bindings = session.scalars(select(models.EvidenceItemBinding).where(
            models.EvidenceItemBinding.case_id == payload.case_id,
            models.EvidenceItemBinding.item_id == row.id,
            models.EvidenceItemBinding.removed_at.is_(None),
            models.EvidenceItemBinding.review_state == "pending",
        ))
        diff.extend(_review_draft(binding, "evidence_binding", "rejected", timestamp)
                    for binding in bindings)
    change = _record(session, payload.case_id, command, actor, entry, diff)
    _settle_draft_proposals(session, change)
    _audit(session, command, payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def confirm_item(session: Session, payload: EvidenceItemRevisionInput, *, actor: str,
                 entry: str) -> EvidenceCatalogOut:
    return set_item_review(session, payload, actor=actor, entry=entry,
                           review_state="confirmed")


def reject_item(session: Session, payload: EvidenceItemRevisionInput, *, actor: str,
                entry: str) -> EvidenceCatalogOut:
    return set_item_review(session, payload, actor=actor, entry=entry,
                           review_state="rejected")


# --- external catalog import ------------------------------------------------

def import_catalog(session: Session, payload: ImportEvidenceCatalogInput, *, actor: str,
                   entry: str) -> EvidenceCatalogOut:
    """Store an imported external catalog as *pending drafts*.

    Groups are appended at the end so existing working-draft numbers do not move;
    items and bindings are ``pending`` and therefore excluded from numbering until
    a human confirms them (D2 / checklist item 4).
    """
    _begin(session, payload.case_id, actor, entry, "import_catalog")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 导入证据目录必须带 request_key")
    material = session.get(models.Material, payload.import_material_id)
    if material is None:
        raise _err(ErrorCode.NOT_FOUND, "导入材料不存在",
                   material_id=str(payload.import_material_id))
    if material.case_id != payload.case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "导入材料不属于本案",
                   material_id=str(payload.import_material_id))
    key = payload.request_key
    if key:
        replay = session.execute(
            select(models.EvidenceItem.id).where(
                models.EvidenceItem.case_id == payload.case_id,
                models.EvidenceItem.request_key == f"{key}#0")
        ).scalar_one_or_none()
        if replay is not None:
            return _finish(session, payload.case_id)
    timestamp = _now()
    diff: list[dict[str, Any]] = []
    groups = _active_groups(session, payload.case_id)
    # 追加在末尾且按稳定序号归一化：既有工作稿编号不动，间隙不产生 tie。
    for index, existing in enumerate(groups):
        if existing.position != index:
            before = _group_dict(existing)
            existing.position = index
            existing.revision += 1
            existing.updated_at = timestamp
            diff.append(_diff("evidence_group", existing.id, before, _group_dict(existing),
                              before["revision"], existing.revision))
    next_position = len(groups)
    group_rows: dict[str, models.EvidenceGroup] = {}
    item_counts: dict[str, int] = {}
    for index, draft in enumerate(payload.rows):
        group_title = draft.group_title.strip()
        group = group_rows.get(group_title)
        if group is None:
            group = models.EvidenceGroup(
                id=uuid.uuid4(), case_id=payload.case_id, position=next_position,
                title=group_title, purpose=draft.group_purpose, created_by=actor,
                request_key=f"{key}#g{len(group_rows)}" if key else None,
                removed_at=None, revision=0, created_at=timestamp, updated_at=timestamp,
            )
            session.add(group)
            session.flush()
            group_rows[group_title] = group
            next_position += 1
            item_counts[group_title] = 0
            diff.append(_diff("evidence_group", group.id, None, _group_dict(group)))
        item = models.EvidenceItem(
            id=uuid.uuid4(), case_id=payload.case_id, group_id=group.id,
            position=item_counts[group_title], title=draft.item_title.strip(),
            purpose=draft.item_purpose, source_kind="imported",
            import_material_id=material.id, review_state="pending", created_by=actor,
            request_key=f"{key}#{index}" if key else None,
            removed_at=None, revision=0, created_at=timestamp, updated_at=timestamp,
        )
        session.add(item)
        session.flush()
        item_counts[group_title] += 1
        diff.append(_diff("evidence_item", item.id, None, _item_dict(item)))
        for binding in draft.bindings:
            bound_material = session.get(models.Material, binding.material_id)
            if bound_material is None:
                raise _err(ErrorCode.NOT_FOUND, "目录绑定的材料不存在",
                           material_id=str(binding.material_id))
            if bound_material.case_id != payload.case_id:
                raise _err(ErrorCode.CASE_MISMATCH, "目录绑定的材料不属于本案",
                           material_id=str(binding.material_id))
            row = models.EvidenceItemBinding(
                id=uuid.uuid4(), case_id=payload.case_id, item_id=item.id,
                material_id=bound_material.id, page_start=binding.page_start,
                page_end=binding.page_end, review_state="pending", created_by=actor,
                removed_at=None, created_at=timestamp,
            )
            session.add(row)
            session.flush()
            diff.append(_diff("evidence_binding", row.id, None, _binding_dict(row)))
    change = _record(session, payload.case_id, "import_catalog", actor, entry, diff)
    change.review_state = "pending"
    _audit(session, "import_catalog", payload.case_id, payload.import_material_id)
    return _finish(session, payload.case_id, change.id,
                   notice=f"已导入 {len(payload.rows)} 条草稿，确认后才参与编号")


# --- review of agent changes ------------------------------------------------

_EVIDENCE_TARGETS = ("evidence_group", "evidence_item", "evidence_binding")
_META_FIELDS = {"revision", "updated_at", "updated_by", "created_at"}
# 完整 ISO 时间戳（必须带 T 与时分秒），避免把普通日期形文本当时间归一化。
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$")


def _norm_value(value: Any) -> Any:
    """时间值统一成 UTC ISO 再比较（psycopg 会话时区 vs diff 里的 UTC）。

    只对 datetime 与「整串就是 ISO 时间戳」的字符串做归一化；普通文本原样返回。
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    if isinstance(value, str) and _ISO_TS_RE.match(value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()
    return value


def _changed_fields(before: dict[str, Any] | None,
                    after: dict[str, Any] | None) -> set[str]:
    if not before or not after:
        return set()
    return {key for key in set(before) | set(after)
            if key not in _META_FIELDS
            and _norm_value(before.get(key)) != _norm_value(after.get(key))}


def _entries_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["target"] != right["target"] or str(left["id"]) != str(right["id"]):
        return False
    if not (left.get("before") and left.get("after")
            and right.get("before") and right.get("after")):
        return True
    return bool(_changed_fields(left["before"], left["after"])
                & _changed_fields(right["before"], right["after"]))


def _changes_overlap(left: models.EvidenceChange, right: models.EvidenceChange) -> bool:
    return any(_entries_overlap(a, b) for a in left.diff for b in right.diff)


def _pending_neighbour(session: Session, case_id: uuid.UUID, change: models.EvidenceChange,
                       direction: str) -> models.EvidenceChange | None:
    query = select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == case_id,
        models.EvidenceChange.review_state == "pending",
        models.EvidenceChange.id != change.id)
    query = (query.where(models.EvidenceChange.seq < change.seq) if direction == "earlier"
             else query.where(models.EvidenceChange.seq > change.seq))
    for other in session.execute(query.order_by(models.EvidenceChange.seq)).scalars().all():
        if _changes_overlap(change, other):
            return other
    return None


def _superseded_by_human(session: Session, case_id: uuid.UUID,
                         change: models.EvidenceChange) -> bool:
    # accept_change/accept_all 是复核动作（联动确认），不算人工改写。
    later = session.execute(
        select(models.EvidenceChange).where(
            models.EvidenceChange.case_id == case_id,
            models.EvidenceChange.actor == "human",
            models.EvidenceChange.seq > change.seq,
            models.EvidenceChange.command.not_in(
                ("revert_change", "accept_change", "accept_all")))
        .order_by(models.EvidenceChange.seq)).scalars().all()
    settled = {(target, str(row.id)) for target, row in _draft_rows(session, change)
               if change.review_state == "pending" and row.review_state != "pending"}
    return any(
        _entries_overlap(proposed, edited)
        for other in later for proposed in change.diff for edited in other.diff
        if (proposed["target"], proposed["id"]) not in settled
        and not (other.command in _DRAFT_REVIEW_COMMANDS
                 and proposed["before"] is None and proposed["target"] in _DRAFT_MODELS)
    )


def _draft_rows(session: Session, change: models.EvidenceChange):
    for item in change.diff:
        target = item["target"]
        if target not in _DRAFT_MODELS or item["before"] is not None:
            continue
        row = session.get(_DRAFT_MODELS[target], uuid.UUID(item["id"]))
        if row is not None and row.case_id == change.case_id and row.removed_at is None:
            yield target, row


def _review_draft(row: models.EvidenceItem | models.EvidenceItemBinding,
                  target: str, state: str, timestamp: datetime) -> dict[str, Any]:
    dict_of = _item_dict if target == "evidence_item" else _binding_dict
    before = dict_of(row)
    row.review_state = state
    if isinstance(row, models.EvidenceItem):
        row.revision += 1
        row.updated_at = timestamp
    after = dict_of(row)
    return _diff(target, row.id, before, after, before.get("revision"), after.get("revision"))


def _settle_draft_proposals(session: Session, decision: models.EvidenceChange) -> None:
    """收口已全部逐条复核的提案，保留创建与复核两份记录。"""
    proposals = domain_changes.pending_changes(session, models.EvidenceChange, decision.case_id)
    for change in proposals:
        if not _changes_overlap(change, decision):
            continue
        rows = list(_draft_rows(session, change))
        if not rows or any(row.review_state == "pending" for _, row in rows):
            continue
        if any(row.review_state == "confirmed" for _, row in rows):
            change.review_state = "accepted"
            change.reviewed_at = _now()
        else:
            domain_changes.mark_reverted(session, change, decision.id)


def _require_group_reviewed(session: Session, item: models.EvidenceItem) -> None:
    changes = domain_changes.pending_changes(session, models.EvidenceChange, item.case_id)
    standalone_groups = domain_changes.pending_addition_ids(
        [change for change in changes if change.command == "add_group"])
    if ("evidence_group", str(item.group_id)) in standalone_groups:
        raise _err(ErrorCode.CONFLICT, "请先确认所属证据分组")


def _confirm_draft_rows(session: Session, change: models.EvidenceChange,
                        timestamp: datetime) -> list[dict[str, Any]]:
    diff: list[dict[str, Any]] = []
    for target, row in _draft_rows(session, change):
        if row.review_state != "pending":
            continue
        if isinstance(row, models.EvidenceItemBinding):
            item = _item_or_error(session, change.case_id, row.item_id)
            if item.review_state == "pending":
                raise _err(ErrorCode.CONFLICT, "请先确认所属证据条目")
            state = ("confirmed" if item.review_state == "confirmed" and item.removed_at is None
                     else "rejected")
        else:
            _require_group_reviewed(session, row)
            state = "confirmed"
        diff.append(_review_draft(row, target, state, timestamp))
    return diff


def accept_change(session: Session, payload: EvidenceChangeIdInput, *, actor: str,
                  entry: str) -> EvidenceCatalogOut:
    _begin_review(session, payload.case_id, actor, "只有承办人可以确认 Agent 修改")
    change = session.get(models.EvidenceChange, payload.change_id)
    if change is None or change.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在", change_id=str(payload.change_id))
    if change.review_state != "pending":
        raise _err(ErrorCode.INVALID_INPUT, "该变更不处于待确认状态")
    if _pending_neighbour(session, payload.case_id, change, "earlier") is not None:
        raise _err(ErrorCode.CONFLICT,
                   "请先处理更早的待确认变更（按写入顺序复核），再确认本条")
    if _superseded_by_human(session, payload.case_id, change):
        raise _err(ErrorCode.CONFLICT,
                   "该对象的相关字段已被人工修改，Agent 变更已过期：请撤回该变更")
    timestamp = _now()
    confirm_diff = _confirm_draft_rows(session, change, timestamp)
    change.review_state = "accepted"
    change.reviewed_at = timestamp
    domain_changes.bump_seq(session, payload.case_id, DOMAIN)
    if confirm_diff:
        # 确认联动的行级变化单独留痕（人工命令），编号随之重算。
        _record(session, payload.case_id, "accept_change", actor, entry, confirm_diff)
    _audit(session, "accept_change", payload.case_id, change.id)
    return _finish(session, payload.case_id)


def accept_all(session: Session, payload: EvidenceAcceptAllInput, *, actor: str,
               entry: str) -> EvidenceCatalogOut:
    _begin_review(session, payload.case_id, actor, "只有承办人可以确认 Agent 修改")
    ids_provided = "change_ids" in payload.model_fields_set
    if ids_provided and not payload.change_ids:
        raise _err(ErrorCode.INVALID_INPUT, "确认范围不能为空：请提交已展示的变更 ID")
    if not ids_provided and payload.expected_seq is None:
        raise _err(ErrorCode.INVALID_INPUT, "一键确认须提交已展示的变更 ID 或操作序号")
    current = domain_changes.lock_seq(session, payload.case_id, DOMAIN)
    if payload.expected_seq is not None and payload.expected_seq != current:
        raise _err(ErrorCode.CONFLICT, "待确认列表已变化，请刷新后重新审核",
                   expected_seq=payload.expected_seq, actual_seq=current)
    query = select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == payload.case_id,
        models.EvidenceChange.review_state == "pending").order_by(models.EvidenceChange.seq)
    if ids_provided:
        query = query.where(models.EvidenceChange.id.in_(payload.change_ids))
    pending = session.execute(query).scalars().all()
    if not pending:
        raise _err(ErrorCode.INVALID_INPUT, "没有待确认的 Agent 修改")
    accepting = [change for change in pending
                 if not _superseded_by_human(session, payload.case_id, change)]
    skipped = len(pending) - len(accepting)
    if not accepting:
        raise _err(ErrorCode.CONFLICT, "待确认变更均已被人工修改覆盖：请逐条撤回后再操作")
    timestamp = _now()
    confirm_diff: list[dict[str, Any]] = []
    for change in accepting:
        confirm_diff.extend(_confirm_draft_rows(session, change, timestamp))
        change.review_state = "accepted"
        change.reviewed_at = timestamp
    domain_changes.bump_seq(session, payload.case_id, DOMAIN)
    if confirm_diff:
        _record(session, payload.case_id, "accept_all", actor, entry, confirm_diff)
    for change in accepting:
        _audit(session, "accept_all", payload.case_id, change.id)
    notice = f"已确认 {len(accepting)} 条 Agent 修改。" + (
        f"另有 {skipped} 条已被人工修改覆盖，未确认（请撤回）。" if skipped else "")
    return _finish(session, payload.case_id, notice=notice)


def _matches_after(current: dict[str, Any], item: dict[str, Any]) -> bool:
    """revision 已被复核等动作推进时，退化为「改过的字段是否仍是 after 值」。"""
    after = item.get("after")
    if not after:
        return False
    if item.get("after_rev") is not None and current.get("revision") == item["after_rev"]:
        return True
    fields = _changed_fields(item.get("before"), after)
    if not fields:
        return _norm_value(current.get("removed_at")) == after.get("removed_at")
    return all(_norm_value(current.get(field)) == after.get(field) for field in fields)


def _merge_diff(diff: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 (target, id) 的多条 diff 合并为一条净变化（首 before → 末 after）。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in diff:
        key = (item["target"], str(item["id"]))
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
            continue
        first = merged[key]
        first["after"] = item["after"]
        first["after_rev"] = item.get("after_rev")
        if first.get("before_rev") is None:
            first["before_rev"] = item.get("before_rev")
    return [merged[key] for key in order if merged[key]["before"] != merged[key]["after"]]


def _apply_evidence_before(session: Session, case_id: uuid.UUID,
                           item: dict[str, Any], timestamp: datetime,
                           diff_out: list[dict[str, Any]]) -> None:
    target = item["target"]
    row_id = uuid.UUID(item["id"])
    before = item["before"]
    if target == "evidence_group":
        row = session.get(models.EvidenceGroup, row_id)
    elif target == "evidence_item":
        row = session.get(models.EvidenceItem, row_id)
    else:
        row = session.get(models.EvidenceItemBinding, row_id)
    if row is None or row.case_id != case_id:
        raise _err(ErrorCode.CONFLICT, "证据目录已被后续修改，无法撤回，请人工处理",
                   target=target, id=item["id"])
    if row.removed_at is not None and before is None:
        raise _err(ErrorCode.CONFLICT, "证据目录已被后续修改，无法撤回，请人工处理",
                   target=target, id=item["id"])
    current = {"evidence_group": _group_dict, "evidence_item": _item_dict,
               "evidence_binding": _binding_dict}[target](row)
    if before is None:
        # Revert of a creation: soft delete keeps the row (and its id) so the
        # revert itself stays revertible; bindings/items/groups are all soft.
        row.removed_at = timestamp
        if hasattr(row, "revision"):
            row.revision += 1
        if hasattr(row, "updated_at"):
            row.updated_at = timestamp
    else:
        # 仅反转本变更实际改过的字段，保留后续独立的人工修改。
        for field in _changed_fields(before, item["after"]):
            value = before.get(field)
            if field == "removed_at":
                row.removed_at = datetime.fromisoformat(value) if value else None
            elif hasattr(row, field):
                setattr(row, field, value)
        if hasattr(row, "revision"):
            row.revision += 1
        if hasattr(row, "updated_at"):
            row.updated_at = timestamp
    session.flush()
    after = {"evidence_group": _group_dict, "evidence_item": _item_dict,
             "evidence_binding": _binding_dict}[target](row)
    diff_out.append(_diff(target, row.id, current, after,
                          current.get("revision"), after.get("revision")))


def revert_change(session: Session, payload: EvidenceChangeIdInput, *, actor: str,
                  entry: str) -> EvidenceCatalogOut:
    _begin_review(session, payload.case_id, actor, "只有承办人可以撤回 Agent 修改")
    change = session.get(models.EvidenceChange, payload.change_id)
    if change is None or change.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在")
    if change.review_state not in ("pending", "accepted", "none"):
        raise _err(ErrorCode.INVALID_INPUT, "该变更已撤回或不可撤回")
    if _pending_neighbour(session, payload.case_id, change, "later") is not None:
        raise _err(ErrorCode.CONFLICT,
                   "请先处理更晚的待确认变更（按写入顺序复核），再撤回本条")
    # 部分逐条复核后，只撤回未决定的行，保留已经作出的人工决定。
    drafts = list(_draft_rows(session, change))
    if (change.review_state == "pending"
            and any(row.review_state != "pending" for _, row in drafts)
            and not _superseded_by_human(session, payload.case_id, change)):
        timestamp = _now()
        diff = [_review_draft(row, target, "rejected", timestamp)
                for target, row in drafts if row.review_state == "pending"]
        reverted = _record(session, payload.case_id, "revert_change", actor, entry, diff)
        domain_changes.mark_reverted(session, change, reverted.id)
        _audit(session, "revert_change", payload.case_id, change.id)
        return _finish(session, payload.case_id, reverted.id,
                       notice="已撤回未确认的草稿，保留逐条复核结果。")
    # 经批量接受产生的确认不阻止撤回，独立的人工决定不能被整批覆盖。
    decisions = session.scalars(select(models.EvidenceChange).where(
        models.EvidenceChange.case_id == payload.case_id,
        models.EvidenceChange.command.in_(("confirm_item", "confirm_binding")),
        models.EvidenceChange.seq > change.seq,
    )).all()
    for target, row in drafts:
        if row.review_state == "confirmed" and any(
                entry["target"] == target and entry["id"] == str(row.id)
                for decision in decisions for entry in decision.diff):
            raise _err(ErrorCode.CONFLICT,
                       "该条目或绑定已人工确认，不能整体撤回；可改为驳回或删除")
    # 人工已改写同一字段：提案过期，只标记撤回，不改动当前工作稿。
    # 已被 accept_change 联动确认的条目不算人工改写（仍随提案整体撤回）。
    if _superseded_by_human(session, payload.case_id, change):
        revert_record = _record(session, payload.case_id, "revert_change", actor, entry, [])
        domain_changes.mark_reverted(session, change, revert_record.id)
        _audit(session, "revert_change", payload.case_id, change.id)
        return _finish(session, payload.case_id, revert_record.id,
                       notice="该变更已被人工修改覆盖，仅标记撤回，当前工作稿未改动。")
    session.expire_all()  # 丢弃 identity map 旧值：守卫必须读最新已提交行
    removing_items = {row.id for target, row in drafts if target == "evidence_item"}
    removing_bindings = {row.id for target, row in drafts if target == "evidence_binding"}
    # Whole-or-nothing: any target edited since the change is a conflict.
    # 绑定没有 revision：用字段级 matchesAfter 检查 after 是否仍成立。
    for item in change.diff:
        if item["target"] not in _EVIDENCE_TARGETS:
            continue
        row_id = uuid.UUID(item["id"])
        model = {"evidence_group": models.EvidenceGroup,
                 "evidence_item": models.EvidenceItem,
                 "evidence_binding": models.EvidenceItemBinding}[item["target"]]
        current = session.get(model, row_id)
        if current is None or current.case_id != payload.case_id:
            raise _err(ErrorCode.CONFLICT, "证据目录已被后续修改，无法撤回，请人工处理")
        if item.get("before") is None:
            if item["target"] == "evidence_group" and any(
                    member.id not in removing_items
                    for member in _active_items(session, payload.case_id, row_id)):
                raise _err(ErrorCode.CONFLICT, "证据分组下还有其他有效条目，不能撤回分组")
            if item["target"] == "evidence_item" and any(
                    binding.id not in removing_bindings
                    for binding in _active_bindings(session, payload.case_id, row_id)):
                raise _err(ErrorCode.CONFLICT, "证据条目已有其他材料绑定，不能撤回条目")
            continue
        if item.get("after") is None:
            continue
        dict_of = {"evidence_group": _group_dict, "evidence_item": _item_dict,
                   "evidence_binding": _binding_dict}[item["target"]]
        if not _matches_after(dict_of(current), item):
            raise _err(ErrorCode.CONFLICT, "证据目录已被后续修改，无法撤回，请人工处理")
    timestamp = _now()
    reverse_diff: list[dict[str, Any]] = []
    for item in change.diff:
        _apply_evidence_before(session, payload.case_id, item, timestamp, reverse_diff)
    reverse_diff = _merge_diff(reverse_diff)
    revert_record = _record(session, payload.case_id, "revert_change", actor, entry,
                            reverse_diff)
    domain_changes.mark_reverted(session, change, revert_record.id)
    _audit(session, "revert_change", payload.case_id, change.id)
    return _finish(session, payload.case_id, revert_record.id)
