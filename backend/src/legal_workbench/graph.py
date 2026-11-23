"""Case relation graph service: entities, relations, sources, views, snapshots.

Structural decisions (docs/decisions/20261120-主体关系图与证据目录模型.md):

* D1 — entities, aliases, relations and sources belong to the *case*; a graph is
  a named view holding membership, per-graph layout coordinates and groups.
  "Remove from this graph" deletes membership only; deleting an entity/relation
  is a case-level soft delete and reports the affected graphs first.
* D2 — the graph never stores an evidence number; ``get_workspace`` resolves
  labels through ``evidence_catalog.resolve_labels``.
* D3 — snapshots freeze one graph as immutable JSONB (labels included), exclude
  pending agent changes, and can be restored into the working draft as a normal
  human command that is recorded and revertible.  Consents attach to a snapshot
  and are append-only.
* Agent writes become pending review; freeze/restore, consent and accept/revert
  are human-only and rejected for ``actor='agent'``.

Every write follows the same order as the progress service:
permission guard -> maintenance gate -> case row lock (FOR UPDATE, archive
re-check inside the lock) -> domain sequence lock -> rule/same-case checks ->
CAS -> change row -> seq + 1 -> audit -> commit.  Reads take the case row
FOR SHARE first so workspace data, catalog numbers and seqs come from one
committed state (never evidence-lock-then-case-lock, which would deadlock).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import audit, cases, domain_changes, evidence_catalog, graph_rules, models
from .errors import ErrorCode, WorkbenchError
from .schemas import (
    AddAliasInput, AddConsentInput, AddEntityInput, AddGraphRelationInput, AddSourceInput,
    AddToGraphInput, AliasOut, ConsentOut, CreateGraphInput, CreateSnapshotInput,
    EntityChanges, EntityOut, EntityRevisionInput, GraphAcceptAllInput, GraphChangeIdInput,
    GraphChangeOut, GraphChanges, GraphDiffEntry, GraphEdgeOut, GraphGroupOut, GraphNodeOut,
    GraphOut, GraphRevisionInput, GraphWorkspace, RelationAttrs,
    RelationChanges, RelationOut, RelationRevisionInput, RemoveAliasInput,
    RemoveFromGraphInput, RemoveGroupInput, RemoveSourceInput, RemovalImpactOut,
    RestoreSnapshotInput, SaveLayoutInput, SetNodeGroupInput, SnapshotDiffEntry,
    SnapshotDiffOut, SnapshotLabelDrift, SnapshotOut, SourceOut, UpdateEntityInput,
    UpdateGraphInput, UpdateRelationInput, UpsertGroupInput,
)

DOMAIN = "graph"

ARCHIVED_MESSAGE = "案件已归档，关系图只读；如需修改请先取消归档"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _err(code: ErrorCode, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details or None)


def _case_or_error(session: Session, case_id: uuid.UUID) -> models.Case:
    row = session.get(models.Case, case_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "案件不存在", case_id=str(case_id))
    return row


# --- serialization ----------------------------------------------------------

def _entity_dict(row: models.CaseEntity) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "kind": row.kind, "name": row.name,
        "roles": list(row.roles or []), "note": row.note, "created_by": row.created_by,
        "updated_by": row.updated_by, "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _alias_dict(row: models.EntityAlias) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "entity_id": str(row.entity_id),
        "alias": row.alias, "created_by": row.created_by,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _relation_dict(row: models.CaseRelation) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id),
        "from_entity_id": str(row.from_entity_id), "to_entity_id": str(row.to_entity_id),
        "directed": row.directed, "kind": row.kind, "label": row.label,
        "credibility": row.credibility, "attrs": dict(row.attrs or {}), "note": row.note,
        "created_by": row.created_by, "updated_by": row.updated_by,
        "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _source_dict(row: models.GraphSource) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id),
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "relation_id": str(row.relation_id) if row.relation_id else None,
        "segment_id": str(row.segment_id) if row.segment_id else None,
        "material_id": str(row.material_id) if row.material_id else None,
        "annotation_id": str(row.annotation_id) if row.annotation_id else None,
        "proves": row.proves, "created_by": row.created_by,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _graph_dict(row: models.RelationGraph) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "title": row.title,
        "description": row.description, "position": row.position,
        "created_by": row.created_by, "updated_by": row.updated_by,
        "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _group_dict(row: models.RelationGraphGroup) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "graph_id": str(row.graph_id),
        "title": row.title, "color": row.color, "x": row.x, "y": row.y,
        "width": row.width, "height": row.height, "collapsed": row.collapsed,
        "revision": row.revision,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _node_dict(row: models.RelationGraphNode) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "graph_id": str(row.graph_id),
        "entity_id": str(row.entity_id),
        "group_id": str(row.group_id) if row.group_id else None,
        "x": row.x, "y": row.y, "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _edge_dict(row: models.RelationGraphEdge) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "graph_id": str(row.graph_id),
        "relation_id": str(row.relation_id), "created_at": row.created_at.isoformat(),
    }


def _snapshot_dict(row: models.RelationGraphSnapshot) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "graph_id": str(row.graph_id),
        "version_no": row.version_no, "stage_label": row.stage_label, "note": row.note,
        "progress_node_id": str(row.progress_node_id) if row.progress_node_id else None,
        "payload": row.payload, "excluded_pending_count": row.excluded_pending_count,
        "created_by": row.created_by, "created_at": row.created_at.isoformat(),
    }


def _consent_dict(row: models.SnapshotConsent) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "snapshot_id": str(row.snapshot_id),
        "action": row.action,
        "parent_consent_id": str(row.parent_consent_id) if row.parent_consent_id else None,
        "consenter": row.consenter, "consented_at": row.consented_at.isoformat(),
        "channel": row.channel, "source_detail": row.source_detail,
        "evidence_material_id": str(row.evidence_material_id) if row.evidence_material_id else None,
        "note": row.note, "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
    }


def _entity_out(row: models.CaseEntity) -> EntityOut:
    return EntityOut(
        id=row.id, case_id=row.case_id, kind=row.kind, name=row.name,
        roles=list(row.roles or []), note=row.note, created_by=row.created_by,
        updated_by=row.updated_by, request_key=row.request_key, removed_at=row.removed_at,
        revision=row.revision, created_at=row.created_at, updated_at=row.updated_at,
    )


def _alias_out(row: models.EntityAlias) -> AliasOut:
    return AliasOut(id=row.id, case_id=row.case_id, entity_id=row.entity_id, alias=row.alias,
                    created_by=row.created_by, removed_at=row.removed_at,
                    created_at=row.created_at)


def _relation_out(row: models.CaseRelation) -> RelationOut:
    return RelationOut(
        id=row.id, case_id=row.case_id, from_entity_id=row.from_entity_id,
        to_entity_id=row.to_entity_id, directed=row.directed, kind=row.kind, label=row.label,
        credibility=row.credibility, attrs=RelationAttrs(**(row.attrs or {})),
        note=row.note, created_by=row.created_by, updated_by=row.updated_by,
        request_key=row.request_key, removed_at=row.removed_at, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _source_out(row: models.GraphSource) -> SourceOut:
    return SourceOut(
        id=row.id, case_id=row.case_id, entity_id=row.entity_id, relation_id=row.relation_id,
        segment_id=row.segment_id, material_id=row.material_id,
        annotation_id=row.annotation_id, proves=row.proves, created_by=row.created_by,
        removed_at=row.removed_at, created_at=row.created_at,
    )


def _graph_out(row: models.RelationGraph) -> GraphOut:
    return GraphOut(
        id=row.id, case_id=row.case_id, title=row.title, description=row.description,
        position=row.position, created_by=row.created_by, updated_by=row.updated_by,
        request_key=row.request_key, removed_at=row.removed_at, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _group_out(row: models.RelationGraphGroup) -> GraphGroupOut:
    return GraphGroupOut(
        id=row.id, case_id=row.case_id, graph_id=row.graph_id, title=row.title,
        color=row.color, x=row.x, y=row.y, width=row.width, height=row.height,
        collapsed=row.collapsed, revision=row.revision, removed_at=row.removed_at,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _node_out(row: models.RelationGraphNode) -> GraphNodeOut:
    return GraphNodeOut(
        id=row.id, case_id=row.case_id, graph_id=row.graph_id, entity_id=row.entity_id,
        group_id=row.group_id, x=row.x, y=row.y, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _edge_out(row: models.RelationGraphEdge) -> GraphEdgeOut:
    return GraphEdgeOut(id=row.id, case_id=row.case_id, graph_id=row.graph_id,
                        relation_id=row.relation_id, created_at=row.created_at)


def _snapshot_out(row: models.RelationGraphSnapshot) -> SnapshotOut:
    return SnapshotOut(
        id=row.id, case_id=row.case_id, graph_id=row.graph_id, version_no=row.version_no,
        stage_label=row.stage_label, note=row.note, progress_node_id=row.progress_node_id,
        payload=row.payload, excluded_pending_count=row.excluded_pending_count,
        created_by=row.created_by, created_at=row.created_at,
    )


def _consent_out(row: models.SnapshotConsent) -> ConsentOut:
    return ConsentOut(
        id=row.id, case_id=row.case_id, snapshot_id=row.snapshot_id, action=row.action,
        parent_consent_id=row.parent_consent_id, consenter=row.consenter,
        consented_at=row.consented_at, channel=row.channel, source_detail=row.source_detail,
        evidence_material_id=row.evidence_material_id, note=row.note,
        created_by=row.created_by, created_at=row.created_at,
    )


def _change_out(row: models.GraphChange) -> GraphChangeOut:
    return GraphChangeOut(
        id=row.id, case_id=row.case_id, seq=row.seq, command=row.command, actor=row.actor,
        entry=row.entry, diff=[GraphDiffEntry(**item) for item in row.diff],
        review_state=row.review_state, reviewed_at=row.reviewed_at,
        reverted_by=row.reverted_by, created_at=row.created_at,
    )


# --- reads ------------------------------------------------------------------

def _seqs(session: Session, case_id: uuid.UUID) -> dict[str, int]:
    """All four domains, zero-filled: a missing key must not read as "no change"."""
    rows = session.execute(
        select(models.CaseChangeSeq).where(models.CaseChangeSeq.case_id == case_id)
    ).scalars().all()
    seqs = {domain: 0 for domain in domain_changes.DOMAINS}
    seqs.update({row.domain: row.seq for row in rows})
    return seqs


def _domain_seq(session: Session, case_id: uuid.UUID, domain: str = DOMAIN) -> int:
    # Plain read: the caller already holds the case row lock (shared for reads,
    # exclusive for writes); taking a seq-row lock here would invert the order.
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id, models.CaseChangeSeq.domain == domain)
    ).scalar_one_or_none()
    return row.seq if row else 0


def get_workspace(session: Session, case_id: uuid.UUID,
                  graph_id: uuid.UUID | None = None) -> GraphWorkspace:
    """Read model for the graph page.

    ``graph_id`` narrows membership/group/snapshot lists to one graph; the case
    pools (entities, relations, sources, evidence catalog) are always complete so
    that switching graphs needs no extra request.
    """
    case = cases.lock_case_shared(session, case_id)
    entities = session.execute(
        select(models.CaseEntity).where(models.CaseEntity.case_id == case_id)
        .order_by(models.CaseEntity.created_at, models.CaseEntity.id)
    ).scalars().all()
    aliases = session.execute(
        select(models.EntityAlias).where(models.EntityAlias.case_id == case_id)
        .order_by(models.EntityAlias.created_at, models.EntityAlias.id)
    ).scalars().all()
    relations = session.execute(
        select(models.CaseRelation).where(models.CaseRelation.case_id == case_id)
        .order_by(models.CaseRelation.created_at, models.CaseRelation.id)
    ).scalars().all()
    sources = session.execute(
        select(models.GraphSource).where(models.GraphSource.case_id == case_id)
        .order_by(models.GraphSource.created_at, models.GraphSource.id)
    ).scalars().all()
    graphs = session.execute(
        select(models.RelationGraph).where(models.RelationGraph.case_id == case_id)
        .order_by(models.RelationGraph.position, models.RelationGraph.id)
    ).scalars().all()
    nodes_query = select(models.RelationGraphNode).where(models.RelationGraphNode.case_id == case_id)
    edges_query = select(models.RelationGraphEdge).where(models.RelationGraphEdge.case_id == case_id)
    groups_query = select(models.RelationGraphGroup).where(models.RelationGraphGroup.case_id == case_id)
    snapshots_query = select(models.RelationGraphSnapshot).where(
        models.RelationGraphSnapshot.case_id == case_id)
    if graph_id is not None:
        nodes_query = nodes_query.where(models.RelationGraphNode.graph_id == graph_id)
        edges_query = edges_query.where(models.RelationGraphEdge.graph_id == graph_id)
        groups_query = groups_query.where(models.RelationGraphGroup.graph_id == graph_id)
        snapshots_query = snapshots_query.where(models.RelationGraphSnapshot.graph_id == graph_id)
    nodes = session.execute(nodes_query).scalars().all()
    edges = session.execute(edges_query).scalars().all()
    groups = session.execute(groups_query).scalars().all()
    snapshots = session.execute(
        snapshots_query.order_by(models.RelationGraphSnapshot.version_no)
    ).scalars().all()
    snapshot_ids = [row.id for row in snapshots]
    consents = (session.execute(
        select(models.SnapshotConsent)
        .where(models.SnapshotConsent.case_id == case_id,
               models.SnapshotConsent.snapshot_id.in_(snapshot_ids))
        .order_by(models.SnapshotConsent.created_at, models.SnapshotConsent.id)
    ).scalars().all() if snapshot_ids else [])
    catalog = evidence_catalog.get_catalog(session, case_id)
    labels = evidence_catalog.resolve_labels(
        session, case_id, [row for row in sources if row.removed_at is None])
    changes = domain_changes.list_changes(session, models.GraphChange, case_id)
    return GraphWorkspace(
        case_id=case_id, case_title=case.title, archived=case.archived_at is not None,
        seq=_domain_seq(session, case_id), seqs=_seqs(session, case_id),
        entities=[_entity_out(row) for row in entities],
        aliases=[_alias_out(row) for row in aliases],
        relations=[_relation_out(row) for row in relations],
        sources=[_source_out(row) for row in sources],
        graphs=[_graph_out(row) for row in graphs],
        nodes=[_node_out(row) for row in nodes],
        edges=[_edge_out(row) for row in edges],
        groups=[_group_out(row) for row in groups],
        snapshots=[_snapshot_out(row) for row in snapshots],
        consents=[_consent_out(row) for row in consents],
        evidence=catalog, labels=labels,
        pending_changes=[_change_out(row) for row in changes if row.review_state == "pending"],
        changes=[_change_out(row) for row in changes],
    )


def list_snapshots(session: Session, case_id: uuid.UUID,
                   graph_id: uuid.UUID) -> list[SnapshotOut]:
    _graph_or_error(session, case_id, graph_id)
    rows = session.execute(
        select(models.RelationGraphSnapshot)
        .where(models.RelationGraphSnapshot.case_id == case_id,
               models.RelationGraphSnapshot.graph_id == graph_id)
        .order_by(models.RelationGraphSnapshot.version_no)
    ).scalars().all()
    return [_snapshot_out(row) for row in rows]


def get_snapshot(session: Session, case_id: uuid.UUID, snapshot_id: uuid.UUID) -> SnapshotOut:
    row = _snapshot_or_error(session, case_id, snapshot_id)
    return _snapshot_out(row)


def get_removal_impact(session: Session, case_id: uuid.UUID, *,
                       entity_id: uuid.UUID | None = None,
                       relation_id: uuid.UUID | None = None) -> RemovalImpactOut:
    """Graphs that would lose a node/edge if this entity/relation were deleted."""
    _case_or_error(session, case_id)
    if (entity_id is None) == (relation_id is None):
        raise _err(ErrorCode.INVALID_INPUT, "须且只能指定一个主体或一条关系")
    graph_ids: set[uuid.UUID] = set()
    if entity_id is not None:
        _entity_or_error(session, case_id, entity_id)
        graph_ids = set(session.execute(
            select(models.RelationGraphNode.graph_id).where(
                models.RelationGraphNode.case_id == case_id,
                models.RelationGraphNode.entity_id == entity_id)
        ).scalars().all())
        for row in session.execute(
                select(models.CaseRelation).where(
                    models.CaseRelation.case_id == case_id,
                    models.CaseRelation.removed_at.is_(None),
                    (models.CaseRelation.from_entity_id == entity_id)
                    | (models.CaseRelation.to_entity_id == entity_id))).scalars().all():
            graph_ids.update(session.execute(
                select(models.RelationGraphEdge.graph_id).where(
                    models.RelationGraphEdge.case_id == case_id,
                    models.RelationGraphEdge.relation_id == row.id)).scalars().all())
    else:
        _relation_or_error(session, case_id, relation_id)
        graph_ids = set(session.execute(
            select(models.RelationGraphEdge.graph_id).where(
                models.RelationGraphEdge.case_id == case_id,
                models.RelationGraphEdge.relation_id == relation_id)
        ).scalars().all())
    graphs = session.execute(
        select(models.RelationGraph).where(
            models.RelationGraph.case_id == case_id,
            models.RelationGraph.id.in_(graph_ids),
            models.RelationGraph.removed_at.is_(None))
        .order_by(models.RelationGraph.position, models.RelationGraph.id)
    ).scalars().all()
    return RemovalImpactOut(graphs=[_graph_out(row) for row in graphs], count=len(graphs))


# --- write preamble ---------------------------------------------------------

def _begin(session: Session, case_id: uuid.UUID, actor: str, entry: str,
           command: str) -> dict[str, Any]:
    rules = graph_rules.get_rules()
    if actor == "agent" and not graph_rules.agent_allowed(rules, command):
        raise _err(ErrorCode.FORBIDDEN, f"Agent 无权执行命令：{command}")
    # 维护门禁 → 案件行锁（锁内复核归档）→ 域序号锁。与 cases.lock_case_writable
    # 同一串行点：归档、进度、材料等所有同案写入都排在这把案件行锁上。
    cases.lock_case_writable(session, case_id, archived_message=ARCHIVED_MESSAGE)
    domain_changes.lock_seq(session, case_id, DOMAIN)
    return rules


def _begin_review(session: Session, case_id: uuid.UUID, actor: str,
                  message: str) -> None:
    """Human-only review preamble (accept/revert) sharing the same lock order."""
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, message)
    cases.lock_case_writable(session, case_id, archived_message=ARCHIVED_MESSAGE)
    domain_changes.lock_seq(session, case_id, DOMAIN)


def _finish(session: Session, case_id: uuid.UUID, change_id: uuid.UUID | None = None,
            notice: str | None = None) -> GraphWorkspace:
    session.commit()
    workspace = get_workspace(session, case_id)
    workspace.last_change_id = change_id
    workspace.notice = notice
    return workspace


def _record(session: Session, case_id: uuid.UUID, command: str, actor: str, entry: str,
            diff: list[dict[str, Any]]) -> models.GraphChange:
    return domain_changes.record(
        session, models.GraphChange, case_id=case_id, domain=DOMAIN,
        command=command, actor=actor, entry=entry, diff=diff)


def _audit(session: Session, command: str, case_id: uuid.UUID, target_id: uuid.UUID) -> None:
    audit.record(session, operation=f"graph.{command}", target_type="graph",
                 target_id=target_id, case_id=case_id)


def _diff(target: str, row_id: uuid.UUID | str, before: dict[str, Any] | None,
          after: dict[str, Any] | None, before_rev: int | None = None,
          after_rev: int | None = None) -> dict[str, Any]:
    return {"target": target, "id": str(row_id), "before": before, "after": after,
            "before_rev": before_rev, "after_rev": after_rev}


def _merge_diff(diff: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 (target, id) 的多条 diff 合并为一条净变化（首 before → 末 after）。

    快照恢复会多次触碰同一对象（复活、字段覆盖、防御性降级）；不合并的话，
    撤回按条目逆写会把中间态当最终态，旧 diff 逆写破坏当前工作稿。
    净变化为空（before == after）的条目直接丢弃。
    """
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
    result = []
    for key in order:
        item = merged[key]
        if item["before"] == item["after"]:
            continue
        result.append(item)
    return result


