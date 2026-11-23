"""Read-only consistency checks over database references and managed files.

Nothing here repairs by guessing: a repair only completes a publish that already
has an explicit database record (``published`` intake item, staged object, or a
stored object whose file is missing while an identical staged copy exists).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ErrorCode, WorkbenchError
from .schemas import CLAIM_MAX_CENTS
from .storage import Storage, hash_file

# 与 claims 服务同口径：只有可读终态的运行/单元才能作为已确认金额的出处。
READABLE_RUN_STATUSES = ("complete", "partial")
TEXT_UNIT_STATUSES = ("extracted", "ocr_done")


@dataclass
class Issue:
    kind: str
    detail: str
    case_id: str | None = None
    object_id: str | None = None
    relative_path: str | None = None


@dataclass
class ConsistencyReport:
    checked_objects: int = 0
    checked_versions: int = 0
    checked_analyses: int = 0
    checked_progress_nodes: int = 0
    checked_graph_entities: int = 0
    checked_graph_sources: int = 0
    checked_snapshots: int = 0
    checked_evidence_items: int = 0
    checked_claim_groups: int = 0
    checked_claim_items: int = 0
    issues: list[Issue] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    staging_entries: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and not self.orphan_files

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": {"stored_objects": self.checked_objects,
                        "file_versions": self.checked_versions,
                        "analysis_versions": self.checked_analyses,
                        "progress_nodes": self.checked_progress_nodes,
                        "graph_entities": self.checked_graph_entities,
                        "graph_sources": self.checked_graph_sources,
                        "graph_snapshots": self.checked_snapshots,
                        "evidence_items": self.checked_evidence_items,
                        "claim_groups": self.checked_claim_groups,
                        "claim_items": self.checked_claim_items},
            "issues": [issue.__dict__ for issue in self.issues],
            "orphan_files": self.orphan_files,
            "staging_entries": self.staging_entries,
        }


def _check_parse_coverage(session: Session, report: ConsistencyReport,
                          version: models.FileVersion | None,
                          run: models.ParseRun) -> None:
    """Compare a run's stored coverage summary against the units actually recorded.

    Coverage is what the UI and index-status report, so a stale summary would
    silently misstate how much of a file is searchable.  Only terminal runs are
    compared: a queued/running run is expected to be out of date mid-flight.
    """
    if run.status in ("queued", "running"):
        return
    units = session.execute(
        select(models.ParseUnit).where(models.ParseUnit.parse_run_id == run.id)
    ).scalars().all()
    expected = {"complete": 0, "partial": 0, "failed": 0, "pending": 0, "unsupported": 0}
    for unit in units:
        bucket = {"extracted": "complete", "ocr_done": "complete",
                  "no_text_detected": "complete", "failed": "failed",
                  "unsupported": "unsupported"}.get(unit.status, "pending")
        expected[bucket] += 1
        if unit.warnings:
            expected["partial"] += 1
    stored = run.coverage or {}
    if not stored.get("total") and not units:
        # A run can legitimately end with no units (unsupported container); the
        # stored summary then carries the run-level outcome instead.
        return
    mismatched = {key: (int(stored.get(key, 0)), expected[key])
                  for key in expected if int(stored.get(key, 0)) != expected[key]}
    if int(stored.get("total", 0)) != len(units):
        mismatched["total"] = (int(stored.get("total", 0)), len(units))
    if mismatched:
        report.issues.append(Issue(
            "coverage_mismatch",
            f"解析覆盖与实际单元不符（登记/实际）：{mismatched}",
            str(run.case_id), str(run.id)))


def check_consistency(session: Session, storage: Storage, *,
                      case_id: uuid.UUID | None = None) -> ConsistencyReport:
    """Compare every database reference against the managed files on disk."""
    report = ConsistencyReport()
    object_query = select(models.StoredObject)
    version_query = select(models.FileVersion)
    analysis_query = select(models.AnalysisVersion)
    if case_id is not None:
        object_query = object_query.where(models.StoredObject.case_id == case_id)
        version_query = version_query.where(models.FileVersion.case_id == case_id)
        analysis_query = analysis_query.where(models.AnalysisVersion.case_id == case_id)

    referenced: set[str] = set()
    run_query = select(models.ParseRun)
    if case_id is not None:
        run_query = run_query.where(models.ParseRun.case_id == case_id)
    for stored in session.execute(object_query).scalars().all():
        report.checked_objects += 1
        referenced.add(stored.relative_path)
        path = storage.managed_path(stored.relative_path)
        if not path.is_file():
            report.issues.append(Issue("missing_object_file", "登记的存储对象文件不存在",
                                       str(stored.case_id), str(stored.id),
                                       stored.relative_path))
            continue
        try:
            digest, size = hash_file(path)
        except OSError as exc:
            report.issues.append(Issue("unreadable_object_file", f"无法读取: {exc.strerror}",
                                       str(stored.case_id), str(stored.id),
                                       stored.relative_path))
            continue
        if digest != stored.sha256 or size != stored.size:
            report.issues.append(Issue(
                "object_hash_mismatch", f"哈希或大小不符（登记 {stored.size} 字节）",
                str(stored.case_id), str(stored.id), stored.relative_path))

    for version in session.execute(version_query).scalars().all():
        report.checked_versions += 1
        stored = session.get(models.StoredObject, version.stored_object_id)
        if stored is None:
            report.issues.append(Issue("dangling_stored_object", "文件版本引用了不存在的存储对象",
                                       str(version.case_id), str(version.id)))
        elif stored.case_id != version.case_id:
            report.issues.append(Issue("cross_case_object", "文件版本与存储对象跨案",
                                       str(version.case_id), str(version.id)))
        if version.active_parse_run_id is not None:
            run = session.get(models.ParseRun, version.active_parse_run_id)
            if run is None:
                report.issues.append(Issue("dangling_parse_run", "活动解析版本不存在",
                                           str(version.case_id), str(version.id)))
            elif run.file_version_id != version.id:
                report.issues.append(Issue("parse_run_version_mismatch",
                                           "活动解析版本属于其他文件版本",
                                           str(version.case_id), str(version.id)))
            else:
                _check_parse_coverage(session, report, version, run)

    for run in session.execute(run_query).scalars().all():
        # Runs that never became active are still checked: a stale coverage
        # summary on any run is a reporting bug waiting to surface.
        if run.id == getattr(
                session.get(models.FileVersion, run.file_version_id),
                "active_parse_run_id", None):
            continue
        _check_parse_coverage(session, report, None, run)

    for version in session.execute(analysis_query).scalars().all():
        report.checked_analyses += 1
        referenced.add(version.relative_path)
        path = storage.managed_path(version.relative_path)
        if not path.is_file():
            report.issues.append(Issue("missing_analysis_file", "分析正文文件不存在",
                                       str(version.case_id), str(version.id),
                                       version.relative_path))
            continue
        try:
            digest, _size = hash_file(path)
        except OSError as exc:
            report.issues.append(Issue("unreadable_analysis_file", f"无法读取: {exc.strerror}",
                                       str(version.case_id), str(version.id),
                                       version.relative_path))
            continue
        if digest != version.sha256:
            report.issues.append(Issue("analysis_hash_mismatch", "分析正文哈希不符",
                                       str(version.case_id), str(version.id),
                                       version.relative_path))

    # Working copies are mutable and unhashed by design, but they are still
    # referenced files: without this they would all be reported as orphans.
    working_query = select(models.WorkingCopy)
    if case_id is not None:
        working_query = working_query.where(models.WorkingCopy.case_id == case_id)
    for working in session.execute(working_query).scalars().all():
        referenced.add(working.relative_path)
        if not storage.managed_path(working.relative_path).is_file():
            report.issues.append(Issue("missing_working_copy_file", "工作副本文件不存在",
                                       str(working.case_id), str(working.id),
                                       working.relative_path))

    case_ids = ([case_id] if case_id is not None else
                [row for row in session.execute(select(models.Case.id)).scalars().all()])
    for current in case_ids:
        for path in storage.iter_case_files(current):
            relative = storage.relative(path)
            if relative not in referenced:
                report.orphan_files.append(relative)

    for entry in storage.iter_staging():
        report.staging_entries.append(entry)

    _check_progress(session, report, case_id)
    _check_graph(session, report, case_id)
    _check_claims(session, report, case_id)
    return report


def _check_progress(session: Session, report: ConsistencyReport,
                    case_id: uuid.UUID | None) -> None:
    """审查：进度一致性检查（主计划第十节）。

    只读核对有效节点的父子归属、同级位置唯一、同程序最多一个进行中阶段，
    以及有效关系/关联的端点归属。报出具体案件与节点，不修复。
    """
    node_query = select(models.ProgressNode).where(models.ProgressNode.removed_at.is_(None))
    if case_id is not None:
        node_query = node_query.where(models.ProgressNode.case_id == case_id)
    nodes = session.execute(node_query).scalars().all()
    by_id = {node.id: node for node in nodes}
    # 同级位置唯一（按「案件 + 父级」分组；不同案件的根节点 parent_id 都是 NULL，
    # 只按 parent_id 分组会把多个案件误判为同级重复/多进行中阶段）。
    by_parent: dict[tuple[uuid.UUID, uuid.UUID | None], list[models.ProgressNode]] = {}
    for node in nodes:
        report.checked_progress_nodes += 1
        if node.parent_id is not None:
            parent = by_id.get(node.parent_id)
            if parent is None:
                report.issues.append(Issue(
                    "progress_dangling_parent", "有效节点的父节点不存在或已删除",
                    str(node.case_id), str(node.id)))
            elif parent.case_id != node.case_id:
                report.issues.append(Issue(
                    "progress_cross_case_parent", "节点与父节点跨案",
                    str(node.case_id), str(node.id)))
        by_parent.setdefault((node.case_id, node.parent_id), []).append(node)
    for (sibling_case, parent_id), siblings in by_parent.items():
        positions = [node.position for node in siblings]
        if len(positions) != len(set(positions)):
            report.issues.append(Issue(
                "progress_duplicate_position",
                f"同级位置重复：{sorted(positions)}",
                str(sibling_case),
                str(parent_id) if parent_id else None))
        # 同程序最多一个进行中阶段（K3）
        active_stages = [node for node in siblings
                         if node.kind == "stage" and node.state == "in_progress"]
        if len(active_stages) > 1:
            report.issues.append(Issue(
                "progress_multiple_active_stages",
                f"同一程序有 {len(active_stages)} 个进行中阶段："
                + "、".join(node.title for node in active_stages),
                str(sibling_case),
                str(parent_id) if parent_id else None))
    # 有效关系端点须为同案有效节点
    relation_query = select(models.ProgressRelation).where(models.ProgressRelation.removed_at.is_(None))
    if case_id is not None:
        relation_query = relation_query.where(models.ProgressRelation.case_id == case_id)
    for relation in session.execute(relation_query).scalars().all():
        for endpoint in (relation.from_node_id, relation.to_node_id):
            node = by_id.get(endpoint)
            if node is None or node.case_id != relation.case_id:
                report.issues.append(Issue(
                    "progress_relation_dangling_endpoint", "有效关系端点不存在、已删除或跨案",
                    str(relation.case_id), str(relation.id)))
                break
    # 有效关联的节点须为同案有效节点
    link_query = select(models.ProgressLink)
    if case_id is not None:
        link_query = link_query.where(models.ProgressLink.case_id == case_id)
    for link in session.execute(link_query).scalars().all():
        node = by_id.get(link.node_id)
        if node is None or node.case_id != link.case_id:
            report.issues.append(Issue(
                "progress_link_dangling_node", "关联指向不存在、已删除或跨案的节点",
                str(link.case_id), str(link.id)))


def _check_graph(session: Session, report: ConsistencyReport,
                 case_id: uuid.UUID | None) -> None:
    """关系图与证据目录一致性（P2 审查项）。

    只读核对，不修复：悬空端点/出处、跨案引用、「有证据」无有效出处、
    快照 JSON 结构、成员指向已删除分组、有效条目挂在已删除分组下。
    软删除主体/关系后其关系与出处保留是设计行为（恢复主体即恢复显示），
    因此端点被软删不算问题；行根本不存在或跨案才是。
    """
    entity_query = select(models.CaseEntity)
    if case_id is not None:
        entity_query = entity_query.where(models.CaseEntity.case_id == case_id)
    entities = session.execute(entity_query).scalars().all()
    entity_by_id = {row.id: row for row in entities}
    for row in entities:
        report.checked_graph_entities += 1
        if not row.name.strip() or not row.kind:
            report.issues.append(Issue("graph_entity_incomplete", "主体缺少名称或类型",
                                       str(row.case_id), str(row.id)))

    relation_query = select(models.CaseRelation)
    if case_id is not None:
        relation_query = relation_query.where(models.CaseRelation.case_id == case_id)
    relations = session.execute(relation_query).scalars().all()
    relation_by_id = {row.id: row for row in relations}

    source_query = select(models.GraphSource)
    if case_id is not None:
        source_query = source_query.where(models.GraphSource.case_id == case_id)
    sources = session.execute(source_query).scalars().all()
    sources_by_relation: dict[uuid.UUID, int] = {}
    for source in sources:
        if source.removed_at is None and source.relation_id is not None:
            sources_by_relation[source.relation_id] = sources_by_relation.get(
                source.relation_id, 0) + 1
        if source.removed_at is not None:
            continue
        report.checked_graph_sources += 1
        if (source.entity_id is None) == (source.relation_id is None):
            report.issues.append(Issue(
                "graph_source_target_invalid", "出处未恰好挂在一个主体或一条关系上",
                str(source.case_id), str(source.id)))
        if source.entity_id is not None:
            target = entity_by_id.get(source.entity_id)
            if target is None or target.case_id != source.case_id:
                report.issues.append(Issue(
                    "graph_source_dangling_target", "出处指向不存在或跨案的主体",
                    str(source.case_id), str(source.id)))
        if source.relation_id is not None:
            target = relation_by_id.get(source.relation_id)
            if target is None or target.case_id != source.case_id:
                report.issues.append(Issue(
                    "graph_source_dangling_target", "出处指向不存在或跨案的关系",
                    str(source.case_id), str(source.id)))
        if sum(value is not None for value in
               (source.segment_id, source.material_id, source.annotation_id)) != 1:
            report.issues.append(Issue(
                "graph_source_ref_invalid", "出处引用未恰好指向片段/材料/标注之一",
                str(source.case_id), str(source.id)))
            continue
        for model, value in ((models.Segment, source.segment_id),
                             (models.Material, source.material_id),
                             (models.Annotation, source.annotation_id)):
            if value is None:
                continue
            ref = session.get(model, value)
            if ref is None:
                report.issues.append(Issue(
                    "graph_source_dangling_ref", "出处引用的片段/材料/标注不存在",
                    str(source.case_id), str(source.id)))
            elif ref.case_id != source.case_id:
                report.issues.append(Issue(
                    "graph_source_cross_case_ref", "出处引用了其他案件的材料",
                    str(source.case_id), str(source.id)))

    for relation in relations:
        if relation.removed_at is not None:
            continue
        for endpoint_id in (relation.from_entity_id, relation.to_entity_id):
            endpoint = entity_by_id.get(endpoint_id)
            if endpoint is None:
                report.issues.append(Issue(
                    "graph_relation_dangling_endpoint", "有效关系的主体不存在",
                    str(relation.case_id), str(relation.id)))
                break
            if endpoint.case_id != relation.case_id:
                report.issues.append(Issue(
                    "graph_relation_cross_case_endpoint", "关系与端点主体跨案",
                    str(relation.case_id), str(relation.id)))
                break
        # 规则 3：有证据必须有出处（软删除的出处不算）。
        if relation.credibility == "evidenced" and sources_by_relation.get(relation.id, 0) == 0:
            report.issues.append(Issue(
                "graph_evidenced_without_source", "「有证据」关系没有任何有效出处",
                str(relation.case_id), str(relation.id)))

    graph_query = select(models.RelationGraph)
    if case_id is not None:
        graph_query = graph_query.where(models.RelationGraph.case_id == case_id)
    graphs = {row.id: row for row in session.execute(graph_query).scalars().all()}

    group_query = select(models.RelationGraphGroup)
    if case_id is not None:
        group_query = group_query.where(models.RelationGraphGroup.case_id == case_id)
    groups = {row.id: row for row in session.execute(group_query).scalars().all()}

    node_query = select(models.RelationGraphNode)
    if case_id is not None:
        node_query = node_query.where(models.RelationGraphNode.case_id == case_id)
    for node in session.execute(node_query).scalars().all():
        if node.graph_id not in graphs:
            report.issues.append(Issue("graph_node_dangling_graph", "图成员指向不存在的图",
                                       str(node.case_id), str(node.id)))
        if node.entity_id not in entity_by_id:
            report.issues.append(Issue("graph_node_dangling_entity", "图成员指向不存在的主体",
                                       str(node.case_id), str(node.id)))
        if node.group_id is not None:
            group = groups.get(node.group_id)
            if group is None or group.removed_at is not None:
                report.issues.append(Issue(
                    "graph_node_dangling_group", "图成员指向不存在或已删除的分组",
                    str(node.case_id), str(node.id)))

    edge_query = select(models.RelationGraphEdge)
    if case_id is not None:
        edge_query = edge_query.where(models.RelationGraphEdge.case_id == case_id)
    for edge in session.execute(edge_query).scalars().all():
        if edge.graph_id not in graphs:
            report.issues.append(Issue("graph_edge_dangling_graph", "图边指向不存在的图",
                                       str(edge.case_id), str(edge.id)))
        if edge.relation_id not in relation_by_id:
            report.issues.append(Issue("graph_edge_dangling_relation", "图边指向不存在的关系",
                                       str(edge.case_id), str(edge.id)))

    snapshot_query = select(models.RelationGraphSnapshot)
    if case_id is not None:
        snapshot_query = snapshot_query.where(models.RelationGraphSnapshot.case_id == case_id)
    for snapshot in session.execute(snapshot_query).scalars().all():
        report.checked_snapshots += 1
        payload = snapshot.payload or {}
        missing = {"schema", "graph", "nodes", "edges", "groups"} - set(payload)
        if missing:
            report.issues.append(Issue(
                "graph_snapshot_payload_incomplete",
                f"快照缺少字段：{sorted(missing)}", str(snapshot.case_id), str(snapshot.id)))
            continue
        if not isinstance(payload.get("nodes"), list) or not isinstance(payload.get("edges"), list) \
                or not isinstance(payload.get("groups"), list):
            report.issues.append(Issue(
                "graph_snapshot_payload_incomplete", "快照节点/关系/分组不是数组",
                str(snapshot.case_id), str(snapshot.id)))
            continue
        graph = graphs.get(snapshot.graph_id)
        if graph is None:
            report.issues.append(Issue("graph_snapshot_dangling_graph", "快照指向不存在的图",
                                       str(snapshot.case_id), str(snapshot.id)))
        if str((payload.get("graph") or {}).get("id")) != str(snapshot.graph_id):
            report.issues.append(Issue(
                "graph_snapshot_graph_mismatch", "快照内容与所属图不一致",
                str(snapshot.case_id), str(snapshot.id)))
        if snapshot.version_no < 1:
            report.issues.append(Issue("graph_snapshot_version_invalid", "快照版本号非法",
                                       str(snapshot.case_id), str(snapshot.id)))

    group_rows_query = select(models.EvidenceGroup)
    item_query = select(models.EvidenceItem)
    binding_query = select(models.EvidenceItemBinding)
    if case_id is not None:
        group_rows_query = group_rows_query.where(models.EvidenceGroup.case_id == case_id)
        item_query = item_query.where(models.EvidenceItem.case_id == case_id)
        binding_query = binding_query.where(models.EvidenceItemBinding.case_id == case_id)
    evidence_groups = {row.id: row for row in session.execute(group_rows_query).scalars().all()}
    items = session.execute(item_query).scalars().all()
    item_by_id = {row.id: row for row in items}
    positions: dict[uuid.UUID, list[int]] = {}
    for item in items:
        if item.removed_at is not None:
            continue
        report.checked_evidence_items += 1
        group = evidence_groups.get(item.group_id)
        if group is None:
            report.issues.append(Issue("evidence_item_dangling_group", "证据条目指向不存在的分组",
                                       str(item.case_id), str(item.id)))
        elif group.removed_at is not None:
            report.issues.append(Issue(
                "evidence_item_in_removed_group", "有效证据条目挂在已删除的分组下",
                str(item.case_id), str(item.id)))
        elif group.case_id != item.case_id:
            report.issues.append(Issue("evidence_item_cross_case_group", "条目与分组跨案",
                                       str(item.case_id), str(item.id)))
        positions.setdefault(item.group_id, []).append(item.position)
    for group_id, values in positions.items():
        if len(values) != len(set(values)):
            report.issues.append(Issue(
                "evidence_item_duplicate_position", f"组内条目位置重复：{sorted(values)}",
                str(evidence_groups[group_id].case_id) if group_id in evidence_groups else None,
                str(group_id)))
    for binding in session.execute(binding_query).scalars().all():
        if binding.removed_at is not None:
            continue
        item = item_by_id.get(binding.item_id)
        if item is None or item.removed_at is not None:
            report.issues.append(Issue(
                "evidence_binding_dangling_item", "有效绑定指向不存在或已删除的条目",
                str(binding.case_id), str(binding.id)))
        material = session.get(models.Material, binding.material_id)
        if material is None or material.case_id != binding.case_id:
            report.issues.append(Issue(
                "evidence_binding_dangling_material", "有效绑定指向不存在或跨案的材料",
                str(binding.case_id), str(binding.id)))


def _check_claims(session: Session, report: ConsistencyReport,
                  case_id: uuid.UUID | None) -> None:
    """诉请金额一致性：分组/金额项归属、位置唯一、出处有效、金额在契约范围内、待确认目标仍存在。

    与进度检查同口径：只读核对并报出具体案件与对象，不修复。金额符号由类别决定，
    存储值必须在公开契约范围内（0–2^53-1），否则小计与瀑布图会双负号或前端丢分。
    """
    group_query = select(models.ClaimGroup).where(models.ClaimGroup.removed_at.is_(None))
    if case_id is not None:
        group_query = group_query.where(models.ClaimGroup.case_id == case_id)
    groups = session.execute(group_query).scalars().all()
    report.checked_claim_groups = len(groups)
    by_id = {group.id: group for group in groups}
    by_case: dict[uuid.UUID, list[models.ClaimGroup]] = {}
    for group in groups:
        by_case.setdefault(group.case_id, []).append(group)
    for owner, siblings in by_case.items():
        positions = [group.position for group in siblings]
        if len(positions) != len(set(positions)):
            report.issues.append(Issue(
                "claims_duplicate_group_position", f"分组位置重复：{sorted(positions)}",
                str(owner)))

    item_query = select(models.ClaimItem).where(models.ClaimItem.removed_at.is_(None))
    if case_id is not None:
        item_query = item_query.where(models.ClaimItem.case_id == case_id)
    items = session.execute(item_query).scalars().all()
    by_group: dict[uuid.UUID, list[models.ClaimItem]] = {}
    for item in items:
        report.checked_claim_items += 1
        group = by_id.get(item.group_id)
        if group is None:
            stored = session.get(models.ClaimGroup, item.group_id)
            if stored is None or stored.removed_at is not None:
                report.issues.append(Issue(
                    "claims_item_dangling_group", "有效金额项的分组不存在或已删除",
                    str(item.case_id), str(item.id)))
            else:
                report.issues.append(Issue(
                    "claims_item_cross_case_group", "金额项与分组跨案",
                    str(item.case_id), str(item.id)))
        elif group.case_id != item.case_id:
            report.issues.append(Issue(
                "claims_item_cross_case_group", "金额项与分组跨案",
                str(item.case_id), str(item.id)))
        by_group.setdefault(item.group_id, []).append(item)
        if int(item.amount_cents) < 0 or int(item.amount_cents) > CLAIM_MAX_CENTS:
            report.issues.append(Issue(
                "claims_amount_out_of_range",
                f"金额超出公开契约范围：{item.amount_cents} 分"
                f"（0–{CLAIM_MAX_CENTS}）",
                str(item.case_id), str(item.id)))
        sources = [(item.segment_id, models.Segment, "片段"),
                   (item.material_id, models.Material, "材料"),
                   (item.annotation_id, models.Annotation, "标注")]
        if sum(1 for value, _model, _label in sources if value is not None) > 1:
            report.issues.append(Issue(
                "claims_multiple_sources", "金额项出处多于一个",
                str(item.case_id), str(item.id)))
        for value, model, label in sources:
            if value is None:
                continue
            target = session.get(model, value)
            if target is None:
                report.issues.append(Issue(
                    "claims_dangling_source", f"出处{label}不存在",
                    str(item.case_id), str(item.id)))
            elif target.case_id != item.case_id:
                report.issues.append(Issue(
                    "claims_cross_case_source", f"出处{label}跨案",
                    str(item.case_id), str(item.id)))
        if item.segment_id is not None:
            # 出处必须是可读终态段：未完成运行的段会被重试删除，确认时也已拒绝，
            # 历史坏数据同样要能被检查发现。
            readable = session.execute(
                select(models.ParseRun.status, models.ParseUnit.status)
                .join(models.ParseUnit,
                      models.ParseUnit.parse_run_id == models.ParseRun.id)
                .join(models.Segment,
                      models.Segment.parse_unit_id == models.ParseUnit.id)
                .where(models.Segment.id == item.segment_id)
            ).first()
            if readable is not None and (
                    readable[0] not in READABLE_RUN_STATUSES
                    or readable[1] not in TEXT_UNIT_STATUSES):
                report.issues.append(Issue(
                    "claims_unreadable_source",
                    f"出处片段所属解析状态为 {readable[0]}/{readable[1]}，不可引用",
                    str(item.case_id), str(item.id)))
    for group_id, siblings in by_group.items():
        positions = [item.position for item in siblings]
        if len(positions) != len(set(positions)):
            report.issues.append(Issue(
                "claims_duplicate_item_position",
                f"同一分组内金额项位置重复：{sorted(positions)}",
                str(siblings[0].case_id), str(group_id)))
    # 分组币种必须与带明确币种的出处一致：否则金额被静默改币种。
    group_by_id = {group.id: group for group in groups}
    for item in items:
        if item.segment_id is None:
            continue
        group = group_by_id.get(item.group_id)
        if group is None:
            continue
        currencies = session.execute(
            select(models.ExactValue.currency)
            .where(models.ExactValue.segment_id == item.segment_id,
                   models.ExactValue.kind == "amount",
                   models.ExactValue.currency.is_not(None))
        ).scalars().all()
        if currencies and group.currency not in {value.upper() for value in currencies}:
            report.issues.append(Issue(
                "claims_currency_mismatch",
                f"分组币种 {group.currency} 与出处币种 {sorted(set(currencies))} 不一致",
                str(item.case_id), str(item.id)))

    # 待确认候选指向的对象必须仍然有效，否则人工确认时只能得到冲突。
    change_query = select(models.ClaimChange).where(models.ClaimChange.review_state == "pending")
    if case_id is not None:
        change_query = change_query.where(models.ClaimChange.case_id == case_id)
    for change in session.execute(change_query).scalars().all():
        for entry in change.diff:
            if entry["before"] is None:
                continue  # 新增候选的目标尚未写入，属正常
            if entry["target"] == "group":
                target = session.get(models.ClaimGroup, uuid.UUID(entry["id"]))
            else:
                target = session.get(models.ClaimItem, uuid.UUID(entry["id"]))
            if target is None or target.removed_at is not None:
                report.issues.append(Issue(
                    "claims_stale_pending_change", "待确认候选的目标已删除",
                    str(change.case_id), str(change.id)))


def repair_consistency(session: Session, storage: Storage, *,
                       case_id: uuid.UUID | None = None) -> dict:
    """Finish publishes that already have a database record; never guess ownership.

    Two safe repairs only:
      * a ``published`` intake item whose stored object row exists -> re-associate
        the material (the normal import path completes it);
      * a missing stored object file that is byte-identical to a staged file with
        the recorded hash -> re-publish that staged file to the recorded path.
    """
    repaired: list[dict] = []
    skipped: list[dict] = []

    item_query = select(models.IntakeItem).where(models.IntakeItem.status == "published")
    if case_id is not None:
        item_query = item_query.where(models.IntakeItem.case_id == case_id)
    for item in session.execute(item_query).scalars().all():
        if item.material_id is not None or item.stored_object_id is None:
            skipped.append({"intake_item_id": str(item.id),
                            "reason": "已有材料或缺少存储对象，交人工判断"})
            continue
        stored = session.get(models.StoredObject, item.stored_object_id)
        if stored is None or stored.case_id != item.case_id:
            skipped.append({"intake_item_id": str(item.id), "reason": "存储对象缺失或跨案"})
            continue
        repaired.append({"intake_item_id": str(item.id),
                         "action": "requeue_published_item",
                         "detail": "可用同一 job 重试完成关联"})

    staged_by_hash: dict[str, list] = {}
    for entry in storage.iter_staging():
        if entry.get("status") == "complete" and entry.get("sha256"):
            staged_by_hash.setdefault(entry["sha256"], []).append(entry)

    object_query = select(models.StoredObject)
    if case_id is not None:
        object_query = object_query.where(models.StoredObject.case_id == case_id)
    for stored in session.execute(object_query).scalars().all():
        path = storage.managed_path(stored.relative_path)
        if path.is_file():
            continue
        candidates = staged_by_hash.get(stored.sha256, [])
        matching = [entry for entry in candidates if entry.get("size") == stored.size]
        if not matching:
            skipped.append({"stored_object_id": str(stored.id),
                            "reason": "没有哈希一致的暂存副本，无法安全恢复"})
            continue
        entry = matching[0]
        # iter_staging reports the sidecar's path; the bytes live beside it as .part.
        meta = storage.managed_path(entry["relative_path"])
        part = meta.with_suffix(".part")
        if not part.is_file():
            skipped.append({"stored_object_id": str(stored.id), "reason": "暂存文件已不存在"})
            continue
        target = path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            skipped.append({"stored_object_id": str(stored.id), "reason": "目标已存在，需人工核对"})
            continue
        target.write_bytes(part.read_bytes())
        digest, size = hash_file(target)
        if digest != stored.sha256 or size != stored.size:
            target.unlink(missing_ok=True)
            skipped.append({"stored_object_id": str(stored.id), "reason": "回读校验失败"})
            continue
        repaired.append({"stored_object_id": str(stored.id),
                         "action": "republish_from_staging",
                         "relative_path": stored.relative_path})

    return {"repaired": repaired, "skipped": skipped}


def assert_repair_target_exists(session: Session, case_id: uuid.UUID) -> None:
    if session.get(models.Case, case_id) is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在", {"case_id": str(case_id)})
