"""Progress HTTP endpoints (actor=human, entry=local-web).

Write commands return the full snapshot; conflicts surface as 409 via the
shared error envelope. Accept/revert are human-only here; MCP/CLI never expose
them. The actor is fixed by this entry point, never taken from the payload.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import progress, progress_rules
from ..schemas import (
    AcceptAllInput, AddNodeInput, AddRelationInput, ChangeIdInput, ChangeSeqsOutput,
    InsertFragmentInput, LinkNodeInput, MoveNodeInput, NodeRevisionInput,
    ProgressRulesOutput, ProgressSnapshot, RemoveRelationInput, SetActiveStageInput,
    UnlinkInput, UpdateNodeInput,
)
from .ids import uuid_of
from .deps import Guarded, get_db, require_module

router = APIRouter(prefix="/api/v1", tags=["progress"],
                   dependencies=[Guarded, Depends(require_module("progress"))])

ACTOR = "human"
ENTRY = "local-web"


@router.get("/progress/rules", response_model=ProgressRulesOutput)
def get_rules() -> ProgressRulesOutput:
    return ProgressRulesOutput(rules=progress_rules.get_rules())


@router.get("/cases/{case_id}/progress", response_model=ProgressSnapshot)
def get_progress(case_id: str, session: Session = Depends(get_db)) -> ProgressSnapshot:
    return progress.get_progress(session, uuid_of(case_id, "case_id"))


@router.get("/cases/{case_id}/changes/seq", response_model=ChangeSeqsOutput)
def get_change_seqs(case_id: str, session: Session = Depends(get_db)) -> ChangeSeqsOutput:
    return progress.get_change_seqs(session, uuid_of(case_id, "case_id"))


@router.post("/cases/{case_id}/progress/nodes", response_model=ProgressSnapshot)
def add_node(case_id: str, payload: AddNodeInput, session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    return progress.add_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/cases/{case_id}/progress/nodes/{node_id}", response_model=ProgressSnapshot)
def update_node(case_id: str, node_id: str, payload: UpdateNodeInput,
                session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(node_id, "node_id") != payload.node_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "节点标识不一致")
    return progress.update_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/nodes/{node_id}/move", response_model=ProgressSnapshot)
def move_node(case_id: str, node_id: str, payload: MoveNodeInput,
              session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(node_id, "node_id") != payload.node_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "节点标识不一致")
    return progress.move_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/active-stage", response_model=ProgressSnapshot)
def set_active_stage(case_id: str, payload: SetActiveStageInput,
                     session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    return progress.set_active_stage(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/fragments", response_model=ProgressSnapshot)
def insert_fragment(case_id: str, payload: InsertFragmentInput,
                    session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    return progress.insert_fragment(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/nodes/{node_id}/remove", response_model=ProgressSnapshot)
def remove_node(case_id: str, node_id: str, payload: NodeRevisionInput,
                session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(node_id, "node_id") != payload.node_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "节点标识不一致")
    return progress.remove_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/nodes/{node_id}/restore", response_model=ProgressSnapshot)
def restore_node(case_id: str, node_id: str, payload: NodeRevisionInput,
                 session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(node_id, "node_id") != payload.node_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "节点标识不一致")
    return progress.restore_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/relations", response_model=ProgressSnapshot)
def add_relation(case_id: str, payload: AddRelationInput,
                 session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    return progress.add_relation(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/relations/{relation_id}/remove", response_model=ProgressSnapshot)
def remove_relation(case_id: str, relation_id: str, payload: RemoveRelationInput,
                    session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(relation_id, "relation_id") != payload.relation_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "关系标识不一致")
    return progress.remove_relation(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/links", response_model=ProgressSnapshot)
def link_node(case_id: str, payload: LinkNodeInput, session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    return progress.link_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/links/{link_id}/remove", response_model=ProgressSnapshot)
def unlink_node(case_id: str, link_id: str, payload: UnlinkInput,
                session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(link_id, "link_id") != payload.link_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "关联标识不一致")
    return progress.unlink_node(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/changes/{change_id}/accept", response_model=ProgressSnapshot)
def accept_change(case_id: str, change_id: str, payload: ChangeIdInput,
                  session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(change_id, "change_id") != payload.change_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "变更标识不一致")
    return progress.accept_change(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/changes/accept-all", response_model=ProgressSnapshot)
def accept_all(case_id: str, payload: AcceptAllInput,
               session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    return progress.accept_all(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/progress/changes/{change_id}/revert", response_model=ProgressSnapshot)
def revert_change(case_id: str, change_id: str, payload: ChangeIdInput,
                  session: Session = Depends(get_db)) -> ProgressSnapshot:
    _check_case(case_id, payload.case_id)
    if uuid_of(change_id, "change_id") != payload.change_id:
        from ..errors import ErrorCode, WorkbenchError
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "变更标识不一致")
    return progress.revert_change(session, payload, actor=ACTOR, entry=ENTRY)


def _check_case(path_case_id: str, payload_case_id) -> None:
    from ..errors import ErrorCode, WorkbenchError
    if uuid_of(path_case_id, "case_id") != payload_case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")