# --- lookups / guards -------------------------------------------------------

def _entity_or_error(session: Session, case_id: uuid.UUID,
                     entity_id: uuid.UUID) -> models.CaseEntity:
    row = session.get(models.CaseEntity, entity_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "主体不存在", entity_id=str(entity_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "主体不属于本案", entity_id=str(entity_id))
    return row


def _relation_or_error(session: Session, case_id: uuid.UUID,
                       relation_id: uuid.UUID) -> models.CaseRelation:
    row = session.get(models.CaseRelation, relation_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "关系不存在", relation_id=str(relation_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "关系不属于本案", relation_id=str(relation_id))
    return row


def _graph_or_error(session: Session, case_id: uuid.UUID,
                    graph_id: uuid.UUID) -> models.RelationGraph:
    row = session.get(models.RelationGraph, graph_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "关系图不存在", graph_id=str(graph_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "关系图不属于本案", graph_id=str(graph_id))
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "关系图已删除", graph_id=str(graph_id))
    return row


def _group_or_error(session: Session, case_id: uuid.UUID,
                    group_id: uuid.UUID) -> models.RelationGraphGroup:
    row = session.get(models.RelationGraphGroup, group_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "分组不存在", group_id=str(group_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "分组不属于本案", group_id=str(group_id))
    return row


def _snapshot_or_error(session: Session, case_id: uuid.UUID,
                       snapshot_id: uuid.UUID) -> models.RelationGraphSnapshot:
    row = session.get(models.RelationGraphSnapshot, snapshot_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "快照不存在", snapshot_id=str(snapshot_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "快照不属于本案", snapshot_id=str(snapshot_id))
    return row


def _cas(row: Any, expected_revision: int, label: str) -> None:
    if row.revision != expected_revision:
        raise _err(ErrorCode.CONFLICT, f"{label}已被修改，请刷新后重试",
                   expected_revision=expected_revision, actual_revision=row.revision)


def _pending_additions(session: Session, case_id: uuid.UUID) -> set[tuple[str, str]]:
    return domain_changes.pending_addition_ids(
        domain_changes.pending_changes(session, models.GraphChange, case_id))


def _guard_pending_addition(pending: set[tuple[str, str]], target: str,
                            object_id: uuid.UUID, actor: str) -> None:
    if actor == "human" and (target, str(object_id)) in pending:
        raise _err(ErrorCode.CONFLICT,
                   "该对象是 Agent 待确认新增：请先在待确认列表中确认后再修改",
                   target=target, id=str(object_id))


def _active_sources(session: Session, case_id: uuid.UUID, *,
                    relation_id: uuid.UUID | None = None,
                    entity_id: uuid.UUID | None = None) -> list[models.GraphSource]:
    query = select(models.GraphSource).where(
        models.GraphSource.case_id == case_id, models.GraphSource.removed_at.is_(None))
    if relation_id is not None:
        query = query.where(models.GraphSource.relation_id == relation_id)
    if entity_id is not None:
        query = query.where(models.GraphSource.entity_id == entity_id)
    return session.execute(query.order_by(models.GraphSource.created_at)).scalars().all()


def _confirmed_sources(session: Session, case_id: uuid.UUID, relation_id: uuid.UUID,
                       pending: set[tuple[str, str]]) -> list[models.GraphSource]:
    return [row for row in _active_sources(session, case_id, relation_id=relation_id)
            if ("source", str(row.id)) not in pending]


def _clean_text(value: str | None, *, limit: int, field: str, allow_none: bool = True) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise _err(ErrorCode.INVALID_INPUT, f"{field}不能为空")
    text = value.strip()
    if not text and not allow_none:
        raise _err(ErrorCode.INVALID_INPUT, f"{field}不能为空")
    if len(text) > limit:
        raise _err(ErrorCode.INVALID_INPUT, f"{field}不能超过 {limit} 字")
    return text or None


def _validate_roles(rules: dict[str, Any], roles: Iterable[str]) -> list[str]:
    result: list[str] = []
    for role in roles:
        if not graph_rules.valid_role(rules, role):
            raise _err(ErrorCode.INVALID_INPUT, f"未知诉讼角色：{role}")
        if role not in result:
            result.append(role)
    return result


def _attrs_payload(attrs: RelationAttrs | None) -> dict[str, Any]:
    if attrs is None:
        return {}
    data = attrs.model_dump(exclude_none=True)
    return {key: (value.isoformat() if hasattr(value, "isoformat") else value)
            for key, value in data.items()}


def _node_for(session: Session, case_id: uuid.UUID, graph_id: uuid.UUID,
              entity_id: uuid.UUID) -> models.RelationGraphNode | None:
    return session.execute(
        select(models.RelationGraphNode).where(
            models.RelationGraphNode.case_id == case_id,
            models.RelationGraphNode.graph_id == graph_id,
            models.RelationGraphNode.entity_id == entity_id)
    ).scalar_one_or_none()


def _ensure_node(session: Session, case_id: uuid.UUID, graph_id: uuid.UUID,
                 entity_id: uuid.UUID, diff: list[dict[str, Any]]) -> models.RelationGraphNode:
    existing = _node_for(session, case_id, graph_id, entity_id)
    if existing is not None:
        return existing
    row = models.RelationGraphNode(
        id=uuid.uuid4(), case_id=case_id, graph_id=graph_id, entity_id=entity_id,
        group_id=None, x=0.0, y=0.0, revision=0, created_at=_now(), updated_at=_now(),
    )
    session.add(row)
    session.flush()
    diff.append(_diff("graph_node", row.id, None, _node_dict(row)))
    return row


def _ensure_edge(session: Session, case_id: uuid.UUID, graph_id: uuid.UUID,
                 relation_id: uuid.UUID, diff: list[dict[str, Any]]) -> models.RelationGraphEdge:
    existing = session.execute(
        select(models.RelationGraphEdge).where(
            models.RelationGraphEdge.case_id == case_id,
            models.RelationGraphEdge.graph_id == graph_id,
            models.RelationGraphEdge.relation_id == relation_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = models.RelationGraphEdge(
        id=uuid.uuid4(), case_id=case_id, graph_id=graph_id, relation_id=relation_id,
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    diff.append(_diff("graph_edge", row.id, None, _edge_dict(row)))
    return row


# --- entity commands --------------------------------------------------------

def add_entity(session: Session, payload: AddEntityInput, *, actor: str,
               entry: str) -> GraphWorkspace:
    rules = _begin(session, payload.case_id, actor, entry, "add_entity")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新建主体必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.CaseEntity).where(
                models.CaseEntity.case_id == payload.case_id,
                models.CaseEntity.request_key == payload.request_key)
        ).scalar_one_or_none()
        if existing is not None:
            return _finish(session, payload.case_id)  # idempotent replay
    if not graph_rules.valid_entity_kind(rules, payload.kind):
        raise _err(ErrorCode.INVALID_INPUT, f"未知主体类型：{payload.kind}")
    name = _clean_text(payload.name, limit=graph_rules.ENTITY_NAME_MAX,
                       field="主体名称", allow_none=False)
    note = _clean_text(payload.note, limit=graph_rules.NOTE_MAX, field="备注")
    roles = _validate_roles(rules, payload.roles)
    timestamp = _now()
    row = models.CaseEntity(
        id=uuid.uuid4(), case_id=payload.case_id, kind=payload.kind, name=name, roles=roles,
        note=note, created_by=actor, updated_by=actor, request_key=payload.request_key,
        removed_at=None, revision=0, created_at=timestamp, updated_at=timestamp,
    )
    session.add(row)
    session.flush()
    diff = [_diff("entity", row.id, None, _entity_dict(row))]
    if payload.graph_id is not None:
        _graph_or_error(session, payload.case_id, payload.graph_id)
        _ensure_node(session, payload.case_id, payload.graph_id, row.id, diff)
    change = _record(session, payload.case_id, "add_entity", actor, entry, diff)
    _audit(session, "add_entity", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def update_entity(session: Session, payload: UpdateEntityInput, *, actor: str,
                  entry: str) -> GraphWorkspace:
    rules = _begin(session, payload.case_id, actor, entry, "update_entity")
    row = _entity_or_error(session, payload.case_id, payload.entity_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "主体已删除")
    _cas(row, payload.expected_revision, "主体")
    _guard_pending_addition(_pending_additions(session, payload.case_id), "entity", row.id, actor)
    changes: EntityChanges = payload.changes
    provided = changes.model_fields_set
    if "kind" in provided:
        if changes.kind is None or not graph_rules.valid_entity_kind(rules, changes.kind):
            raise _err(ErrorCode.INVALID_INPUT, f"未知主体类型：{changes.kind}")
    if "name" in provided:
        _clean_text(changes.name, limit=graph_rules.ENTITY_NAME_MAX,
                    field="主体名称", allow_none=False)
    if "roles" in provided:
        _validate_roles(rules, changes.roles or [])
    if "note" in provided:
        _clean_text(changes.note, limit=graph_rules.NOTE_MAX, field="备注")
    before = _entity_dict(row)
    before_rev = row.revision
    if "kind" in provided:
        row.kind = changes.kind
    if "name" in provided:
        row.name = changes.name.strip()
    if "roles" in provided:
        row.roles = _validate_roles(rules, changes.roles or [])
    if "note" in provided:
        row.note = changes.note.strip() if changes.note else None
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "update_entity", actor, entry,
                     [_diff("entity", row.id, before, _entity_dict(row),
                            before_rev, row.revision)])
    _audit(session, "update_entity", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_entity(session: Session, payload: EntityRevisionInput, *, actor: str,
                  entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "remove_entity")
    row = _entity_or_error(session, payload.case_id, payload.entity_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "主体已删除")
    _cas(row, payload.expected_revision, "主体")
    impact = get_removal_impact(session, payload.case_id, entity_id=row.id)
    linked = session.execute(
        select(models.CaseRelation).where(
            models.CaseRelation.case_id == payload.case_id,
            models.CaseRelation.removed_at.is_(None),
            (models.CaseRelation.from_entity_id == row.id)
            | (models.CaseRelation.to_entity_id == row.id))
    ).scalars().all()
    before = _entity_dict(row)
    before_rev = row.revision
    row.removed_at = _now()
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "remove_entity", actor, entry,
                     [_diff("entity", row.id, before, _entity_dict(row),
                            before_rev, row.revision)])
    _audit(session, "remove_entity", payload.case_id, row.id)
    titles = "、".join(graph.title for graph in impact.graphs) or "无"
    notice = (f"已删除主体「{before['name']}」（案件级软删除），影响 {impact.count} 张图：{titles}。"
              + (f"其 {len(linked)} 条关系暂不显示，恢复主体后一并恢复。" if linked else "")
              + "可用撤回恢复。")
    return _finish(session, payload.case_id, change.id, notice)


def restore_entity(session: Session, payload: EntityRevisionInput, *, actor: str,
                   entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "restore_entity")
    row = _entity_or_error(session, payload.case_id, payload.entity_id)
    if row.removed_at is None:
        raise _err(ErrorCode.INVALID_INPUT, "主体未删除")
    _cas(row, payload.expected_revision, "主体")
    before = _entity_dict(row)
    before_rev = row.revision
    row.removed_at = None
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "restore_entity", actor, entry,
                     [_diff("entity", row.id, before, _entity_dict(row),
                            before_rev, row.revision)])
    _audit(session, "restore_entity", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def add_alias(session: Session, payload: AddAliasInput, *, actor: str,
              entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "add_alias")
    entity = _entity_or_error(session, payload.case_id, payload.entity_id)
    if entity.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "主体已删除")
    _guard_pending_addition(_pending_additions(session, payload.case_id), "entity",
                            entity.id, actor)
    alias = _clean_text(payload.alias, limit=graph_rules.ALIAS_MAX,
                        field="别名", allow_none=False)
    duplicate = session.execute(
        select(models.EntityAlias).where(
            models.EntityAlias.case_id == payload.case_id,
            models.EntityAlias.entity_id == entity.id,
            models.EntityAlias.removed_at.is_(None),
            models.EntityAlias.alias.ilike(alias))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _err(ErrorCode.INVALID_INPUT, "该别名已存在")
    row = models.EntityAlias(
        id=uuid.uuid4(), case_id=payload.case_id, entity_id=entity.id, alias=alias,
        created_by=actor, removed_at=None, created_at=_now(),
    )
    session.add(row)
    session.flush()
    change = _record(session, payload.case_id, "add_alias", actor, entry,
                     [_diff("alias", row.id, None, _alias_dict(row))])
    _audit(session, "add_alias", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_alias(session: Session, payload: RemoveAliasInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "remove_alias")
    row = session.get(models.EntityAlias, payload.alias_id)
    if row is None or row.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "别名不存在")
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "别名已删除")
    _guard_pending_addition(_pending_additions(session, payload.case_id), "alias",
                            row.id, actor)
    before = _alias_dict(row)
    row.removed_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "remove_alias", actor, entry,
                     [_diff("alias", row.id, before, _alias_dict(row))])
    _audit(session, "remove_alias", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


# --- relation commands ------------------------------------------------------

def add_relation(session: Session, payload: AddGraphRelationInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    rules = _begin(session, payload.case_id, actor, entry, "add_relation")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新建关系必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.CaseRelation).where(
                models.CaseRelation.case_id == payload.case_id,
                models.CaseRelation.request_key == payload.request_key)
        ).scalar_one_or_none()
        if existing is not None:
            return _finish(session, payload.case_id)
    source = _entity_or_error(session, payload.case_id, payload.from_entity_id)
    target = _entity_or_error(session, payload.case_id, payload.to_entity_id)
    for entity in (source, target):
        if entity.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "主体已删除")
        _guard_pending_addition(_pending_additions(session, payload.case_id), "entity",
                                entity.id, actor)
    if source.id == target.id:
        raise _err(ErrorCode.INVALID_INPUT, "关系起点与终点不能是同一主体")
    if not graph_rules.valid_relation_kind(rules, payload.kind):
        raise _err(ErrorCode.INVALID_INPUT, f"未知关系类型：{payload.kind}")
    if not graph_rules.valid_credibility(rules, payload.credibility):
        raise _err(ErrorCode.INVALID_INPUT, f"未知可信状态：{payload.credibility}")
    if payload.credibility == "evidenced":
        # Rule 3: "evidenced" needs at least one source; a new relation has none.
        raise _err(ErrorCode.INVALID_INPUT,
                   "「有证据」必须至少关联一条有效出处：请先以其他状态新建关系，添加出处后再改为有证据")
    label = _clean_text(payload.label, limit=graph_rules.RELATION_LABEL_MAX, field="关系标签")
    note = _clean_text(payload.note, limit=graph_rules.NOTE_MAX, field="备注")
    attrs = _attrs_payload(payload.attrs)
    duplicate = session.execute(
        select(models.CaseRelation).where(
            models.CaseRelation.case_id == payload.case_id,
            models.CaseRelation.from_entity_id == source.id,
            models.CaseRelation.to_entity_id == target.id,
            models.CaseRelation.kind == payload.kind,
            models.CaseRelation.removed_at.is_(None))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _err(ErrorCode.INVALID_INPUT, "这两个主体之间已有同类关系")
    if payload.graph_id is not None:
        _graph_or_error(session, payload.case_id, payload.graph_id)
    timestamp = _now()
    row = models.CaseRelation(
        id=uuid.uuid4(), case_id=payload.case_id, from_entity_id=source.id,
        to_entity_id=target.id, directed=payload.directed, kind=payload.kind,
        label=label, credibility=payload.credibility, attrs=attrs, note=note,
        created_by=actor, updated_by=actor, request_key=payload.request_key,
        removed_at=None, revision=0, created_at=timestamp, updated_at=timestamp,
    )
    session.add(row)
    session.flush()
    diff = [_diff("relation", row.id, None, _relation_dict(row))]
    if payload.graph_id is not None:
        _ensure_node(session, payload.case_id, payload.graph_id, source.id, diff)
        _ensure_node(session, payload.case_id, payload.graph_id, target.id, diff)
        _ensure_edge(session, payload.case_id, payload.graph_id, row.id, diff)
    change = _record(session, payload.case_id, "add_relation", actor, entry, diff)
    _audit(session, "add_relation", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def update_relation(session: Session, payload: UpdateRelationInput, *, actor: str,
                    entry: str) -> GraphWorkspace:
    rules = _begin(session, payload.case_id, actor, entry, "update_relation")
    row = _relation_or_error(session, payload.case_id, payload.relation_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "关系已删除")
    _cas(row, payload.expected_revision, "关系")
    pending = _pending_additions(session, payload.case_id)
    _guard_pending_addition(pending, "relation", row.id, actor)
    changes: RelationChanges = payload.changes
    provided = changes.model_fields_set
    if "kind" in provided and (changes.kind is None
                               or not graph_rules.valid_relation_kind(rules, changes.kind)):
        raise _err(ErrorCode.INVALID_INPUT, f"未知关系类型：{changes.kind}")
    if "credibility" in provided and (changes.credibility is None
                                      or not graph_rules.valid_credibility(rules, changes.credibility)):
        raise _err(ErrorCode.INVALID_INPUT, f"未知可信状态：{changes.credibility}")
    if "label" in provided:
        _clean_text(changes.label, limit=graph_rules.RELATION_LABEL_MAX, field="关系标签")
    if "note" in provided:
        _clean_text(changes.note, limit=graph_rules.NOTE_MAX, field="备注")
    if changes.credibility == "evidenced" and "credibility" in provided:
        confirmed = _confirmed_sources(session, payload.case_id, row.id, pending)
        if not confirmed:
            pending_count = len(_active_sources(session, payload.case_id, relation_id=row.id))
            if actor == "human":
                raise _err(
                    ErrorCode.INVALID_INPUT,
                    "该关系的出处于待确认状态（Agent 提交）：请先在待确认列表确认该出处，再把可信状态改为「有证据」"
                    if pending_count else
                    "「有证据」必须至少关联一条有效出处：请先在出处中关联材料或片段")
            if pending_count == 0:
                raise _err(ErrorCode.INVALID_INPUT,
                           "「有证据」必须至少关联一条有效出处：请先在出处中关联材料或片段")
    before = _relation_dict(row)
    before_rev = row.revision
    if "kind" in provided:
        row.kind = changes.kind
    if "label" in provided:
        row.label = changes.label.strip() if changes.label else None
    if "credibility" in provided:
        row.credibility = changes.credibility
    if "directed" in provided and changes.directed is not None:
        row.directed = changes.directed
    if "attrs" in provided:
        row.attrs = _attrs_payload(changes.attrs)
    if "note" in provided:
        row.note = changes.note.strip() if changes.note else None
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "update_relation", actor, entry,
                     [_diff("relation", row.id, before, _relation_dict(row),
                            before_rev, row.revision)])
    _audit(session, "update_relation", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_relation(session: Session, payload: RelationRevisionInput, *, actor: str,
                    entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "remove_relation")
    row = _relation_or_error(session, payload.case_id, payload.relation_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "关系已删除")
    _cas(row, payload.expected_revision, "关系")
    impact = get_removal_impact(session, payload.case_id, relation_id=row.id)
    before = _relation_dict(row)
    before_rev = row.revision
    row.removed_at = _now()
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "remove_relation", actor, entry,
                     [_diff("relation", row.id, before, _relation_dict(row),
                            before_rev, row.revision)])
    _audit(session, "remove_relation", payload.case_id, row.id)
    titles = "、".join(graph.title for graph in impact.graphs) or "无"
    return _finish(session, payload.case_id, change.id,
                   f"已删除关系（案件级软删除），影响 {impact.count} 张图：{titles}。")


def restore_relation(session: Session, payload: RelationRevisionInput, *, actor: str,
                     entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "restore_relation")
    row = _relation_or_error(session, payload.case_id, payload.relation_id)
    if row.removed_at is None:
        raise _err(ErrorCode.INVALID_INPUT, "关系未删除")
    _cas(row, payload.expected_revision, "关系")
    for endpoint_id in (row.from_entity_id, row.to_entity_id):
        endpoint = _entity_or_error(session, payload.case_id, endpoint_id)
        if endpoint.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "关系端点主体已删除，请先恢复主体")
    duplicate = session.execute(
        select(models.CaseRelation).where(
            models.CaseRelation.case_id == payload.case_id,
            models.CaseRelation.from_entity_id == row.from_entity_id,
            models.CaseRelation.to_entity_id == row.to_entity_id,
            models.CaseRelation.kind == row.kind,
            models.CaseRelation.removed_at.is_(None),
            models.CaseRelation.id != row.id)
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _err(ErrorCode.INVALID_INPUT, "已存在同向同类有效关系，无法恢复")
    before = _relation_dict(row)
    before_rev = row.revision
    row.removed_at = None
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "restore_relation", actor, entry,
                     [_diff("relation", row.id, before, _relation_dict(row),
                            before_rev, row.revision)])
    _audit(session, "restore_relation", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


# --- source commands --------------------------------------------------------

def _check_ref(session: Session, case_id: uuid.UUID, payload: AddSourceInput) -> None:
    ref = payload.ref
    if ref.segment_id is not None:
        row = session.get(models.Segment, ref.segment_id)
        model, label = models.Segment, "片段"
    elif ref.material_id is not None:
        row = session.get(models.Material, ref.material_id)
        model, label = models.Material, "材料"
    else:
        row = session.get(models.Annotation, ref.annotation_id)
        model, label = models.Annotation, "标注"
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, f"{label}不存在")
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, f"{label}不属于本案")
    if ref.segment_id is not None:
        # 只允许引用已完成解析的片段：未发布/进行中的段会被解析重试删除，
        # 引用它会让 worker 撞外键、阻断重试。条件 = 同案 FK + parse_run 已
        # 终态完成（complete/partial）。不要求仍是活动版本：材料重新解析后
        # 旧已发布段是不可变 source_ref，必须继续可引用、可 read_source。
        run = session.get(models.ParseRun, row.parse_run_id)
        if run is None or run.case_id != case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "片段不属于本案")
        if run.status not in ("complete", "partial"):
            raise _err(ErrorCode.INVALID_INPUT,
                       "该片段所属解析尚未完成：请等待解析完成后再引用")
        version = session.get(models.FileVersion, run.file_version_id)
        if version is None or version.case_id != case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "片段不属于本案")


