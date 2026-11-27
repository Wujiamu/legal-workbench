"""Deterministic spreadsheet column mapping and relation-graph import (Wave 2B)."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from openpyxl import load_workbook
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cases, evidence_catalog, graph, graph_rules, materials, models
from .errors import ErrorCode, WorkbenchError
from .schemas import (
    AddEntityInput,
    AddGraphRelationInput,
    AddSourceInput,
    AddToGraphInput,
    Contract,
    GraphWorkspace,
    RelationAttrs,
    RelationChanges,
    SourceRefInput,
    UpdateRelationInput,
)
from .storage import Storage
from plainfolio_parse import tables
from plainfolio_parse.errors import ParseError

from .textnorm import normalize_with_map

MAX_IMPORT_BYTES = evidence_catalog.MAX_IMPORT_BYTES
TABLE_SUFFIXES = frozenset({".xlsx", ".csv"})

_OUTBOUND_DIRECTIONS = frozenset({"支出", "转出", "借出", "out", "debit", "付", "付款", "汇出", "出账"})
_INBOUND_DIRECTIONS = frozenset({"收入", "转入", "借入", "in", "credit", "收", "收款", "汇入", "入账", "进账"})



def _err(code: ErrorCode, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details or None)


# 独立包的错误码 → 工作台 ErrorCode；只映射真正会出现的六类，其余按内部错误处理。
_PARSE_CODE_MAP = {
    "invalid_input": ErrorCode.INVALID_INPUT,
    "not_found": ErrorCode.NOT_FOUND,
    "forbidden": ErrorCode.FORBIDDEN,
    "unsupported_format": ErrorCode.UNSUPPORTED_FORMAT,
    "parse_failed": ErrorCode.PARSE_FAILED,
    "internal_error": ErrorCode.INTERNAL_ERROR,
}


# --- contracts ---------------------------------------------------------------


class TablePreviewInput(Contract):
    material_id: UUID | None = None
    sheet_name: str | None = None
    header_row: int = Field(default=1, ge=1, le=100)


class TablePreviewOutput(Contract):
    sheets: list[str]
    selected_sheet: str
    header_row: int
    headers: list[str]
    sample_rows: list[list[str]]
    total_rows: int


class TableCompileInput(Contract):
    material_id: UUID
    graph_id: UUID | None = None
    sheet_name: str | None = None
    header_row: int = Field(default=1, ge=1, le=100)
    from_col: int | None = Field(default=None, ge=0)
    from_constant: str | None = Field(default=None, max_length=200)
    to_col: int | None = Field(default=None, ge=0)
    to_constant: str | None = Field(default=None, max_length=200)
    direction_col: int | None = Field(default=None, ge=0)
    relation_kind: str = "fund_flow"
    amount_col: int | None = Field(default=None, ge=0)
    currency_col: int | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", max_length=8)
    share_pct_col: int | None = Field(default=None, ge=0)
    date_col: int | None = Field(default=None, ge=0)
    label_col: int | None = Field(default=None, ge=0)
    filter_min_amount: str | None = Field(default=None, max_length=40)
    aggregate_by_pair: bool = True


class TableNewEntity(Contract):
    name: str
    entity_kind: str


class CompiledTableEdge(Contract):
    from_name: str
    to_name: str
    from_entity_id: UUID | None
    to_entity_id: UUID | None
    existing_relation_id: UUID | None
    relation_kind: str
    label: str | None
    amount_cents: int | None
    currency: str | None
    share_pct: float | None
    date_start: str | None
    date_end: str | None
    tx_count: int
    row_numbers: list[int]
    proves: str


class TableSkippedRow(Contract):
    row_number: int
    reason: str
    raw_row: list[str]


class TableCompileOutput(Contract):
    case_id: UUID
    material_id: UUID
    selected_sheet: str
    new_entities: list[TableNewEntity]
    compiled_edges: list[CompiledTableEdge]
    skipped_rows: list[TableSkippedRow]
    processed_rows: int


class TableCommitInput(TableCompileInput):
    graph_id: UUID
    request_key: str | None = Field(default=None, max_length=200)


class TableCommitOutput(Contract):
    created_entities: int
    created_relations: int
    attached_sources: int
    skipped_rows: list[TableSkippedRow]
    workspace: GraphWorkspace


# --- table reading -----------------------------------------------------------


def _cell_str(value: Any) -> str:
    return tables.cell_str(value)


def _trim_row(cells: list[str]) -> list[str]:
    return tables.trim_row(cells)


def _trim_trailing_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    return tables.trim_trailing_empty_rows(rows)


def _decode_csv(data: bytes) -> str:
    return _from_package(tables.decode_csv, data)


_MAX_IMPORT_ROWS = tables._MAX_IMPORT_ROWS


def _extract_sheet_rows(sheet: Any) -> list[list[str]]:
    return _from_package(tables.extract_sheet_rows, sheet)


def _from_package(fn, *args, **kwargs):
    """把独立包的 ``ParseError`` 精确转回工作台 ``WorkbenchError``（消息不变）。"""
    try:
        return fn(*args, **kwargs)
    except ParseError as exc:
        code = _PARSE_CODE_MAP.get(exc.code, ErrorCode.INTERNAL_ERROR)
        details = {key: value for key, value in exc.details.items() if key != "reason"}
        raise _err(code, exc.message, **details) from exc


def _read_table_rows(data: bytes, suffix: str, sheet_name: str | None) -> tuple[list[str], str, list[list[str]]]:
    return _from_package(tables.read_table_rows, data, suffix, sheet_name)


def _material_table_bytes(session: Session, storage: Storage, case_id: UUID,
                          material_id: UUID) -> tuple[bytes, str]:
    material = materials._material_or_error(session, case_id, material_id)
    if material.original_file_version_id is None:
        raise _err(ErrorCode.NOT_FOUND, "材料尚无原件版本", material_id=str(material_id))
    version = materials._file_version_or_error(session, case_id, material.original_file_version_id)
    stored = session.get(models.StoredObject, version.stored_object_id)
    if stored is None or stored.case_id != case_id:
        raise _err(ErrorCode.NOT_FOUND, "文件内容记录缺失", file_version_id=str(version.id))
    if stored.integrity_status != "ok":
        raise _err(ErrorCode.INTEGRITY_ERROR, "原件校验失败，禁止读取", stored_object_id=str(stored.id))
    if not storage.verify(stored.relative_path, stored.sha256, stored.size):
        raise _err(ErrorCode.INTEGRITY_ERROR, "文件内容与登记哈希不一致", stored_object_id=str(stored.id))
    if stored.size > MAX_IMPORT_BYTES:
        raise _err(ErrorCode.INVALID_INPUT, "表格文件超过 10 MB，请先裁剪或分拆", size=stored.size)
    stored_suffix = PurePosixPath(stored.relative_path).suffix.lower()
    display_suffix = PurePosixPath(material.display_name).suffix.lower()
    if stored_suffix in TABLE_SUFFIXES:
        suffix = stored_suffix
    elif stored_suffix in ("", ".bin") and display_suffix in TABLE_SUFFIXES:
        suffix = display_suffix
    else:
        suffix = stored_suffix or display_suffix
        raise _err(ErrorCode.UNSUPPORTED_FORMAT, "仅支持 .xlsx / .csv 表格导入关系图", suffix=suffix)
    return storage.managed_path(stored.relative_path).read_bytes(), suffix


# --- value normalization & parsing ------------------------------------------


def _norm_name(value: str) -> str:
    normalized, _ = normalize_with_map(value)
    return normalized.strip().lower()


def _infer_entity_kind(name: str) -> str:
    return tables.infer_entity_kind(name)


def _normalize_currency(raw: str, fallback: str | None) -> str | None:
    return tables.normalize_currency(raw, fallback)


def _detect_inline_currency(raw: str) -> str | None:
    return tables.detect_inline_currency(raw)


def _parse_amount_cents(raw: str) -> tuple[int, bool]:
    """Parse monetary text using Decimal only; returns ``(abs_cents, is_negative)``."""
    return tables.parse_amount_cents(raw)


def _parse_share_pct(raw: str) -> float:
    return tables.parse_share_pct(raw)


def _parse_date(raw: str) -> date | None:
    return tables.parse_date(raw)


def _cell_at(row: list[str], col: int | None) -> str:
    if col is None or col < 0 or col >= len(row):
        return ""
    return row[col].strip()


def _format_row_ranges(row_numbers: list[int]) -> str:
    ordered = sorted(set(row_numbers))
    ranges: list[str] = []
    start = end = ordered[0]
    for num in ordered[1:]:
        if num == end + 1:
            end = num
            continue
        ranges.append(f"{start}" if start == end else f"{start}-{end}")
        start = end = num
    ranges.append(f"{start}" if start == end else f"{start}-{end}")
    return "、".join(ranges)


def _format_proves(sheet: str, row_numbers: list[int], tx_count: int) -> str:
    limit = graph_rules.PROVES_MAX
    if len(row_numbers) == 1:
        text = f"{sheet} 第{row_numbers[0]}行"
    else:
        text = f"{sheet} 第{_format_row_ranges(row_numbers)}行（共{tx_count}笔）"
        if len(text) > limit:
            text = f"{sheet} 第{min(row_numbers)}-{max(row_numbers)}行（共{tx_count}笔）"
    return text[:limit]


# --- preview service ---------------------------------------------------------


def preview_table_columns(
    session: Session,
    storage: Storage,
    case_id: UUID,
    *,
    material_id: UUID | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
    sheet_name: str | None = None,
    header_row: int = 1,
) -> TablePreviewOutput:
    """Inspect sheet names, header labels and up to 20 sample rows."""
    if not 1 <= header_row <= 100:
        raise _err(ErrorCode.INVALID_INPUT, "表头行号须在 1 到 100 之间", header_row=header_row)
    if (material_id is None) == (file_bytes is None):
        raise _err(ErrorCode.INVALID_INPUT, "必须且只能提供 material_id 或 file_bytes 之一")
    cases.lock_case_shared(session, case_id)
    if material_id is not None:
        data, suffix = _material_table_bytes(session, storage, case_id, material_id)
    else:
        assert file_bytes is not None
        data = file_bytes
        suffix = PurePosixPath((filename or "").strip()).suffix.lower()
    sheets, selected_sheet, rows = _read_table_rows(data, suffix, sheet_name)
    header_idx = header_row - 1
    raw_header = rows[header_idx] if header_idx < len(rows) else []
    data_rows = [row for row in rows[header_row:] if any(cell != "" for cell in row)]
    col_count = max([len(raw_header)] + [len(row) for row in data_rows], default=0)
    headers = [
        raw_header[i] if i < len(raw_header) and raw_header[i] else f"列{i + 1}"
        for i in range(col_count)
    ]
    sample_rows = [
        [row[i] if i < len(row) else "" for i in range(col_count)]
        for row in data_rows[:20]
    ]
    return TablePreviewOutput(
        sheets=sheets,
        selected_sheet=selected_sheet,
        header_row=header_row,
        headers=headers,
        sample_rows=sample_rows,
        total_rows=len(data_rows),
    )


# --- compile service ---------------------------------------------------------


def _entity_lookup(session: Session, case_id: UUID) -> dict[str, models.CaseEntity]:
    entities = session.scalars(
        select(models.CaseEntity)
        .where(models.CaseEntity.case_id == case_id, models.CaseEntity.removed_at.is_(None))
        .order_by(models.CaseEntity.created_at, models.CaseEntity.id)
    ).all()
    by_id = {row.id: row for row in entities}
    lookup: dict[str, models.CaseEntity] = {}
    for row in entities:
        lookup.setdefault(_norm_name(row.name), row)
    aliases = session.scalars(
        select(models.EntityAlias)
        .where(models.EntityAlias.case_id == case_id, models.EntityAlias.removed_at.is_(None))
        .order_by(models.EntityAlias.created_at, models.EntityAlias.id)
    ).all()
    for alias in aliases:
        target = by_id.get(alias.entity_id)
        if target is not None:
            lookup.setdefault(_norm_name(alias.alias), target)
    return lookup


def _relation_lookup(
    session: Session, case_id: UUID, relation_kind: str,
) -> dict[tuple[UUID, UUID], tuple[UUID, str | None]]:
    rows = session.scalars(
        select(models.CaseRelation).where(
            models.CaseRelation.case_id == case_id,
            models.CaseRelation.kind == relation_kind,
            models.CaseRelation.removed_at.is_(None),
            models.CaseRelation.credibility != "ruled_out",
        )
    ).all()
    return {
        (row.from_entity_id, row.to_entity_id): (
            row.id,
            (row.attrs or {}).get("currency") if isinstance(row.attrs, dict) else None,
        )
        for row in rows
    }


def _ruled_out_pairs(session: Session, case_id: UUID, relation_kind: str) -> set[tuple[UUID, UUID]]:
    rows = session.execute(
        select(models.CaseRelation.from_entity_id, models.CaseRelation.to_entity_id).where(
            models.CaseRelation.case_id == case_id,
            models.CaseRelation.kind == relation_kind,
            models.CaseRelation.removed_at.is_(None),
            models.CaseRelation.credibility == "ruled_out",
        )
    ).all()
    return {(row[0], row[1]) for row in rows}


def _validate_compile_input(payload: TableCompileInput) -> tuple[str, str, str, int | None]:
    rules = graph_rules.get_rules()
    if not graph_rules.valid_relation_kind(rules, payload.relation_kind):
        raise _err(ErrorCode.INVALID_INPUT, f"未知关系类型：{payload.relation_kind}")
    from_const = (payload.from_constant or "").strip()
    to_const = (payload.to_constant or "").strip()
    if payload.from_col is None and not from_const:
        raise _err(ErrorCode.INVALID_INPUT, "必须指定关系起点列或起点常量")
    if payload.to_col is None and not to_const:
        raise _err(ErrorCode.INVALID_INPUT, "必须指定关系终点列或终点常量")
    currency = _normalize_currency(payload.currency, "CNY")
    if currency is None:
        raise _err(ErrorCode.INVALID_INPUT, "默认币种格式无效", currency=payload.currency)
    min_amount_cents: int | None = None
    if payload.filter_min_amount is not None and payload.filter_min_amount.strip():
        if payload.amount_col is None:
            raise _err(ErrorCode.INVALID_INPUT, "指定最小金额过滤条件时必须映射金额列")
        try:
            abs_cents, is_negative = _parse_amount_cents(payload.filter_min_amount)
        except ValueError as exc:
            raise _err(ErrorCode.INVALID_INPUT, "最小金额过滤条件无效",
                       filter_min_amount=payload.filter_min_amount) from exc
        if is_negative:
            raise _err(ErrorCode.INVALID_INPUT, "最小金额过滤条件不能为负数",
                       filter_min_amount=payload.filter_min_amount)
        min_amount_cents = abs_cents
    return from_const, to_const, currency, min_amount_cents


def _parse_row(
    raw_row: list[str],
    row_number: int,
    payload: TableCompileInput,
    from_const: str,
    to_const: str,
    default_currency: str,
    min_amount_cents: int | None,
    entity_map: dict[str, models.CaseEntity],
    ruled_out_pairs: set[tuple[UUID, UUID]] | None = None,
) -> tuple[dict[str, Any] | None, TableSkippedRow | None]:
    """Validate and normalize one table row; returns (parsed_dict, skipped_row)."""
    raw_copy = list(raw_row)
    from_name = _cell_at(raw_row, payload.from_col) or from_const
    to_name = _cell_at(raw_row, payload.to_col) or to_const
    if not from_name:
        return None, TableSkippedRow(row_number=row_number, reason="missing_source_entity", raw_row=raw_copy)
    if not to_name:
        return None, TableSkippedRow(row_number=row_number, reason="missing_target_entity", raw_row=raw_copy)
    if len(from_name) > graph_rules.ENTITY_NAME_MAX or len(to_name) > graph_rules.ENTITY_NAME_MAX:
        return None, TableSkippedRow(row_number=row_number, reason="entity_name_too_long", raw_row=raw_copy)

    amount_cents: int | None = None
    is_negative = False
    inline_currency: str | None = None
    if payload.amount_col is not None:
        amount_raw = _cell_at(raw_row, payload.amount_col)
        if amount_raw:
            inline_currency = _detect_inline_currency(amount_raw)
            try:
                amount_cents, is_negative = _parse_amount_cents(amount_raw)
            except ValueError:
                return None, TableSkippedRow(row_number=row_number, reason="invalid_amount", raw_row=raw_copy)

    row_currency = default_currency
    if payload.currency_col is not None:
        currency_raw = _cell_at(raw_row, payload.currency_col)
        normalized_currency = _normalize_currency(currency_raw, inline_currency or default_currency)
        if normalized_currency is None:
            return None, TableSkippedRow(row_number=row_number, reason="invalid_currency", raw_row=raw_copy)
        row_currency = normalized_currency
    elif inline_currency is not None:
        row_currency = inline_currency

    share_pct: float | None = None
    if payload.share_pct_col is not None:
        share_raw = _cell_at(raw_row, payload.share_pct_col)
        if share_raw:
            try:
                share_pct = _parse_share_pct(share_raw)
            except ValueError:
                return None, TableSkippedRow(row_number=row_number, reason="invalid_share_pct", raw_row=raw_copy)

    if payload.direction_col is not None:
        if _norm_name(_cell_at(raw_row, payload.direction_col)) in _INBOUND_DIRECTIONS:
            from_name, to_name = to_name, from_name
    if is_negative:
        from_name, to_name = to_name, from_name

    from_entity = entity_map.get(_norm_name(from_name))
    to_entity = entity_map.get(_norm_name(to_name))
    if _norm_name(from_name) == _norm_name(to_name) or (
        from_entity is not None and to_entity is not None and from_entity.id == to_entity.id
    ):
        return None, TableSkippedRow(row_number=row_number, reason="same_source_and_target", raw_row=raw_copy)
    if (
        ruled_out_pairs is not None
        and from_entity is not None
        and to_entity is not None
        and (from_entity.id, to_entity.id) in ruled_out_pairs
    ):
        return None, TableSkippedRow(row_number=row_number, reason="existing_ruled_out", raw_row=raw_copy)

    if min_amount_cents is not None and (amount_cents is None or amount_cents < min_amount_cents):
        return None, None

    return {
        "row_number": row_number,
        "from_name": from_name,
        "to_name": to_name,
        "from_entity": from_entity,
        "to_entity": to_entity,
        "amount_cents": amount_cents,
        "currency": row_currency,
        "share_pct": share_pct,
        "date": _parse_date(_cell_at(raw_row, payload.date_col)) if payload.date_col is not None else None,
        "label": _cell_at(raw_row, payload.label_col) or None if payload.label_col is not None else None,
    }, None


def _build_compiled_edge(
    bucket: dict[str, Any],
    existing_relations: dict[tuple[UUID, UUID], tuple[UUID, str | None]],
    relation_kind: str,
    selected_sheet: str,
) -> CompiledTableEdge:
    from_eid = bucket["from_entity_id"]
    to_eid = bucket["to_entity_id"]
    existing_entry = (
        existing_relations.get((from_eid, to_eid))
        if from_eid is not None and to_eid is not None
        else None
    )
    existing_rel_id = existing_entry[0] if existing_entry is not None else None
    dates: list[date] = bucket["dates"]
    labels: list[str] = bucket["labels"]
    edge_amount = bucket["amount_cents"]
    row_numbers = bucket["row_numbers"]
    tx_count = bucket["tx_count"]
    return CompiledTableEdge(
        from_name=bucket["from_name"],
        to_name=bucket["to_name"],
        from_entity_id=from_eid,
        to_entity_id=to_eid,
        existing_relation_id=existing_rel_id,
        relation_kind=relation_kind,
        label="、".join(labels)[:graph_rules.RELATION_LABEL_MAX] if labels else None,
        amount_cents=edge_amount,
        currency=bucket["currency"] if edge_amount is not None else None,
        share_pct=bucket["share_pct"],
        date_start=min(dates).isoformat() if dates else None,
        date_end=max(dates).isoformat() if dates else None,
        tx_count=tx_count,
        row_numbers=row_numbers,
        proves=_format_proves(selected_sheet, row_numbers, tx_count),
    )


def compile_table_relations(
    session: Session,
    storage: Storage,
    case_id: UUID,
    payload: TableCompileInput,
) -> TableCompileOutput:
    """Compile spreadsheet rows into deterministic entity/relation proposals."""
    cases.lock_case_shared(session, case_id)
    if payload.graph_id is not None:
        graph._graph_or_error(session, case_id, payload.graph_id)
    from_const, to_const, currency, min_amount_cents = _validate_compile_input(payload)
    data, suffix = _material_table_bytes(session, storage, case_id, payload.material_id)
    _, selected_sheet, rows = _read_table_rows(data, suffix, payload.sheet_name)

    entity_map = _entity_lookup(session, case_id)
    existing_relations = _relation_lookup(session, case_id, payload.relation_kind)
    ruled_out_pairs = _ruled_out_pairs(session, case_id, payload.relation_kind)
    new_entity_map: dict[str, TableNewEntity] = {}
    new_entities: list[TableNewEntity] = []
    skipped_rows: list[TableSkippedRow] = []
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    processed_rows = 0

    for row_number, raw_row in enumerate(rows[payload.header_row:], start=payload.header_row + 1):
        if not any(cell != "" for cell in raw_row):
            continue
        processed_rows += 1
        parsed, skipped = _parse_row(
            raw_row, row_number, payload, from_const, to_const, currency,
            min_amount_cents, entity_map, ruled_out_pairs,
        )
        if skipped is not None:
            skipped_rows.append(skipped)
            continue
        if parsed is None:
            continue

        from_entity = parsed["from_entity"]
        to_entity = parsed["to_entity"]
        for party_name, matched in ((parsed["from_name"], from_entity), (parsed["to_name"], to_entity)):
            if matched is None:
                norm_key = _norm_name(party_name)
                if norm_key not in new_entity_map:
                    entity_spec = TableNewEntity(name=party_name, entity_kind=_infer_entity_kind(party_name))
                    new_entity_map[norm_key] = entity_spec
                    new_entities.append(entity_spec)

        canonical_from = from_entity.name if from_entity else new_entity_map[_norm_name(parsed["from_name"])].name
        canonical_to = to_entity.name if to_entity else new_entity_map[_norm_name(parsed["to_name"])].name
        from_key = ("id", str(from_entity.id)) if from_entity else ("name", _norm_name(parsed["from_name"]))
        to_key = ("id", str(to_entity.id)) if to_entity else ("name", _norm_name(parsed["to_name"]))
        currency_key = parsed["currency"] if payload.amount_col is not None else None
        if payload.aggregate_by_pair:
            bucket_key = (from_key, to_key, currency_key)
        else:
            bucket_key = (from_key, to_key, currency_key, row_number)

        bucket = buckets.setdefault(bucket_key, {
            "from_name": canonical_from,
            "to_name": canonical_to,
            "from_entity_id": from_entity.id if from_entity else None,
            "to_entity_id": to_entity.id if to_entity else None,
            "currency": parsed["currency"],
            "amount_cents": None,
            "share_pct": None,
            "dates": [],
            "labels": [],
            "row_numbers": [],
            "tx_count": 0,
        })
        if parsed["amount_cents"] is not None:
            bucket["amount_cents"] = (bucket["amount_cents"] or 0) + parsed["amount_cents"]
        if parsed["share_pct"] is not None:
            bucket["share_pct"] = parsed["share_pct"]
        if parsed["date"] is not None:
            bucket["dates"].append(parsed["date"])
        if parsed["label"] and parsed["label"] not in bucket["labels"]:
            bucket["labels"].append(parsed["label"])
        bucket["row_numbers"].append(row_number)
        bucket["tx_count"] += 1

    compiled_edges = [
        _build_compiled_edge(bucket, existing_relations, payload.relation_kind, selected_sheet)
        for bucket in buckets.values()
    ]
    return TableCompileOutput(
        case_id=case_id,
        material_id=payload.material_id,
        selected_sheet=selected_sheet,
        new_entities=new_entities,
        compiled_edges=compiled_edges,
        skipped_rows=skipped_rows,
        processed_rows=processed_rows,
    )


# --- commit service ----------------------------------------------------------


def _sub_request_key(base_key: str | None, kind: str, index: int) -> str | None:
    if not base_key:
        return None
    suffix = f"#{kind}{index}"
    if len(base_key) + len(suffix) <= 200:
        return f"{base_key}{suffix}"
    digest = hashlib.sha256(base_key.encode("utf-8")).hexdigest()[:32]
    return f"{digest}{suffix}"


def _created_id_from_workspace(ws: GraphWorkspace, target: str) -> UUID | None:
    if ws.last_change_id is None:
        return None
    change = next((item for item in ws.changes if item.id == ws.last_change_id), None)
    if change is None:
        return None
    return next(
        (entry.id for entry in change.diff if entry.target == target and entry.before is None),
        None,
    )


def _relation_on_graph(session: Session, case_id: UUID, graph_id: UUID,
                       relation_id: UUID, from_id: UUID, to_id: UUID) -> bool:
    edge_exists = session.execute(
        select(models.RelationGraphEdge.id).where(
            models.RelationGraphEdge.case_id == case_id,
            models.RelationGraphEdge.graph_id == graph_id,
            models.RelationGraphEdge.relation_id == relation_id,
        )
    ).first() is not None
    if not edge_exists:
        return False
    node_count = len(session.execute(
        select(models.RelationGraphNode.entity_id).where(
            models.RelationGraphNode.case_id == case_id,
            models.RelationGraphNode.graph_id == graph_id,
            models.RelationGraphNode.entity_id.in_([from_id, to_id]),
        )
    ).scalars().all())
    return node_count == 2


def _attach_material_source(
    session: Session,
    case_id: UUID,
    relation_id: UUID,
    material_id: UUID,
    proves: str,
    request_key: str | None,
    *,
    actor: str,
    entry: str,
) -> GraphWorkspace:
    """Attach material source to relation_id and promote suspected -> evidenced."""
    ws = graph.add_source(
        session,
        AddSourceInput(
            case_id=case_id,
            relation_id=relation_id,
            ref=SourceRefInput(material_id=material_id),
            proves=proves,
            request_key=request_key,
        ),
        actor=actor,
        entry=entry,
    )
    rel_out = next(row for row in ws.relations if row.id == relation_id)
    if rel_out.credibility == "suspected":
        ws = graph.update_relation(
            session,
            UpdateRelationInput(
                case_id=case_id,
                relation_id=relation_id,
                expected_revision=rel_out.revision,
                changes=RelationChanges(credibility="evidenced"),
            ),
            actor=actor,
            entry=entry,
        )
    return ws


def _create_missing_entities(
    session: Session,
    case_id: UUID,
    payload: TableCommitInput,
    new_entities: list[TableNewEntity],
    entity_map: dict[str, UUID],
    *,
    actor: str,
    entry: str,
) -> tuple[int, GraphWorkspace | None]:
    created = 0
    last_ws: GraphWorkspace | None = None
    for idx, spec in enumerate(new_entities):
        norm_key = _norm_name(spec.name)
        if norm_key in entity_map:
            continue
        req_key = _sub_request_key(payload.request_key, "e", idx)
        last_ws = graph.add_entity(
            session,
            AddEntityInput(
                case_id=case_id,
                kind=spec.entity_kind,
                name=spec.name,
                graph_id=payload.graph_id,
                request_key=req_key,
            ),
            actor=actor,
            entry=entry,
        )
        entity_id = _created_id_from_workspace(last_ws, "entity")
        if entity_id is not None:
            created += 1
        else:
            row = session.execute(
                select(models.CaseEntity).where(
                    models.CaseEntity.case_id == case_id,
                    models.CaseEntity.request_key == req_key,
                )
            ).scalar_one_or_none()
            entity_id = row.id if row is not None else entity_map[norm_key]
        entity_map[norm_key] = entity_id
    return created, last_ws


def _commit_edge(
    session: Session,
    case_id: UUID,
    payload: TableCommitInput,
    edge: CompiledTableEdge,
    idx: int,
    entity_map: dict[str, UUID],
    relation_map: dict[tuple[UUID, UUID], tuple[UUID, str | None]],
    raw_rows_by_number: dict[int, list[str]],
    *,
    actor: str,
    entry: str,
) -> tuple[int, int, GraphWorkspace | None, list[TableSkippedRow]]:
    from_id = edge.from_entity_id or entity_map[_norm_name(edge.from_name)]
    to_id = edge.to_entity_id or entity_map[_norm_name(edge.to_name)]
    existing_entry = relation_map.get((from_id, to_id))
    existing_rel_id = existing_entry[0] if existing_entry is not None else edge.existing_relation_id
    existing_currency = existing_entry[1] if existing_entry is not None else None
    src_key = _sub_request_key(payload.request_key, "s", idx)

    if existing_rel_id is None:
        rel_req_key = _sub_request_key(payload.request_key, "r", idx)
        attrs = RelationAttrs(
            amount_cents=edge.amount_cents,
            currency=edge.currency,
            share_pct=edge.share_pct,
            date_start=date.fromisoformat(edge.date_start) if edge.date_start else None,
            date_end=date.fromisoformat(edge.date_end) if edge.date_end else None,
        )
        ws = graph.add_relation(
            session,
            AddGraphRelationInput(
                case_id=case_id,
                from_entity_id=from_id,
                to_entity_id=to_id,
                kind=edge.relation_kind,
                label=edge.label,
                credibility="suspected",
                directed=True,
                attrs=attrs,
                graph_id=payload.graph_id,
                request_key=rel_req_key,
            ),
            actor=actor,
            entry=entry,
        )
        rel_id = _created_id_from_workspace(ws, "relation")
        created_rel = 1 if rel_id is not None else 0
        if rel_id is None:
            rel_id = session.execute(
                select(models.CaseRelation.id).where(
                    models.CaseRelation.case_id == case_id,
                    models.CaseRelation.request_key == rel_req_key,
                )
            ).scalar_one()
        relation_map[(from_id, to_id)] = (rel_id, edge.currency)
        ws = _attach_material_source(
            session, case_id, rel_id, payload.material_id, edge.proves, src_key,
            actor=actor, entry=entry,
        )
        return created_rel, 1, ws, []

    if (
        existing_currency is not None
        and edge.currency is not None
        and existing_currency != edge.currency
    ):
        conflicts = [
            TableSkippedRow(
                row_number=row_num,
                reason="currency_conflict_same_pair",
                raw_row=raw_rows_by_number.get(row_num, [edge.from_name, edge.to_name, str(edge.currency)]),
            )
            for row_num in edge.row_numbers
        ]
        return 0, 0, None, conflicts

    attached = 0
    last_ws: GraphWorkspace | None = None
    already_attached = session.execute(
        select(models.GraphSource.id).where(
            models.GraphSource.case_id == case_id,
            models.GraphSource.relation_id == existing_rel_id,
            models.GraphSource.material_id == payload.material_id,
            models.GraphSource.removed_at.is_(None),
        )
    ).first() is not None
    if not already_attached:
        last_ws = _attach_material_source(
            session, case_id, existing_rel_id, payload.material_id, edge.proves, src_key,
            actor=actor, entry=entry,
        )
        attached = 1
    else:
        rel_row = session.get(models.CaseRelation, existing_rel_id)
        if rel_row is not None and rel_row.credibility == "suspected":
            last_ws = graph.update_relation(
                session,
                UpdateRelationInput(
                    case_id=case_id,
                    relation_id=existing_rel_id,
                    expected_revision=rel_row.revision,
                    changes=RelationChanges(credibility="evidenced"),
                ),
                actor=actor,
                entry=entry,
            )
    relation_map[(from_id, to_id)] = (existing_rel_id, existing_currency or edge.currency)
    if not _relation_on_graph(session, case_id, payload.graph_id, existing_rel_id, from_id, to_id):
        last_ws = graph.add_to_graph(
            session,
            AddToGraphInput(case_id=case_id, graph_id=payload.graph_id, relation_ids=[existing_rel_id]),
            actor=actor,
            entry=entry,
        )
    return 0, attached, last_ws, []


def commit_table_relations(
    session: Session,
    storage: Storage,
    case_id: UUID,
    payload: TableCommitInput,
    *,
    actor: str = "human",
    entry: str = "local-web",
) -> TableCommitOutput:
    """Persist compiled entities, relations and material sources via graph.py commands."""
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 表格导入必须带 request_key")
    cases.lock_case_writable(session, case_id, archived_message=graph.ARCHIVED_MESSAGE)
    try:
        graph._graph_or_error(session, case_id, payload.graph_id)
        compiled = compile_table_relations(session, storage, case_id, payload)
        data, suffix = _material_table_bytes(session, storage, case_id, payload.material_id)
        _, _, raw_rows = _read_table_rows(data, suffix, payload.sheet_name)
        raw_rows_by_number = {
            row_num: list(raw_row)
            for row_num, raw_row in enumerate(raw_rows[payload.header_row:], start=payload.header_row + 1)
        }
        if actor == "human" and compiled.compiled_edges:
            pending = graph._pending_additions(session, case_id)
            for edge in compiled.compiled_edges:
                if edge.from_entity_id is not None:
                    graph._guard_pending_addition(pending, "entity", edge.from_entity_id, actor)
                if edge.to_entity_id is not None:
                    graph._guard_pending_addition(pending, "entity", edge.to_entity_id, actor)
                if edge.existing_relation_id is not None:
                    graph._guard_pending_addition(pending, "relation", edge.existing_relation_id, actor)
        entity_map = {key: row.id for key, row in _entity_lookup(session, case_id).items()}
        relation_map = _relation_lookup(session, case_id, payload.relation_kind)
    finally:
        # Release preflight read locks before invoking graph.py write commands.
        session.rollback()

    created_entities, last_ws = _create_missing_entities(
        session, case_id, payload, compiled.new_entities, entity_map, actor=actor, entry=entry,
    )
    created_relations = 0
    attached_sources = 0
    skipped_rows = list(compiled.skipped_rows)
    for idx, edge in enumerate(compiled.compiled_edges):
        rel_delta, src_delta, ws, edge_skipped = _commit_edge(
            session, case_id, payload, edge, idx, entity_map, relation_map,
            raw_rows_by_number, actor=actor, entry=entry,
        )
        created_relations += rel_delta
        attached_sources += src_delta
        skipped_rows.extend(edge_skipped)
        if ws is not None:
            last_ws = ws

    workspace = last_ws if last_ws is not None else graph.get_workspace(session, case_id)
    return TableCommitOutput(
        created_entities=created_entities,
        created_relations=created_relations,
        attached_sources=attached_sources,
        skipped_rows=skipped_rows,
        workspace=workspace,
    )
