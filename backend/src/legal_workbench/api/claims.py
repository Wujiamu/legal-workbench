"""Claim amount HTTP endpoints (actor=human, entry=local-web).

Mirrors ``api/progress.py``: write commands return the full snapshot, conflicts
surface as 409 through the shared error envelope, and the actor is fixed by this
entry point rather than taken from the payload.  Accept/revert are human-only
here; MCP and CLI never expose them, and the service layer keeps its own
role/ownership guard behind these routes.

Path and body identifiers must agree before the service call: a mismatched
``case_id`` is a cross-case request, and a mismatched group/item/change id would
otherwise let the path describe one target while the body mutates another.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import claims
from ..errors import ErrorCode, WorkbenchError
from ..schemas import (
    AddClaimGroupInput, AddClaimItemInput, ChangeIdInput, ClaimAcceptAllInput,
    ClaimAmountCandidates, ClaimGroupRevisionInput, ClaimItemRevisionInput,
    ClaimSnapshot, ReorderClaimGroupInput, ReorderClaimItemInput,
    UpdateClaimGroupInput, UpdateClaimItemInput,
)
from .deps import Guarded, get_db, require_module
from .ids import uuid_of

router = APIRouter(prefix="/api/v1", tags=["claims"],
                   dependencies=[Guarded, Depends(require_module("claims"))])

ACTOR = "human"
ENTRY = "local-web"


# --- reads ------------------------------------------------------------------

@router.get("/cases/{case_id}/claims", response_model=ClaimSnapshot)
def get_claims(case_id: str, session: Session = Depends(get_db)) -> ClaimSnapshot:
    """Active groups/items with per-group subtotals, per-currency totals, pending
    agent candidates and the claims change sequence."""
    return claims.get_claims(session, uuid_of(case_id, "case_id"))


@router.get("/cases/{case_id}/claims/amount-candidates", response_model=ClaimAmountCandidates)
def list_claim_amount_candidates(
        case_id: str, query: str | None = Query(default=None, max_length=200),
        session: Session = Depends(get_db)) -> ClaimAmountCandidates:
    """This case's ``exact_values(kind='amount')`` rows as pickable candidates.

    Rows whose value cannot be represented in whole cents come back with
    ``amount_cents=None`` so the UI asks for manual entry instead of rounding.
    """
    return claims.list_amount_candidates(session, uuid_of(case_id, "case_id"), query=query)


# --- group commands ---------------------------------------------------------

@router.post("/cases/{case_id}/claims/groups", response_model=ClaimSnapshot)
def claims_add_group(case_id: str, payload: AddClaimGroupInput,
                     session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    return claims.add_group(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/cases/{case_id}/claims/groups/{group_id}", response_model=ClaimSnapshot)
def claims_update_group(case_id: str, group_id: str, payload: UpdateClaimGroupInput,
                        session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(group_id, payload.group_id, "group_id", "分组标识不一致")
    return claims.update_group(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/groups/{group_id}/move", response_model=ClaimSnapshot)
def claims_reorder_group(case_id: str, group_id: str, payload: ReorderClaimGroupInput,
                         session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(group_id, payload.group_id, "group_id", "分组标识不一致")
    return claims.reorder_groups(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/groups/{group_id}/remove", response_model=ClaimSnapshot)
def claims_remove_group(case_id: str, group_id: str, payload: ClaimGroupRevisionInput,
                        session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(group_id, payload.group_id, "group_id", "分组标识不一致")
    return claims.remove_group(session, payload, actor=ACTOR, entry=ENTRY)


# --- item commands ----------------------------------------------------------

@router.post("/cases/{case_id}/claims/items", response_model=ClaimSnapshot)
def claims_add_item(case_id: str, payload: AddClaimItemInput,
                    session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    return claims.add_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.patch("/cases/{case_id}/claims/items/{item_id}", response_model=ClaimSnapshot)
def claims_update_item(case_id: str, item_id: str, payload: UpdateClaimItemInput,
                       session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(item_id, payload.item_id, "item_id", "金额项标识不一致")
    return claims.update_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/items/{item_id}/move", response_model=ClaimSnapshot)
def claims_reorder_item(case_id: str, item_id: str, payload: ReorderClaimItemInput,
                        session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(item_id, payload.item_id, "item_id", "金额项标识不一致")
    return claims.reorder_items(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/items/{item_id}/remove", response_model=ClaimSnapshot)
def claims_remove_item(case_id: str, item_id: str, payload: ClaimItemRevisionInput,
                       session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(item_id, payload.item_id, "item_id", "金额项标识不一致")
    return claims.remove_item(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/items/{item_id}/restore", response_model=ClaimSnapshot)
def claims_restore_item(case_id: str, item_id: str, payload: ClaimItemRevisionInput,
                        session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(item_id, payload.item_id, "item_id", "金额项标识不一致")
    return claims.restore_item(session, payload, actor=ACTOR, entry=ENTRY)


# --- human review (never exposed over MCP/CLI) ------------------------------

@router.post("/cases/{case_id}/claims/changes/{change_id}/accept", response_model=ClaimSnapshot)
def claims_accept_change(case_id: str, change_id: str, payload: ChangeIdInput,
                         session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(change_id, payload.change_id, "change_id", "变更标识不一致")
    return claims.accept_change(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/changes/accept-all", response_model=ClaimSnapshot)
def claims_accept_all(case_id: str, payload: ClaimAcceptAllInput,
                      session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    return claims.accept_all(session, payload, actor=ACTOR, entry=ENTRY)


@router.post("/cases/{case_id}/claims/changes/{change_id}/revert", response_model=ClaimSnapshot)
def claims_revert_change(case_id: str, change_id: str, payload: ChangeIdInput,
                         session: Session = Depends(get_db)) -> ClaimSnapshot:
    _check_case(case_id, payload.case_id)
    _check_target(change_id, payload.change_id, "change_id", "变更标识不一致")
    return claims.revert_change(session, payload, actor=ACTOR, entry=ENTRY)


def _check_case(path_case_id: str, payload_case_id) -> None:
    if uuid_of(path_case_id, "case_id") != payload_case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "案件标识不一致")


def _check_target(path_value: str, payload_value, field: str, message: str) -> None:
    if uuid_of(path_value, field) != payload_value:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, message)