def add_source(session: Session, payload: AddSourceInput, *, actor: str,
               entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "add_source")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新增出处必须带 request_key")
    if payload.entity_id is not None:
        target = _entity_or_error(session, payload.case_id, payload.entity_id)
        if target.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "主体已删除")
        _guard_pending_addition(_pending_additions(session, payload.case_id), "entity",
                                target.id, actor)
    else:
        target = _relation_or_error(session, payload.case_id, payload.relation_id)
        if target.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "关系已删除")
        _guard_pending_addition(_pending_additions(session, payload.case_id), "relation",
                                target.id, actor)
    _check_ref(session, payload.case_id, payload)
    proves = _clean_text(payload.proves, limit=graph_rules.PROVES_MAX, field="证明要点")
    row = models.GraphSource(
        id=uuid.uuid4(), case_id=payload.case_id,
        entity_id=payload.entity_id, relation_id=payload.relation_id,
        segment_id=payload.ref.segment_id, material_id=payload.ref.material_id,
        annotation_id=payload.ref.annotation_id, proves=proves, created_by=actor,
        removed_at=None, created_at=_now(),
    )
    session.add(row)
    session.flush()
    change = _record(session, payload.case_id, "add_source", actor, entry,
                     [_diff("source", row.id, None, _source_dict(row))])
    _audit(session, "add_source", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_source(session: Session, payload: RemoveSourceInput, *, actor: str,
                  entry: str) -> GraphWorkspace:
    rules = _begin(session, payload.case_id, actor, entry, "remove_source")
    row = session.get(models.GraphSource, payload.source_id)
    if row is None or row.case_id != payload.case_id or row.removed_at is not None:
        raise _err(ErrorCode.NOT_FOUND, "出处不存在")
    pending = _pending_additions(session, payload.case_id)
    _guard_pending_addition(pending, "source", row.id, actor)
    relation = session.get(models.CaseRelation, row.relation_id) if row.relation_id else None
    remaining = (len([item for item in _confirmed_sources(
        session, payload.case_id, relation.id, pending) if item.id != row.id])
        if relation is not None else 0)
    downgrade_to = payload.downgrade_to
    if (relation is not None and relation.removed_at is None
            and relation.credibility == "evidenced" and remaining == 0):
        if downgrade_to is None:
            pending_count = len(_active_sources(session, payload.case_id,
                                                relation_id=relation.id)) - len(
                _confirmed_sources(session, payload.case_id, relation.id, pending))
            raise _err(
                ErrorCode.INVALID_INPUT,
                "这是「有证据」关系最后一条已确认出处：删除前必须同时选择降级后的可信状态"
                "（待查 / 对方主张 / 已排除），系统不会静默降级"
                + ("；若 Agent 提交的待确认出处已核实，也可先在待确认列表确认该出处"
                   if pending_count > 0 else ""))
        if downgrade_to == "evidenced":
            raise _err(ErrorCode.INVALID_INPUT, "降级后的可信状态不能仍是「有证据」")
        if not graph_rules.valid_credibility(rules, downgrade_to):
            raise _err(ErrorCode.INVALID_INPUT, f"未知可信状态：{downgrade_to}")
    before = _source_dict(row)
    row.removed_at = _now()
    diff = [_diff("source", row.id, before, _source_dict(row))]
    notice = None
    if (relation is not None and downgrade_to is not None
            and relation.credibility == "evidenced" and remaining == 0):
        r_before = _relation_dict(relation)
        r_rev = relation.revision
        relation.credibility = downgrade_to
        relation.revision += 1
        relation.updated_by = actor
        relation.updated_at = _now()
        diff.append(_diff("relation", relation.id, r_before, _relation_dict(relation),
                          r_rev, relation.revision))
        label = next((entry["label"] for entry in rules["credibility"]
                      if entry["value"] == downgrade_to), downgrade_to)
        notice = f"已删除最后一条出处，关系降级为「{label}」"
    session.flush()
    change = _record(session, payload.case_id, "remove_source", actor, entry, diff)
    _audit(session, "remove_source", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id, notice)


# --- graph / view commands --------------------------------------------------

def create_graph(session: Session, payload: CreateGraphInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "create_graph")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新建关系图必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.RelationGraph).where(
                models.RelationGraph.case_id == payload.case_id,
                models.RelationGraph.request_key == payload.request_key)
        ).scalar_one_or_none()
        if existing is not None:
            return _finish(session, payload.case_id)
    title = _clean_text(payload.title, limit=graph_rules.GRAPH_TITLE_MAX,
                        field="图名称", allow_none=False)
    description = _clean_text(payload.description, limit=graph_rules.GRAPH_DESC_MAX,
                              field="图说明")
    position = len(session.execute(
        select(models.RelationGraph).where(
            models.RelationGraph.case_id == payload.case_id,
            models.RelationGraph.removed_at.is_(None))).scalars().all())
    timestamp = _now()
    row = models.RelationGraph(
        id=uuid.uuid4(), case_id=payload.case_id, title=title, description=description,
        position=position, created_by=actor, updated_by=actor,
        request_key=payload.request_key, removed_at=None, revision=0,
        created_at=timestamp, updated_at=timestamp,
    )
    session.add(row)
    session.flush()
    change = _record(session, payload.case_id, "create_graph", actor, entry,
                     [_diff("graph", row.id, None, _graph_dict(row))])
    _audit(session, "create_graph", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def update_graph(session: Session, payload: UpdateGraphInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "update_graph")
    row = _graph_or_error(session, payload.case_id, payload.graph_id)
    _cas(row, payload.expected_revision, "关系图")
    changes: GraphChanges = payload.changes
    provided = changes.model_fields_set
    if "title" in provided:
        _clean_text(changes.title, limit=graph_rules.GRAPH_TITLE_MAX,
                    field="图名称", allow_none=False)
    if "description" in provided:
        _clean_text(changes.description, limit=graph_rules.GRAPH_DESC_MAX, field="图说明")
    before = _graph_dict(row)
    before_rev = row.revision
    if "title" in provided:
        row.title = changes.title.strip()
    if "description" in provided:
        row.description = changes.description.strip() if changes.description else None
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "update_graph", actor, entry,
                     [_diff("graph", row.id, before, _graph_dict(row),
                            before_rev, row.revision)])
    _audit(session, "update_graph", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_graph(session: Session, payload: GraphRevisionInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "remove_graph")
    row = _graph_or_error(session, payload.case_id, payload.graph_id)
    _cas(row, payload.expected_revision, "关系图")
    before = _graph_dict(row)
    before_rev = row.revision
    row.removed_at = _now()
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    diff = [_diff("graph", row.id, before, _graph_dict(row), before_rev, row.revision)]
    # Membership is view data; it is deleted with the view (snapshots stay).
    for edge in session.execute(select(models.RelationGraphEdge).where(
            models.RelationGraphEdge.case_id == payload.case_id,
            models.RelationGraphEdge.graph_id == row.id)).scalars().all():
        diff.append(_diff("graph_edge", edge.id, _edge_dict(edge), None))
        session.delete(edge)
    for node in session.execute(select(models.RelationGraphNode).where(
            models.RelationGraphNode.case_id == payload.case_id,
            models.RelationGraphNode.graph_id == row.id)).scalars().all():
        diff.append(_diff("graph_node", node.id, _node_dict(node), None))
        session.delete(node)
    for group in session.execute(select(models.RelationGraphGroup).where(
            models.RelationGraphGroup.case_id == payload.case_id,
            models.RelationGraphGroup.graph_id == row.id)).scalars().all():
        diff.append(_diff("graph_group", group.id, _group_dict(group), None))
        session.delete(group)
    session.flush()
    change = _record(session, payload.case_id, "remove_graph", actor, entry, diff)
    _audit(session, "remove_graph", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id,
                   "已删除关系图（软删除，可撤回）；其快照仍保留可查。")


def add_to_graph(session: Session, payload: AddToGraphInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "add_to_graph")
    graph = _graph_or_error(session, payload.case_id, payload.graph_id)
    diff: list[dict[str, Any]] = []
    for entity_id in payload.entity_ids:
        entity = _entity_or_error(session, payload.case_id, entity_id)
        if entity.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "主体已删除", entity_id=str(entity_id))
        _ensure_node(session, payload.case_id, graph.id, entity.id, diff)
    for relation_id in payload.relation_ids:
        relation = _relation_or_error(session, payload.case_id, relation_id)
        if relation.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "关系已删除", relation_id=str(relation_id))
        for endpoint_id in (relation.from_entity_id, relation.to_entity_id):
            endpoint = _entity_or_error(session, payload.case_id, endpoint_id)
            if endpoint.removed_at is not None:
                raise _err(ErrorCode.INVALID_INPUT, "关系端点主体已删除")
            _ensure_node(session, payload.case_id, graph.id, endpoint.id, diff)
        _ensure_edge(session, payload.case_id, graph.id, relation.id, diff)
    if not diff:
        return _finish(session, payload.case_id)
    change = _record(session, payload.case_id, "add_to_graph", actor, entry, diff)
    _audit(session, "add_to_graph", payload.case_id, graph.id)
    return _finish(session, payload.case_id, change.id)


