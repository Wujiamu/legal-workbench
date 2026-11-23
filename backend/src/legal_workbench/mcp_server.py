"""MCP stdio server: agent tools over the shared service layer.

stdout carries protocol data only; all logging goes to stderr.  Destructive
operations (deleting originals, arbitrary SQL, restoring backups) are
deliberately absent.
"""
from __future__ import annotations

import logging
import sys
import uuid
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from . import analysis, source_notes, audit, cases, imports, jobs, materials, parsing, progress, search
from .config import load_settings
from .db import get_session_factory
from .errors import ErrorCode, WorkbenchError
from .schemas import (CreateAnnotationInput, ListAnnotationsInput, ReviewStatus,
                      SearchMode, SearchScope, StatementKind, StrictCents,
                      AddNodeInput, AddRelationInput, InsertFragmentInput, LinkNodeInput,
                      LinkTarget, MoveNodeInput, NodeChanges, NodeRevisionInput,
                      RemoveRelationInput, SetActiveStageInput, UnlinkInput, UpdateNodeInput)
from .storage import Storage

if hasattr(sys.stderr, "reconfigure") and (
    sys.platform == "win32" or (getattr(sys.stderr, "encoding", None) or "").lower().replace("-", "") != "utf8"
):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("legal_workbench.mcp")

server = FastMCP("legal-workbench")


def _storage() -> Storage:
    return Storage(load_settings().data_root)


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"{field} 不是合法 UUID",
                             {"field": field, "value": str(value)})


def _run(fn):
    """Execute one service call; failures surface as structured errors, never text."""
    session = get_session_factory()()
    try:
        return fn(session)
    finally:
        session.close()


@server.tool()
def create_case(title: str, description: str | None = None) -> dict[str, Any]:
    """创建一个新案件。"""
    result = _run(lambda s: cases.create_case(s, title=title, description=description,
                                              actor="agent", entry="mcp"))
    return result.model_dump(mode="json")


