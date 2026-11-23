"""Case-scoped chart projection, citation preflight and review bundle."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy.orm import Session

from . import cases, claims, evidence_catalog, graph, models, search
from .coverage import coverage_for_version
from .errors import ErrorCode, WorkbenchError
from .schemas import Contract, RelationAttrs, UtcDatetime

ChartType = Literal["graph", "claims"]
ChartProfile = Literal["working", "client", "court"]
ExternalProfile = Literal["client", "court"]


class ChartRequest(Contract):
    chart_type: ChartType
    target_id: UUID
    profile: ChartProfile
    snapshot_id: UUID | None = None
    excluded_suspected: list[UUID] = Field(default_factory=list, max_length=500)


class ChartAuditQuery(Contract):
    chart_type: ChartType
    target_id: UUID
    profile: ExternalProfile
    snapshot_id: UUID | None = None


class ChartWaiver(Contract):
    code: str = Field(max_length=100)
    target_id: UUID
    reason: str = Field(min_length=1, max_length=500)


class CitationRequest(ChartRequest):
    format: Literal["json", "md", "csv"] = "json"
    waivers: list[ChartWaiver] = Field(default_factory=list, max_length=500)


class ChartMeta(Contract):
    case_id: UUID
    chart_type: ChartType
    target_id: UUID
    profile: ChartProfile
    title: str
    generated_at: UtcDatetime
    snapshot_id: UUID | None = None
    snapshot_version: int | None = None
    frozen_at: UtcDatetime | None = None
    digest: str | None = None
    currency: str | None = None
    contains_suspected: bool = False


class ChartIssue(Contract):
    severity: Literal["blocker", "warning"]
    code: str
    target_id: UUID
    message: str
    can_override: bool = False


class ChartPreflight(Contract):
    chart_meta: ChartMeta
    issues: list[ChartIssue]


class ChartSource(Contract):
    source_id: UUID
    segment_id: UUID | None
    material_id: UUID | None
    annotation_id: UUID | None
    context: str | None = None
    context_before: str | None = None
    context_after: str | None = None
    locator: dict = Field(default_factory=dict)
    page: int | None = None
    evidence_labels: list[str] = Field(default_factory=list)
    material_title: str = ""
    file_name: str = ""
    file_version_id: UUID | None = None
    proves: str | None = None


class ChartAssertion(Contract):
    target_id: UUID
    display: str
    object_type: Literal["entity", "relation", "claim"]
    kind: str
    credibility: str | None = None
    from_entity_id: UUID | None = None
    to_entity_id: UUID | None = None
    directed: bool = False
    x: float | None = None
    y: float | None = None
    roles: list[str] = Field(default_factory=list)
    attrs: RelationAttrs | None = None
    amount_cents: int | None = None
    held_for_proof: bool = False
    sources: list[ChartSource] = Field(default_factory=list)


class CitationRow(Contract):
    index: int
    target_id: UUID
    assertion: str
    evidence_number: str
    evidence_name: str
    location: str
    proves: str
    file_name: str
    file_version_id: UUID | None


class ChartAuditBundle(Contract):
    chart_meta: ChartMeta
    assertions: list[ChartAssertion]
    preflight: ChartPreflight


class ChartCitationTable(Contract):
    chart_meta: ChartMeta
    assertions: list[ChartAssertion]
    rows: list[CitationRow]
    waivers: list[ChartWaiver]


def _same_case(session: Session, model: type, key: UUID | None, case_id: UUID):
    if key is None:
        return None
    row = session.get(model, key)
    if row is not None and row.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "图表或出处不属于本案")
    return row


def _source_record(case_id: UUID, ref: dict, source_id: UUID) -> models.GraphSource:
    return models.GraphSource(
        id=source_id, case_id=case_id,
        **{field: UUID(str(ref[field])) if ref.get(field) else None
           for field in ("segment_id", "material_id", "annotation_id")},
    )


def _graph_projection(session: Session, case_id: UUID, request: ChartRequest, meta: ChartMeta):
    row = graph._graph_or_error(session, case_id, request.target_id)
    pending = bool(graph._pending_scope(session, case_id, row.id))
    inputs = []
    if request.snapshot_id:
        snapshot = graph.get_snapshot(session, case_id, request.snapshot_id)
        if snapshot.graph_id != row.id:
            raise WorkbenchError(ErrorCode.CASE_MISMATCH, "关系图快照不属于所选图")
        meta.snapshot_id = snapshot.id
        meta.snapshot_version = snapshot.version_no
        meta.frozen_at = snapshot.created_at
        payload = snapshot.payload
    elif request.profile != "working" and (
        "graph", str(row.id)
    ) in graph._pending_additions(session, case_id):
        return pending, inputs
    elif request.profile == "court":
        return pending, inputs
    else:
        entries = {} if request.profile == "working" else graph._pending_entries(session, case_id)
        payload = graph._build_payload(session, case_id, row, entries)
    meta.title = payload["graph"]["title"]
    names = {UUID(node["entity_id"]): node["name"] for node in payload["nodes"]}
    for node in payload["nodes"]:
        inputs.append((ChartAssertion(target_id=node["entity_id"], display=node["name"],
                                      object_type="entity", kind=node["kind"],
                                      x=node["x"], y=node["y"], roles=node["roles"]),
                       node["sources"]))
    kinds = {rule["value"]: rule["label"] for rule in graph.graph_rules.get_rules()["relation_kinds"]}
    excluded = set(request.excluded_suspected)
    allowed = {UUID(edge["relation_id"]) for edge in payload["edges"]
               if edge["credibility"] == "suspected" and request.profile == "court"}
    if excluded - allowed:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "仅能排除本图当庭范围的待查关系")
    for edge in payload["edges"]:
        if request.profile != "working" and edge["credibility"] == "ruled_out":
            continue
        edge_id = UUID(edge["relation_id"])
        if edge_id in excluded:
            continue
        left, right = UUID(edge["from_entity_id"]), UUID(edge["to_entity_id"])
        kind = kinds.get(edge["kind"], edge["kind"])
        caption = f"{names[left]} {'→' if edge['directed'] else '—'} {names[right]} · {kind}"
        if edge["label"]:
            caption += f" · {edge['label']}"
        inputs.append((ChartAssertion(
            target_id=edge_id, display=caption, object_type="relation", kind=edge["kind"],
            credibility=edge["credibility"], from_entity_id=left, to_entity_id=right,
            directed=edge["directed"], attrs=RelationAttrs.model_validate(edge["attrs"] or {}),
        ), edge["sources"]))
    return pending, inputs


def _claims_projection(session: Session, case_id: UUID, request: ChartRequest, meta: ChartMeta):
    if request.snapshot_id or request.excluded_suspected:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "诉请金额采用当前分组的冻结摘要")
    group = _same_case(session, models.ClaimGroup, request.target_id, case_id)
    if group is None or group.removed_at is not None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "诉请分组不存在")
    state = claims.get_claims(session, case_id)
    meta.title = group.title
    meta.currency = group.currency
    meta.frozen_at = meta.generated_at
    # Note: claims.py stages agent changes strictly in ClaimChange without mutating
    # ClaimGroup/ClaimItem rows until human approval, so state.items holds confirmed rows only.
    pending = any(
        diff.id == group.id
        or str((diff.after or {}).get("group_id")) == str(group.id)
        or str((diff.before or {}).get("group_id")) == str(group.id)
        for change in state.pending_changes for diff in change.diff
    )
    inputs = []
    for item in sorted(state.items, key=lambda item: (item.position, item.id)):
        if item.group_id != group.id or item.removed_at is not None:
            continue
        sign = "-" if item.amount_cents < 0 else ""
        abs_cents = abs(item.amount_cents)
        display = f"{item.title} · {group.currency} {sign}{abs_cents // 100}.{abs_cents % 100:02d}"
        if item.category == "deduction":
            display += "（抵扣）"
        ref = {key: getattr(item, key) for key in ("segment_id", "material_id", "annotation_id")}
        inputs.append((ChartAssertion(target_id=item.id, display=display, object_type="claim",
                                      kind=item.category, amount_cents=item.amount_cents),
                       [dict(source_id=item.id, ref=ref)] if any(ref.values()) else []))
    return pending, inputs


_ADAPTERS = {"graph": _graph_projection, "claims": _claims_projection}


def _resolve_bundle(session: Session, case_id: UUID, request: ChartRequest,
                    *, include_context: bool) -> ChartAuditBundle:
    cases.lock_case_shared(session, case_id)
    external = request.profile != "working"
    court = request.profile == "court"
    meta = ChartMeta(case_id=case_id, chart_type=request.chart_type,
                     target_id=request.target_id, profile=request.profile,
                     title="", generated_at=datetime.now(timezone.utc))
    issues: list[ChartIssue] = []

    def issue(code: str, target: UUID, message: str, *, blocker=False, can_override=False):
        existing = next((i for i in issues if i.code == code and i.target_id == target), None)
        if existing is not None:
            if blocker:
                existing.severity = "blocker"
            if message not in existing.message:
                existing.message += "；" + message
            return
        issues.append(ChartIssue(code=code, target_id=target, message=message,
                                 severity="blocker" if blocker else "warning",
                                 can_override=can_override))

    pending, inputs = _ADAPTERS[request.chart_type](session, case_id, request, meta)
    if court and request.chart_type == "graph" and request.snapshot_id is None:
        issue("missing_snapshot", request.target_id, "当庭版须选用已冻结的关系图快照", blocker=True)
    if pending and external:
        issue("pending_changes_present", request.target_id, "存在待确认变更", blocker=court)

    source_refs = {}
    for _, specs in inputs:
        for spec in specs:
            source_id = UUID(str(spec["source_id"]))
            ref = _source_record(case_id, spec["ref"], source_id)
            for model, key in ((models.Segment, ref.segment_id), (models.Material, ref.material_id),
                               (models.Annotation, ref.annotation_id)):
                _same_case(session, model, key, case_id)
            source_refs[source_id] = ref

    labels = evidence_catalog.resolve_labels(session, case_id, list(source_refs.values()))
    coverage_by_version = {}
    contents = {}
    for assertion, specs in inputs:
        if assertion.credibility == "suspected":
            meta.contains_suspected = True
            if court:
                issue("suspected_in_court", assertion.target_id,
                      "待查关系需要排除或明确标为待举证主张", blocker=True, can_override=True)
        if external and assertion.object_type != "entity" and not specs:
            issue("missing_source", assertion.target_id, "展示项缺少出处", blocker=court, can_override=court)
        for spec in specs:
            source_id = UUID(str(spec["source_id"]))
            ref = source_refs[source_id]
            label = labels[str(source_id)]
            source_link = _same_case(session, models.GraphSource, source_id, case_id) \
                if request.chart_type == "graph" else None
            removed = source_link is not None and source_link.removed_at is not None
            if label.material_id is None or removed:
                if external:
                    issue("source_integrity_warning", assertion.target_id,
                          "引用的出处已失效或被移除", blocker=court)
                continue
            version = _same_case(session, models.FileVersion, label.file_version_id, case_id)
            if external and not label.labels:
                issue("uncataloged_source", assertion.target_id, "引用材料未编入已确认目录",
                      blocker=court, can_override=court)
            if external and version is not None:
                if version.id not in coverage_by_version:
                    coverage_by_version[version.id] = coverage_for_version(session, version)
                coverage = coverage_by_version[version.id]
                if coverage.failed or coverage.partial or coverage.pending or coverage.unsupported:
                    issue("source_integrity_warning", assertion.target_id, "出处解析存在缺页或告警")
            if external and request.snapshot_id and spec.get("label") != label.text:
                issue("source_integrity_warning", assertion.target_id, "现行证据编号与冻结时不同")

            data = ChartSource(
                source_id=source_id, segment_id=ref.segment_id, material_id=label.material_id,
                annotation_id=ref.annotation_id, page=label.page, evidence_labels=label.labels,
                material_title=label.material_title, file_version_id=label.file_version_id,
                proves=spec.get("proves"),
            )
            material = _same_case(session, models.Material, label.material_id, case_id)
            intake_item = _same_case(session, models.IntakeItem, material.intake_item_id, case_id) \
                if material is not None else None
            if intake_item is not None:
                data.file_name = PurePosixPath(intake_item.relative_source.replace(chr(92), "/")).name
            if ref.segment_id:
                if ref.segment_id not in contents:
                    if include_context:
                        contents[ref.segment_id] = search.read_source(
                            session, case_id=case_id, source_ref=ref.segment_id)
                    else:
                        contents[ref.segment_id] = _same_case(session, models.Segment, ref.segment_id, case_id)
                origin = contents[ref.segment_id]
                data.locator = {
                    key: origin.locator[key]
                    for key in (
                        "page", "sheet", "row", "paragraph", "line_start", "line_end",
                        "line", "block", "frame", "table_path", "cell", "type",
                    )
                    if key in origin.locator
                }
                if include_context:
                    data.context = origin.text
                    data.context_before = origin.context_before
                    data.context_after = origin.context_after
            assertion.sources.append(data)

    assertions = [assertion for assertion, _ in inputs]
    if request.chart_type == "claims":
        frozen = [
            item.model_dump(mode="json", exclude={
                "sources": {"__all__": {"context", "context_before", "context_after"}},
            }) for item in assertions
        ]
        packed = json.dumps([str(case_id), str(meta.target_id), meta.title, meta.currency, frozen],
                            sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        meta.digest = hashlib.sha256(packed.encode("utf-8")).hexdigest()
    preflight = ChartPreflight(chart_meta=meta, issues=issues)
    return ChartAuditBundle(chart_meta=meta, assertions=assertions, preflight=preflight)


def preflight_chart_export(session: Session, case_id: UUID, request: ChartRequest) -> ChartPreflight:
    return _resolve_bundle(session, case_id, request, include_context=False).preflight


def build_chart_audit_bundle(session: Session, case_id: UUID,
                             request: ChartRequest) -> ChartAuditBundle:
    if request.profile not in ("client", "court") or request.excluded_suspected:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "审计包仅接受客户沟通态或当庭呈递态")
    return _resolve_bundle(session, case_id, request, include_context=True)


def chart_citation_table(session: Session, case_id: UUID, request: CitationRequest,
                         *, human: bool = False) -> ChartCitationTable:
    if request.waivers and not human:
        raise WorkbenchError(ErrorCode.FORBIDDEN, "仅人工入口可逐项登记例外")
    bundle = _resolve_bundle(session, case_id, request, include_context=False)
    blockers = {(i.code, i.target_id): i for i in bundle.preflight.issues if i.severity == "blocker"}
    waived = {}
    for waiver in request.waivers:
        key = (waiver.code, waiver.target_id)
        item = blockers.get(key)
        if item is None or not item.can_override or not waiver.reason.strip() or key in waived:
            raise WorkbenchError(ErrorCode.INVALID_INPUT, "例外必须对应一项可豁免的阻断及其说明")
        waived[key] = waiver
    if set(blockers) - set(waived):
        raise WorkbenchError(ErrorCode.CONFLICT, "图表引用预检未通过",
                             {"preflight": bundle.preflight.model_dump(mode="json")})
    groups, items, _ = evidence_catalog.confirmed_catalog_rows(session, case_id)
    numbers, _ = evidence_catalog.compute_numbers(groups, items)
    names = {numbers[str(item.id)]: item.title for item in items if str(item.id) in numbers}
    rows: list[CitationRow] = []
    for assertion in bundle.assertions:
        assertion.held_for_proof = ("suspected_in_court", assertion.target_id) in waived
        if assertion.held_for_proof:
            assertion.display += "（待举证主张）"
        for source in assertion.sources:
            location = _format_source_location(source.page, source.locator)
            for evidence_label in source.evidence_labels or ["未编号"]:
                number = evidence_label.split(" 第", 1)[0]
                rows.append(CitationRow(
                    index=len(rows) + 1, target_id=assertion.target_id, assertion=assertion.display,
                    evidence_number=number, evidence_name=names.get(number, source.material_title),
                    location=location, proves=source.proves or "", file_name=source.file_name,
                    file_version_id=source.file_version_id,
                ))
    return ChartCitationTable(chart_meta=bundle.chart_meta, assertions=bundle.assertions,
                              rows=rows, waivers=list(waived.values()))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _format_source_location(page: int | None, locator: dict[str, Any]) -> str:
    """Render deterministic human-readable source location across all supported formats."""
    if page:
        return f"第{page}页"
    if locator.get("sheet"):
        loc = str(locator["sheet"])
        if locator.get("row"):
            loc += f" 第{locator['row']}行"
        return loc
    paragraph = _as_int(locator.get("paragraph"))
    if paragraph is not None:
        return f"第{paragraph + 1}段"
    if locator.get("table_path") is not None and locator.get("cell"):
        return f"表格 {locator['cell']}"
    line_start = _as_int(locator.get("line_start"))
    if line_start is not None:
        line_end = _as_int(locator.get("line_end"))
        end = line_end if line_end is not None else line_start
        return f"第{line_start}行" if line_start == end else f"第{line_start}-{end}行"
    line = _as_int(locator.get("line"))
    if line is not None:
        return f"第{line}行"
    frame = _as_int(locator.get("frame"))
    if frame is not None:
        return f"第{frame + 1}帧"
    block = _as_int(locator.get("block"))
    if block is not None:
        return f"第{block + 1}段"
    return ""


_HEADERS = ("角标序号", "图中对象／断言", "证据编号", "证据名称", "精确定位", "证明要点", "原件文件名", "文件版本")


def _csv_safe(value: str) -> str:
    """Prefix spreadsheet formula triggers and leading control chars with an apostrophe."""
    if value.startswith(("\t", "\r")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_citation_table(table: ChartCitationTable, format: Literal["md", "csv"]) -> str:
    data = [
        [str(row.index), row.assertion, row.evidence_number, row.evidence_name, row.location,
         row.proves, row.file_name, str(row.file_version_id or "")]
        for row in table.rows
    ]
    notes = [f"{waiver.code} · {waiver.target_id} · {waiver.reason}" for waiver in table.waivers]
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_HEADERS)
        for row in data:
            writer.writerow([_csv_safe(item) for item in row])
        for note in notes:
            writer.writerow([_csv_safe("人工例外"), _csv_safe(note)])
        return buffer.getvalue()
    def markdown_cell(value: str) -> str:
        escaped = html.escape(value, quote=False).replace(chr(92), chr(92) * 2)
        for character in "|`[]":
            escaped = escaped.replace(character, chr(92) + character)
        return " ".join(escaped.splitlines())

    escaped = ([markdown_cell(part) for part in row] for row in data)
    lines = ["# 图示引用索引附表", "", "| " + " | ".join(_HEADERS) + " |",
             "| " + " | ".join("---" for _ in _HEADERS) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in escaped)
    if notes:
        lines.extend(["", "## 人工例外", "", *("- " + markdown_cell(note) for note in notes)])
    return "\n".join(lines) + "\n"