def remove_from_graph(session: Session, payload: RemoveFromGraphInput, *, actor: str,
                      entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "remove_from_graph")
    graph = _graph_or_error(session, payload.case_id, payload.graph_id)
    diff: list[dict[str, Any]] = []
    if payload.entity_id is not None:
        node = _node_for(session, payload.case_id, graph.id, payload.entity_id)
        if node is None:
            raise _err(ErrorCode.NOT_FOUND, "该主体不在本图")
        # Edges touching the removed node would dangle inside this view.
        for edge in session.execute(
                select(models.RelationGraphEdge).where(
                    models.RelationGraphEdge.case_id == payload.case_id,
                    models.RelationGraphEdge.graph_id == graph.id)).scalars().all():
            relation = session.get(models.CaseRelation, edge.relation_id)
            if relation is not None and payload.entity_id in (
                    relation.from_entity_id, relation.to_entity_id):
                diff.append(_diff("graph_edge", edge.id, _edge_dict(edge), None))
                session.delete(edge)
        diff.append(_diff("graph_node", node.id, _node_dict(node), None))
        session.delete(node)
    else:
        edge = session.execute(
            select(models.RelationGraphEdge).where(
                models.RelationGraphEdge.case_id == payload.case_id,
                models.RelationGraphEdge.graph_id == graph.id,
                models.RelationGraphEdge.relation_id == payload.relation_id)
        ).scalar_one_or_none()
        if edge is None:
            raise _err(ErrorCode.NOT_FOUND, "该关系不在本图")
        diff.append(_diff("graph_edge", edge.id, _edge_dict(edge), None))
        session.delete(edge)
    session.flush()
    change = _record(session, payload.case_id, "remove_from_graph", actor, entry, diff)
    _audit(session, "remove_from_graph", payload.case_id, graph.id)
    return _finish(session, payload.case_id, change.id,
                   "已从本图移除；案件主体与关系不受影响，其他图不变。")


def save_layout(session: Session, payload: SaveLayoutInput, *, actor: str,
                entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "save_layout")
    graph = _graph_or_error(session, payload.case_id, payload.graph_id)
    diff: list[dict[str, Any]] = []
    for item in payload.nodes:
        node = session.execute(
            select(models.RelationGraphNode).where(
                models.RelationGraphNode.case_id == payload.case_id,
                models.RelationGraphNode.graph_id == graph.id,
                or_(
                    models.RelationGraphNode.entity_id == item.node_id,
                    models.RelationGraphNode.id == item.node_id,
                ))
        ).scalar_one_or_none()
        if node is None:
            raise _err(ErrorCode.NOT_FOUND, "主体不在本图", node_id=str(item.node_id))
        if node.x == item.x and node.y == item.y:
            continue
        before = _node_dict(node)
        before_rev = node.revision
        node.x = item.x
        node.y = item.y
        node.revision += 1
        node.updated_at = _now()
        diff.append(_diff("graph_node", node.id, before, _node_dict(node),
                          before_rev, node.revision))
    for item in payload.groups:
        group = _group_or_error(session, payload.case_id, item.group_id)
        if group.graph_id != graph.id or group.removed_at is not None:
            raise _err(ErrorCode.NOT_FOUND, "分组不在本图", group_id=str(item.group_id))
        width = item.width if item.width is not None else group.width
        height = item.height if item.height is not None else group.height
        if (group.x, group.y, group.width, group.height) == (item.x, item.y, width, height):
            continue
        before = _group_dict(group)
        before_rev = group.revision
        group.x, group.y, group.width, group.height = item.x, item.y, width, height
        group.revision += 1
        group.updated_at = _now()
        diff.append(_diff("graph_group", group.id, before, _group_dict(group),
                          before_rev, group.revision))
    if not diff:
        return _finish(session, payload.case_id)
    session.flush()
    change = _record(session, payload.case_id, "save_layout", actor, entry, diff)
    _audit(session, "save_layout", payload.case_id, graph.id)
    return _finish(session, payload.case_id, change.id)


