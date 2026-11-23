"""Minimal audit trail: who did what, on which target, and under which request.

Only identifiers and operation names are recorded — never case text, file
content, credentials or full connection strings.  Recording never blocks the
operation it describes: an audit failure is logged, not raised, because losing an
audit row must not lose the user's work.

The actor and request id travel in a context variable so service functions can
record without every caller threading two extra parameters through the stack.
The API middleware, CLI and MCP each set the context once at their entry point.
"""
from __future__ import annotations

import contextvars
import logging
import uuid

from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger("legal_workbench.audit")

# Operation names are a closed set (mirrored by a CHECK constraint) so the table
# stays queryable; adding a new audited operation means adding it in both places.
OPERATIONS = (
    "session.login",
    "session.logout",
    "case.create",
    "intake.import",
    "intake.upload",
    "material.classify",
    "material.rename",
    "material.working_copy",
    "file_version.register",
    "parse.reparse",
    "analysis.save",
    "analysis.review",
    "backup.create",
    "backup.restore",
    "maintenance.clear",
    "progress.add_node",
    "progress.update_node",
    "progress.move_node",
    "progress.set_active_stage",
    "progress.insert_fragment",
    "progress.remove_node",
    "progress.restore_node",
    "progress.add_relation",
    "progress.remove_relation",
    "progress.link",
    "progress.unlink",
    "progress.accept_change",
    "progress.accept_all",
    "progress.revert_change",
    "case.archive",
    "case.unarchive",
    "time.start",
    "time.stop",
    "time.add",
    "time.remove",
    # 案件主体关系图（0007）。冻结/恢复、客户同意与复核只在人工入口产生。
    "graph.add_entity",
    "graph.update_entity",
    "graph.remove_entity",
    "graph.restore_entity",
    "graph.add_alias",
    "graph.remove_alias",
    "graph.add_relation",
    "graph.update_relation",
    "graph.remove_relation",
    "graph.restore_relation",
    "graph.add_source",
    "graph.remove_source",
    "graph.create_graph",
    "graph.update_graph",
    "graph.remove_graph",
    "graph.add_to_graph",
    "graph.remove_from_graph",
    "graph.save_layout",
    "graph.upsert_group",
    "graph.remove_group",
    "graph.create_snapshot",
    "graph.restore_snapshot",
    "graph.add_snapshot_consent",
    "graph.accept_change",
    "graph.accept_all",
    "graph.revert_change",
    # 证据目录（0007）。
    "evidence.add_group",
    "evidence.update_group",
    "evidence.reorder_groups",
    "evidence.remove_group",
    "evidence.add_item",
    "evidence.update_item",
    "evidence.move_item",
    "evidence.remove_item",
    "evidence.add_binding",
    "evidence.remove_binding",
    "evidence.confirm_item",
    "evidence.reject_item",
    "evidence.confirm_binding",
    "evidence.reject_binding",
    "evidence.import_catalog",
    "evidence.accept_change",
    "evidence.accept_all",
    "evidence.revert_change",
    "claims.add_group",
    "claims.update_group",
    "claims.reorder_groups",
    "claims.remove_group",
    "claims.add_item",
    "claims.update_item",
    "claims.reorder_items",
    "claims.remove_item",
    "claims.restore_item",
    "claims.accept_change",
    "claims.accept_all",
    "claims.revert_change",
)

_actor: contextvars.ContextVar[str] = contextvars.ContextVar("lwb_audit_actor", default="local")
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lwb_audit_request_id", default=None)


def set_context(*, actor: str, request_id: uuid.UUID | str | None = None) -> None:
    """Set the actor for the current request/command/tool call."""
    _actor.set(actor)
    _request_id.set(str(request_id) if request_id is not None else None)


def current_actor() -> str:
    return _actor.get()


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """Accept a UUID, a UUID string, or any stable string (hashed into a UUID).

    Audit rows must keep ``target_id`` type-consistent, but some targets are not
    UUIDs (a dump digest, a maintenance-flag row id).  Hashing keeps them stable
    and joinable without widening the column type.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_OID, str(value))


def record(session: Session, *, operation: str, target_type: str,
           target_id: uuid.UUID | str | None = None,
           case_id: uuid.UUID | None = None,
           commit: bool = False) -> None:
    """Append one audit event.

    ``commit=False`` (the default) joins the caller's transaction, so the event
    and the change it describes are committed together or not at all.
    """
    if operation not in OPERATIONS:
        raise ValueError(f"未登记的审计操作: {operation}")
    try:
        session.add(models.AuditEvent(
            id=uuid.uuid4(),
            case_id=case_id,
            actor=_actor.get(),
            operation=operation,
            target_type=target_type,
            target_id=_coerce_uuid(target_id),
            request_id=_request_id.get(),
        ))
        if commit:
            session.commit()
    except Exception:  # noqa: BLE001 - audit must never break the operation
        logger.warning("审计事件写入失败 operation=%s target_type=%s", operation, target_type)
        session.rollback()
