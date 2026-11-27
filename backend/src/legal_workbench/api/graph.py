"""Relation-graph HTTP endpoints (actor=human, entry=local-web).

Every write delegates to the shared ``graph`` service; this module only fixes the
actor, checks that path and body identifiers agree, and returns the full
workspace snapshot.  Freeze/restore, client consent and accept/revert are exposed
here because this is the human entry point — never through MCP/CLI.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from .. import graph, graph_assist, table_graph_import
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (
    AddAliasInput, AddConsentInput, AddEntityInput, AddSourceInput, AddToGraphInput,
    CreateGraphInput, CreateSnapshotInput, EntityRevisionInput, GraphAcceptAllInput,
    AddGraphRelationInput, GraphChangeIdInput, GraphRevisionInput, GraphRulesOutput,
    GraphWorkspace, RelationRevisionInput, RemoveAliasInput, RemoveFromGraphInput,
    RemoveGroupInput, RemoveSourceInput, RemovalImpactOut, RemovalImpactQuery,
    RestoreSnapshotInput, SaveLayoutInput, SearchScope, SetNodeGroupInput, SnapshotDiffOut,
    SnapshotDiffQuery, SnapshotListOutput, SnapshotOut, UpdateEntityInput, UpdateGraphInput,
    UpdateRelationInput, UpsertGroupInput,
)
from ..storage import Storage
from .deps import Guarded, get_db, get_storage, require_module
from .ids import uuid_of

router = APIRouter(prefix="/api/v1", tags=["graph"],
                   dependencies=[Guarded, Depends(require_module("graph"))])

ACTOR = "human"
ENTRY = "local-web"


def _same(*pairs: tuple[str, uuid.UUID]) -> None:
    """Reject path/body identifier drift before any service call."""
    first_label, first = pairs[0]
    for label, value in pairs[1:]:
        if value != first:
            raise WorkbenchError(ErrorCode.CASE_MISMATCH,
                                 f"{label}与{first_label}不一致")


def _check_case(path_case_id: str, payload) -> uuid.UUID:
    case_id = uuid_of(path_case_id, "case_id")
    _same(("路径案件", case_id), ("请求案件", payload.case_id))
    return case_id


@router.get("/graph/rules", response_model=GraphRulesOutput)
def get_graph_rules() -> GraphRulesOutput:
    from .. import graph_rules

    return GraphRulesOutput(rules=graph_rules.get_rules())


@router.get("/cases/{case_id}/graphs", response_model=GraphWorkspace)
def get_workspace(case_id: str, session: Session = Depends(get_db)) -> GraphWorkspace:
    return graph.get_workspace(session, uuid_of(case_id, "case_id"))


@router.get("/cases/{case_id}/graphs/assist/cooccurrence",
            response_model=graph_assist.CooccurrenceOutput)
def cooccurrence(case_id: str, entity_a_id: str, entity_b_id: str,
                 scope: SearchScope = SearchScope.ORIGINALS,
                 limit: int = Query(50, ge=1, le=100),
                 session: Session = Depends(get_db)) -> graph_assist.CooccurrenceOutput:
    return graph_assist.find_entity_cooccurrences(
        session, uuid_of(case_id, "case_id"), uuid_of(entity_a_id, "entity_a_id"),
        uuid_of(entity_b_id, "entity_b_id"), scope=scope, limit=limit)


@router.get("/cases/{case_id}/graphs/assist/segment-hints",
            response_model=graph_assist.SegmentHints)
def segment_hints(case_id: str, segment_id: str,
                  session: Session = Depends(get_db)) -> graph_assist.SegmentHints:
    return graph_assist.match_entities_in_segment(
        session, uuid_of(case_id, "case_id"), uuid_of(segment_id, "segment_id"))


@router.get("/cases/{case_id}/graphs/assist/audit",
            response_model=graph_assist.GraphAuditOutput)
def graph_audit(case_id: str, graph_id: str | None = None,
                min_mentions: int = Query(2, ge=1),
                session: Session = Depends(get_db)) -> graph_assist.GraphAuditOutput:
    return graph_assist.audit_graph_gaps(
        session, uuid_of(case_id, "case_id"),
        graph_id=uuid_of(graph_id, "graph_id") if graph_id is not None else None,
        min_mentions=min_mentions,
    )


@router.get("/cases/{case_id}/graphs/assist/entity-candidates",
            response_model=graph_assist.EntityCandidateOutput)
def entity_candidates(case_id: str, material_ids: list[str] | None = Query(None),
                      session: Session = Depends(get_db)) -> graph_assist.EntityCandidateOutput:
    parsed_ids = [uuid_of(mid, "material_id") for mid in material_ids] if material_ids else None
    return graph_assist.extract_entity_alias_candidates(
        session, uuid_of(case_id, "case_id"), material_ids=parsed_ids,
    )


@router.post("/cases/{case_id}/graphs/import-table/preview",
             response_model=table_graph_import.TablePreviewOutput)
def preview_table_import(case_id: str, payload: table_graph_import.TablePreviewInput,
                         session: Session = Depends(get_db),
                         storage: Storage = Depends(get_storage)
                         ) -> table_graph_import.TablePreviewOutput:
    if payload.material_id is None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "请选择案卷表格材料")
    return table_graph_import.preview_table_columns(
        session, storage, uuid_of(case_id, "case_id"),
        material_id=payload.material_id,
        sheet_name=payload.sheet_name,
        header_row=payload.header_row,
    )


@router.post("/cases/{case_id}/graphs/import-table/preview-upload",
             response_model=table_graph_import.TablePreviewOutput)
async def preview_table_import_upload(
    case_id: str,
    file: UploadFile = File(...),
    sheet_name: str | None = Form(None),
    header_row: int = Form(1, ge=1, le=100),
    session: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> table_graph_import.TablePreviewOutput:
    case_uuid = uuid_of(case_id, "case_id")
    data = await file.read(table_graph_import.MAX_IMPORT_BYTES + 1)
    if len(data) > table_graph_import.MAX_IMPORT_BYTES:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "表格文件超过 10 MB 限制")
    return table_graph_import.preview_table_columns(
        session, storage, case_uuid,
        file_bytes=data,
        filename=file.filename or "",
        sheet_name=sheet_name,
        header_row=header_row,
    )


@router.post("/cases/{case_id}/graphs/import-table/compile",
             response_model=table_graph_import.TableCompileOutput)
def compile_table_import(case_id: str, payload: table_graph_import.TableCompileInput,
                         session: Session = Depends(get_db),
                         storage: Storage = Depends(get_storage)
                         ) -> table_graph_import.TableCompileOutput:
    return table_graph_import.compile_table_relations(
        session, storage, uuid_of(case_id, "case_id"), payload,
    )


@router.post("/cases/{case_id}/graphs/import-table/commit",
             response_model=table_graph_import.TableCommitOutput)
def commit_table_import(case_id: str, payload: table_graph_import.TableCommitInput,
                        session: Session = Depends(get_db),
                        storage: Storage = Depends(get_storage)
                        ) -> table_graph_import.TableCommitOutput:
    return table_graph_import.commit_table_relations(
        session, storage, uuid_of(case_id, "case_id"), payload, actor=ACTOR, entry=ENTRY,
    )


@router.get("/cases/{case_id}/graphs/{graph_id}", response_model=GraphWorkspace)
def get_graph(case_id: str, graph_id: str,
              session: Session = Depends(get_db)) -> GraphWorkspace:
    case_uuid = uuid_of(case_id, "case_id")
    graph_uuid = uuid_of(graph_id, "graph_id")
    workspace = graph.get_workspace(session, case_uuid, graph_uuid)
    if not any(item.id == graph_uuid and item.removed_at is None for item in workspace.graphs):
        raise WorkbenchError(ErrorCode.NOT_FOUND, "关系图不存在", {"graph_id": graph_id})
    return workspace


@router.get("/cases/{case_id}/graphs/{graph_id}/snapshots", response_model=SnapshotListOutput)
def list_snapshots(case_id: str, graph_id: str,
                   session: Session = Depends(get_db)) -> SnapshotListOutput:
    return SnapshotListOutput(snapshots=graph.list_snapshots(
        session, uuid_of(case_id, "case_id"), uuid_of(graph_id, "graph_id")))


@router.get("/cases/{case_id}/snapshots/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(case_id: str, snapshot_id: str,
                 session: Session = Depends(get_db)) -> SnapshotOut:
    return graph.get_snapshot(session, uuid_of(case_id, "case_id"),
                              uuid_of(snapshot_id, "snapshot_id"))


@router.get("/cases/{case_id}/snapshots/{snapshot_id}/diff", response_model=SnapshotDiffOut)
def diff_snapshot(case_id: str, snapshot_id: str,
                  query: SnapshotDiffQuery = Depends(),
                  session: Session = Depends(get_db)) -> SnapshotDiffOut:
    return graph.diff_snapshots(session, uuid_of(case_id, "case_id"),
                                uuid_of(snapshot_id, "snapshot_id"), query.target_id)


@router.get("/cases/{case_id}/graph/removal-impact", response_model=RemovalImpactOut)
def removal_impact(case_id: str, query: RemovalImpactQuery = Depends(),
                   session: Session = Depends(get_db)) -> RemovalImpactOut:
    return graph.get_removal_impact(session, uuid_of(case_id, "case_id"),
                                    entity_id=query.entity_id,
                                    relation_id=query.relation_id)


# --- entity commands --------------------------------------------------------

@router.post("/cases/{case_id}/graph/entities", response_model=GraphWorkspace)
def add_entity(case_id: str, payload: AddEntityInput,
               session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    return graph.add_entity(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/cases/{case_id}/graph/entities/{entity_id}", response_model=GraphWorkspace)
def update_entity(case_id: str, entity_id: str, payload: UpdateEntityInput,
                  session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径主体", uuid_of(entity_id, "entity_id")),
          ("请求主体", payload.entity_id))
    return graph.update_entity(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/entities/{entity_id}/remove",
             response_model=GraphWorkspace)
def remove_entity(case_id: str, entity_id: str, payload: EntityRevisionInput,
                  session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径主体", uuid_of(entity_id, "entity_id")),
          ("请求主体", payload.entity_id))
    return graph.remove_entity(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/entities/{entity_id}/restore",
             response_model=GraphWorkspace)
def restore_entity(case_id: str, entity_id: str, payload: EntityRevisionInput,
                   session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径主体", uuid_of(entity_id, "entity_id")),
          ("请求主体", payload.entity_id))
    return graph.restore_entity(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/entities/{entity_id}/aliases",
             response_model=GraphWorkspace)
def add_alias(case_id: str, entity_id: str, payload: AddAliasInput,
              session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径主体", uuid_of(entity_id, "entity_id")),
          ("请求主体", payload.entity_id))
    return graph.add_alias(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/aliases/{alias_id}/remove",
             response_model=GraphWorkspace)
def remove_alias(case_id: str, alias_id: str, payload: RemoveAliasInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径别名", uuid_of(alias_id, "alias_id")),
          ("请求别名", payload.alias_id))
    return graph.remove_alias(session, payload, actor=ACTOR, entry=ENTRY)


# --- relation and source commands -------------------------------------------

@router.post("/cases/{case_id}/graph/relations", response_model=GraphWorkspace)
def add_relation(case_id: str, payload: AddGraphRelationInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    return graph.add_relation(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/cases/{case_id}/graph/relations/{relation_id}",
              response_model=GraphWorkspace)
def update_relation(case_id: str, relation_id: str, payload: UpdateRelationInput,
                    session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径关系", uuid_of(relation_id, "relation_id")),
          ("请求关系", payload.relation_id))
    return graph.update_relation(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/relations/{relation_id}/remove",
             response_model=GraphWorkspace)
def remove_relation(case_id: str, relation_id: str, payload: RelationRevisionInput,
                    session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径关系", uuid_of(relation_id, "relation_id")),
          ("请求关系", payload.relation_id))
    return graph.remove_relation(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/relations/{relation_id}/restore",
             response_model=GraphWorkspace)
def restore_relation(case_id: str, relation_id: str, payload: RelationRevisionInput,
                     session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径关系", uuid_of(relation_id, "relation_id")),
          ("请求关系", payload.relation_id))
    return graph.restore_relation(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/sources", response_model=GraphWorkspace)
def add_source(case_id: str, payload: AddSourceInput,
               session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    return graph.add_source(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/sources/{source_id}/remove",
             response_model=GraphWorkspace)
def remove_source(case_id: str, source_id: str, payload: RemoveSourceInput,
                  session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径出处", uuid_of(source_id, "source_id")),
          ("请求出处", payload.source_id))
    return graph.remove_source(session, payload, actor=ACTOR, entry=ENTRY)


# --- graphs, membership, layout and groups ----------------------------------

@router.post("/cases/{case_id}/graph/graphs", response_model=GraphWorkspace)
def create_graph(case_id: str, payload: CreateGraphInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    return graph.create_graph(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/cases/{case_id}/graph/graphs/{graph_id}", response_model=GraphWorkspace)
def update_graph(case_id: str, graph_id: str, payload: UpdateGraphInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.update_graph(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/graphs/{graph_id}/remove",
             response_model=GraphWorkspace)
def remove_graph(case_id: str, graph_id: str, payload: GraphRevisionInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.remove_graph(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/graphs/{graph_id}/members",
             response_model=GraphWorkspace)
def add_to_graph(case_id: str, graph_id: str, payload: AddToGraphInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.add_to_graph(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/graphs/{graph_id}/members/remove",
             response_model=GraphWorkspace)
def remove_from_graph(case_id: str, graph_id: str, payload: RemoveFromGraphInput,
                      session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.remove_from_graph(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/graphs/{graph_id}/layout",
             response_model=GraphWorkspace)
def save_layout(case_id: str, graph_id: str, payload: SaveLayoutInput,
                session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.save_layout(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/graphs/{graph_id}/groups",
             response_model=GraphWorkspace)
def upsert_group(case_id: str, graph_id: str, payload: UpsertGroupInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.upsert_group(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/groups/{group_id}/remove",
             response_model=GraphWorkspace)
def remove_group(case_id: str, group_id: str, payload: RemoveGroupInput,
                 session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径分组", uuid_of(group_id, "group_id")),
          ("请求分组", payload.group_id))
    return graph.remove_group(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/graphs/{graph_id}/nodes/{entity_id}/group",
             response_model=GraphWorkspace)
def set_node_group(case_id: str, graph_id: str, entity_id: str, payload: SetNodeGroupInput,
                   session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    _same(("路径主体", uuid_of(entity_id, "entity_id")),
          ("请求主体", payload.entity_id))
    return graph.set_node_group(session, payload, actor=ACTOR, entry=ENTRY)


# --- snapshots, consent and review (human only) -----------------------------

@router.post("/cases/{case_id}/graph/graphs/{graph_id}/snapshots",
             response_model=GraphWorkspace)
def create_snapshot(case_id: str, graph_id: str, payload: CreateSnapshotInput,
                    session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径图", uuid_of(graph_id, "graph_id")),
          ("请求图", payload.graph_id))
    return graph.create_snapshot(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/snapshots/{snapshot_id}/restore",
             response_model=GraphWorkspace)
def restore_snapshot(case_id: str, snapshot_id: str, payload: RestoreSnapshotInput,
                     session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径快照", uuid_of(snapshot_id, "snapshot_id")),
          ("请求快照", payload.snapshot_id))
    return graph.restore_snapshot(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/snapshots/{snapshot_id}/consents",
             response_model=GraphWorkspace)
def add_snapshot_consent(case_id: str, snapshot_id: str, payload: AddConsentInput,
                         session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径快照", uuid_of(snapshot_id, "snapshot_id")),
          ("请求快照", payload.snapshot_id))
    return graph.add_snapshot_consent(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/changes/{change_id}/accept",
             response_model=GraphWorkspace)
def accept_change(case_id: str, change_id: str, payload: GraphChangeIdInput,
                  session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径变更", uuid_of(change_id, "change_id")),
          ("请求变更", payload.change_id))
    return graph.accept_change(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/changes/accept-all", response_model=GraphWorkspace)
def accept_all(case_id: str, payload: GraphAcceptAllInput,
               session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    return graph.accept_all(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/graph/changes/{change_id}/revert",
             response_model=GraphWorkspace)
def revert_change(case_id: str, change_id: str, payload: GraphChangeIdInput,
                  session: Session = Depends(get_db)) -> GraphWorkspace:
    _check_case(case_id, payload)
    _same(("路径变更", uuid_of(change_id, "change_id")),
          ("请求变更", payload.change_id))
    return graph.revert_change(session, payload, actor=ACTOR, entry=ENTRY)