def upsert_group(session: Session, payload: UpsertGroupInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "upsert_group")
    graph = _graph_or_error(session, payload.case_id, payload.graph_id)
    title = _clean_text(payload.title, limit=graph_rules.GROUP_TITLE_MAX,
                        field="分组名称", allow_none=False)
    color = _clean_text(payload.color, limit=graph_rules.GROUP_COLOR_MAX, field="分组颜色")
    diff: list[dict[str, Any]] = []
    if payload.group_id is None:
        row = models.RelationGraphGroup(
            id=uuid.uuid4(), case_id=payload.case_id, graph_id=graph.id, title=title,
            color=color, x=payload.x, y=payload.y, width=payload.width,
            height=payload.height, collapsed=payload.collapsed, revision=0,
            removed_at=None, created_at=_now(), updated_at=_now(),
        )
        session.add(row)
        session.flush()
        diff.append(_diff("graph_group", row.id, None, _group_dict(row)))
    else:
        row = _group_or_error(session, payload.case_id, payload.group_id)
        if row.graph_id != graph.id:
            raise _err(ErrorCode.CASE_MISMATCH, "分组不属于该图")
        if payload.expected_revision is None:
            raise _err(ErrorCode.INVALID_INPUT, "修改分组须提交 expected_revision")
        _cas(row, payload.expected_revision, "分组")
        before = _group_dict(row)
        before_rev = row.revision
        row.title = title
        row.color = color
        row.x, row.y = payload.x, payload.y
        row.width, row.height = payload.width, payload.height
        row.collapsed = payload.collapsed
        row.removed_at = None
        row.revision += 1
        row.updated_at = _now()
        diff.append(_diff("graph_group", row.id, before, _group_dict(row),
                          before_rev, row.revision))
    if payload.entity_ids:
        for entity_id in payload.entity_ids:
            node = _node_for(session, payload.case_id, graph.id, entity_id)
            if node is None:
                raise _err(ErrorCode.NOT_FOUND, "主体不在本图", entity_id=str(entity_id))
            if node.group_id == row.id:
                continue
            before = _node_dict(node)
            before_rev = node.revision
            node.group_id = row.id
            node.revision += 1
            node.updated_at = _now()
            diff.append(_diff("graph_node", node.id, before, _node_dict(node),
                              before_rev, node.revision))
    session.flush()
    change = _record(session, payload.case_id, "upsert_group", actor, entry, diff)
    _audit(session, "upsert_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def set_node_group(session: Session, payload: SetNodeGroupInput, *, actor: str,
                   entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "upsert_group")
    graph = _graph_or_error(session, payload.case_id, payload.graph_id)
    node = _node_for(session, payload.case_id, graph.id, payload.entity_id)
    if node is None:
        raise _err(ErrorCode.NOT_FOUND, "主体不在本图")
    if payload.group_id is not None:
        group = _group_or_error(session, payload.case_id, payload.group_id)
        if group.graph_id != graph.id or group.removed_at is not None:
            raise _err(ErrorCode.NOT_FOUND, "分组不在本图")
    if node.group_id == payload.group_id:
        return _finish(session, payload.case_id)
    before = _node_dict(node)
    before_rev = node.revision
    node.group_id = payload.group_id
    node.revision += 1
    node.updated_at = _now()
    session.flush()
    change = _record(session, payload.case_id, "upsert_group", actor, entry,
                     [_diff("graph_node", node.id, before, _node_dict(node),
                            before_rev, node.revision)])
    _audit(session, "upsert_group", payload.case_id, node.id)
    return _finish(session, payload.case_id, change.id)


def remove_group(session: Session, payload: RemoveGroupInput, *, actor: str,
                 entry: str) -> GraphWorkspace:
    _begin(session, payload.case_id, actor, entry, "remove_group")
    group = _group_or_error(session, payload.case_id, payload.group_id)
    if group.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    _cas(group, payload.expected_revision, "分组")
    before = _group_dict(group)
    before_rev = group.revision
    group.removed_at = _now()
    group.revision += 1
    group.updated_at = _now()
    diff = [_diff("graph_group", group.id, before, _group_dict(group),
                  before_rev, group.revision)]
    for node in session.execute(select(models.RelationGraphNode).where(
            models.RelationGraphNode.case_id == payload.case_id,
            models.RelationGraphNode.group_id == group.id)).scalars().all():
        node_before = _node_dict(node)
        node_rev = node.revision
        node.group_id = None
        node.revision += 1
        node.updated_at = _now()
        diff.append(_diff("graph_node", node.id, node_before, _node_dict(node),
                          node_rev, node.revision))
    session.flush()
    change = _record(session, payload.case_id, "remove_group", actor, entry, diff)
    _audit(session, "remove_group", payload.case_id, group.id)
    return _finish(session, payload.case_id, change.id)


# --- snapshot helpers -------------------------------------------------------