@server.tool()
def list_cases(offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """列出案件。"""
    result = _run(lambda s: cases.list_cases(s, offset=offset, limit=limit))
    return result.model_dump(mode="json")


@server.tool()
def import_materials(case_id: str, source_path: str, source_label: str,
                     request_key: str) -> dict[str, Any]:
    """从 IMPORT_ROOTS 允许的服务器路径导入材料目录或文件。"""
    created = _run(lambda s: imports.create_intake(
        s, _storage(), case_id=_uuid(case_id, "case_id"), source_path=source_path,
        source_label=source_label, request_key=request_key))
    return {"intake_id": str(created.intake_id), "job_id": str(created.job_id),
            "created": created.created, "item_count": created.item_count}


@server.tool()
def list_materials(case_id: str, category: str | None = None, offset: int = 0,
                   limit: int = 50) -> dict[str, Any]:
    """列出案件材料及解析覆盖。"""
    items, total, case_total, categories = _run(lambda s: materials.list_materials(
        s, case_id=_uuid(case_id, "case_id"), category=category,
        offset=offset, limit=limit))
    return {"items": [item.model_dump(mode="json") for item in items],
            "offset": offset, "limit": limit, "total": total,
            "case_total": case_total, "categories": categories}


@server.tool()
def update_classification(case_id: str, material_id: str, category: str,
                          expected_revision: int) -> dict[str, Any]:
    """修改材料分类（自由文本，空值归为待分类）。"""
    revision = _run(lambda s: materials.update_classification(
        s, case_id=_uuid(case_id, "case_id"), material_id=_uuid(material_id, "material_id"),
        category=category, expected_revision=expected_revision))
    return {"revision": revision}


@server.tool()
def create_working_copy(case_id: str, file_version_id: str, display_name: str) -> dict[str, Any]:
    """基于某个文件版本创建可编辑工作副本。"""
    working_id, relative = _run(lambda s: materials.create_working_copy(
        s, _storage(), case_id=_uuid(case_id, "case_id"),
        file_version_id=_uuid(file_version_id, "file_version_id"),
        display_name=display_name))
    return {"working_copy_id": str(working_id), "relative_path": relative}


@server.tool()
def register_file_version(case_id: str, working_copy_id: str,
                          expected_revision: int) -> dict[str, Any]:
    """把工作副本登记为新的不可变文件版本并排队解析。"""
    version_id, job_id = _run(lambda s: materials.register_file_version(
        s, _storage(), case_id=_uuid(case_id, "case_id"),
        working_copy_id=_uuid(working_copy_id, "working_copy_id"),
        expected_revision=expected_revision))
    return {"file_version_id": str(version_id), "job_id": str(job_id)}


@server.tool()
def search_case(case_id: str, query: str, mode: str = "literal", scope: str = "originals",
                currency: str | None = None, offset: int = 0,
                limit: int = 20) -> dict[str, Any]:
    """在案件内检索：literal、identifier、amount（需 currency）、date。"""
    result = _run(lambda s: search.search_case(
        s, case_id=_uuid(case_id, "case_id"), query=query, mode=SearchMode(mode),
        scope=SearchScope(scope), currency=currency, offset=offset, limit=limit))
    return result.model_dump(mode="json")


@server.tool()
def read_source(case_id: str, source_ref: str, context_chars: int = 200) -> dict[str, Any]:
    """按 source_ref 读取不可变片段及前后上下文。"""
    result = _run(lambda s: search.read_source(
        s, case_id=_uuid(case_id, "case_id"), source_ref=_uuid(source_ref, "source_ref"),
        context_chars=context_chars))
    return result.model_dump(mode="json")


@server.tool()
def annotate_file(case_id: uuid.UUID, file_version_id: uuid.UUID,
                  kind: Literal["transcription", "interpretation"], body: str,
                  page: int | None = None) -> dict[str, Any]:
    """保存本地 Agent 标注，供承办人对照固定文件版本复核。"""
    payload = CreateAnnotationInput(case_id=case_id, file_version_id=file_version_id,
                                    page=page, kind=kind, body=body)
    return _run(lambda session: source_notes.create_annotation(
        session, payload, author="agent", storage=_storage())).model_dump(mode="json")


@server.tool()
def list_annotations(case_id: uuid.UUID, query: str | None = None,
                     file_version_id: uuid.UUID | None = None,
                     offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """检索同案标注，返回作者和人工复核后的内容。"""
    payload = ListAnnotationsInput(case_id=case_id, query=query,
                                   file_version_id=file_version_id, offset=offset, limit=limit)
    return _run(lambda session: source_notes.list_annotations(
        session, payload)).model_dump(mode="json")


@server.tool()
def get_job_status(case_id: str, job_id: str) -> dict[str, Any]:
    """查询任务状态、进度与逐项错误。"""
    result = _run(lambda s: jobs.get_job_status(
        s, case_id=_uuid(case_id, "case_id"), job_id=_uuid(job_id, "job_id")))
    return result.model_dump(mode="json")


@server.tool()
def get_index_status(case_id: str) -> dict[str, Any]:
    """查询案件索引覆盖数量。"""
    result = _run(lambda s: search.get_index_status(s, case_id=_uuid(case_id, "case_id")))
    return result.model_dump(mode="json")


@server.tool()
def save_analysis(case_id: str, title: str, content: str, statement_kind: str,
                  source_refs: list[str], analysis_id: str | None = None,
                  expected_revision: int | None = None) -> dict[str, Any]:
    """保存分析成果（Markdown 正文 + 来源引用），每次保存生成新版本。"""
    result = _run(lambda s: analysis.save_analysis(
        s, _storage(), case_id=_uuid(case_id, "case_id"), title=title, content=content,
        statement_kind=StatementKind(statement_kind),
        source_refs=[_uuid(ref, "source_ref") for ref in source_refs],
        analysis_id=_uuid(analysis_id, "analysis_id") if analysis_id else None,
        expected_revision=expected_revision))
    return result.model_dump(mode="json")


@server.tool()
def review_analysis(case_id: str, analysis_id: str, version_id: str,
                    expected_revision: int, review_status: str) -> dict[str, Any]:
    """标记分析版本审核状态（pending/approved/rejected），不修改正文。"""
    result = _run(lambda s: analysis.review_analysis(
        s, case_id=_uuid(case_id, "case_id"), analysis_id=_uuid(analysis_id, "analysis_id"),
        version_id=_uuid(version_id, "version_id"), expected_revision=expected_revision,
        review_status=ReviewStatus(review_status)))
    return result.model_dump(mode="json")


@server.tool()
def get_analysis_version(case_id: str, analysis_id: str, version_id: str) -> dict[str, Any]:
    """读取某个分析版本的正文与来源引用。"""
    result = _run(lambda s: analysis.get_analysis_version(
        s, case_id=_uuid(case_id, "case_id"), analysis_id=_uuid(analysis_id, "analysis_id"),
        version_id=_uuid(version_id, "version_id")))
    return result.model_dump(mode="json")


@server.tool(name="export-chart-audit-bundle")
def export_chart_audit_bundle(case_id: str, chart_type: Literal["graph", "claims"],
                              target_id: str, profile: Literal["client", "court"],
                              snapshot_id: str | None = None) -> dict[str, Any]:
    """只读复核审计包。每条出处绑定同案原文，附完整程序预检。"""
    from . import chart_export

    request = chart_export.ChartRequest(
        chart_type=chart_type, target_id=_uuid(target_id, "target_id"), profile=profile,
        snapshot_id=_uuid(snapshot_id, "snapshot_id") if snapshot_id is not None else None,
    )
    result = _run(lambda session: chart_export.build_chart_audit_bundle(
        session, _uuid(case_id, "case_id"), request))
    return result.model_dump(mode="json")


def main() -> None:
    # The MCP server is a single local process serving one caller: attribute the
    # whole stdio session to it rather than threading an actor through 14 tools.
    audit.set_context(actor="mcp", request_id=uuid.uuid4())
    server.run(transport="stdio")


# --- 办案进度（actor=agent, entry=mcp）--------------------------------------
# 只暴露读取与 agent_commands 白名单内的写命令；确认/撤回不在此提供（K5/6.3）。

@server.tool()
def get_progress(case_id: str) -> dict[str, Any]:
    """读取案件进度快照：节点树、关系、关联、待确认变更与变化序号。"""
    return _run(lambda s: progress.get_progress(s, _uuid(case_id, "case_id"))).model_dump(mode="json")


@server.tool()
def get_progress_rules() -> dict[str, Any]:
    """读取进度规则配置：种类、状态、包含关系、关系类型、模板片段、标题预设。"""
    from . import progress_rules
    return {"rules": progress_rules.get_rules()}


@server.tool()
def progress_add_node(case_id: str, kind: str, title: str, request_key: str,
                      parent_id: str | None = None, state: str | None = None,
                      note: str | None = None) -> dict[str, Any]:
    """新建进度节点（Agent 修改进入待确认）。必须带 request_key 以幂等。"""
    payload = AddNodeInput(case_id=_uuid(case_id, "case_id"), kind=kind, title=title,
                           request_key=request_key,
                           parent_id=_uuid(parent_id, "parent_id") if parent_id else None,
                           state=state, note=note)
    return _run(lambda s: progress.add_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_update_node(case_id: str, node_id: str, expected_revision: int,
                         title: str | None = None, state: str | None = None,
                         note: str | None = None, clear_note: bool = False) -> dict[str, Any]:
    """修改进度节点（带 expected_revision 做 CAS）。

    只纳入实际提供的字段（审查 N1）：未提供的 title/state/note 不动；
    显式清空备注用 clear_note=True（None 过滤后无法表达显式 null）。
    """
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if state is not None:
        fields["state"] = state
    if clear_note:
        fields["note"] = None
    elif note is not None:
        fields["note"] = note
    payload = UpdateNodeInput(case_id=_uuid(case_id, "case_id"), node_id=_uuid(node_id, "node_id"),
                              expected_revision=expected_revision,
                              changes=NodeChanges(**fields))
    return _run(lambda s: progress.update_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_set_active_stage(case_id: str, stage_id: str, expected_revision: int) -> dict[str, Any]:
    """把某阶段设为进行中（同程序原进行中阶段记为已完成，一条变更）。"""
    payload = SetActiveStageInput(case_id=_uuid(case_id, "case_id"),
                                  stage_id=_uuid(stage_id, "stage_id"),
                                  expected_revision=expected_revision)
    return _run(lambda s: progress.set_active_stage(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_move_node(case_id: str, node_id: str, expected_revision: int,
                       new_parent_id: str | None = None,
                       before_node_id: str | None = None) -> dict[str, Any]:
    """移动节点到新父级/位置（拒绝成环与违反包含规则）。"""
    payload = MoveNodeInput(case_id=_uuid(case_id, "case_id"), node_id=_uuid(node_id, "node_id"),
                            expected_revision=expected_revision,
                            new_parent_id=_uuid(new_parent_id, "new_parent_id") if new_parent_id else None,
                            before_node_id=_uuid(before_node_id, "before_node_id") if before_node_id else None)
    return _run(lambda s: progress.move_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_insert_fragment(case_id: str, fragment_key: str,
                             parent_id: str | None = None,
                             before_node_id: str | None = None) -> dict[str, Any]:
    """在规则允许的位置插入模板片段（如二审、执行）。before_node_id 指定插到某节点之前（审查 N2）。"""
    payload = InsertFragmentInput(case_id=_uuid(case_id, "case_id"), fragment_key=fragment_key,
                                  parent_id=_uuid(parent_id, "parent_id") if parent_id else None,
                                  before_node_id=_uuid(before_node_id, "before_node_id") if before_node_id else None)
    return _run(lambda s: progress.insert_fragment(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_remove_node(case_id: str, node_id: str, expected_revision: int) -> dict[str, Any]:
    """软删除进度节点（须先移除或移动子节点）。"""
    payload = NodeRevisionInput(case_id=_uuid(case_id, "case_id"), node_id=_uuid(node_id, "node_id"),
                                expected_revision=expected_revision)
    return _run(lambda s: progress.remove_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_add_relation(case_id: str, from_node_id: str, to_node_id: str,
                          relation_type: str, note: str | None = None) -> dict[str, Any]:
    """建立非包含关系（首版 causes/relates）。"""
    payload = AddRelationInput(case_id=_uuid(case_id, "case_id"),
                               from_node_id=_uuid(from_node_id, "from_node_id"),
                               to_node_id=_uuid(to_node_id, "to_node_id"),
                               relation_type=relation_type, note=note)
    return _run(lambda s: progress.add_relation(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_link_material(case_id: str, node_id: str, material_id: str) -> dict[str, Any]:
    """把进度节点关联到同案材料。"""
    payload = LinkNodeInput(case_id=_uuid(case_id, "case_id"), node_id=_uuid(node_id, "node_id"),
                            target=LinkTarget(material_id=_uuid(material_id, "material_id")))
    return _run(lambda s: progress.link_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_link_analysis(case_id: str, node_id: str, analysis_id: str) -> dict[str, Any]:
    """把进度节点关联到同案分析（审查：P3 入口覆盖）。"""
    payload = LinkNodeInput(case_id=_uuid(case_id, "case_id"), node_id=_uuid(node_id, "node_id"),
                            target=LinkTarget(analysis_id=_uuid(analysis_id, "analysis_id")))
    return _run(lambda s: progress.link_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_link_annotation(case_id: str, node_id: str, annotation_id: str) -> dict[str, Any]:
    """把进度节点关联到同案标注（审查：P3 入口覆盖）。"""
    payload = LinkNodeInput(case_id=_uuid(case_id, "case_id"), node_id=_uuid(node_id, "node_id"),
                            target=LinkTarget(annotation_id=_uuid(annotation_id, "annotation_id")))
    return _run(lambda s: progress.link_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_remove_relation(case_id: str, relation_id: str) -> dict[str, Any]:
    """移除非包含关系（软删除，审查：P3 入口覆盖）。"""
    payload = RemoveRelationInput(case_id=_uuid(case_id, "case_id"),
                                  relation_id=_uuid(relation_id, "relation_id"))
    return _run(lambda s: progress.remove_relation(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def progress_unlink(case_id: str, link_id: str) -> dict[str, Any]:
    """解除进度节点的业务关联（审查：P3 入口覆盖）。"""
    payload = UnlinkInput(case_id=_uuid(case_id, "case_id"), link_id=_uuid(link_id, "link_id"))
    return _run(lambda s: progress.unlink_node(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


# --- 主体关系图与证据目录（actor=agent, entry=mcp）--------------------------
# 只暴露 graph_rules.AGENT_COMMANDS / EVIDENCE_AGENT_COMMANDS 内的写命令与只读查询。
# 快照冻结/恢复、客户同意、证据复核与确认/撤回一律不在此提供（D3 / 检查清单 2）。

@server.tool()
def get_graph(case_id: str, graph_id: str | None = None) -> dict[str, Any]:
    """读取关系图工作区：主体、别名、关系、出处、分图、布局、快照、目录与待确认变更。"""
    from . import graph
    return _run(lambda s: graph.get_workspace(
        s, _uuid(case_id, "case_id"),
        _uuid(graph_id, "graph_id") if graph_id else None)).model_dump(mode="json")


@server.tool()
def get_graph_rules() -> dict[str, Any]:
    """读取关系图规则：主体类型、诉讼角色、关系类型、四档可信状态、阶段预设与 Agent 白名单。"""
    from . import graph_rules
    return {"rules": graph_rules.get_rules()}


@server.tool()
def get_graph_snapshot(case_id: str, snapshot_id: str) -> dict[str, Any]:
    """读取一份已冻结快照（只读；冻结内容不可修改）。"""
    from . import graph
    return _run(lambda s: graph.get_snapshot(
        s, _uuid(case_id, "case_id"),
        _uuid(snapshot_id, "snapshot_id"))).model_dump(mode="json")


@server.tool()
def graph_removal_impact(case_id: str, entity_id: str | None = None,
                         relation_id: str | None = None) -> dict[str, Any]:
    """删除主体/关系前查询受影响的图清单与数量。"""
    from . import graph
    return _run(lambda s: graph.get_removal_impact(
        s, _uuid(case_id, "case_id"),
        entity_id=_uuid(entity_id, "entity_id") if entity_id else None,
        relation_id=_uuid(relation_id, "relation_id") if relation_id else None,
    )).model_dump(mode="json")


@server.tool()
def graph_add_entity(case_id: str, kind: str, name: str, request_key: str,
                     roles: list[str] | None = None, note: str | None = None,
                     graph_id: str | None = None) -> dict[str, Any]:
    """新建主体（Agent 修改进入待确认）。必须带 request_key 以幂等。"""
    from . import graph
    from .schemas import AddEntityInput
    payload = AddEntityInput(
        case_id=_uuid(case_id, "case_id"), kind=kind, name=name, roles=roles or [],
        note=note, graph_id=_uuid(graph_id, "graph_id") if graph_id else None,
        request_key=request_key)
    return _run(lambda s: graph.add_entity(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_update_entity(case_id: str, entity_id: str, expected_revision: int,
                        kind: str | None = None, name: str | None = None,
                        roles: list[str] | None = None, note: str | None = None,
                        clear_note: bool = False) -> dict[str, Any]:
    """修改主体（CAS）。只提交实际提供的字段；清空备注用 clear_note=True。"""
    from . import graph
    from .schemas import EntityChanges, UpdateEntityInput
    fields: dict[str, Any] = {}
    if kind is not None:
        fields["kind"] = kind
    if name is not None:
        fields["name"] = name
    if roles is not None:
        fields["roles"] = roles
    if clear_note:
        fields["note"] = None
    elif note is not None:
        fields["note"] = note
    payload = UpdateEntityInput(case_id=_uuid(case_id, "case_id"),
                                entity_id=_uuid(entity_id, "entity_id"),
                                expected_revision=expected_revision,
                                changes=EntityChanges(**fields))
    return _run(lambda s: graph.update_entity(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


# --- 诉请金额（actor=agent, entry=mcp）--------------------------------------
# 与进度一致：只暴露查询、候选检索与白名单写命令；确认/撤回不在此提供。

CLAIM_CATEGORIES = ("principal", "interest", "liquidated_damages", "expense", "deduction", "other")


def _claim_category(value: str) -> str:
    if value not in CLAIM_CATEGORIES:
        raise WorkbenchError(ErrorCode.INVALID_INPUT,
                             f"金额类别须为：{'、'.join(CLAIM_CATEGORIES)}", {"category": value})
    return value


@server.tool()
def get_claims(case_id: str) -> dict[str, Any]:
    """读取案件诉请金额快照：分组、金额项、组小计、分币种总计、待确认候选与序号。"""
    from . import claims
    return _run(lambda s: claims.get_claims(s, _uuid(case_id, "case_id"))).model_dump(mode="json")


@server.tool()
def claim_amount_candidates(case_id: str, query: str | None = None) -> dict[str, Any]:
    """检索本案已抽取的金额精确值候选（带出处片段/材料），用于带入金额。"""
    from . import claims
    return _run(lambda s: claims.list_amount_candidates(
        s, _uuid(case_id, "case_id"), query=query)).model_dump(mode="json")


@server.tool()
def claim_add_group(case_id: str, title: str, request_key: str, currency: str = "CNY",
                    include_in_total: bool = True, note: str | None = None) -> dict[str, Any]:
    """提交诉请分组候选（进入待确认，人工确认后才写入明细）。必须带 request_key。"""
    from . import claims
    from .schemas import AddClaimGroupInput
    payload = AddClaimGroupInput(case_id=_uuid(case_id, "case_id"), title=title,
                                 request_key=request_key, currency=currency,
                                 include_in_total=include_in_total, note=note)
    return _run(lambda s: claims.add_group(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_add_alias(case_id: str, entity_id: str, alias: str) -> dict[str, Any]:
    """给主体加别名（同名不自动合并，重复别名被拒）。"""
    from . import graph
    from .schemas import AddAliasInput
    payload = AddAliasInput(case_id=_uuid(case_id, "case_id"),
                            entity_id=_uuid(entity_id, "entity_id"), alias=alias)
    return _run(lambda s: graph.add_alias(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_add_relation(case_id: str, from_entity_id: str, to_entity_id: str, kind: str,
                       credibility: str, request_key: str, label: str | None = None,
                       directed: bool = True, note: str | None = None,
                       graph_id: str | None = None,
                       share_pct: float | None = None, amount_cents: int | None = None,
                       currency: str | None = None, date_start: str | None = None,
                       date_end: str | None = None) -> dict[str, Any]:
    """新建关系（Agent 修改进入待确认）。「有证据」须先以其他状态建立并补出处。"""
    from datetime import date

    from . import graph
    from .schemas import AddGraphRelationInput, RelationAttrs
    attrs = RelationAttrs(
        share_pct=share_pct, amount_cents=amount_cents, currency=currency,
        date_start=date.fromisoformat(date_start) if date_start else None,
        date_end=date.fromisoformat(date_end) if date_end else None)
    payload = AddGraphRelationInput(
        case_id=_uuid(case_id, "case_id"),
        from_entity_id=_uuid(from_entity_id, "from_entity_id"),
        to_entity_id=_uuid(to_entity_id, "to_entity_id"), kind=kind, label=label,
        credibility=credibility, directed=directed, attrs=attrs, note=note,
        graph_id=_uuid(graph_id, "graph_id") if graph_id else None,
        request_key=request_key)
    return _run(lambda s: graph.add_relation(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_update_relation(case_id: str, relation_id: str, expected_revision: int,
                          kind: str | None = None, label: str | None = None,
                          credibility: str | None = None, directed: bool | None = None,
                          note: str | None = None, clear_note: bool = False) -> dict[str, Any]:
    """修改关系（CAS）。只提交实际提供的字段；「有证据」仍须已有已确认出处。"""
    from . import graph
    from .schemas import RelationChanges, UpdateRelationInput
    fields: dict[str, Any] = {}
    for key, value in (("kind", kind), ("label", label), ("credibility", credibility),
                       ("directed", directed)):
        if value is not None:
            fields[key] = value
    if clear_note:
        fields["note"] = None
    elif note is not None:
        fields["note"] = note
    payload = UpdateRelationInput(case_id=_uuid(case_id, "case_id"),
                                  relation_id=_uuid(relation_id, "relation_id"),
                                  expected_revision=expected_revision,
                                  changes=RelationChanges(**fields))
    return _run(lambda s: graph.update_relation(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_add_source(case_id: str, request_key: str, proves: str | None = None,
                     entity_id: str | None = None, relation_id: str | None = None,
                     segment_id: str | None = None, material_id: str | None = None,
                     annotation_id: str | None = None) -> dict[str, Any]:
    """给主体或关系加出处：片段 / 材料 / 标注三选一，挂在主体或关系之一。"""
    from . import graph
    from .schemas import AddSourceInput, SourceRefInput
    payload = AddSourceInput(
        case_id=_uuid(case_id, "case_id"),
        entity_id=_uuid(entity_id, "entity_id") if entity_id else None,
        relation_id=_uuid(relation_id, "relation_id") if relation_id else None,
        ref=SourceRefInput(
            segment_id=_uuid(segment_id, "segment_id") if segment_id else None,
            material_id=_uuid(material_id, "material_id") if material_id else None,
            annotation_id=_uuid(annotation_id, "annotation_id") if annotation_id else None),
        proves=proves, request_key=request_key)
    return _run(lambda s: graph.add_source(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_create_graph(case_id: str, title: str, request_key: str,
                       description: str | None = None) -> dict[str, Any]:
    """新建一张命名关系图（一案多图；主体与关系仍为案件级共享）。"""
    from . import graph
    from .schemas import CreateGraphInput
    payload = CreateGraphInput(case_id=_uuid(case_id, "case_id"), title=title,
                               description=description, request_key=request_key)
    return _run(lambda s: graph.create_graph(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_update_graph(case_id: str, graph_id: str, expected_revision: int,
                       title: str | None = None, description: str | None = None) -> dict[str, Any]:
    """重命名关系图或修改说明（CAS）。"""
    from . import graph
    from .schemas import GraphChanges, UpdateGraphInput
    fields = {key: value for key, value in
              (("title", title), ("description", description)) if value is not None}
    payload = UpdateGraphInput(case_id=_uuid(case_id, "case_id"),
                               graph_id=_uuid(graph_id, "graph_id"),
                               expected_revision=expected_revision,
                               changes=GraphChanges(**fields))
    return _run(lambda s: graph.update_graph(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def graph_add_to_graph(case_id: str, graph_id: str, entity_ids: list[str] | None = None,
                       relation_ids: list[str] | None = None) -> dict[str, Any]:
    """把案件主体/关系加入某张图；关系的两端主体会自动纳入本图。"""
    from . import graph
    from .schemas import AddToGraphInput
    payload = AddToGraphInput(
        case_id=_uuid(case_id, "case_id"), graph_id=_uuid(graph_id, "graph_id"),
        entity_ids=[_uuid(item, "entity_id") for item in (entity_ids or [])],
        relation_ids=[_uuid(item, "relation_id") for item in (relation_ids or [])])
    return _run(lambda s: graph.add_to_graph(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def get_evidence_catalog(case_id: str) -> dict[str, Any]:
    """读取证据目录：分组、条目、绑定、材料清单与当前编号。"""
    from . import evidence_catalog
    return _run(lambda s: evidence_catalog.get_catalog(
        s, _uuid(case_id, "case_id"))).model_dump(mode="json")


@server.tool()
def evidence_add_group(case_id: str, title: str, request_key: str,
                       purpose: str | None = None) -> dict[str, Any]:
    """新建证据分组（Agent 修改进入待确认）。"""
    from . import evidence_catalog
    from .schemas import AddEvidenceGroupInput
    payload = AddEvidenceGroupInput(case_id=_uuid(case_id, "case_id"), title=title,
                                    purpose=purpose, request_key=request_key)
    return _run(lambda s: evidence_catalog.add_group(
        s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def evidence_add_item(case_id: str, group_id: str, title: str, request_key: str,
                      purpose: str | None = None) -> dict[str, Any]:
    """新建证据条目（Agent 修改进入待确认）。"""
    from . import evidence_catalog
    from .schemas import AddEvidenceItemInput
    payload = AddEvidenceItemInput(case_id=_uuid(case_id, "case_id"),
                                   group_id=_uuid(group_id, "group_id"), title=title,
                                   purpose=purpose, request_key=request_key)
    return _run(lambda s: evidence_catalog.add_item(
        s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def evidence_update_item(case_id: str, item_id: str, expected_revision: int,
                         title: str | None = None, purpose: str | None = None) -> dict[str, Any]:
    """修改证据条目的名称或证明目的（CAS）。"""
    from . import evidence_catalog
    from .schemas import EvidenceItemChanges, UpdateEvidenceItemInput
    fields = {key: value for key, value in
              (("title", title), ("purpose", purpose)) if value is not None}
    payload = UpdateEvidenceItemInput(case_id=_uuid(case_id, "case_id"),
                                      item_id=_uuid(item_id, "item_id"),
                                      expected_revision=expected_revision,
                                      changes=EvidenceItemChanges(**fields))
    return _run(lambda s: evidence_catalog.update_item(
        s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def evidence_add_binding(case_id: str, item_id: str, material_id: str, request_key: str,
                         page_start: int | None = None,
                         page_end: int | None = None) -> dict[str, Any]:
    """给证据条目绑定材料与页码范围（Agent 提交为待确认，确认前不参与编号）。"""
    from . import evidence_catalog
    from .schemas import AddEvidenceBindingInput
    payload = AddEvidenceBindingInput(
        case_id=_uuid(case_id, "case_id"), item_id=_uuid(item_id, "item_id"),
        material_id=_uuid(material_id, "material_id"), page_start=page_start,
        page_end=page_end, request_key=request_key)
    return _run(lambda s: evidence_catalog.add_binding(
        s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def evidence_import_catalog(case_id: str, import_material_id: str, rows: list[dict[str, Any]],
                            request_key: str) -> dict[str, Any]:
    """提交外部 Word/Excel 目录解析出的草稿（待确认，确认前不参与编号）。"""
    from . import evidence_catalog
    from .schemas import ImportEvidenceCatalogInput
    payload = ImportEvidenceCatalogInput(
        case_id=_uuid(case_id, "case_id"),
        import_material_id=_uuid(import_material_id, "import_material_id"),
        rows=[dict(row) for row in rows], request_key=request_key)
    return _run(lambda s: evidence_catalog.import_catalog(
        s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def claim_update_group(case_id: str, group_id: str, expected_revision: int, request_key: str,
                       title: str | None = None, currency: str | None = None,
                       include_in_total: bool | None = None, note: str | None = None,
                       clear_note: bool = False) -> dict[str, Any]:
    """提交分组修改候选（带 expected_revision 做 CAS）。

    只纳入实际提供的字段；显式清空备注用 clear_note=True。Agent 必须带
    request_key：同一键重放返回原变更，命令/内容不同则冲突。
    """
    from . import claims
    from .schemas import ClaimGroupChanges, UpdateClaimGroupInput
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if currency is not None:
        fields["currency"] = currency
    if include_in_total is not None:
        fields["include_in_total"] = include_in_total
    if clear_note:
        fields["note"] = None
    elif note is not None:
        fields["note"] = note
    payload = UpdateClaimGroupInput(case_id=_uuid(case_id, "case_id"),
                                    group_id=_uuid(group_id, "group_id"),
                                    expected_revision=expected_revision,
                                    changes=ClaimGroupChanges(**fields),
                                    request_key=request_key)
    return _run(lambda s: claims.update_group(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def claim_add_item(case_id: str, group_id: str, title: str, category: str,
                   amount_cents: StrictCents, request_key: str, calc_note: str | None = None,
                   segment_id: str | None = None, material_id: str | None = None,
                   annotation_id: str | None = None) -> dict[str, Any]:
    """提交金额项候选（整数分，进入待确认）。出处三选一：segment/material/annotation。"""
    from . import claims
    from .schemas import AddClaimItemInput
    payload = AddClaimItemInput(
        case_id=_uuid(case_id, "case_id"), group_id=_uuid(group_id, "group_id"),
        title=title, category=_claim_category(category), amount_cents=amount_cents,
        calc_note=calc_note, request_key=request_key,
        segment_id=_uuid(segment_id, "segment_id") if segment_id else None,
        material_id=_uuid(material_id, "material_id") if material_id else None,
        annotation_id=_uuid(annotation_id, "annotation_id") if annotation_id else None)
    return _run(lambda s: claims.add_item(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


@server.tool()
def claim_update_item(case_id: str, item_id: str, expected_revision: int, request_key: str,
                      title: str | None = None, category: str | None = None,
                      amount_cents: StrictCents | None = None, calc_note: str | None = None,
                      clear_calc_note: bool = False, segment_id: str | None = None,
                      material_id: str | None = None, annotation_id: str | None = None,
                      clear_source: bool = False) -> dict[str, Any]:
    """提交金额项修改候选（带 expected_revision 做 CAS）。

    只纳入实际提供的字段；出处提供任一 id 即整体替换，clear_source=True 清空出处。
    Agent 必须带 request_key：同一键重放返回原变更，命令/内容不同则冲突。
    """
    from . import claims
    from .schemas import ClaimItemChanges, UpdateClaimItemInput
    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if category is not None:
        fields["category"] = _claim_category(category)
    if amount_cents is not None:
        fields["amount_cents"] = amount_cents
    if clear_calc_note:
        fields["calc_note"] = None
    elif calc_note is not None:
        fields["calc_note"] = calc_note
    sources = {"segment_id": segment_id, "material_id": material_id,
               "annotation_id": annotation_id}
    if clear_source:
        fields.update({name: None for name in sources})
    elif any(value is not None for value in sources.values()):
        fields.update({name: _uuid(value, name) if value else None
                       for name, value in sources.items()})
    payload = UpdateClaimItemInput(case_id=_uuid(case_id, "case_id"),
                                   item_id=_uuid(item_id, "item_id"),
                                   expected_revision=expected_revision,
                                   changes=ClaimItemChanges(**fields),
                                   request_key=request_key)
    return _run(lambda s: claims.update_item(s, payload, actor="agent", entry="mcp")).model_dump(mode="json")


if __name__ == "__main__":
    main()
