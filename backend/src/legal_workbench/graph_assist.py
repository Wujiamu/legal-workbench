"""Deterministic read-only graph queries over the current same-case index."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from . import cases, coverage, evidence_catalog, graph, graph_rules, materials, models, search
from .errors import ErrorCode, WorkbenchError
from .schemas import Contract, Coverage, ResolvedLabelOut, SearchScope, SourceLocation
from plainfolio_parse.identifiers import (
    clean_org_parts as _clean_org_parts,
    compact_cjk as _compact_cjk,
    extract_header_org_names as _extract_header_org_names,
    extract_identifiers,
    extract_org_spans as _extract_org_spans,
    looks_like_cn_id as _looks_like_cn_id,
    norm_key as _norm_key,
    split_mid_connectors as _split_mid_connectors,
    valid_uscc as _valid_uscc,
    window_has_keyword,
)

from .textnorm import normalize_with_map, original_index

_GENERIC_TERMS = frozenset({"公司", "集团", "银行", "甲方", "乙方", "丙方", "原告", "被告", "对方", "本方"})


class CooccurrenceQuery(Contract):
    entity_a_id: UUID
    entity_b_id: UUID
    scope: SearchScope = SearchScope.ORIGINALS
    limit: int = Field(default=50, ge=1, le=100)


class SegmentHintsQuery(Contract):
    segment_id: UUID


class GraphAuditQuery(Contract):
    graph_id: UUID | None = None
    min_mentions: int = Field(default=2, ge=1)


class EntityCandidatesQuery(Contract):
    material_ids: list[UUID] | None = None


class CoverageMatrixQuery(Contract):
    graph_id: UUID | None = None


class AssistExactValue(Contract):
    kind: Literal["amount", "date"]
    raw_text: str
    canonical_value: dict[str, Any]
    amount: str | None
    currency: str | None


class AssistSegment(Contract):
    location: SourceLocation
    citation: ResolvedLabelOut
    exact_values: list[AssistExactValue]
    match_terms: list[str]


class Cooccurrence(Contract):
    kind: Literal["segment", "page", "sheet"]
    fragments: list[AssistSegment]


class CooccurrenceOutput(Contract):
    entity_a_terms: list[str]
    entity_b_terms: list[str]
    hits: list[Cooccurrence]


class MatchedEntity(Contract):
    entity_id: UUID
    name: str
    match_terms: list[str]


class SegmentHints(Contract):
    fragment: AssistSegment
    entities: list[MatchedEntity]


def _terms(values: list[str]) -> list[str]:
    normalized = [normalize_with_map(value)[0].strip() for value in values]
    return list(dict.fromkeys(term for term in normalized
                              if len(term) > 1 and term not in _GENERIC_TERMS))


def _entity_matches(text: str, terms_by_entity: dict[UUID, list[str]]) -> dict[UUID, list[str]]:
    # An exact repeated mention can support both sides only with independent
    # identifiable occurrences. A short alias inside another entity's full name
    # does not by itself identify an additional party.
    occurrences = [
        (entity_id, term, match.start(), match.end())
        for entity_id, terms in terms_by_entity.items()
        for term in terms
        for match in re.finditer(re.escape(term), text)
    ]
    # Resolve overlapping spans per character, preferring the longer name.
    # Equally long spans from different entities remain ambiguous.
    owner: dict[int, tuple[int, UUID | None]] = {}
    for entity_id, _, left, right in occurrences:
        size = right - left
        for position in range(left, right):
            prior = owner.get(position)
            if prior is None or size > prior[0]:
                owner[position] = (size, entity_id)
            elif prior[0] == size and prior[1] != entity_id:
                owner[position] = (size, None)
    accepted: dict[UUID, list[str]] = defaultdict(list)
    for entity_id, term, left, right in occurrences:
        if all(owner[position][1] == entity_id for position in range(left, right)):
            if term not in accepted[entity_id]:
                accepted[entity_id].append(term)
    return accepted


def _aliases(session: Session, case_id: UUID, entity_ids: list[UUID]) -> dict[UUID, list[str]]:
    result = defaultdict(list)
    for row in session.scalars(select(models.EntityAlias).where(
        models.EntityAlias.case_id == case_id,
        models.EntityAlias.entity_id.in_(entity_ids),
        models.EntityAlias.removed_at.is_(None),
    ).order_by(models.EntityAlias.created_at, models.EntityAlias.id)):
        result[row.entity_id].append(row.alias)
    return result


def _active_entity(session: Session, case_id: UUID, entity_id: UUID) -> models.CaseEntity:
    entity = graph._entity_or_error(session, case_id, entity_id)
    if entity.removed_at is not None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "主体已移除")
    return entity


def _segment_rows(case_id: UUID):
    return (
        select(models.Segment, models.FileVersion, models.ParseUnit.method)
        .join(models.ParseRun, and_(models.Segment.parse_run_id == models.ParseRun.id,
                                   models.Segment.case_id == models.ParseRun.case_id))
        .join(models.FileVersion, and_(models.ParseRun.file_version_id == models.FileVersion.id,
                                       models.ParseRun.case_id == models.FileVersion.case_id))
        .join(models.ParseUnit, and_(models.Segment.parse_unit_id == models.ParseUnit.id,
                                     models.Segment.case_id == models.ParseUnit.case_id))
        .where(models.Segment.case_id == case_id,
               models.FileVersion.active_parse_run_id == models.ParseRun.id)
    )


def _related_scope(segment: models.Segment) -> tuple[str, UUID, str | int] | None:
    page = segment.locator.get("page")
    if isinstance(page, int):
        return "page", segment.parse_run_id, page
    sheet = segment.locator.get("sheet")
    if isinstance(sheet, str):
        return "sheet", segment.parse_run_id, sheet
    return None


def _fragments(session: Session, case_id: UUID, rows: dict,
               matches: dict[UUID, list[str]]) -> dict[UUID, AssistSegment]:
    refs = [models.GraphSource(id=segment_id, case_id=case_id, segment_id=segment_id)
            for segment_id in rows]
    citations = evidence_catalog.resolve_labels(session, case_id, refs)
    exact = defaultdict(list)
    for value in session.scalars(select(models.ExactValue).where(
        models.ExactValue.case_id == case_id,
        models.ExactValue.segment_id.in_(rows),
        models.ExactValue.kind.in_(("amount", "date")),
    ).order_by(models.ExactValue.segment_id, models.ExactValue.id)):
        exact[value.segment_id].append(AssistExactValue(
            kind=value.kind, raw_text=value.raw_text, canonical_value=value.canonical_value,
            amount=str(value.amount_value) if value.amount_value is not None else None,
            currency=value.currency))
    return {
        segment_id: AssistSegment(
            location=SourceLocation(
                source_ref=segment_id, material_id=version.material_id, file_version_id=version.id,
                parse_run_id=segment.parse_run_id, locator=segment.locator, text=segment.text,
                method=method or "native"),
            citation=citations[str(segment_id)], exact_values=exact[segment_id],
            match_terms=matches[segment_id],
        )
        for segment_id, (segment, version, method) in rows.items()
    }


def find_entity_cooccurrences(session: Session, case_id: UUID, entity_a_id: UUID,
                              entity_b_id: UUID, scope: SearchScope = SearchScope.ORIGINALS,
                              limit: int = 50) -> CooccurrenceOutput:
    if not 1 <= limit <= 100:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "请选择两个不同主体并指定有效的返回数量")
    if scope not in (SearchScope.ORIGINALS, SearchScope.WORKING):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "共现查询仅支持原件或工作副本")
    cases.lock_case_shared(session, case_id)
    a = _active_entity(session, case_id, entity_a_id)
    b = _active_entity(session, case_id, entity_b_id)
    if a.id == b.id:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "请选择两个不同主体")
    aliases = _aliases(session, case_id, [a.id, b.id])
    terms_a = _terms([a.name, *aliases[a.id]])
    terms_b = _terms([b.name, *aliases[b.id]])
    shared = set(terms_a) & set(terms_b)
    terms_a = [term for term in terms_a if term not in shared]
    terms_b = [term for term in terms_b if term not in shared]
    result = CooccurrenceOutput(entity_a_terms=terms_a, entity_b_terms=terms_b, hits=[])
    if not terms_a or not terms_b:
        return result

    active_run_ids = search._active_runs_query(case_id, scope).with_only_columns(models.ParseRun.id)
    predicates = [models.Segment.normalized_text.contains(term, autoescape=True)
                  for term in (*terms_a, *terms_b)]
    query = _segment_rows(case_id).where(
        models.Segment.parse_run_id.in_(active_run_ids), or_(*predicates),
    ).order_by(models.FileVersion.material_id, models.Segment.parse_run_id, models.Segment.id)
    groups = defaultdict(lambda: [None, None])
    exact_groups = set()
    same_segment = []
    rows = {}
    matches = {}
    entity_terms = {a.id: terms_a, b.id: terms_b}
    with session.execute(query.execution_options(yield_per=128)) as candidates:
        for row in candidates:
            segment, _version, _method = row
            found = _entity_matches(segment.normalized_text, entity_terms)
            found_a, found_b = found[a.id], found[b.id]
            location = _related_scope(segment)
            if found_a and found_b:
                same_segment.append(("segment", [segment.id]))
                if location:
                    exact_groups.add(location)
            elif location and (found_a or found_b):
                position = 0 if found_a else 1
                if groups[location][position] is not None:
                    continue
                groups[location][position] = segment.id
            else:
                continue
            rows[segment.id] = row
            matches[segment.id] = found_a + found_b
            if len(same_segment) == limit:
                break

    pairs = same_segment + [
        (location[0], [sides[0], sides[1]])
        for location, sides in groups.items()
        if location not in exact_groups and all(sides)
    ]
    pairs = pairs[:limit]
    wanted = {item for _, fragments in pairs for item in fragments}
    if wanted:
        fragments = _fragments(session, case_id, {key: rows[key] for key in wanted}, matches)
        result.hits = [Cooccurrence(kind=kind, fragments=[fragments[key] for key in keys])
                       for kind, keys in pairs]
    return result


def match_entities_in_segment(session: Session, case_id: UUID, segment_id: UUID) -> SegmentHints:
    cases.lock_case_shared(session, case_id)
    segment = session.get(models.Segment, segment_id)
    if segment is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "片段不存在")
    if segment.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "片段不属于本案")
    row = session.execute(_segment_rows(case_id).where(models.Segment.id == segment_id)).one_or_none()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "片段不在活跃索引中")
    entities = list(session.scalars(select(models.CaseEntity).where(
        models.CaseEntity.case_id == case_id,
        models.CaseEntity.removed_at.is_(None),
    ).order_by(models.CaseEntity.name, models.CaseEntity.id)))
    pending = graph._pending_additions(session, case_id)
    entities = [entry for entry in entities if ("entity", str(entry.id)) not in pending]
    aliases = _aliases(session, case_id, [entity.id for entity in entities])
    found = _entity_matches(segment.normalized_text, {
        entity.id: _terms([entity.name, *aliases[entity.id]]) for entity in entities
    })
    matched = [MatchedEntity(entity_id=entity.id, name=entity.name, match_terms=found[entity.id])
               for entity in entities if found[entity.id]]
    terms = list(dict.fromkeys(term for entity in matched for term in entity.match_terms))
    fragment = _fragments(session, case_id, {segment_id: row}, {segment_id: terms})[segment_id]
    return SegmentHints(fragment=fragment, entities=matched)


# --- Wave 1C & Wave 2A contracts --------------------------------------------

CorpusMaterialStatus = Literal[
    "referenced", "parsed_unused", "partial_or_failed", "pending_or_unsupported",
]
_CORPUS_STATUSES: tuple[CorpusMaterialStatus, ...] = (
    "referenced", "parsed_unused", "partial_or_failed", "pending_or_unsupported",
)
_REF_KIND_ORDER = ("graph", "evidence", "annotation", "claim")

IdentifierKind = Literal["cn_mobile", "uscc", "bank_account", "email"]
EntityKind = Literal["person", "org", "agency", "court", "other"]


class CorpusMaterialItem(Contract):
    material_id: UUID
    display_name: str
    category: str
    status: CorpusMaterialStatus
    file_version_id: UUID | None
    coverage: Coverage
    reference_kinds: list[str]


class CoverageCell(Contract):
    category: str
    status: CorpusMaterialStatus
    count: int
    material_ids: list[UUID]


class CorpusCoverageMatrix(Contract):
    case_id: UUID
    graph_id: UUID | None
    categories: list[str]
    totals: dict[str, int]
    cells: list[CoverageCell]
    materials: list[CorpusMaterialItem]


class OrphanSample(Contract):
    segment_id: UUID
    material_id: UUID
    material_title: str
    file_version_id: UUID
    page: int | None
    excerpt: str


class OrphanOrgCandidate(Contract):
    name: str
    mention_count: int
    material_count: int
    material_ids: list[UUID]
    sample_segments: list[OrphanSample]


class BridgeOccurrence(Contract):
    segment_id: UUID
    material_id: UUID
    material_title: str
    file_version_id: UUID
    page: int | None
    excerpt: str
    matched_entity_ids: list[UUID]
    matched_entity_names: list[str]


class IdentifierBridge(Contract):
    kind: IdentifierKind
    value: str
    material_count: int
    entity_ids: list[UUID]
    occurrences: list[BridgeOccurrence]


class GraphAuditOutput(Contract):
    case_id: UUID
    graph_id: UUID | None
    coverage: CorpusCoverageMatrix
    orphan_orgs: list[OrphanOrgCandidate]
    identifier_bridges: list[IdentifierBridge]


class EntityCandidate(Contract):
    name: str
    entity_kind: EntityKind
    suggested_roles: list[str]
    candidate_aliases: list[str]
    mention_count: int
    existing_entity_id: UUID | None
    existing_entity_name: str | None
    sources: list[OrphanSample]


class EntityCandidateOutput(Contract):
    case_id: UUID
    candidates: list[EntityCandidate]


# --- Wave 1C & Wave 2A constants and shared helpers -------------------------

_EXCERPT_RADIUS = 60
_MAX_SAMPLES = 5
_MAX_CANDIDATE_SOURCES = 10

_STRICT_ORG_SUFFIX_CORE = (
    r"银行[^,，。;；:：、()（）\s]{0,24}(?:支行|分行)"
    r"|(?:有限责任公司|股份有限公司|有限公司)[^,，。;；:：、()（）\s]{0,20}(?:分公司|营业部)"
    r"|有限责任公司|股份有限公司|有限公司"
    r"|\((?:有限合伙|普通合伙|特殊普通合伙)\)"
    r"|(?:有限合伙|普通合伙|合伙企业)(?:\((?:有限合伙|普通合伙|特殊普通合伙)\))?"
    r"|人民法院|仲裁委员会"
    r"|(?:律师事务所|会计师事务所|资产评估事务所)(?:\((?:特殊的?普通合伙|普通合伙|有限合伙)\))?"
)
_STRICT_ORG_SUFFIX_END_RE = re.compile(rf"(?:{_STRICT_ORG_SUFFIX_CORE})$")
_ORG_CHAR_CLASS = r"[\u4e00-\u9fffA-Za-z0-9·&\-]"
_ORG_PREFIX_EXPR = rf"{_ORG_CHAR_CLASS}(?:{_ORG_CHAR_CLASS}|\({_ORG_CHAR_CLASS}{{1,12}}\))*?"
_ORG_INLINE_RE = re.compile(rf"({_ORG_PREFIX_EXPR})({_STRICT_ORG_SUFFIX_CORE})")
_LEGAL_FORM_SUFFIX_RE = re.compile(
    r"(?:有限责任公司|股份有限公司|有限公司"
    r"|\((?:有限合伙|普通合伙|特殊普通合伙)\)"
    r"|(?:有限合伙|普通合伙|合伙企业)(?:\((?:有限合伙|普通合伙|特殊普通合伙)\))?)$"
)
_FIRM_SUFFIX_RE = re.compile(
    r"(?:律师事务所|会计师事务所|资产评估事务所)(?:\((?:特殊的?普通合伙|普通合伙|有限合伙)\))?$"
)

_HEADER_PARTY_ROLES_RE = re.compile(
    r"^(原告|被告|第三人|申请人|被申请人|上诉人|被上诉人|法定代表人|委托诉讼代理人)"
    r"(?:\([^()]{1,12}\))?\s*([:：\s])\s*(.+)$"
)
_HEADER_ROLE_MAP = {
    "原告": "plaintiff",
    "申请人": "plaintiff",
    "上诉人": "plaintiff",
    "被告": "defendant",
    "被申请人": "defendant",
    "被上诉人": "defendant",
    "第三人": "third_party",
    "法定代表人": "legal_rep",
    "委托诉讼代理人": "legal_rep",
}
_CONTRACT_ROLE_MAP = {
    "担保人": "guarantor",
    "保证人": "guarantor",
    "出借人": "creditor",
    "借款人": "debtor",
}
_NON_NAME_HEADER_PREFIXES = (
    "诉称", "辩称", "认为", "主张", "请求", "陈述", "答辩", "提交", "称",
    "详见", "见附表", "略", "无", "未知", "待查", "同上", "不详", "空白", "本院",
)
_CONTRACT_PARTY_DEF_RE = re.compile(
    r"(甲方|乙方|丙方|丁方|卖方|买方|出借人|借款人|保证人|担保人|发包人|承包人|转让方|受让方)"
    r"(?:[（(][^）)]{1,20}[）)])?\s*[:：\s]*([^,，。;；\n()（）]{2,60}?(?:\([^()]{1,12}\))?[^,，。;；\n()（）]{0,20}?)"
    r"[（(][^）)]{0,12}?(?:以下)?简?称为?[:：\s]*[“\"「『]?([^”\"」』）)]{2,20})[”\"」』]?[）)]"
)
_INLINE_ORG_DEF_RE = re.compile(
    rf"({_ORG_PREFIX_EXPR}(?:有限责任公司|股份有限公司|有限公司|合伙企业(?:\((?:有限合伙|普通合伙|特殊普通合伙)\))?))"
    r"[（(][^）)]{0,12}?(?:以下)?简?称为?[:：\s]*[“\"「『]?([^”\"」』）)]{2,20})[”\"」』]?[）)]"
)

_REGION_PREFIX_RE = re.compile(
    r"^(?:中国|北京|上海|天津|重庆|广东|深圳|广州|浙江|杭州|宁波|江苏|南京|苏州|无锡|"
    r"四川|成都|湖北|武汉|湖南|长沙|河南|郑州|河北|山东|济南|青岛|福建|福州|厦门|"
    r"安徽|合肥|陕西|西安|辽宁|沈阳|大连|吉林|黑龙江|云南|昆明|贵州|广西|海南|"
    r"山西|江西|内蒙古|新疆|宁夏|青海|西藏|甘肃|香港|澳门|台湾)(?:省|市|自治区|区|县)?"
)
_INDUSTRY_WORDS = (
    "科技", "贸易", "商贸", "实业", "发展", "投资", "管理", "咨询", "建设",
    "工程", "建筑", "物流", "信息", "网络", "电子", "软件", "技术", "文化",
    "传媒", "置业", "地产", "能源", "环保", "医药", "健康", "金融", "服务",
    "控股", "集团",
)
_NON_ALIAS_TERMS = _GENERIC_TERMS | frozenset({
    "丁方", "卖方", "买方", "出借人", "借款人", "保证人", "担保人", "发包人", "承包人",
    "转让方", "受让方", "第三人", "申请人", "被申请人", "上诉人", "被上诉人",
    "法定代表人", "委托诉讼代理人", "有限公司", "有限责任公司", "股份有限公司",
    "科技公司", "贸易公司", "实业公司", "投资公司", "控股公司", "集团公司",
    "律师事务所", "会计师事务所", "资产评估事务所", "上述公司", "该公司", "本公司", "贵公司",
})

def _active_original_segments(
    session: Session, case_id: UUID, material_ids: list[UUID] | None = None,
) -> list[tuple[models.Segment, models.FileVersion, models.Material]]:
    active_run_ids = search._active_runs_query(case_id, SearchScope.ORIGINALS).with_only_columns(
        models.ParseRun.id
    )
    query = (
        select(models.Segment, models.FileVersion, models.Material)
        .join(models.ParseRun, and_(models.Segment.parse_run_id == models.ParseRun.id,
                                   models.Segment.case_id == models.ParseRun.case_id))
        .join(models.FileVersion, and_(models.ParseRun.file_version_id == models.FileVersion.id,
                                       models.ParseRun.case_id == models.FileVersion.case_id))
        .join(models.Material, and_(models.FileVersion.material_id == models.Material.id,
                                    models.FileVersion.case_id == models.Material.case_id))
        .where(models.Segment.case_id == case_id,
               models.Segment.parse_run_id.in_(active_run_ids))
        .order_by(models.Material.id, models.Segment.parse_run_id, models.Segment.id)
    )
    if material_ids is not None:
        if not material_ids:
            return []
        query = query.where(models.FileVersion.material_id.in_(material_ids))
    return list(session.execute(query).all())


def _segment_page(segment: models.Segment) -> int | None:
    if not isinstance(segment.locator, dict):
        return None
    page = segment.locator.get("page")
    return page if isinstance(page, int) and page >= 1 else None


def _segment_excerpt(
    segment: models.Segment, norm_start: int = 0, norm_end: int = 0, *,
    needle: str | None = None, radius: int = _EXCERPT_RADIUS,
) -> str:
    text = segment.text or ""
    if not text:
        return ""
    runs = segment.offset_map or []
    start: int | None = None
    end: int | None = None
    if runs and norm_end > norm_start:
        start = original_index(runs, norm_start)
        end_idx = original_index(runs, max(norm_end - 1, norm_start))
        if start is not None and end_idx is not None:
            end = end_idx + 1
    if start is None or end is None:
        if needle:
            pos = text.find(needle)
            if pos >= 0:
                start, end = pos, pos + len(needle)
        if start is None or end is None:
            start = min(max(norm_start, 0), len(text))
            end = min(max(norm_end, start), len(text))
    ctx_start = max(0, start - radius)
    ctx_end = min(len(text), end + radius)
    return text[ctx_start:ctx_end]


def _make_sample(
    segment: models.Segment, version: models.FileVersion, material: models.Material,
    norm_start: int = 0, norm_end: int = 0, *, needle: str | None = None,
) -> OrphanSample:
    return OrphanSample(
        segment_id=segment.id,
        material_id=material.id,
        material_title=material.display_name,
        file_version_id=version.id,
        page=_segment_page(segment),
        excerpt=_segment_excerpt(segment, norm_start, norm_end, needle=needle),
    )


def _active_entities_and_aliases(
    session: Session, case_id: UUID,
) -> tuple[list[models.CaseEntity], dict[UUID, list[str]]]:
    entities = list(session.scalars(
        select(models.CaseEntity)
        .where(models.CaseEntity.case_id == case_id, models.CaseEntity.removed_at.is_(None))
        .order_by(models.CaseEntity.name, models.CaseEntity.id)
    ))
    if not entities:
        return [], {}
    aliases = _aliases(session, case_id, [entity.id for entity in entities])
    return entities, aliases


def _resolve_ref_material_ids(
    session: Session, case_id: UUID,
    rows: list[tuple[UUID | None, UUID | None, UUID | None]],
) -> set[UUID]:
    """Resolve (material_id, segment_id, annotation_id) tuples to material_ids."""
    direct_materials = {mid for mid, _, _ in rows if mid is not None}
    segment_ids = {sid for _, sid, _ in rows if sid is not None}
    annotation_ids = {aid for _, _, aid in rows if aid is not None}
    resolved = set(direct_materials)
    if segment_ids:
        seg_materials = session.scalars(
            select(models.FileVersion.material_id)
            .join(models.ParseRun, and_(models.ParseRun.file_version_id == models.FileVersion.id,
                                       models.ParseRun.case_id == models.FileVersion.case_id))
            .join(models.Segment, and_(models.Segment.parse_run_id == models.ParseRun.id,
                                       models.Segment.case_id == models.ParseRun.case_id))
            .where(models.Segment.case_id == case_id, models.Segment.id.in_(segment_ids))
        ).all()
        resolved.update(seg_materials)
    if annotation_ids:
        ann_materials = session.scalars(
            select(models.FileVersion.material_id)
            .join(models.Annotation, and_(models.Annotation.file_version_id == models.FileVersion.id,
                                          models.Annotation.case_id == models.FileVersion.case_id))
            .where(models.Annotation.case_id == case_id, models.Annotation.id.in_(annotation_ids))
        ).all()
        resolved.update(ann_materials)
    return resolved


def _referenced_kinds_by_material(
    session: Session, case_id: UUID, graph_id: UUID | None,
) -> dict[UUID, list[str]]:
    by_material: dict[UUID, set[str]] = defaultdict(set)

    # 1. GraphSource (optionally scoped to one graph_id)
    gs_query = (
        select(
            models.GraphSource.material_id,
            models.GraphSource.segment_id,
            models.GraphSource.annotation_id,
        )
        .outerjoin(models.CaseEntity, and_(
            models.GraphSource.entity_id == models.CaseEntity.id,
            models.GraphSource.case_id == models.CaseEntity.case_id,
        ))
        .outerjoin(models.CaseRelation, and_(
            models.GraphSource.relation_id == models.CaseRelation.id,
            models.GraphSource.case_id == models.CaseRelation.case_id,
        ))
        .where(
            models.GraphSource.case_id == case_id,
            models.GraphSource.removed_at.is_(None),
            or_(
                and_(models.GraphSource.entity_id.is_not(None), models.CaseEntity.removed_at.is_(None)),
                and_(models.GraphSource.relation_id.is_not(None), models.CaseRelation.removed_at.is_(None)),
            ),
        )
    )
    if graph_id is not None:
        graph_entity_ids = select(models.RelationGraphNode.entity_id).where(
            models.RelationGraphNode.case_id == case_id,
            models.RelationGraphNode.graph_id == graph_id,
        )
        graph_relation_ids = select(models.RelationGraphEdge.relation_id).where(
            models.RelationGraphEdge.case_id == case_id,
            models.RelationGraphEdge.graph_id == graph_id,
        )
        gs_query = gs_query.where(or_(
            models.GraphSource.entity_id.in_(graph_entity_ids),
            models.GraphSource.relation_id.in_(graph_relation_ids),
        ))
    gs_rows = session.execute(gs_query).all()
    for mid in _resolve_ref_material_ids(session, case_id, gs_rows):
        by_material[mid].add("graph")

    # 2. EvidenceItemBinding (non-removed, not rejected, on active non-rejected item)
    ev_materials = session.scalars(
        select(models.EvidenceItemBinding.material_id)
        .join(models.EvidenceItem, and_(
            models.EvidenceItemBinding.item_id == models.EvidenceItem.id,
            models.EvidenceItemBinding.case_id == models.EvidenceItem.case_id,
        ))
        .where(
            models.EvidenceItemBinding.case_id == case_id,
            models.EvidenceItemBinding.removed_at.is_(None),
            models.EvidenceItemBinding.review_state != "rejected",
            models.EvidenceItem.removed_at.is_(None),
            models.EvidenceItem.review_state != "rejected",
        )
    ).all()
    for mid in ev_materials:
        by_material[mid].add("evidence")

    # 3. Annotation -> FileVersion.material_id
    ann_materials = session.scalars(
        select(models.FileVersion.material_id)
        .join(models.Annotation, and_(
            models.Annotation.file_version_id == models.FileVersion.id,
            models.Annotation.case_id == models.FileVersion.case_id,
        ))
        .where(models.Annotation.case_id == case_id)
    ).all()
    for mid in ann_materials:
        by_material[mid].add("annotation")

    # 4. ClaimItem (non-removed on non-removed group)
    claim_rows = session.execute(
        select(
            models.ClaimItem.material_id,
            models.ClaimItem.segment_id,
            models.ClaimItem.annotation_id,
        )
        .join(models.ClaimGroup, and_(
            models.ClaimItem.group_id == models.ClaimGroup.id,
            models.ClaimItem.case_id == models.ClaimGroup.case_id,
        ))
        .where(
            models.ClaimItem.case_id == case_id,
            models.ClaimItem.removed_at.is_(None),
            models.ClaimGroup.removed_at.is_(None),
        )
    ).all()
    for mid in _resolve_ref_material_ids(session, case_id, claim_rows):
        by_material[mid].add("claim")

    return {
        mid: [kind for kind in _REF_KIND_ORDER if kind in kinds]
        for mid, kinds in by_material.items()
    }


def _material_bucket(cov: Coverage, ref_kinds: list[str]) -> CorpusMaterialStatus:
    if cov.failed > 0 or cov.partial > 0:
        return "partial_or_failed"
    if cov.complete == 0 and (cov.pending > 0 or cov.unsupported > 0):
        return "pending_or_unsupported"
    if ref_kinds:
        return "referenced"
    return "parsed_unused"


def get_corpus_coverage_matrix(
    session: Session, case_id: UUID, graph_id: UUID | None = None,
) -> CorpusCoverageMatrix:
    """Compute per-material parsing and citation coverage matrix for a case."""
    cases.lock_case_shared(session, case_id)
    if graph_id is not None:
        graph._graph_or_error(session, case_id, graph_id)

    mats = list(session.scalars(
        select(models.Material)
        .where(models.Material.case_id == case_id)
        .order_by(models.Material.category, models.Material.display_name, models.Material.id)
    ))
    versions = list(session.scalars(
        select(models.FileVersion)
        .where(models.FileVersion.case_id == case_id)
        .order_by(models.FileVersion.created_at.desc(), models.FileVersion.id.desc())
    ))
    version_by_id = {v.id: v for v in versions}
    latest_version_by_material: dict[UUID, models.FileVersion] = {}
    for v in versions:
        latest_version_by_material.setdefault(v.material_id, v)

    ref_kinds_map = _referenced_kinds_by_material(session, case_id, graph_id)

    categories: list[str] = []
    seen_categories: set[str] = set()
    totals: dict[str, int] = {status: 0 for status in _CORPUS_STATUSES}
    totals["total"] = len(mats)
    cell_materials: dict[tuple[str, CorpusMaterialStatus], list[UUID]] = defaultdict(list)
    items: list[CorpusMaterialItem] = []

    for mat in mats:
        if mat.category not in seen_categories:
            seen_categories.add(mat.category)
            categories.append(mat.category)
        version = (
            version_by_id.get(mat.original_file_version_id)
            if mat.original_file_version_id is not None
            else latest_version_by_material.get(mat.id)
        )
        cov = coverage.coverage_for_version(session, version)
        ref_kinds = ref_kinds_map.get(mat.id, [])
        status = _material_bucket(cov, ref_kinds)
        totals[status] += 1
        cell_materials[(mat.category, status)].append(mat.id)
        items.append(CorpusMaterialItem(
            material_id=mat.id,
            display_name=mat.display_name,
            category=mat.category,
            status=status,
            file_version_id=version.id if version is not None else None,
            coverage=cov,
            reference_kinds=ref_kinds,
        ))

    cells = [
        CoverageCell(
            category=cat,
            status=status,
            count=len(cell_materials[(cat, status)]),
            material_ids=cell_materials[(cat, status)],
        )
        for cat in categories
        for status in _CORPUS_STATUSES
    ]
    return CorpusCoverageMatrix(
        case_id=case_id,
        graph_id=graph_id,
        categories=categories,
        totals=totals,
        cells=cells,
        materials=items,
    )


# --- Organization extraction helpers ----------------------------------------

def find_orphan_orgs(
    session: Session, case_id: UUID, min_mentions: int = 2,
) -> list[OrphanOrgCandidate]:
    """Find strict-suffix organizations mentioned frequently in originals that are missing from graph."""
    if min_mentions < 1:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "最小提及次数必须大于等于 1")
    cases.lock_case_shared(session, case_id)

    entities, aliases = _active_entities_and_aliases(session, case_id)
    known_keys: set[str] = set()
    for entity in entities:
        known_keys.add(_norm_key(entity.name))
        for alias in aliases.get(entity.id, []):
            known_keys.add(_norm_key(alias))

    mentions_by_key: dict[str, int] = defaultdict(int)
    display_by_key: dict[str, str] = {}
    materials_by_key: dict[str, list[UUID]] = defaultdict(list)
    seen_materials_by_key: dict[str, set[UUID]] = defaultdict(set)
    samples_by_key: dict[str, list[OrphanSample]] = defaultdict(list)
    seen_sample_segs_by_key: dict[str, set[UUID]] = defaultdict(set)

    for segment, version, material in _active_original_segments(session, case_id):
        norm_text = normalize_with_map(segment.normalized_text or segment.text)[0]
        inline_hits = _extract_org_spans(norm_text)
        header_hits = _extract_header_org_names(segment.text or "")

        segment_mentions: list[tuple[str, int, int]] = list(inline_hits)
        inline_keys = {_norm_key(name) for name, _, _ in inline_hits}
        for header_org in header_hits:
            if _norm_key(header_org) not in inline_keys:
                segment_mentions.append((header_org, 0, len(header_org)))

        for name, start, end in segment_mentions:
            key = _norm_key(name)
            if key in known_keys:
                continue
            display_by_key.setdefault(key, name)
            mentions_by_key[key] += 1
            if material.id not in seen_materials_by_key[key]:
                seen_materials_by_key[key].add(material.id)
                materials_by_key[key].append(material.id)
            if segment.id not in seen_sample_segs_by_key[key] and len(samples_by_key[key]) < _MAX_SAMPLES:
                seen_sample_segs_by_key[key].add(segment.id)
                samples_by_key[key].append(
                    _make_sample(segment, version, material, start, end, needle=name)
                )

    candidates = [
        OrphanOrgCandidate(
            name=display_by_key[key],
            mention_count=count,
            material_count=len(materials_by_key[key]),
            material_ids=materials_by_key[key],
            sample_segments=samples_by_key[key],
        )
        for key, count in mentions_by_key.items()
        if count >= min_mentions
    ]
    candidates.sort(key=lambda item: (-item.mention_count, -item.material_count, item.name))
    return candidates


# --- Identifier bridges (Wave 1C) -------------------------------------------

def _extract_identifiers_from_text(norm_text: str) -> list[tuple[str, str, int, int]]:
    """兼容包装：规则在 ``plainfolio_parse.identifiers``，这里只把结果换成既有元组形状。"""
    return [(hit.kind, hit.value, hit.start, hit.end) for hit in extract_identifiers(norm_text)]


def find_identifier_bridges(session: Session, case_id: UUID) -> list[IdentifierBridge]:
    """Detect high-confidence shared identifiers bridging materials or graph entities."""
    cases.lock_case_shared(session, case_id)

    entities, aliases = _active_entities_and_aliases(session, case_id)
    entity_terms = {
        entity.id: _terms([entity.name, *aliases.get(entity.id, [])])
        for entity in entities
    }
    entity_terms = {eid: terms for eid, terms in entity_terms.items() if terms}
    entity_by_id = {entity.id: entity for entity in entities}

    seg_rows = _active_original_segments(session, case_id)
    seg_ids = [seg.id for seg, _, _ in seg_rows]
    exact_by_seg: dict[UUID, list[models.ExactValue]] = defaultdict(list)
    if seg_ids:
        for ev in session.scalars(select(models.ExactValue).where(
            models.ExactValue.case_id == case_id,
            models.ExactValue.segment_id.in_(seg_ids),
            models.ExactValue.kind == "identifier",
        )):
            exact_by_seg[ev.segment_id].append(ev)

    groups: dict[tuple[IdentifierKind, str], list[BridgeOccurrence]] = defaultdict(list)
    seen_occ_keys: set[tuple[IdentifierKind, str, UUID]] = set()

    for segment, version, material in seg_rows:
        norm_text = normalize_with_map(segment.normalized_text or segment.text)[0]
        extracted = _extract_identifiers_from_text(norm_text)

        # Also validate any persisted ExactValue(kind='identifier') rows on this segment
        # using the exact same strict validators (e.g. if raw_text came from unnormalized source).
        for ev in exact_by_seg.get(segment.id, []):
            ev_norm = normalize_with_map(ev.raw_text)[0].strip()
            if not ev_norm:
                continue
            for kind, val, _s, _e in _extract_identifiers_from_text(ev_norm):
                if kind == "bank_account":
                    # Must have bank context in the surrounding segment, not inside raw_text alone.
                    continue
                if not any(k == kind and v == val for k, v, _, _ in extracted):
                    span = ev.span or {}
                    extracted.append((kind, val, int(span.get("start", 0)), int(span.get("end", 0))))

        if not extracted:
            continue

        matched_map = _entity_matches(norm_text, entity_terms) if entity_terms else {}
        matched_ids = [entity.id for entity in entities if matched_map.get(entity.id)]
        matched_names = [entity_by_id[eid].name for eid in matched_ids]

        for kind, val, start, end in extracted:
            occ_key = (kind, val, segment.id)
            if occ_key in seen_occ_keys:
                continue
            seen_occ_keys.add(occ_key)
            groups[(kind, val)].append(BridgeOccurrence(
                segment_id=segment.id,
                material_id=material.id,
                material_title=material.display_name,
                file_version_id=version.id,
                page=_segment_page(segment),
                excerpt=_segment_excerpt(segment, start, end, needle=val),
                matched_entity_ids=matched_ids,
                matched_entity_names=matched_names,
            ))

    bridges: list[IdentifierBridge] = []
    for (kind, val), occs in groups.items():
        mat_ids = {occ.material_id for occ in occs}
        ent_ids_ordered: list[UUID] = list(dict.fromkeys(
            eid for occ in occs for eid in occ.matched_entity_ids
        ))
        if len(mat_ids) < 2 and len(ent_ids_ordered) < 2:
            continue
        bridges.append(IdentifierBridge(
            kind=kind,
            value=val,
            material_count=len(mat_ids),
            entity_ids=ent_ids_ordered,
            occurrences=occs,
        ))

    bridges.sort(key=lambda b: (-b.material_count, -len(b.entity_ids), b.kind, b.value))
    return bridges


# --- Audit aggregator (Wave 1C) ---------------------------------------------

def audit_graph_gaps(
    session: Session, case_id: UUID, graph_id: UUID | None = None, min_mentions: int = 2,
) -> GraphAuditOutput:
    """Run coverage matrix, orphan organization, and identifier bridge audits together."""
    if min_mentions < 1:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "最小提及次数必须大于等于 1")
    cov = get_corpus_coverage_matrix(session, case_id, graph_id=graph_id)
    orphans = find_orphan_orgs(session, case_id, min_mentions=min_mentions)
    bridges = find_identifier_bridges(session, case_id)
    return GraphAuditOutput(
        case_id=case_id,
        graph_id=graph_id,
        coverage=cov,
        orphan_orgs=orphans,
        identifier_bridges=bridges,
    )


# --- Wave 2A: Programmatic entity & alias candidate extraction --------------

def _infer_entity_kind(name: str) -> EntityKind:
    if name.endswith("人民法院"):
        return "court"
    if _STRICT_ORG_SUFFIX_END_RE.search(name):
        return "org"
    return "person"


def _strip_party_tail_parens(text: str) -> str:
    """Clean a header party clause while preserving org-internal parentheses."""
    compact = _compact_cjk(text)
    m_inline = _ORG_INLINE_RE.match(compact)
    if m_inline:
        cleaned = _clean_org_parts(m_inline.group(1), m_inline.group(2))
        if cleaned is not None:
            return cleaned[1]
    # Natural person or non-strict-org header: drop trailing parenthetical notes.
    head = re.split(r"[()（）]", compact, maxsplit=1)[0].strip()
    return head


def _org_stem(name: str) -> str:
    stem = _LEGAL_FORM_SUFFIX_RE.sub("", name)
    if stem == name:
        stem = _FIRM_SUFFIX_RE.sub("", name)
    return stem


def _derive_short_forms(full_name: str) -> list[str]:
    """Generate plausible Chinese short org forms from a full strict-suffix org name."""
    if not _STRICT_ORG_SUFFIX_END_RE.search(full_name):
        return []
    forms: list[str] = []
    if _FIRM_SUFFIX_RE.search(full_name):
        stem = _FIRM_SUFFIX_RE.sub("", full_name)
        if len(stem) >= 2:
            forms.extend([f"{stem}所", f"{stem}律所"])
            no_reg = _REGION_PREFIX_RE.sub("", stem, count=1)
            if len(no_reg) >= 2 and no_reg != stem:
                forms.extend([f"{no_reg}所", f"{no_reg}律所"])
        return [f for f in dict.fromkeys(forms) if f != full_name and f not in _NON_ALIAS_TERMS]

    if "银行" in full_name and (full_name.endswith("支行") or full_name.endswith("分行")):
        simplified = re.sub(r"(?:股份有限公司|有限公司)", "", full_name)
        if simplified != full_name and len(simplified) >= 4:
            forms.append(simplified)
        return [f for f in dict.fromkeys(forms) if f not in _NON_ALIAS_TERMS]

    stem = _LEGAL_FORM_SUFFIX_RE.sub("", full_name)
    if not stem or stem == full_name:
        return []
    forms.append(f"{stem}公司")
    no_reg = _REGION_PREFIX_RE.sub("", stem, count=1)
    if len(no_reg) >= 2 and no_reg != stem:
        forms.append(f"{no_reg}公司")
        if len(no_reg) >= 4 and any(no_reg.endswith(ind) for ind in _INDUSTRY_WORDS):
            forms.append(no_reg)
        for ind in _INDUSTRY_WORDS:
            if no_reg.endswith(ind) and len(no_reg) - len(ind) >= 2:
                core = no_reg[: -len(ind)]
                forms.append(f"{core}公司")
                region = stem[: len(stem) - len(no_reg)]
                if region:
                    forms.append(f"{region}{core}公司")
                break
    elif len(stem) >= 4:
        if any(stem.endswith(ind) for ind in _INDUSTRY_WORDS):
            forms.append(stem)
        for ind in _INDUSTRY_WORDS:
            if stem.endswith(ind) and len(stem) - len(ind) >= 2:
                forms.append(f"{stem[: -len(ind)]}公司")
                break

    if stem.endswith("集团") and len(stem) >= 4:
        forms.append(stem)
        if len(no_reg) >= 4 and no_reg != stem:
            forms.append(no_reg)

    return [
        f for f in dict.fromkeys(forms)
        if f != full_name
        and f not in _NON_ALIAS_TERMS
        and (len(f) >= 3 if f.endswith(("公司", "集团", "所")) else len(f) >= 4)
    ]


def _is_org_cluster_match(short_org: str, long_org: str) -> bool:
    """True when a shorter strict-suffix org form plausibly abbreviates a longer org name."""
    if len(short_org) >= len(long_org):
        return False
    short_stem = _org_stem(short_org)
    long_stem = _org_stem(long_org)
    if len(short_stem) < 2 or len(long_stem) <= len(short_stem):
        return False
    if short_org in long_org:
        return True
    if long_stem.endswith(short_stem) or long_stem.startswith(short_stem):
        return True
    # Shared core after stripping region prefix from the longer name (e.g. 明远有限公司 vs 上海明远科技有限公司).
    no_reg_long = _REGION_PREFIX_RE.sub("", long_stem, count=1)
    no_reg_short = _REGION_PREFIX_RE.sub("", short_stem, count=1)
    return (
        len(no_reg_short) >= 2
        and no_reg_short not in _INDUSTRY_WORDS
        and _REGION_PREFIX_RE.match(long_stem) is not None
        and no_reg_long.startswith(no_reg_short)
    )


def _clean_alias(raw: str) -> str | None:
    cleaned = _compact_cjk(raw).strip(" \t\r\n“”\"'「」『』()（）,，。;；:：、")
    if len(cleaned) < 2 or cleaned in _NON_ALIAS_TERMS:
        return None
    return cleaned


def extract_entity_alias_candidates(
    session: Session, case_id: UUID, material_ids: list[UUID] | None = None,
) -> EntityCandidateOutput:
    """Programmatically extract entity and alias candidates from active original segments."""
    cases.lock_case_shared(session, case_id)
    if material_ids is not None:
        for mid in material_ids:
            materials._material_or_error(session, case_id, mid)

    rules = graph_rules.get_rules()
    entities, existing_aliases = _active_entities_and_aliases(session, case_id)
    existing_by_key: dict[str, models.CaseEntity] = {}
    registered_aliases_by_eid: dict[UUID, set[str]] = {}
    for entity in entities:
        existing_by_key.setdefault(_norm_key(entity.name), entity)
        reg = {_norm_key(entity.name)}
        for alias in existing_aliases.get(entity.id, []):
            ak = _norm_key(alias)
            reg.add(ak)
            existing_by_key.setdefault(ak, entity)
        registered_aliases_by_eid[entity.id] = reg

    seg_rows = _active_original_segments(session, case_id, material_ids=material_ids)
    if not seg_rows:
        return EntityCandidateOutput(case_id=case_id, candidates=[])

    # Accumulators keyed by canonical normalized key.
    cand_name: dict[str, str] = {}
    cand_kind: dict[str, EntityKind] = {}
    cand_roles: dict[str, list[str]] = defaultdict(list)
    cand_aliases: dict[str, list[str]] = defaultdict(list)
    cand_mentions: dict[str, int] = defaultdict(int)
    cand_sources: dict[str, list[OrphanSample]] = defaultdict(list)
    cand_seen_segs: dict[str, set[UUID]] = defaultdict(set)

    def _canonical_key(name: str) -> tuple[str, str]:
        nk = _norm_key(name)
        existing = existing_by_key.get(nk)
        if existing is not None:
            return _norm_key(existing.name), existing.name
        return nk, name

    def _record_mention(
        raw_name: str,
        *,
        kind: EntityKind | None = None,
        role: str | None = None,
        alias: str | None = None,
        segment: models.Segment,
        version: models.FileVersion,
        material: models.Material,
        norm_start: int = 0,
        norm_end: int = 0,
        count_mention: bool = True,
    ) -> str | None:
        cleaned_name = _compact_cjk(raw_name).strip(" \t\r\n,，。;；:：、")
        if len(cleaned_name) < 2 or cleaned_name in _NON_ALIAS_TERMS:
            return None
        key, canonical = _canonical_key(cleaned_name)
        cand_name.setdefault(key, canonical)
        inferred = kind or _infer_entity_kind(canonical)
        # Prefer org/court over person if seen anywhere as strict org/court.
        if key not in cand_kind or (cand_kind[key] == "person" and inferred != "person"):
            cand_kind[key] = inferred
        if role and graph_rules.valid_role(rules, role) and role not in cand_roles[key]:
            cand_roles[key].append(role)
        if alias:
            calias = _clean_alias(alias)
            if calias and _norm_key(calias) != key and calias not in cand_aliases[key]:
                cand_aliases[key].append(calias)
        if count_mention:
            cand_mentions[key] += 1
        if segment.id not in cand_seen_segs[key] and len(cand_sources[key]) < _MAX_CANDIDATE_SOURCES:
            cand_seen_segs[key].add(segment.id)
            cand_sources[key].append(
                _make_sample(segment, version, material, norm_start, norm_end, needle=cleaned_name)
            )
        return key

    # Pass 1: Extract header parties, contract 以下简称 definitions, and inline strict orgs.
    seg_norm_texts: list[tuple[models.Segment, models.FileVersion, models.Material, str]] = []
    seg_occupied_spans: dict[UUID, list[tuple[int, int]]] = defaultdict(list)
    for segment, version, material in seg_rows:
        norm_text = normalize_with_map(segment.normalized_text or segment.text)[0]
        seg_norm_texts.append((segment, version, material, norm_text))
        counted_spans: list[tuple[str, int, int]] = []

        # (a) Document header parties
        for line in (segment.text or "").splitlines():
            norm_line = normalize_with_map(line)[0].strip()
            hm = _HEADER_PARTY_ROLES_RE.match(norm_line)
            if not hm:
                continue
            label, sep, rest = hm.group(1), hm.group(2), hm.group(3).strip()
            if sep not in (":", "：") and any(rest.startswith(bad) for bad in _NON_NAME_HEADER_PREFIXES):
                continue
            clause = re.split(r"[,，。;；]", rest, maxsplit=1)[0].strip()
            party = _strip_party_tail_parens(clause)
            if not party or any(party.startswith(bad) for bad in _NON_NAME_HEADER_PREFIXES):
                continue
            kind = _infer_entity_kind(party)
            if kind == "person" and (len(party) > 15 or any(ch.isdigit() for ch in party)):
                continue
            role = _HEADER_ROLE_MAP[label]
            key = _record_mention(
                party, kind=kind, role=role,
                segment=segment, version=version, material=material,
                count_mention=True,
            )
            if key:
                pos = norm_text.find(party)
                if pos >= 0:
                    counted_spans.append((key, pos, pos + len(party)))
                    seg_occupied_spans[segment.id].append((pos, pos + len(party)))

        # (b) Contract party & inline 以下简称 definitions
        for cm in _CONTRACT_PARTY_DEF_RE.finditer(norm_text):
            role_label, raw_party, raw_alias = cm.group(1), cm.group(2), cm.group(3)
            party = _strip_party_tail_parens(raw_party)
            if not party or party in _NON_ALIAS_TERMS:
                continue
            c_role = _CONTRACT_ROLE_MAP.get(role_label)
            key = _record_mention(
                party, role=c_role, alias=raw_alias,
                segment=segment, version=version, material=material,
                norm_start=cm.start(2), norm_end=cm.end(2),
                count_mention=not any(
                    k == _canonical_key(party)[0] and abs(s - cm.start(2)) <= 3
                    for k, s, _e in counted_spans
                ),
            )
            seg_occupied_spans[segment.id].append((cm.start(), cm.end()))
            if key:
                counted_spans.append((key, cm.start(2), cm.end(2)))

        for om in _INLINE_ORG_DEF_RE.finditer(norm_text):
            raw_org, raw_alias = om.group(1), om.group(2)
            org_matches = list(_ORG_INLINE_RE.finditer(_compact_cjk(raw_org)))
            if not org_matches:
                continue
            cleaned_org = org_matches[-1]
            parts = _clean_org_parts(cleaned_org.group(1), cleaned_org.group(2))
            if not parts:
                continue
            org_name = parts[1]
            org_start = max(om.start(1), om.end(1) - len(org_name))
            key = _canonical_key(org_name)[0]
            already = any(k == key and s <= om.end(1) and org_start <= e for k, s, e in counted_spans)
            rec_key = _record_mention(
                org_name, kind=_infer_entity_kind(org_name), alias=raw_alias,
                segment=segment, version=version, material=material,
                norm_start=org_start, norm_end=om.end(1),
                count_mention=not already,
            )
            seg_occupied_spans[segment.id].append((org_start, om.end()))
            if rec_key and not already:
                counted_spans.append((rec_key, org_start, om.end(1)))

        # (c1) Strict inline org extraction
        for org_name, start, end in _extract_org_spans(norm_text):
            key = _canonical_key(org_name)[0]
            already = any(k == key and s < end and start < e for k, s, e in counted_spans)
            if already:
                continue
            rec_key = _record_mention(
                org_name, kind=_infer_entity_kind(org_name),
                segment=segment, version=version, material=material,
                norm_start=start, norm_end=end, count_mention=True,
            )
            if rec_key:
                counted_spans.append((rec_key, start, end))
                seg_occupied_spans[segment.id].append((start, end))

    # Pass 2a: Cluster shorter strict-suffix org candidates into longer unambiguous full orgs.
    org_keys = [k for k, kind in cand_kind.items() if kind in ("org", "court", "agency")]
    long_to_merge: dict[str, str] = {}
    for short_key in org_keys:
        if short_key in existing_by_key:
            continue
        short_name = cand_name[short_key]
        owners = [
            long_key for long_key in org_keys
            if long_key != short_key and _is_org_cluster_match(short_name, cand_name[long_key])
        ]
        if len(owners) == 1:
            long_to_merge[short_key] = owners[0]

    # Resolve transitive chains safely (max 1-2 hops, no cycles because len strictly increases).
    for short_key, target_key in list(long_to_merge.items()):
        while target_key in long_to_merge:
            target_key = long_to_merge[target_key]
        short_name = cand_name[short_key]
        cand_mentions[target_key] += cand_mentions.pop(short_key, 0)
        for role in cand_roles.pop(short_key, []):
            if role not in cand_roles[target_key]:
                cand_roles[target_key].append(role)
        if short_name not in cand_aliases[target_key]:
            cand_aliases[target_key].append(short_name)
        for al in cand_aliases.pop(short_key, []):
            if _norm_key(al) != target_key and al not in cand_aliases[target_key]:
                cand_aliases[target_key].append(al)
        for sample in cand_sources.pop(short_key, []):
            if (
                sample.segment_id not in cand_seen_segs[target_key]
                and len(cand_sources[target_key]) < _MAX_CANDIDATE_SOURCES
            ):
                cand_seen_segs[target_key].add(sample.segment_id)
                cand_sources[target_key].append(sample)
        cand_name.pop(short_key, None)
        cand_kind.pop(short_key, None)
        cand_seen_segs.pop(short_key, None)

    # Pass 2b: Detect unambiguous non-suffix short org mentions (e.g. "XX科技公司" or "XX科技") in text.
    remaining_org_keys = [k for k, kind in cand_kind.items() if kind in ("org", "court", "agency")]
    short_form_owners: dict[str, set[str]] = defaultdict(set)
    for key in remaining_org_keys:
        for sf in _derive_short_forms(cand_name[key]):
            short_form_owners[sf].add(key)
    for entity in entities:
        if entity.kind in ("org", "court", "agency"):
            ek = _norm_key(entity.name)
            for sf in _derive_short_forms(entity.name):
                short_form_owners[sf].add(ek)

    unambiguous_terms: dict[str, list[str]] = defaultdict(list)
    for sf, owners in short_form_owners.items():
        if len(owners) == 1:
            owner_key = next(iter(owners))
            if owner_key in cand_name:
                unambiguous_terms[owner_key].append(sf)

    if unambiguous_terms:
        all_terms_ordered = sorted(
            [(key, term) for key, terms in unambiguous_terms.items() for term in terms],
            key=lambda item: (-len(item[1]), item[0], item[1]),
        )
        for segment, version, material, norm_text in seg_norm_texts:
            # Exclude already-matched org spans and definition spans so aliases are not double-counted.
            occupied: list[tuple[int, int]] = [
                (m.start(), m.end()) for m in _ORG_INLINE_RE.finditer(norm_text)
            ]
            occupied.extend(seg_occupied_spans.get(segment.id, ()))
            for key, term in all_terms_ordered:
                for match in re.finditer(re.escape(term), norm_text):
                    s, e = match.start(), match.end()
                    if any(os < e and s < oe for os, oe in occupied):
                        continue
                    # Avoid partial ASCII word matches.
                    if (s > 0 and norm_text[s - 1].isascii() and norm_text[s - 1].isalnum()) or (
                        e < len(norm_text) and norm_text[e].isascii() and norm_text[e].isalnum()
                    ):
                        continue
                    occupied.append((s, e))
                    _record_mention(
                        cand_name[key],
                        alias=term,
                        segment=segment,
                        version=version,
                        material=material,
                        norm_start=s,
                        norm_end=e,
                        count_mention=True,
                    )

    # Assemble final EntityCandidateOutput, filtering out aliases already registered on existing entities.
    output_candidates: list[EntityCandidate] = []
    for key, name in cand_name.items():
        existing = existing_by_key.get(key)
        registered = registered_aliases_by_eid.get(existing.id, set()) if existing else {_norm_key(name)}
        filtered_aliases = [
            al for al in cand_aliases.get(key, [])
            if _norm_key(al) not in registered
        ]
        output_candidates.append(EntityCandidate(
            name=name,
            entity_kind=cand_kind[key],
            suggested_roles=cand_roles.get(key, []),
            candidate_aliases=filtered_aliases,
            mention_count=cand_mentions[key],
            existing_entity_id=existing.id if existing else None,
            existing_entity_name=existing.name if existing else None,
            sources=cand_sources.get(key, []),
        ))

    output_candidates.sort(key=lambda item: (-item.mention_count, item.name))
    return EntityCandidateOutput(case_id=case_id, candidates=output_candidates)