def _pending_entries(session: Session, case_id: uuid.UUID) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(target, id) → 该对象全部待确认 diff 条目，按写入顺序（P1 pendingEntries）。"""
    entries: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for change in domain_changes.pending_changes(session, models.GraphChange, case_id):
        if change.actor != "agent":
            continue
        for item in change.diff:
            if item["target"] not in _REVIEW_TARGETS:
                continue
            entries.setdefault((item["target"], str(item["id"])), []).append(item)
    return entries


def _confirmed_fields(record: dict[str, Any],
                      items: list[dict[str, Any]]) -> dict[str, Any]:
    """从当前记录倒序撤销待确认字段（P1 confirmedFields）。

    字段只在当前值仍等于该条 after 时回退：连续叠加回退到最早确认值，
    人工后续改写保持人工值。
    """
    result = dict(record)
    for item in reversed(items):
        before, after = item.get("before"), item.get("after")
        if not before or not after:
            continue
        for key in set(before) | set(after):
            if key in _META_FIELDS:
                continue
            if _norm_value(before.get(key)) == _norm_value(after.get(key)):
                continue
            if _norm_value(result.get(key)) != _norm_value(after.get(key)):
                continue
            result[key] = before.get(key)
    return result


def _confirmed_record(record: dict[str, Any],
                      items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """单条记录的已确认状态；None 表示该对象只存在于待确认新增里。"""
    if not items:
        return record
    if items[0].get("before") is None:
        return None
    removal = next((item for item in items if item.get("after") is None), None)
    if removal is not None and removal.get("before"):
        return dict(removal["before"])
    return _confirmed_fields(record, items)


def _confirmed_rows(session: Session, case_id: uuid.UUID, model: type, target: str,
                    dict_of: Any, entries: dict[tuple[str, str], list[dict[str, Any]]],
                    ) -> dict[uuid.UUID, dict[str, Any]]:
    """案件池某表的已确认视图：过滤软删除与待确认新增，字段回退到确认态。"""
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for row in session.execute(select(model).where(model.case_id == case_id)).scalars().all():
        base = _confirmed_record(dict_of(row), entries.get((target, str(row.id))))
        if base is None:
            continue
        if base.get("removed_at") is not None:
            continue
        result[row.id] = base
    return result


def _pending_scope(session: Session, case_id: uuid.UUID,
                   graph_id: uuid.UUID) -> list[models.GraphChange]:
    """Pending agent changes that would touch this graph or its numbering.

    Mirrors P1 ``pendingChangesInGraph``: membership, aliases, graph rename,
    sources and any catalog change that can move a number shown in this graph.
    """
    entity_ids = {str(row.entity_id) for row in session.execute(
        select(models.RelationGraphNode).where(
            models.RelationGraphNode.case_id == case_id,
            models.RelationGraphNode.graph_id == graph_id)).scalars().all()}
    relation_ids = {str(row.relation_id) for row in session.execute(
        select(models.RelationGraphEdge).where(
            models.RelationGraphEdge.case_id == case_id,
            models.RelationGraphEdge.graph_id == graph_id)).scalars().all()}
    sources = session.execute(
        select(models.GraphSource).where(models.GraphSource.case_id == case_id)).scalars().all()
    source_ids = {str(row.id) for row in sources
                  if (row.entity_id is not None and str(row.entity_id) in entity_ids)
                  or (row.relation_id is not None and str(row.relation_id) in relation_ids)}
    # Materials whose labels are displayed in this graph: a pending catalog
    # change on them can move the numbers shown here.
    labels = evidence_catalog.resolve_labels(
        session, case_id, [row for row in sources if str(row.id) in source_ids])
    graph_material_ids = {str(label.material_id) for label in labels.values()
                          if label.material_id is not None}
    bindings = session.execute(
        select(models.EvidenceItemBinding).where(
            models.EvidenceItemBinding.case_id == case_id)).scalars().all()
    scoped: list[models.GraphChange] = []
    for change in domain_changes.pending_changes(session, models.GraphChange, case_id):
        relevant = False
        for item in change.diff:
            target, item_id = item["target"], str(item["id"])
            record = item.get("after") or item.get("before") or {}
            if target == "entity" and item_id in entity_ids:
                relevant = True
            elif target == "relation" and item_id in relation_ids:
                relevant = True
            elif target == "source" and item_id in source_ids:
                relevant = True
            elif target == "graph" and item_id == str(graph_id):
                relevant = True
            elif target == "alias" and str(record.get("entity_id", "")) in entity_ids:
                relevant = True
            elif target in ("graph_node", "graph_edge", "graph_group") \
                    and str(record.get("graph_id", "")) == str(graph_id):
                relevant = True
            if relevant:
                break
        if relevant:
            scoped.append(change)
    for change in domain_changes.pending_changes(session, models.EvidenceChange, case_id):
        relevant = False
        for item in change.diff:
            target = item["target"]
            record = item.get("after") or item.get("before") or {}
            if target == "evidence_binding":
                if str(record.get("material_id", "")) in graph_material_ids:
                    relevant = True
            elif target == "evidence_item":
                if any(str(binding.item_id) == str(item["id"])
                       and str(binding.material_id) in graph_material_ids
                       for binding in bindings):
                    relevant = True
            elif target == "evidence_group":
                # 组的新增/删除/重排会移动其后所有组号；保守计入提示。
                relevant = True
            if relevant:
                break
        if relevant:
            scoped.append(change)
    return scoped


def _build_payload(session: Session, case_id: uuid.UUID, graph: models.RelationGraph,
                   entries: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, Any]:
    """Freeze one graph from the *confirmed* state (D3).

    Every pool (entities, relations, aliases, sources, membership, groups and
    the graph title itself) is read through the confirmed-state view: pending
    additions are absent, stacked pending edits roll back to the earliest
    confirmed value, later human edits keep the human value.
    """
    entities = _confirmed_rows(session, case_id, models.CaseEntity, "entity",
                               _entity_dict, entries)
    relations = _confirmed_rows(session, case_id, models.CaseRelation, "relation",
                                _relation_dict, entries)
    aliases_confirmed = _confirmed_rows(session, case_id, models.EntityAlias, "alias",
                                        _alias_dict, entries)
    sources_confirmed = _confirmed_rows(session, case_id, models.GraphSource, "source",
                                        _source_dict, entries)
    groups_confirmed = _confirmed_rows(session, case_id, models.RelationGraphGroup,
                                       "graph_group", _group_dict, entries)
    groups = {gid: spec for gid, spec in groups_confirmed.items()
              if spec["graph_id"] == str(graph.id)}
    group_ids = set(groups)
    nodes_confirmed = _confirmed_rows(session, case_id, models.RelationGraphNode,
                                      "graph_node", _node_dict, entries)
    nodes = [spec for spec in nodes_confirmed.values()
             if spec["graph_id"] == str(graph.id)
             and uuid.UUID(spec["entity_id"]) in entities]
    member_ids = {uuid.UUID(spec["entity_id"]) for spec in nodes}
    edges_confirmed = _confirmed_rows(session, case_id, models.RelationGraphEdge,
                                      "graph_edge", _edge_dict, entries)
    edges = []
    for spec in edges_confirmed.values():
        if spec["graph_id"] != str(graph.id):
            continue
        relation_id = uuid.UUID(spec["relation_id"])
        relation = relations.get(relation_id)
        if relation is None:
            continue
        if (uuid.UUID(relation["from_entity_id"]) in member_ids
                and uuid.UUID(relation["to_entity_id"]) in member_ids):
            edges.append(spec)
    aliases: dict[uuid.UUID, list[str]] = {}
    for spec in aliases_confirmed.values():
        aliases.setdefault(uuid.UUID(spec["entity_id"]), []).append(spec["alias"])
    # Sources resolve against live rows (refs are immutable); only confirmed
    # sources take part.  Labels come from the confirmed catalog only (D2).
    source_rows = {row.id: row for row in session.execute(
        select(models.GraphSource).where(
            models.GraphSource.case_id == case_id,
            models.GraphSource.removed_at.is_(None))).scalars().all()}
    sources_by_entity: dict[uuid.UUID, list[models.GraphSource]] = {}
    sources_by_relation: dict[uuid.UUID, list[models.GraphSource]] = {}
    all_sources: list[models.GraphSource] = []
    for source_id in sources_confirmed:
        row = source_rows.get(source_id)
        if row is None:
            continue
        all_sources.append(row)
        if row.entity_id is not None and row.entity_id in member_ids:
            sources_by_entity.setdefault(row.entity_id, []).append(row)
        if row.relation_id is not None:
            sources_by_relation.setdefault(row.relation_id, []).append(row)
    labels = evidence_catalog.resolve_labels(session, case_id, all_sources)

    def source_payload(row: models.GraphSource) -> dict[str, Any]:
        ref = {"segment_id": str(row.segment_id) if row.segment_id else None,
               "material_id": str(row.material_id) if row.material_id else None,
               "annotation_id": str(row.annotation_id) if row.annotation_id else None}
        return {"source_id": str(row.id), "ref": ref, "proves": row.proves,
                "label": labels[str(row.id)].text}

    payload_nodes = []
    for node in sorted(nodes, key=lambda spec: spec["entity_id"]):
        entity = entities[uuid.UUID(node["entity_id"])]
        group_id = uuid.UUID(node["group_id"]) if node.get("group_id") else None
        payload_nodes.append({
            "entity_id": entity["id"], "name": entity["name"], "kind": entity["kind"],
            "roles": list(entity["roles"] or []),
            "aliases": sorted(aliases.get(uuid.UUID(entity["id"]), [])),
            "x": node["x"], "y": node["y"],
            "group_id": str(group_id) if group_id in group_ids else None,
            "sources": [source_payload(row)
                        for row in sorted(sources_by_entity.get(uuid.UUID(entity["id"]), []),
                                          key=lambda row: str(row.id))],
        })
    payload_edges = []
    for edge in sorted(edges, key=lambda spec: spec["relation_id"]):
        relation = relations[uuid.UUID(edge["relation_id"])]
        payload_edges.append({
            "relation_id": relation["id"],
            "from_entity_id": relation["from_entity_id"],
            "to_entity_id": relation["to_entity_id"],
            "directed": relation["directed"], "kind": relation["kind"],
            "label": relation["label"], "credibility": relation["credibility"],
            "attrs": dict(relation["attrs"] or {}), "note": relation["note"],
            "sources": [source_payload(row)
                        for row in sorted(sources_by_relation.get(uuid.UUID(relation["id"]), []),
                                          key=lambda row: str(row.id))],
        })
    payload_groups = [{
        "group_id": spec["id"], "title": spec["title"], "color": spec["color"],
        "x": spec["x"], "y": spec["y"], "width": spec["width"], "height": spec["height"],
        "collapsed": spec["collapsed"],
    } for spec in sorted(groups.values(), key=lambda spec: spec["id"])]
    confirmed_graph = _confirmed_record(_graph_dict(graph),
                                        entries.get(("graph", str(graph.id)))) or _graph_dict(graph)
    return {
        "schema": 1,
        "graph": {"id": str(graph.id), "title": confirmed_graph["title"],
                  "description": confirmed_graph["description"]},
        "nodes": payload_nodes, "edges": payload_edges, "groups": payload_groups,
    }


def create_snapshot(session: Session, payload: CreateSnapshotInput, *, actor: str,
                    entry: str) -> GraphWorkspace:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "快照冻结仅限人工入口")
    _begin(session, payload.case_id, actor, entry, "create_snapshot")
    graph = _graph_or_error(session, payload.case_id, payload.graph_id)
    stage = payload.stage_label.strip()
    if not graph_rules.valid_snapshot_stage(graph_rules.get_rules(), stage):
        raise _err(ErrorCode.INVALID_INPUT, "阶段标签不能为空且不超过 100 字")
    note = _clean_text(payload.note, limit=graph_rules.SNAPSHOT_NOTE_MAX, field="快照说明")
    if payload.progress_node_id is not None:
        node = session.get(models.ProgressNode, payload.progress_node_id)
        if node is None:
            raise _err(ErrorCode.NOT_FOUND, "进度节点不存在")
        if node.case_id != payload.case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "进度节点不属于本案")
    if ("graph", str(graph.id)) in _pending_additions(session, payload.case_id):
        raise _err(ErrorCode.CONFLICT,
                   "该关系图是 Agent 待确认新建：请先在待确认列表中确认后再冻结快照")
    scoped = _pending_scope(session, payload.case_id, graph.id)
    if scoped and not payload.acknowledge_pending:
        raise _err(ErrorCode.CONFLICT,
                   f"本图有 {len(scoped)} 条待确认的 Agent 变更：确认后冻结可包含，"
                   "或选择「按已确认状态继续冻结」",
                   pending_count=len(scoped))
    payload_json = _build_payload(session, payload.case_id, graph,
                                  _pending_entries(session, payload.case_id))
    # 不变量：快照里的实线（有证据）必须带真实出处，绝不冻结无出处的「有证据」边。
    unsourced = [edge for edge in payload_json["edges"]
                 if edge["credibility"] == "evidenced" and not edge["sources"]]
    if unsourced:
        raise _err(ErrorCode.INVALID_INPUT,
                   f"快照不能包含没有有效出处的「有证据」关系（{len(unsourced)} 条）："
                   "请先确认出处或调整该关系的可信状态后重试")
    version_no = 1 + (session.execute(
        select(models.RelationGraphSnapshot.version_no).where(
            models.RelationGraphSnapshot.case_id == payload.case_id,
            models.RelationGraphSnapshot.graph_id == graph.id)
        .order_by(models.RelationGraphSnapshot.version_no.desc()).limit(1)
    ).scalar_one_or_none() or 0)
    row = models.RelationGraphSnapshot(
        id=uuid.uuid4(), case_id=payload.case_id, graph_id=graph.id,
        version_no=version_no, stage_label=stage, note=note,
        progress_node_id=payload.progress_node_id, payload=payload_json,
        excluded_pending_count=len(scoped), created_by="human", created_at=_now(),
    )
    session.add(row)
    session.flush()
    change = _record(session, payload.case_id, "create_snapshot", actor, entry,
                     [_diff("snapshot", row.id, None, _snapshot_dict(row))])
    _audit(session, "create_snapshot", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id,
                   f"已冻结 v{version_no}（{stage}）"
                   + (f"，按已确认状态生成，排除 {len(scoped)} 条待确认变更" if scoped else ""))


def _current_payload(session: Session, case_id: uuid.UUID,
                     graph_id: uuid.UUID) -> dict[str, Any]:
    graph = _graph_or_error(session, case_id, graph_id)
    return _build_payload(session, case_id, graph, _pending_entries(session, case_id))


def restore_snapshot(session: Session, payload: RestoreSnapshotInput, *, actor: str,
                     entry: str) -> GraphWorkspace:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "快照恢复仅限人工入口")
    _begin(session, payload.case_id, actor, entry, "restore_snapshot")
    snapshot = _snapshot_or_error(session, payload.case_id, payload.snapshot_id)
    graph = _graph_or_error(session, payload.case_id, snapshot.graph_id)
    scoped = _pending_scope(session, payload.case_id, graph.id)
    if scoped:
        raise _err(ErrorCode.CONFLICT,
                   f"本图有 {len(scoped)} 条 Agent 待确认变更，请先复核（确认或撤回）后再恢复快照",
                   pending_count=len(scoped))
    snapshot_payload = snapshot.payload
    diff: list[dict[str, Any]] = []
    skipped = 0
    restored_sources = 0
    timestamp = _now()

    def restore_source(entry_source: dict[str, Any], entity_id: uuid.UUID | None,
                       relation_id: uuid.UUID | None) -> None:
        nonlocal skipped, restored_sources
        ref = entry_source.get("ref") or {}
        ref_id = ref.get("segment_id") or ref.get("material_id") or ref.get("annotation_id")
        model = (models.Segment if ref.get("segment_id") else models.Material
                 if ref.get("material_id") else models.Annotation)
        if ref_id is None or session.get(model, uuid.UUID(ref_id)) is None:
            skipped += 1
            return
        source_id = uuid.UUID(entry_source["source_id"])
        existing = session.get(models.GraphSource, source_id)
        if existing is None:
            row = models.GraphSource(
                id=source_id, case_id=payload.case_id, entity_id=entity_id,
                relation_id=relation_id, segment_id=ref.get("segment_id"),
                material_id=ref.get("material_id"), annotation_id=ref.get("annotation_id"),
                proves=entry_source.get("proves"), created_by="human",
                removed_at=None, created_at=timestamp,
            )
            session.add(row)
            session.flush()
            diff.append(_diff("source", row.id, None, _source_dict(row)))
            restored_sources += 1
        elif existing.removed_at is not None:
            before = _source_dict(existing)
            existing.removed_at = None
            session.flush()
            diff.append(_diff("source", existing.id, before, _source_dict(existing)))
            restored_sources += 1

    available_entities: set[uuid.UUID] = set()
    for node in snapshot_payload.get("nodes", []):
        entity = session.get(models.CaseEntity, uuid.UUID(node["entity_id"]))
        if entity is None or entity.case_id != payload.case_id:
            skipped += 1
            continue
        available_entities.add(entity.id)
        if entity.removed_at is not None:
            before = _entity_dict(entity)
            before_rev = entity.revision
            entity.removed_at = None
            entity.revision += 1
            entity.updated_by = actor
            entity.updated_at = timestamp
            diff.append(_diff("entity", entity.id, before, _entity_dict(entity),
                              before_rev, entity.revision))
        for alias_text in node.get("aliases", []):
            exists = session.execute(
                select(models.EntityAlias.id).where(
                    models.EntityAlias.case_id == payload.case_id,
                    models.EntityAlias.entity_id == entity.id,
                    models.EntityAlias.removed_at.is_(None),
                    models.EntityAlias.alias == alias_text)
            ).scalar_one_or_none()
            if exists is not None:
                continue
            row = models.EntityAlias(
                id=uuid.uuid4(), case_id=payload.case_id, entity_id=entity.id,
                alias=alias_text, created_by="human", removed_at=None,
                created_at=timestamp,
            )
            session.add(row)
            session.flush()
            diff.append(_diff("alias", row.id, None, _alias_dict(row)))
        for entry_source in node.get("sources", []):
            restore_source(entry_source, entity.id, None)

    available_relations: set[uuid.UUID] = set()
    for edge in snapshot_payload.get("edges", []):
        relation = session.get(models.CaseRelation, uuid.UUID(edge["relation_id"]))
        if (relation is None or relation.case_id != payload.case_id
                or uuid.UUID(edge["from_entity_id"]) not in available_entities
                or uuid.UUID(edge["to_entity_id"]) not in available_entities):
            skipped += 1
            continue
        available_relations.add(relation.id)
        if relation.removed_at is not None:
            before = _relation_dict(relation)
            before_rev = relation.revision
            relation.removed_at = None
            relation.revision += 1
            relation.updated_by = actor
            relation.updated_at = timestamp
            diff.append(_diff("relation", relation.id, before, _relation_dict(relation),
                              before_rev, relation.revision))
        before = _relation_dict(relation)
        before_rev = relation.revision
        changed = (relation.credibility != edge["credibility"]
                   or relation.label != edge.get("label")
                   or relation.kind != edge["kind"]
                   or relation.directed != edge.get("directed", True)
                   or (relation.attrs or {}) != (edge.get("attrs") or {})
                   or relation.note != edge.get("note"))
        if changed:
            relation.credibility = edge["credibility"]
            relation.label = edge.get("label")
            relation.kind = edge["kind"]
            relation.directed = edge.get("directed", True)
            relation.attrs = edge.get("attrs") or {}
            relation.note = edge.get("note")
            relation.revision += 1
            relation.updated_by = actor
            relation.updated_at = timestamp
            diff.append(_diff("relation", relation.id, before, _relation_dict(relation),
                              before_rev, relation.revision))
        for entry_source in edge.get("sources", []):
            restore_source(entry_source, None, relation.id)

    available_groups: set[uuid.UUID] = set()
    for group_spec in snapshot_payload.get("groups", []):
        group = session.get(models.RelationGraphGroup, uuid.UUID(group_spec["group_id"]))
        if group is None:
            group = models.RelationGraphGroup(
                id=uuid.UUID(group_spec["group_id"]), case_id=payload.case_id,
                graph_id=graph.id, title=group_spec["title"], color=group_spec.get("color"),
                x=group_spec.get("x", 0), y=group_spec.get("y", 0),
                width=group_spec.get("width", 360), height=group_spec.get("height", 220),
                collapsed=group_spec.get("collapsed", False), revision=0,
                removed_at=None, created_at=timestamp, updated_at=timestamp,
            )
            session.add(group)
            session.flush()
            diff.append(_diff("graph_group", group.id, None, _group_dict(group)))
            available_groups.add(group.id)
            continue
        if group.case_id != payload.case_id or group.graph_id != graph.id:
            skipped += 1
            continue
        if group.removed_at is not None:
            before = _group_dict(group)
            group.removed_at = None
            group.revision += 1
            group.updated_at = timestamp
            diff.append(_diff("graph_group", group.id, before, _group_dict(group),
                              before["revision"], group.revision))
        available_groups.add(group.id)
        before = _group_dict(group)
        before_rev = group.revision
        if (group.title, group.color, group.x, group.y, group.width, group.height,
                group.collapsed) != (group_spec["title"], group_spec.get("color"),
                                     group_spec.get("x", 0), group_spec.get("y", 0),
                                     group_spec.get("width", 360), group_spec.get("height", 220),
                                     group_spec.get("collapsed", False)):
            group.title = group_spec["title"]
            group.color = group_spec.get("color")
            group.x, group.y = group_spec.get("x", 0), group_spec.get("y", 0)
            group.width = group_spec.get("width", 360)
            group.height = group_spec.get("height", 220)
            group.collapsed = group_spec.get("collapsed", False)
            group.revision += 1
            group.updated_at = timestamp
            diff.append(_diff("graph_group", group.id, before, _group_dict(group),
                              before_rev, group.revision))

    wanted_entities: set[uuid.UUID] = set()
    for node_spec in snapshot_payload.get("nodes", []):
        entity_id = uuid.UUID(node_spec["entity_id"])
        if entity_id not in available_entities:
            continue
        wanted_entities.add(entity_id)
        node = _node_for(session, payload.case_id, graph.id, entity_id)
        group_id = (uuid.UUID(node_spec["group_id"])
                    if node_spec.get("group_id") in {str(g) for g in available_groups} else None)
        if node is None:
            row = models.RelationGraphNode(
                id=uuid.uuid4(), case_id=payload.case_id, graph_id=graph.id,
                entity_id=entity_id, group_id=group_id, x=node_spec.get("x", 0),
                y=node_spec.get("y", 0), revision=0, created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(row)
            session.flush()
            diff.append(_diff("graph_node", row.id, None, _node_dict(row)))
            continue
        if (node.x, node.y, node.group_id) != (node_spec.get("x", 0),
                                               node_spec.get("y", 0), group_id):
            before = _node_dict(node)
            before_rev = node.revision
            node.x, node.y = node_spec.get("x", 0), node_spec.get("y", 0)
            node.group_id = group_id
            node.revision += 1
            node.updated_at = timestamp
            diff.append(_diff("graph_node", node.id, before, _node_dict(node),
                              before_rev, node.revision))
    for node in session.execute(select(models.RelationGraphNode).where(
            models.RelationGraphNode.case_id == payload.case_id,
            models.RelationGraphNode.graph_id == graph.id)).scalars().all():
        if node.entity_id not in wanted_entities:
            diff.append(_diff("graph_node", node.id, _node_dict(node), None))
            session.delete(node)

    wanted_relations: set[uuid.UUID] = set()
    for edge_spec in snapshot_payload.get("edges", []):
        relation_id = uuid.UUID(edge_spec["relation_id"])
        relation = session.get(models.CaseRelation, relation_id)
        if (relation is None or relation.removed_at is not None
                or relation_id not in available_relations
                or relation.from_entity_id not in wanted_entities
                or relation.to_entity_id not in wanted_entities):
            continue
        wanted_relations.add(relation_id)
        _ensure_edge(session, payload.case_id, graph.id, relation_id, diff)
    for edge in session.execute(select(models.RelationGraphEdge).where(
            models.RelationGraphEdge.case_id == payload.case_id,
            models.RelationGraphEdge.graph_id == graph.id)).scalars().all():
        if edge.relation_id not in wanted_relations:
            diff.append(_diff("graph_edge", edge.id, _edge_dict(edge), None))
            session.delete(edge)

    # Defensive: an "evidenced" relation restored without any surviving source
    # would violate rule 3.  Downgrade explicitly and say so (never silently).
    downgraded = 0
    for relation in session.execute(select(models.CaseRelation).where(
            models.CaseRelation.case_id == payload.case_id,
            models.CaseRelation.removed_at.is_(None))).scalars().all():
        if relation.credibility != "evidenced":
            continue
        if _active_sources(session, payload.case_id, relation_id=relation.id):
            continue
        before = _relation_dict(relation)
        before_rev = relation.revision
        relation.credibility = "suspected"
        relation.revision += 1
        relation.updated_by = actor
        relation.updated_at = timestamp
        diff.append(_diff("relation", relation.id, before, _relation_dict(relation),
                          before_rev, relation.revision))
        downgraded += 1
    # 覆盖语义：冻结后才添加的出处（属于本图主体/关系）一并移除，保证工作稿与快照一致。
    payload_source_ids = {
        source["source_id"]
        for holder in (snapshot_payload.get("nodes", []) + snapshot_payload.get("edges", []))
        for source in holder.get("sources", [])}
    for source in session.execute(select(models.GraphSource).where(
            models.GraphSource.case_id == payload.case_id,
            models.GraphSource.removed_at.is_(None))).scalars().all():
        in_graph = ((source.entity_id is not None and source.entity_id in wanted_entities)
                    or (source.relation_id is not None
                        and source.relation_id in wanted_relations))
        if not in_graph or str(source.id) in payload_source_ids:
            continue
        before = _source_dict(source)
        source.removed_at = timestamp
        diff.append(_diff("source", source.id, before, _source_dict(source)))
    session.flush()
    # 同一对象在恢复中可能被多次触碰（复活 + 字段覆盖 + 降级）：合并为一条净变化，
    # 避免撤回时旧 diff 逆写把中间态当成最终态。
    diff = _merge_diff(diff)
    change = _record(session, payload.case_id, "restore_snapshot", actor, entry, diff)
    _audit(session, "restore_snapshot", payload.case_id, snapshot.id)
    parts = [f"已按快照 v{snapshot.version_no} 恢复工作稿（可撤回）"]
    if restored_sources:
        parts.append(f"恢复出处 {restored_sources} 条")
    if skipped:
        parts.append(f"跳过 {skipped} 处已失效引用")
    if downgraded:
        parts.append(f"{downgraded} 条「有证据」关系因出处不存在已降级为待查")
    return _finish(session, payload.case_id, change.id, "；".join(parts))


def add_snapshot_consent(session: Session, payload: AddConsentInput, *, actor: str,
                         entry: str) -> GraphWorkspace:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "客户同意登记仅限人工入口")
    rules = _begin(session, payload.case_id, actor, entry, "add_snapshot_consent")
    snapshot = _snapshot_or_error(session, payload.case_id, payload.snapshot_id)
    if not graph_rules.valid_consent_channel(rules, payload.channel):
        raise _err(ErrorCode.INVALID_INPUT, f"未知同意方式：{payload.channel}")
    consenter = _clean_text(payload.consenter, limit=graph_rules.CONSENTER_MAX,
                            field="同意人", allow_none=False)
    detail = _clean_text(payload.source_detail, limit=graph_rules.SOURCE_DETAIL_MAX,
                         field="来源说明", allow_none=False)
    note = _clean_text(payload.note, limit=graph_rules.CONSENT_NOTE_MAX, field="备注")
    parent_id = payload.parent_consent_id
    if payload.action == "confirm":
        if parent_id is not None:
            raise _err(ErrorCode.INVALID_INPUT, "首次同意不能指向原记录")
    else:
        if parent_id is None:
            raise _err(ErrorCode.INVALID_INPUT, "更正或撤回必须指向原同意记录")
        parent = session.get(models.SnapshotConsent, parent_id)
        if parent is None or parent.case_id != payload.case_id:
            raise _err(ErrorCode.NOT_FOUND, "原同意记录不存在")
        if parent.snapshot_id != snapshot.id:
            raise _err(ErrorCode.CASE_MISMATCH, "原同意记录不属于该快照")
    if payload.evidence_material_id is not None:
        material = session.get(models.Material, payload.evidence_material_id)
        if material is None:
            raise _err(ErrorCode.NOT_FOUND, "凭证材料不存在")
        if material.case_id != payload.case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "凭证材料不属于本案")
    row = models.SnapshotConsent(
        id=uuid.uuid4(), case_id=payload.case_id, snapshot_id=snapshot.id,
        action=payload.action, parent_consent_id=parent_id, consenter=consenter,
        consented_at=payload.consented_at, channel=payload.channel,
        source_detail=detail, evidence_material_id=payload.evidence_material_id,
        note=note, created_by="human", created_at=_now(),
    )
    session.add(row)
    session.flush()
    change = _record(session, payload.case_id, "add_snapshot_consent", actor, entry,
                     [_diff("consent", row.id, None, _consent_dict(row))])
    _audit(session, "add_snapshot_consent", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id,
                   "已追加客户同意记录；原记录保留，更正与撤回以新记录留痕")


# --- snapshot comparison ----------------------------------------------------

def diff_snapshots(session: Session, case_id: uuid.UUID, base_id: uuid.UUID,
                   target_id: uuid.UUID | None = None) -> SnapshotDiffOut:
    """Compare two snapshots, or a snapshot against the current working draft."""
    cases.lock_case_shared(session, case_id)
    base = _snapshot_or_error(session, case_id, base_id)
    if target_id is not None:
        target_row = _snapshot_or_error(session, case_id, target_id)
        target_payload = target_row.payload
        target_key = str(target_row.id)
    else:
        target_payload = _current_payload(session, case_id, base.graph_id)
        target_key = "current"
    entries: list[SnapshotDiffEntry] = []

    def index(payload: dict[str, Any], key: str, id_field: str) -> dict[str, dict[str, Any]]:
        return {str(item[id_field]): item for item in payload.get(key, [])}

    for key, target in (("nodes", "entity"), ("edges", "relation"),
                        ("groups", "group")):
        base_items = index(base.payload, key, "group_id" if key == "groups" else
                           "entity_id" if key == "nodes" else "relation_id")
        target_items = index(target_payload, key, "group_id" if key == "groups" else
                             "entity_id" if key == "nodes" else "relation_id")
        for item_id in sorted(set(base_items) | set(target_items)):
            before, after = base_items.get(item_id), target_items.get(item_id)
            if before is None:
                entries.append(SnapshotDiffEntry(target=target, id=item_id,
                                                 change="added", before=None, after=after))
            elif after is None:
                entries.append(SnapshotDiffEntry(target=target, id=item_id,
                                                 change="removed", before=before, after=None))
            elif before != after:
                entries.append(SnapshotDiffEntry(target=target, id=item_id,
                                                 change="changed", before=before, after=after))
    # Sources are compared across all nodes/edges.
    def sources(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for node in payload.get("nodes", []):
            for source in node.get("sources", []):
                result[str(source["source_id"])] = {**source, "owner": node["entity_id"]}
        for edge in payload.get("edges", []):
            for source in edge.get("sources", []):
                result[str(source["source_id"])] = {**source, "owner": edge["relation_id"]}
        return result

    base_sources, target_sources = sources(base.payload), sources(target_payload)
    for source_id in sorted(set(base_sources) | set(target_sources)):
        before, after = base_sources.get(source_id), target_sources.get(source_id)
        if before is None:
            entries.append(SnapshotDiffEntry(target="source", id=source_id, change="added",
                                             before=None, after=after))
        elif after is None:
            entries.append(SnapshotDiffEntry(target="source", id=source_id, change="removed",
                                             before=before, after=None))
        elif before != after:
            entries.append(SnapshotDiffEntry(target="source", id=source_id, change="changed",
                                             before=before, after=after))
    # Layout moves (same node, different coordinates).
    base_layout = {str(node["entity_id"]): (node.get("x"), node.get("y"), node.get("group_id"))
                   for node in base.payload.get("nodes", [])}
    target_layout = {str(node["entity_id"]): (node.get("x"), node.get("y"), node.get("group_id"))
                     for node in target_payload.get("nodes", [])}
    for entity_id in sorted(set(base_layout) & set(target_layout)):
        if base_layout[entity_id] != target_layout[entity_id]:
            entries.append(SnapshotDiffEntry(
                target="layout", id=entity_id, change="changed",
                before={"x": base_layout[entity_id][0], "y": base_layout[entity_id][1],
                        "group_id": base_layout[entity_id][2]},
                after={"x": target_layout[entity_id][0], "y": target_layout[entity_id][1],
                       "group_id": target_layout[entity_id][2]}))
    drift: list[SnapshotLabelDrift] = []
    current_sources = {str(row.id): row for row in session.execute(
        select(models.GraphSource).where(
            models.GraphSource.case_id == case_id,
            models.GraphSource.removed_at.is_(None))).scalars().all()}
    if base_sources:
        current_labels = evidence_catalog.resolve_labels(
            session, case_id,
            [current_sources[source_id] for source_id in base_sources
             if source_id in current_sources])
        for source_id, frozen in sorted(base_sources.items()):
            resolved = current_labels.get(source_id)
            current_text = resolved.text if resolved is not None else None
            if current_text != frozen.get("label"):
                drift.append(SnapshotLabelDrift(source_id=source_id,
                                                frozen=frozen.get("label") or "",
                                                current=current_text))
    return SnapshotDiffOut(base=str(base.id), target=target_key, entries=entries,
                           label_drift=drift)


# --- review of agent changes ------------------------------------------------

_META_FIELDS = {"revision", "updated_at", "updated_by", "created_at"}
_REVIEW_TARGETS = ("entity", "alias", "relation", "source", "graph",
                   "graph_node", "graph_edge", "graph_group")
# 完整 ISO 时间戳（必须带 T 与时分秒），避免把普通日期形文本当时间归一化。
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$")


def _norm_value(value: Any) -> Any:
    """时间值统一成 UTC ISO 再比较（psycopg 会话时区 vs diff 里的 UTC）。

    只对 datetime 与「整串就是 ISO 时间戳」的字符串做归一化；普通文本
    （如恰好形如日期的标签）原样返回，避免误吞业务字段。
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
    fields: set[str] = set()
    for key in set(before) | set(after):
        if key in _META_FIELDS:
            continue
        if _norm_value(before.get(key)) != _norm_value(after.get(key)):
            fields.add(key)
    return fields


def _entries_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["target"] != right["target"] or str(left["id"]) != str(right["id"]):
        return False
    if not (left.get("before") and left.get("after")
            and right.get("before") and right.get("after")):
        # 新增/删除按整条对象算。
        return True
    return bool(_changed_fields(left["before"], left["after"])
                & _changed_fields(right["before"], right["after"]))


def _changes_overlap(left: models.GraphChange, right: models.GraphChange) -> bool:
    return any(_entries_overlap(a, b) for a in left.diff for b in right.diff)


def _pending_neighbour(session: Session, case_id: uuid.UUID, change: models.GraphChange,
                       direction: str) -> models.GraphChange | None:
    """同一对象上更早/更晚的待确认变更（复核必须按写入顺序）。"""
    query = select(models.GraphChange).where(
        models.GraphChange.case_id == case_id,
        models.GraphChange.review_state == "pending",
        models.GraphChange.id != change.id)
    query = (query.where(models.GraphChange.seq < change.seq) if direction == "earlier"
             else query.where(models.GraphChange.seq > change.seq))
    for other in session.execute(query.order_by(models.GraphChange.seq)).scalars().all():
        if _changes_overlap(change, other):
            return other
    return None


def _guard_accept_order(session: Session, case_id: uuid.UUID,
                        change: models.GraphChange) -> None:
    if _pending_neighbour(session, case_id, change, "earlier") is not None:
        raise _err(ErrorCode.CONFLICT,
                   "请先处理更早的待确认变更（按写入顺序复核），再确认本条")


def _superseded_by_human(session: Session, case_id: uuid.UUID,
                         change: models.GraphChange) -> bool:
    """人工写入已改同一字段：旧提案过期，不能再确认（只能撤回）。

    save_layout 只改视图坐标，不否定 Agent 提议的纳入关系；revert_change 是
    复核动作本身，不算人工改写。
    """
    later = session.execute(
        select(models.GraphChange).where(
            models.GraphChange.case_id == case_id,
            models.GraphChange.actor == "human",
            models.GraphChange.seq > change.seq,
            models.GraphChange.command.not_in(
                ("revert_change", "save_layout", "accept_change", "accept_all")))
        .order_by(models.GraphChange.seq)).scalars().all()
    return any(_changes_overlap(change, other) for other in later)


def _guard_superseded(session: Session, case_id: uuid.UUID,
                      change: models.GraphChange) -> None:
    if _superseded_by_human(session, case_id, change):
        raise _err(ErrorCode.CONFLICT,
                   "该对象的相关字段已被人工修改，Agent 变更已过期：请撤回该变更")


def _guard_evidenced_accept(session: Session, case_id: uuid.UUID,
                            accepting: list[models.GraphChange]) -> None:
    """确认「有证据」前必须已有已确认出处；同批确认的出处一并计入。"""
    pending_sources = _pending_additions(session, case_id)
    added: dict[str, set[str]] = {}
    for change in accepting:
        for item in change.diff:
            if item["target"] != "source" or item.get("before") is not None:
                continue
            after = item.get("after") or {}
            relation_id = after.get("relation_id")
            if relation_id:
                added.setdefault(str(relation_id), set()).add(str(item["id"]))
    for change in accepting:
        for item in change.diff:
            if item["target"] != "relation" or not item.get("after"):
                continue
            after = item["after"]
            if after.get("credibility") != "evidenced":
                continue
            relation_id = uuid.UUID(str(item["id"]))
            confirmed = len(_confirmed_sources(session, case_id, relation_id, pending_sources))
            if confirmed + len(added.get(str(relation_id), set())) == 0:
                raise _err(ErrorCode.CONFLICT,
                           "该关系没有已确认的出处：请先确认出处变更，再确认「有证据」升级")


def accept_change(session: Session, payload: GraphChangeIdInput, *, actor: str,
                  entry: str) -> GraphWorkspace:
    _begin_review(session, payload.case_id, actor, "只有承办人可以确认 Agent 修改")
    change = session.get(models.GraphChange, payload.change_id)
    if change is None or change.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在", change_id=str(payload.change_id))
    if change.review_state != "pending":
        raise _err(ErrorCode.INVALID_INPUT, "该变更不处于待确认状态")
    _guard_accept_order(session, payload.case_id, change)
    _guard_superseded(session, payload.case_id, change)
    _guard_evidenced_accept(session, payload.case_id, [change])
    change.review_state = "accepted"
    change.reviewed_at = _now()
    domain_changes.bump_seq(session, payload.case_id, DOMAIN)
    _audit(session, "accept_change", payload.case_id, change.id)
    return _finish(session, payload.case_id)


def accept_all(session: Session, payload: GraphAcceptAllInput, *, actor: str,
               entry: str) -> GraphWorkspace:
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
    query = select(models.GraphChange).where(
        models.GraphChange.case_id == payload.case_id,
        models.GraphChange.review_state == "pending").order_by(models.GraphChange.seq)
    if ids_provided:
        query = query.where(models.GraphChange.id.in_(payload.change_ids))
    pending = session.execute(query).scalars().all()
    if not pending:
        raise _err(ErrorCode.INVALID_INPUT, "没有待确认的 Agent 修改")
    # 已被人工改写覆盖的提案不再确认，逐条提示撤回；同批内按写入顺序整体确认。
    accepting = [change for change in pending
                 if not _superseded_by_human(session, payload.case_id, change)]
    skipped = len(pending) - len(accepting)
    if not accepting:
        raise _err(ErrorCode.CONFLICT, "待确认变更均已被人工修改覆盖：请逐条撤回后再操作")
    _guard_evidenced_accept(session, payload.case_id, accepting)
    timestamp = _now()
    for change in accepting:
        change.review_state = "accepted"
        change.reviewed_at = timestamp
    domain_changes.bump_seq(session, payload.case_id, DOMAIN)
    for change in accepting:
        _audit(session, "accept_all", payload.case_id, change.id)
    notice = f"已确认 {len(accepting)} 条 Agent 修改。" + (
        f"另有 {skipped} 条已被人工修改覆盖，未确认（请撤回）。" if skipped else "")
    return _finish(session, payload.case_id, notice=notice)


def _apply_graph_before(session: Session, case_id: uuid.UUID, item: dict[str, Any],
                        timestamp: datetime, diff_out: list[dict[str, Any]]) -> None:
    target = item["target"]
    row_id = uuid.UUID(item["id"])
    before = item["before"]
    models_by_target = {
        "entity": models.CaseEntity, "alias": models.EntityAlias,
        "relation": models.CaseRelation, "source": models.GraphSource,
        "graph": models.RelationGraph, "graph_group": models.RelationGraphGroup,
    }
    if target in ("graph_node", "graph_edge"):
        model = models.RelationGraphNode if target == "graph_node" else models.RelationGraphEdge
        row = session.get(model, row_id)
        if row is not None and row.case_id != case_id:
            raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理",
                       target=target, id=item["id"])
        if before is None:
            if row is not None:
                diff_out.append(_diff(target, row_id,
                                      _node_dict(row) if target == "graph_node" else _edge_dict(row),
                                      None))
                session.delete(row)
                session.flush()
            return
        if str(before.get("case_id", case_id)) != str(case_id):
            raise _err(ErrorCode.CASE_MISMATCH, "变更记录不属于本案")
        if row is None:
            if target == "graph_node":
                row = models.RelationGraphNode(
                    id=row_id, case_id=case_id, graph_id=uuid.UUID(before["graph_id"]),
                    entity_id=uuid.UUID(before["entity_id"]),
                    group_id=uuid.UUID(before["group_id"]) if before.get("group_id") else None,
                    x=before.get("x", 0), y=before.get("y", 0),
                    revision=before.get("revision", 0) + 1,
                    created_at=datetime.fromisoformat(before["created_at"]),
                    updated_at=timestamp,
                )
            else:
                row = models.RelationGraphEdge(
                    id=row_id, case_id=case_id, graph_id=uuid.UUID(before["graph_id"]),
                    relation_id=uuid.UUID(before["relation_id"]),
                    created_at=datetime.fromisoformat(before["created_at"]),
                )
            session.add(row)
            session.flush()
            diff_out.append(_diff(target, row_id, None,
                                  _node_dict(row) if target == "graph_node" else _edge_dict(row)))
            return
        current = _node_dict(row) if target == "graph_node" else _edge_dict(row)
        for field in ("x", "y", "group_id") if target == "graph_node" else ():
            value = before.get(field)
            setattr(row, field, uuid.UUID(value) if field == "group_id" and value else value)
        if target == "graph_node":
            row.revision += 1
            row.updated_at = timestamp
        session.flush()
        diff_out.append(_diff(target, row_id, current,
                              _node_dict(row) if target == "graph_node" else _edge_dict(row),
                              current.get("revision"), getattr(row, "revision", None)))
        return
    model = models_by_target.get(target)
    if model is None:
        raise _err(ErrorCode.CONFLICT, "该变更不可撤回，请人工处理", target=target)
    row = session.get(model, row_id)
    if row is None or row.case_id != case_id:
        raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理",
                   target=target, id=item["id"])
    dict_of = {"entity": _entity_dict, "alias": _alias_dict, "relation": _relation_dict,
               "source": _source_dict, "graph": _graph_dict,
               "graph_group": _group_dict}[target]
    current = dict_of(row)
    if before is None:
        if hasattr(row, "removed_at"):
            row.removed_at = timestamp
        else:
            raise _err(ErrorCode.CONFLICT, "该变更不可撤回，请人工处理", target=target)
    else:
        for field, value in before.items():
            if field in ("id", "case_id", "created_at", "revision"):
                continue
            if field == "removed_at":
                row.removed_at = datetime.fromisoformat(value) if value else None
            elif field == "roles":
                row.roles = list(value or [])
            elif field == "attrs":
                row.attrs = dict(value or {})
            elif hasattr(row, field):
                setattr(row, field, value)
        if hasattr(row, "revision"):
            row.revision += 1
        if hasattr(row, "updated_at"):
            row.updated_at = timestamp
        if hasattr(row, "updated_by"):
            row.updated_by = "human"
    session.flush()
    after = dict_of(row)
    diff_out.append(_diff(target, row.id, current, after,
                          current.get("revision"), after.get("revision")))


def revert_change(session: Session, payload: GraphChangeIdInput, *, actor: str,
                  entry: str) -> GraphWorkspace:
    _begin_review(session, payload.case_id, actor, "只有承办人可以撤回 Agent 修改")
    change = session.get(models.GraphChange, payload.change_id)
    if change is None or change.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在")
    if change.review_state not in ("pending", "accepted", "none"):
        raise _err(ErrorCode.INVALID_INPUT, "该变更已撤回或不可撤回")
    if any(item["target"] in ("snapshot", "consent") for item in change.diff):
        raise _err(ErrorCode.INVALID_INPUT,
                   "快照与客户同意记录不可撤回：请追加新的快照或同意记录")
    if _pending_neighbour(session, payload.case_id, change, "later") is not None:
        raise _err(ErrorCode.CONFLICT,
                   "请先处理更晚的待确认变更（按写入顺序复核），再撤回本条")
    # 人工已改写同一字段：提案过期，只标记撤回，不改动当前工作稿。
    if _superseded_by_human(session, payload.case_id, change):
        revert_record = _record(session, payload.case_id, "revert_change", actor, entry, [])
        domain_changes.mark_reverted(session, change, revert_record.id)
        _audit(session, "revert_change", payload.case_id, change.id)
        return _finish(session, payload.case_id, revert_record.id,
                       "该变更已被人工修改覆盖，仅标记撤回，当前工作稿未改动。")
    session.expire_all()  # 丢弃 identity map 旧值：守卫必须读最新已提交行
    # Whole-or-nothing: any target edited since the change is a conflict.
    # 同一对象多条 diff（恢复合并前的历史数据）按净 after（最后一条）检查。
    net_items: dict[tuple[str, str], dict[str, Any]] = {}
    for item in change.diff:
        key = (item["target"], str(item["id"]))
        if key not in net_items:
            net_items[key] = dict(item)
        else:
            net_items[key]["after"] = item.get("after")
            net_items[key]["after_rev"] = item.get("after_rev")
    for item in net_items.values():
        _revert_guard(session, payload.case_id, item)
    timestamp = _now()
    reverse_diff: list[dict[str, Any]] = []
    # 先整体计算净逆变化（首 after → 末 before），再一次性应用：恢复命令里同一
    # 对象可能被多次触碰，逐条应用会让后面的守卫看到中间态而误判冲突。
    net_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in change.diff:
        key = (item["target"], str(item["id"]))
        if key not in net_by_key:
            net_by_key[key] = {"target": item["target"], "id": str(item["id"]),
                               "before": item.get("after"), "after": item.get("before"),
                               "before_rev": item.get("after_rev"),
                               "after_rev": item.get("before_rev")}
            order.append(key)
        else:
            net_by_key[key]["after"] = item.get("before")
            net_by_key[key]["after_rev"] = item.get("before_rev")
    for key in order:
        item = net_by_key[key]
        if item["before"] == item["after"]:
            continue
        # 逆变化条目：before=原 after（当前态），after=原 before（要恢复的态）。
        # _apply_graph_before 恢复的是 item["before"]，所以这里交换回来。
        restore_item = {"target": item["target"], "id": item["id"],
                        "before": item["after"], "after": item["before"],
                        "before_rev": item.get("after_rev"),
                        "after_rev": item.get("before_rev")}
        _apply_graph_before(session, payload.case_id, restore_item, timestamp, reverse_diff)
    reverse_diff = _merge_diff(reverse_diff)
    revert_record = _record(session, payload.case_id, "revert_change", actor, entry,
                            reverse_diff)
    domain_changes.mark_reverted(session, change, revert_record.id)
    _audit(session, "revert_change", payload.case_id, change.id)
    return _finish(session, payload.case_id, revert_record.id)


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


def _revert_guard(session: Session, case_id: uuid.UUID, item: dict[str, Any]) -> None:
    target = item["target"]
    row_id = uuid.UUID(str(item["id"]))
    before = item.get("before")
    if target == "entity":
        row = session.get(models.CaseEntity, row_id)
        if row is None or row.case_id != case_id:
            raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
        if before is None:
            linked = session.execute(select(models.CaseRelation.id).where(
                models.CaseRelation.case_id == case_id,
                models.CaseRelation.removed_at.is_(None),
                (models.CaseRelation.from_entity_id == row_id)
                | (models.CaseRelation.to_entity_id == row_id))).first()
            if linked is not None:
                raise _err(ErrorCode.CONFLICT, "该主体已有有效关系，请先撤回相关关系")
            return
        if not _matches_after(_entity_dict(row), item):
            raise _err(ErrorCode.CONFLICT, "主体已被后续修改，无法撤回，请人工处理")
        return
    if target == "relation":
        row = session.get(models.CaseRelation, row_id)
        if row is None or row.case_id != case_id:
            raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
        if before is None:
            if _active_sources(session, case_id, relation_id=row_id):
                raise _err(ErrorCode.CONFLICT, "该关系已有出处，请先撤回出处变更")
            return
        if item.get("after") is None:
            for endpoint_id in (row.from_entity_id, row.to_entity_id):
                endpoint = session.get(models.CaseEntity, endpoint_id)
                if endpoint is None or endpoint.removed_at is not None:
                    raise _err(ErrorCode.CONFLICT, "关系端点已删除，无法撤回该移除")
            return
        if not _matches_after(_relation_dict(row), item):
            raise _err(ErrorCode.CONFLICT, "关系已被后续修改，无法撤回，请人工处理")
        return
    if target == "source":
        row = session.get(models.GraphSource, row_id)
        if row is None or row.case_id != case_id:
            raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
        if before is None:
            # 撤回一条出处：若是「有证据」关系仅剩的有效出处，先要求处理关系状态。
            if row.relation_id is not None:
                relation = session.get(models.CaseRelation, row.relation_id)
                if (relation is not None and relation.removed_at is None
                        and relation.credibility == "evidenced"):
                    remaining = [s for s in _active_sources(
                        session, case_id, relation_id=relation.id) if s.id != row.id]
                    if not remaining:
                        raise _err(ErrorCode.CONFLICT,
                                   "该出处是「有证据」关系的唯一有效出处："
                                   "请先撤回或降低该关系的可信状态，再撤回出处")
            return
        # 出处没有 revision：只比较 removed_at（软删除/恢复是唯一可逆字段）。
        after = item.get("after") or {}
        if _norm_value(row.removed_at.isoformat() if row.removed_at else None) \
                != after.get("removed_at"):
            raise _err(ErrorCode.CONFLICT, "出处已被后续修改，无法撤回，请人工处理")
        return
    if target == "alias":
        row = session.get(models.EntityAlias, row_id)
        if row is None or row.case_id != case_id:
            raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
        if before is not None and not _matches_after(_alias_dict(row), item):
            raise _err(ErrorCode.CONFLICT, "别名已被后续修改，无法撤回，请人工处理")
        return
    if target in ("graph_node", "graph_edge"):
        model = models.RelationGraphNode if target == "graph_node" else models.RelationGraphEdge
        row = session.get(model, row_id)
        if row is not None and row.case_id != case_id:
            raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
        if target == "graph_node" and row is not None and item.get("after_rev") is not None:
            if row.revision != item["after_rev"]:
                raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
        return
    model = {"graph": models.RelationGraph,
             "graph_group": models.RelationGraphGroup}.get(target)
    if model is None:
        raise _err(ErrorCode.CONFLICT, "该变更不可撤回，请人工处理", target=target)
    row = session.get(model, row_id)
    if row is None or row.case_id != case_id:
        raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
    dict_of = _graph_dict if target == "graph" else _group_dict
    if before is not None and not _matches_after(dict_of(row), item):
        raise _err(ErrorCode.CONFLICT, "对象已被后续修改，无法撤回，请人工处理")
