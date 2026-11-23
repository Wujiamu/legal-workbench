"""Agent-facing CLI: JSON on stdout, diagnostics on stderr, non-zero on failure.

Every subcommand calls the same service function the HTTP API and MCP server use;
there is no shell interpolation of user input and no raw SQL entry point.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from . import analysis, audit, cases, imports, jobs, materials, parsing, search, worker
from .config import load_settings, resolve_pg_bin
from .db import get_engine, get_session_factory
from .errors import ErrorCode, WorkbenchError
from .schemas import (ReviewStatus, SearchMode, SearchScope, StatementKind)
from .storage import Storage

logger = logging.getLogger("legal_workbench.cli")

# 与 schemas.ClaimCategory 保持一致；CLI 只做参数值校验，业务规则仍在服务层。
CLAIM_CATEGORIES = ("principal", "interest", "liquidated_damages", "expense", "deduction", "other")


def _dump(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if is_dataclass(value) and not isinstance(value, type):
        return json.dumps(_jsonable(asdict(value)), ensure_ascii=False)
    return json.dumps(_jsonable(value), ensure_ascii=False)


def _jsonable(value: Any) -> Any:
    """Recursively turn dataclasses, UUIDs and models into JSON-safe values."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "标识符格式不合法", {"value": value})


def _storage() -> Storage:
    return Storage(load_settings().data_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lwb-cli", description="Legal Workbench local CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str):
        return sub.add_parser(name, help=help_text)

    p = add("create-case", "创建案件")
    p.add_argument("--title", required=True)
    p.add_argument("--description")

    p = add("list-cases", "列出案件")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)

    p = add("import", "从允许目录导入材料")
    p.add_argument("--case-id", required=True)
    p.add_argument("--source-path", required=True)
    p.add_argument("--source-label", required=True)
    p.add_argument("--request-key", required=True)

    p = add("list-materials", "列出材料")
    p.add_argument("--case-id", required=True)
    p.add_argument("--category")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)

    p = add("update-classification", "修改材料分类")
    p.add_argument("--case-id", required=True)
    p.add_argument("--material-id", required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--expected-revision", type=int, required=True)

    p = add("update-display-name", "修改材料显示名")
    p.add_argument("--case-id", required=True)
    p.add_argument("--material-id", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--expected-revision", type=int, required=True)

    p = add("create-working-copy", "创建工作副本")
    p.add_argument("--case-id", required=True)
    p.add_argument("--file-version-id", required=True)
    p.add_argument("--display-name", required=True)

    p = add("register-file-version", "登记工作副本为新版本")
    p.add_argument("--case-id", required=True)
    p.add_argument("--working-copy-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)

    p = add("reparse", "重新解析文件版本")
    p.add_argument("--case-id", required=True)
    p.add_argument("--file-version-id", required=True)
    p.add_argument("--force-ocr-page", type=int, action="append", default=[])

    p = add("search", "在案件内检索")
    p.add_argument("--case-id", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--mode", required=True, choices=[m.value for m in SearchMode])
    p.add_argument("--scope", default=SearchScope.ORIGINALS.value,
                   choices=[s.value for s in SearchScope])
    p.add_argument("--currency")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)

    p = add("read-source", "读取来源片段")
    p.add_argument("--case-id", required=True)
    p.add_argument("--source-ref", required=True)
    p.add_argument("--context-chars", type=int, default=200)

    p = add("get-original", "查询原件版本与校验状态")
    p.add_argument("--case-id", required=True)
    p.add_argument("--material-id", required=True)

    p = add("job-status", "查询任务状态")
    p.add_argument("--case-id", required=True)
    p.add_argument("--job-id", required=True)

    p = add("retry-job", "重试任务（复用同一 job_id）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--job-id", required=True)

    p = add("index-status", "查询案件索引覆盖")
    p.add_argument("--case-id", required=True)

    p = add("save-analysis", "保存分析成果")
    p.add_argument("--case-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--content", help="Markdown 正文（与 --content-file 二选一）")
    p.add_argument("--content-file", help="从 UTF-8 文件读取 Markdown 正文")
    p.add_argument("--statement-kind", required=True,
                   choices=[k.value for k in StatementKind])
    p.add_argument("--source-ref", action="append", default=[])
    p.add_argument("--analysis-id")
    p.add_argument("--expected-revision", type=int)

    p = add("review-analysis", "审核分析版本")
    p.add_argument("--case-id", required=True)
    p.add_argument("--analysis-id", required=True)
    p.add_argument("--version-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--review-status", required=True, choices=[s.value for s in ReviewStatus])

    p = add("list-analyses", "列出分析成果")
    p.add_argument("--case-id", required=True)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)

    p = add("get-analysis", "读取分析摘要")
    p.add_argument("--case-id", required=True)
    p.add_argument("--analysis-id", required=True)

    p = add("list-analysis-versions", "列出分析版本")
    p.add_argument("--case-id", required=True)
    p.add_argument("--analysis-id", required=True)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)

    p = add("get-analysis-version", "读取分析版本正文")
    p.add_argument("--case-id", required=True)
    p.add_argument("--analysis-id", required=True)
    p.add_argument("--version-id", required=True)

    p = add("run-worker", "执行一次任务队列（单 worker）")
    p.add_argument("--once", action="store_true", default=True)

    p = add("check-consistency", "只读一致性检查")
    p.add_argument("--case-id")

    p = add("repair-consistency", "完成已有记录支持的未完成发布")
    p.add_argument("--case-id")

    p = add("backup", "备份数据库与受管文件")
    p.add_argument("--destination", required=True)

    p = add("verify-backup", "校验备份清单与哈希")
    p.add_argument("--destination", required=True)

    p = add("restore", "从备份恢复到空数据库与空 DATA_ROOT")
    p.add_argument("--destination", required=True)
    p.add_argument("--data-root", required=True)

    p = add("clear-maintenance", "备份崩溃后由操作者显式解除维护标志")

    # 办案进度（actor=agent, entry=cli）；确认/撤回不在此提供。
    p = add("progress", "读取案件进度快照")
    p.add_argument("--case-id", required=True)
    p = add("progress-rules", "读取进度规则配置")
    p = add("progress-add-node", "新建进度节点（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--request-key", required=True)
    p.add_argument("--parent-id")
    p.add_argument("--state")
    p.add_argument("--note")
    p = add("progress-set-active-stage", "设某阶段为进行中")
    p.add_argument("--case-id", required=True)
    p.add_argument("--stage-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p = add("progress-insert-fragment", "插入模板片段")
    p.add_argument("--case-id", required=True)
    p.add_argument("--fragment-key", required=True)
    p.add_argument("--parent-id")
    p.add_argument("--before-node-id")
    # 审查：P3 入口覆盖——普通编辑、移动、删除、关系与业务关联。
    p = add("progress-update-node", "修改进度节点（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--node-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--title")
    p.add_argument("--state")
    p.add_argument("--note")
    p = add("progress-move-node", "移动节点到新父级/位置")
    p.add_argument("--case-id", required=True)
    p.add_argument("--node-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--new-parent-id")
    p.add_argument("--before-node-id")
    p = add("progress-remove-node", "软删除进度节点")
    p.add_argument("--case-id", required=True)
    p.add_argument("--node-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p = add("progress-add-relation", "建立非包含关系")
    p.add_argument("--case-id", required=True)
    p.add_argument("--from-node-id", required=True)
    p.add_argument("--to-node-id", required=True)
    p.add_argument("--relation-type", required=True)
    p.add_argument("--note")
    p = add("progress-remove-relation", "移除非包含关系")
    p.add_argument("--case-id", required=True)
    p.add_argument("--relation-id", required=True)
    p = add("progress-link", "关联同案业务对象（材料/分析/标注三选一）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--node-id", required=True)
    p.add_argument("--material-id")
    p.add_argument("--analysis-id")
    p.add_argument("--annotation-id")
    p = add("progress-unlink", "解除业务关联")
    p.add_argument("--case-id", required=True)
    p.add_argument("--link-id", required=True)

    # 主体关系图与证据目录（actor=agent, entry=cli）。
    # 只提供 Agent 白名单内的写命令与只读查询；快照冻结/恢复、客户同意、
    # 证据复核与确认/撤回不在 CLI 提供（D3 / 检查清单 2）。
    p = add("graph", "读取关系图工作区")
    p.add_argument("--case-id", required=True)
    p.add_argument("--graph-id")
    p = add("graph-rules", "读取关系图规则配置")
    p = add("graph-add-entity", "新建主体（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", action="append", default=[])
    p.add_argument("--note")
    p.add_argument("--graph-id")
    p.add_argument("--request-key", required=True)
    p = add("graph-update-entity", "修改主体（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--kind")
    p.add_argument("--name")
    p.add_argument("--role", action="append", default=None)
    p.add_argument("--note")
    p.add_argument("--clear-note", action="store_true")
    p = add("graph-add-alias", "给主体加别名")
    p.add_argument("--case-id", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--alias", required=True)
    p = add("graph-add-relation", "新建关系（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--from-entity-id", required=True)
    p.add_argument("--to-entity-id", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--credibility", required=True,
                   choices=["evidenced", "suspected", "disputed", "ruled_out"])
    p.add_argument("--label")
    p.add_argument("--directed", choices=["true", "false"], default="true")
    p.add_argument("--share-pct", type=float)
    p.add_argument("--amount-cents", type=int)
    p.add_argument("--currency")
    p.add_argument("--date-start")
    p.add_argument("--date-end")
    p.add_argument("--note")
    p.add_argument("--graph-id")
    p.add_argument("--request-key", required=True)
    p = add("graph-update-relation", "修改关系（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--relation-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--kind")
    p.add_argument("--label")
    p.add_argument("--credibility",
                   choices=["evidenced", "suspected", "disputed", "ruled_out"])
    p.add_argument("--directed", choices=["true", "false"])
    p.add_argument("--note")
    p.add_argument("--clear-note", action="store_true")
    p = add("graph-add-source", "给主体/关系加出处（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--entity-id")
    p.add_argument("--relation-id")
    p.add_argument("--segment-id")
    p.add_argument("--material-id")
    p.add_argument("--annotation-id")
    p.add_argument("--proves")
    p.add_argument("--request-key", required=True)
    p = add("graph-create-graph", "新建命名关系图")
    p.add_argument("--case-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--request-key", required=True)
    p = add("graph-update-graph", "重命名关系图（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--graph-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--title")
    p.add_argument("--description")
    p = add("graph-add-to-graph", "把主体/关系加入某张图")
    p.add_argument("--case-id", required=True)
    p.add_argument("--graph-id", required=True)
    p.add_argument("--entity-id", action="append", default=[])
    p.add_argument("--relation-id", action="append", default=[])
    p = add("graph-removal-impact", "查询删除主体/关系影响的图")
    p.add_argument("--case-id", required=True)
    p.add_argument("--entity-id")
    p.add_argument("--relation-id")
    p = add("evidence-catalog", "读取证据目录")
    p.add_argument("--case-id", required=True)
    p = add("evidence-add-group", "新建证据分组（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--purpose")
    p.add_argument("--request-key", required=True)
    p = add("evidence-add-item", "新建证据条目（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--group-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--purpose")
    p.add_argument("--request-key", required=True)
    p = add("evidence-update-item", "修改证据条目（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--item-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--title")
    p.add_argument("--purpose")
    p = add("evidence-add-binding", "给证据条目绑定材料（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--item-id", required=True)
    p.add_argument("--material-id", required=True)
    p.add_argument("--page-start", type=int)
    p.add_argument("--page-end", type=int)
    p.add_argument("--request-key", required=True)
    p = add("evidence-import-catalog", "提交外部目录草稿（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--import-material-id", required=True)
    p.add_argument("--rows-file", required=True,
                   help="JSON 文件：{\"rows\": [...]} 或直接是行数组")
    p.add_argument("--request-key", required=True)

    p = add("export-chart-audit-bundle", "读取对外图表复核审计包")
    p.add_argument("--case-id", required=True)
    p.add_argument("--chart-type", choices=["graph", "claims"], required=True)
    p.add_argument("--target-id", required=True)
    p.add_argument("--profile", choices=["client", "court"], required=True)
    p.add_argument("--snapshot-id")

    # 诉请金额（actor=agent, entry=cli）；确认/撤回不在此提供。
    p = add("claims", "读取案件诉请金额快照")
    p.add_argument("--case-id", required=True)
    p = add("claim-candidates", "检索本案已抽取的金额候选")
    p.add_argument("--case-id", required=True)
    p.add_argument("--query")
    p = add("claim-add-group", "提交诉请分组候选（待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--request-key", required=True)
    p.add_argument("--currency", default="CNY")
    p.add_argument("--note")
    p.add_argument("--exclude-from-total", action="store_true",
                   help="不把该分组计入全案总计（重叠程序如一审/二审）")
    p = add("claim-update-group", "提交分组修改候选（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--group-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--request-key", required=True)
    p.add_argument("--title")
    p.add_argument("--currency")
    p.add_argument("--note")
    p.add_argument("--clear-note", action="store_true")
    p.add_argument("--include-in-total", action=argparse.BooleanOptionalAction, default=None)
    p = add("claim-add-item", "提交金额项候选（整数分，待确认）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--group-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--category", required=True, choices=CLAIM_CATEGORIES)
    p.add_argument("--amount-cents", type=int, required=True)
    p.add_argument("--request-key", required=True)
    p.add_argument("--calc-note")
    p.add_argument("--segment-id")
    p.add_argument("--material-id")
    p.add_argument("--annotation-id")
    p = add("claim-update-item", "提交金额项修改候选（CAS）")
    p.add_argument("--case-id", required=True)
    p.add_argument("--item-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--request-key", required=True)
    p.add_argument("--title")
    p.add_argument("--category", choices=CLAIM_CATEGORIES)
    p.add_argument("--amount-cents", type=int)
    p.add_argument("--calc-note")
    p.add_argument("--clear-calc-note", action="store_true")
    p.add_argument("--segment-id")
    p.add_argument("--material-id")
    p.add_argument("--annotation-id")
    p.add_argument("--clear-source", action="store_true")
    return parser


def dispatch(args: argparse.Namespace) -> Any:
    audit.set_context(actor="cli", request_id=uuid.uuid4())
    session = get_session_factory()()
    try:
        if args.command == "create-case":
            return cases.create_case(session, title=args.title, description=args.description,
                                     actor="agent", entry="cli")
        if args.command == "list-cases":
            return cases.list_cases(session, offset=args.offset, limit=args.limit)
        if args.command == "import":
            return imports.create_intake(
                session, _storage(), case_id=_uuid(args.case_id), source_path=args.source_path,
                source_label=args.source_label, request_key=args.request_key)
        if args.command == "list-materials":
            items, total, case_total, categories = materials.list_materials(
                session, case_id=_uuid(args.case_id), category=args.category,
                offset=args.offset, limit=args.limit)
            return {"items": items, "offset": args.offset, "limit": args.limit,
                    "total": total, "case_total": case_total, "categories": categories}
        if args.command == "update-classification":
            revision = materials.update_classification(
                session, case_id=_uuid(args.case_id), material_id=_uuid(args.material_id),
                category=args.category, expected_revision=args.expected_revision)
            return {"revision": revision}
        if args.command == "update-display-name":
            revision = materials.update_display_name(
                session, case_id=_uuid(args.case_id), material_id=_uuid(args.material_id),
                display_name=args.display_name, expected_revision=args.expected_revision)
            return {"revision": revision}
        if args.command == "create-working-copy":
            working_id, relative = materials.create_working_copy(
                session, _storage(), case_id=_uuid(args.case_id),
                file_version_id=_uuid(args.file_version_id), display_name=args.display_name)
            return {"working_copy_id": working_id, "relative_path": relative}
        if args.command == "register-file-version":
            version_id, job_id = materials.register_file_version(
                session, _storage(), case_id=_uuid(args.case_id),
                working_copy_id=_uuid(args.working_copy_id),
                expected_revision=args.expected_revision)
            return {"file_version_id": version_id, "job_id": job_id}
        if args.command == "reparse":
            run_id, job_id = parsing.reparse(
                session, case_id=_uuid(args.case_id), file_version_id=_uuid(args.file_version_id),
                force_ocr_pages=args.force_ocr_page)
            return {"parse_run_id": run_id, "job_id": job_id}
        if args.command == "search":
            return search.search_case(
                session, case_id=_uuid(args.case_id), query=args.query,
                mode=SearchMode(args.mode), scope=SearchScope(args.scope),
                currency=args.currency, offset=args.offset, limit=args.limit)
        if args.command == "read-source":
            return search.read_source(
                session, case_id=_uuid(args.case_id), source_ref=_uuid(args.source_ref),
                context_chars=args.context_chars)
        if args.command == "get-original":
            return _get_original(session, _uuid(args.case_id), _uuid(args.material_id))
        if args.command == "job-status":
            return jobs.get_job_status(session, case_id=_uuid(args.case_id),
                                       job_id=_uuid(args.job_id))
        if args.command == "retry-job":
            job = jobs.retry_job(session, case_id=_uuid(args.case_id), job_id=_uuid(args.job_id))
            return {"job_id": job.id, "status": job.status}
        if args.command == "index-status":
            return search.get_index_status(session, case_id=_uuid(args.case_id))
        if args.command == "save-analysis":
            if (args.content is None) == (args.content_file is None):
                raise WorkbenchError(
                    ErrorCode.INVALID_INPUT,
                    "请提供 --content 或 --content-file（且两者只能选其一）",
                )
            content = args.content
            if args.content_file is not None:
                if not args.content_file.strip():
                    raise WorkbenchError(ErrorCode.INVALID_INPUT, "--content-file 路径不能为空")
                try:
                    with open(args.content_file, encoding="utf-8") as handle:
                        content = handle.read()
                except (OSError, UnicodeDecodeError) as exc:
                    raise WorkbenchError(
                        ErrorCode.INVALID_INPUT,
                        f"无法读取分析正文文件：{args.content_file}",
                        {"error": type(exc).__name__},
                    ) from exc
            return analysis.save_analysis(
                session, _storage(), case_id=_uuid(args.case_id), title=args.title,
                content=content, statement_kind=StatementKind(args.statement_kind),
                source_refs=[_uuid(ref) for ref in args.source_ref],
                analysis_id=_uuid(args.analysis_id) if args.analysis_id else None,
                expected_revision=args.expected_revision)
        if args.command == "review-analysis":
            return analysis.review_analysis(
                session, case_id=_uuid(args.case_id), analysis_id=_uuid(args.analysis_id),
                version_id=_uuid(args.version_id), expected_revision=args.expected_revision,
                review_status=ReviewStatus(args.review_status))
        if args.command == "list-analyses":
            return analysis.list_analyses(session, case_id=_uuid(args.case_id),
                                          offset=args.offset, limit=args.limit)
        if args.command == "get-analysis":
            return analysis.get_analysis(session, case_id=_uuid(args.case_id),
                                         analysis_id=_uuid(args.analysis_id))
        if args.command == "list-analysis-versions":
            return analysis.list_analysis_versions(
                session, case_id=_uuid(args.case_id), analysis_id=_uuid(args.analysis_id),
                offset=args.offset, limit=args.limit)
        if args.command == "get-analysis-version":
            return analysis.get_analysis_version(
                session, case_id=_uuid(args.case_id), analysis_id=_uuid(args.analysis_id),
                version_id=_uuid(args.version_id))
        if args.command == "check-consistency":
            from . import consistency

            report = consistency.check_consistency(
                session, _storage(),
                case_id=_uuid(args.case_id) if args.case_id else None)
            return report.to_dict()
        if args.command == "repair-consistency":
            from . import consistency

            return consistency.repair_consistency(
                session, _storage(),
                case_id=_uuid(args.case_id) if args.case_id else None)
        if args.command == "backup":
            from pathlib import Path

            from . import backup

            settings = load_settings()
            pg_bin = resolve_pg_bin(settings=settings)
            result = backup.create_backup(
                session, _storage(), Path(args.destination),
                database_url=settings.database_url,
                pg_bin=str(pg_bin) if pg_bin else None)
            return {"destination": str(result.destination),
                    "database_sha256": result.database_sha256,
                    "file_count": result.file_count, "total_bytes": result.total_bytes,
                    "complete": result.complete}
        if args.command == "verify-backup":
            from pathlib import Path

            from . import backup

            manifest = backup.verify_backup(Path(args.destination))
            return {"complete": manifest["complete"], "schema_version": manifest["schema_version"],
                    "created_at": manifest["created_at"],
                    "files": len(manifest["files"])}
        if args.command == "restore":
            from pathlib import Path

            from . import backup

            settings = load_settings()
            pg_bin = resolve_pg_bin(settings=settings)
            result = backup.restore_backup(
                Path(args.destination), data_root=Path(args.data_root),
                database_url=settings.database_url,
                pg_bin=str(pg_bin) if pg_bin else None)
            audit.record(session, operation="backup.restore", target_type="backup",
                         target_id=result["database_sha256"], commit=True)
            return result
        if args.command == "clear-maintenance":
            from . import backup

            backup.clear_maintenance(session)
            audit.record(session, operation="maintenance.clear", target_type="maintenance",
                         target_id="1", commit=True)
            return {"maintenance": False}
        if args.command == "progress":
            from . import progress
            return progress.get_progress(session, _uuid(args.case_id))
        if args.command == "progress-rules":
            from . import progress_rules
            return {"rules": progress_rules.get_rules()}
        if args.command == "progress-add-node":
            from . import progress
            from .schemas import AddNodeInput
            payload = AddNodeInput(
                case_id=_uuid(args.case_id), kind=args.kind, title=args.title,
                request_key=args.request_key,
                parent_id=_uuid(args.parent_id) if args.parent_id else None,
                state=args.state, note=args.note)
            return progress.add_node(session, payload, actor="agent", entry="cli")
        if args.command == "progress-set-active-stage":
            from . import progress
            from .schemas import SetActiveStageInput
            payload = SetActiveStageInput(
                case_id=_uuid(args.case_id), stage_id=_uuid(args.stage_id),
                expected_revision=args.expected_revision)
            return progress.set_active_stage(session, payload, actor="agent", entry="cli")
        if args.command == "progress-insert-fragment":
            from . import progress
            from .schemas import InsertFragmentInput
            payload = InsertFragmentInput(
                case_id=_uuid(args.case_id), fragment_key=args.fragment_key,
                parent_id=_uuid(args.parent_id) if args.parent_id else None,
                before_node_id=_uuid(args.before_node_id) if args.before_node_id else None)
            return progress.insert_fragment(session, payload, actor="agent", entry="cli")
        if args.command == "progress-update-node":
            from . import progress
            from .schemas import NodeChanges, UpdateNodeInput
            changes = NodeChanges(**{k: v for k, v in
                                     (("title", args.title), ("state", args.state), ("note", args.note))
                                     if v is not None})
            payload = UpdateNodeInput(case_id=_uuid(args.case_id), node_id=_uuid(args.node_id),
                                      expected_revision=args.expected_revision, changes=changes)
            return progress.update_node(session, payload, actor="agent", entry="cli")
        if args.command == "progress-move-node":
            from . import progress
            from .schemas import MoveNodeInput
            payload = MoveNodeInput(
                case_id=_uuid(args.case_id), node_id=_uuid(args.node_id),
                expected_revision=args.expected_revision,
                new_parent_id=_uuid(args.new_parent_id) if args.new_parent_id else None,
                before_node_id=_uuid(args.before_node_id) if args.before_node_id else None)
            return progress.move_node(session, payload, actor="agent", entry="cli")
        if args.command == "progress-remove-node":
            from . import progress
            from .schemas import NodeRevisionInput
            payload = NodeRevisionInput(case_id=_uuid(args.case_id), node_id=_uuid(args.node_id),
                                        expected_revision=args.expected_revision)
            return progress.remove_node(session, payload, actor="agent", entry="cli")
        if args.command == "progress-add-relation":
            from . import progress
            from .schemas import AddRelationInput
            payload = AddRelationInput(
                case_id=_uuid(args.case_id), from_node_id=_uuid(args.from_node_id),
                to_node_id=_uuid(args.to_node_id), relation_type=args.relation_type, note=args.note)
            return progress.add_relation(session, payload, actor="agent", entry="cli")
        if args.command == "progress-remove-relation":
            from . import progress
            from .schemas import RemoveRelationInput
            payload = RemoveRelationInput(case_id=_uuid(args.case_id), relation_id=_uuid(args.relation_id))
            return progress.remove_relation(session, payload, actor="agent", entry="cli")
        if args.command == "progress-link":
            from . import progress
            from .schemas import LinkNodeInput, LinkTarget
            targets = {k: _uuid(v) for k, v in
                       (("material_id", args.material_id), ("analysis_id", args.analysis_id),
                        ("annotation_id", args.annotation_id)) if v}
            if len(targets) != 1:
                raise WorkbenchError(ErrorCode.INVALID_INPUT, "关联必须且只能指定一个对象")
            payload = LinkNodeInput(case_id=_uuid(args.case_id), node_id=_uuid(args.node_id),
                                    target=LinkTarget(**targets))
            return progress.link_node(session, payload, actor="agent", entry="cli")
        if args.command == "progress-unlink":
            from . import progress
            from .schemas import UnlinkInput
            payload = UnlinkInput(case_id=_uuid(args.case_id), link_id=_uuid(args.link_id))
            return progress.unlink_node(session, payload, actor="agent", entry="cli")
        if args.command == "graph":
            from . import graph
            return graph.get_workspace(
                session, _uuid(args.case_id),
                _uuid(args.graph_id) if args.graph_id else None)
        if args.command == "graph-rules":
            from . import graph_rules
            return {"rules": graph_rules.get_rules()}
        if args.command == "graph-add-entity":
            from . import graph
            from .schemas import AddEntityInput
            payload = AddEntityInput(
                case_id=_uuid(args.case_id), kind=args.kind, name=args.name,
                roles=list(args.role or []), note=args.note,
                graph_id=_uuid(args.graph_id) if args.graph_id else None,
                request_key=args.request_key)
            return graph.add_entity(session, payload, actor="agent", entry="cli")
        if args.command == "graph-update-entity":
            from . import graph
            from .schemas import EntityChanges, UpdateEntityInput
            fields: dict[str, Any] = {}
            if args.kind is not None:
                fields["kind"] = args.kind
            if args.name is not None:
                fields["name"] = args.name
            if args.role is not None:
                fields["roles"] = list(args.role)
            if args.clear_note:
                fields["note"] = None
            elif args.note is not None:
                fields["note"] = args.note
            payload = UpdateEntityInput(
                case_id=_uuid(args.case_id), entity_id=_uuid(args.entity_id),
                expected_revision=args.expected_revision,
                changes=EntityChanges(**fields))
            return graph.update_entity(session, payload, actor="agent", entry="cli")
        if args.command == "graph-add-alias":
            from . import graph
            from .schemas import AddAliasInput
            payload = AddAliasInput(case_id=_uuid(args.case_id),
                                    entity_id=_uuid(args.entity_id), alias=args.alias)
            return graph.add_alias(session, payload, actor="agent", entry="cli")
        if args.command == "graph-add-relation":
            from datetime import date

            from . import graph
            from .schemas import AddGraphRelationInput, RelationAttrs
            attrs = RelationAttrs(
                share_pct=args.share_pct, amount_cents=args.amount_cents,
                currency=args.currency,
                date_start=date.fromisoformat(args.date_start) if args.date_start else None,
                date_end=date.fromisoformat(args.date_end) if args.date_end else None)
            payload = AddGraphRelationInput(
                case_id=_uuid(args.case_id),
                from_entity_id=_uuid(args.from_entity_id),
                to_entity_id=_uuid(args.to_entity_id), kind=args.kind,
                label=args.label, credibility=args.credibility,
                directed=args.directed == "true", attrs=attrs, note=args.note,
                graph_id=_uuid(args.graph_id) if args.graph_id else None,
                request_key=args.request_key)
            return graph.add_relation(session, payload, actor="agent", entry="cli")
        if args.command == "graph-update-relation":
            from . import graph
            from .schemas import RelationChanges, UpdateRelationInput
            fields = {}
            for key, value in (("kind", args.kind), ("label", args.label),
                               ("credibility", args.credibility)):
                if value is not None:
                    fields[key] = value
            if args.directed is not None:
                fields["directed"] = args.directed == "true"
            if args.clear_note:
                fields["note"] = None
            elif args.note is not None:
                fields["note"] = args.note
            payload = UpdateRelationInput(
                case_id=_uuid(args.case_id), relation_id=_uuid(args.relation_id),
                expected_revision=args.expected_revision,
                changes=RelationChanges(**fields))
            return graph.update_relation(session, payload, actor="agent", entry="cli")
        if args.command == "graph-add-source":
            from . import graph
            from .schemas import AddSourceInput, SourceRefInput
            payload = AddSourceInput(
                case_id=_uuid(args.case_id),
                entity_id=_uuid(args.entity_id) if args.entity_id else None,
                relation_id=_uuid(args.relation_id) if args.relation_id else None,
                ref=SourceRefInput(
                    segment_id=_uuid(args.segment_id) if args.segment_id else None,
                    material_id=_uuid(args.material_id) if args.material_id else None,
                    annotation_id=_uuid(args.annotation_id) if args.annotation_id else None),
                proves=args.proves, request_key=args.request_key)
            return graph.add_source(session, payload, actor="agent", entry="cli")
        if args.command == "graph-create-graph":
            from . import graph
            from .schemas import CreateGraphInput
            payload = CreateGraphInput(case_id=_uuid(args.case_id), title=args.title,
                                       description=args.description,
                                       request_key=args.request_key)
            return graph.create_graph(session, payload, actor="agent", entry="cli")
        if args.command == "graph-update-graph":
            from . import graph
            from .schemas import GraphChanges, UpdateGraphInput
            fields = {key: value for key, value in
                      (("title", args.title), ("description", args.description))
                      if value is not None}
            payload = UpdateGraphInput(case_id=_uuid(args.case_id),
                                       graph_id=_uuid(args.graph_id),
                                       expected_revision=args.expected_revision,
                                       changes=GraphChanges(**fields))
            return graph.update_graph(session, payload, actor="agent", entry="cli")
        if args.command == "graph-add-to-graph":
            from . import graph
            from .schemas import AddToGraphInput
            payload = AddToGraphInput(
                case_id=_uuid(args.case_id), graph_id=_uuid(args.graph_id),
                entity_ids=[_uuid(item) for item in args.entity_id],
                relation_ids=[_uuid(item) for item in args.relation_id])
            return graph.add_to_graph(session, payload, actor="agent", entry="cli")
        if args.command == "graph-removal-impact":
            from . import graph
            return graph.get_removal_impact(
                session, _uuid(args.case_id),
                entity_id=_uuid(args.entity_id) if args.entity_id else None,
                relation_id=_uuid(args.relation_id) if args.relation_id else None)
        if args.command == "evidence-catalog":
            from . import evidence_catalog
            return evidence_catalog.get_catalog(session, _uuid(args.case_id))
        if args.command == "evidence-add-group":
            from . import evidence_catalog
            from .schemas import AddEvidenceGroupInput
            payload = AddEvidenceGroupInput(case_id=_uuid(args.case_id), title=args.title,
                                            purpose=args.purpose,
                                            request_key=args.request_key)
            return evidence_catalog.add_group(session, payload, actor="agent", entry="cli")
        if args.command == "evidence-add-item":
            from . import evidence_catalog
            from .schemas import AddEvidenceItemInput
            payload = AddEvidenceItemInput(case_id=_uuid(args.case_id),
                                           group_id=_uuid(args.group_id), title=args.title,
                                           purpose=args.purpose,
                                           request_key=args.request_key)
            return evidence_catalog.add_item(session, payload, actor="agent", entry="cli")
        if args.command == "evidence-update-item":
            from . import evidence_catalog
            from .schemas import EvidenceItemChanges, UpdateEvidenceItemInput
            fields = {key: value for key, value in
                      (("title", args.title), ("purpose", args.purpose)) if value is not None}
            payload = UpdateEvidenceItemInput(
                case_id=_uuid(args.case_id), item_id=_uuid(args.item_id),
                expected_revision=args.expected_revision,
                changes=EvidenceItemChanges(**fields))
            return evidence_catalog.update_item(session, payload, actor="agent", entry="cli")
        if args.command == "evidence-add-binding":
            from . import evidence_catalog
            from .schemas import AddEvidenceBindingInput
            payload = AddEvidenceBindingInput(
                case_id=_uuid(args.case_id), item_id=_uuid(args.item_id),
                material_id=_uuid(args.material_id), page_start=args.page_start,
                page_end=args.page_end, request_key=args.request_key)
            return evidence_catalog.add_binding(session, payload, actor="agent", entry="cli")
        if args.command == "evidence-import-catalog":
            from . import evidence_catalog
            from .schemas import ImportEvidenceCatalogInput
            with open(args.rows_file, encoding="utf-8") as handle:
                loaded = json.load(handle)
            rows = loaded.get("rows") if isinstance(loaded, dict) else loaded
            if not isinstance(rows, list):
                raise WorkbenchError(ErrorCode.INVALID_INPUT,
                                     "目录草稿文件须是行数组或 {\"rows\": [...]}")
            payload = ImportEvidenceCatalogInput(
                case_id=_uuid(args.case_id),
                import_material_id=_uuid(args.import_material_id),
                rows=rows, request_key=args.request_key)
            return evidence_catalog.import_catalog(session, payload, actor="agent", entry="cli")
        if args.command == "export-chart-audit-bundle":
            from . import chart_export
            request = chart_export.ChartRequest(
                chart_type=args.chart_type, target_id=_uuid(args.target_id), profile=args.profile,
                snapshot_id=_uuid(args.snapshot_id) if args.snapshot_id is not None else None,
            )
            return chart_export.build_chart_audit_bundle(session, _uuid(args.case_id), request)

        if args.command == "claims":
            from . import claims
            return claims.get_claims(session, _uuid(args.case_id))
        if args.command == "claim-candidates":
            from . import claims
            return claims.list_amount_candidates(session, _uuid(args.case_id), query=args.query)
        if args.command == "claim-add-group":
            from . import claims
            from .schemas import AddClaimGroupInput
            payload = AddClaimGroupInput(
                case_id=_uuid(args.case_id), title=args.title, request_key=args.request_key,
                currency=args.currency, include_in_total=not args.exclude_from_total,
                note=args.note)
            return claims.add_group(session, payload, actor="agent", entry="cli")
        if args.command == "claim-update-group":
            from . import claims
            from .schemas import ClaimGroupChanges, UpdateClaimGroupInput
            fields: dict[str, Any] = {}
            if args.title is not None:
                fields["title"] = args.title
            if args.currency is not None:
                fields["currency"] = args.currency
            if args.include_in_total is not None:
                fields["include_in_total"] = args.include_in_total
            if args.clear_note:
                fields["note"] = None
            elif args.note is not None:
                fields["note"] = args.note
            payload = UpdateClaimGroupInput(
                case_id=_uuid(args.case_id), group_id=_uuid(args.group_id),
                expected_revision=args.expected_revision, changes=ClaimGroupChanges(**fields),
                request_key=args.request_key)
            return claims.update_group(session, payload, actor="agent", entry="cli")
        if args.command == "claim-add-item":
            from . import claims
            from .schemas import AddClaimItemInput
            payload = AddClaimItemInput(
                case_id=_uuid(args.case_id), group_id=_uuid(args.group_id), title=args.title,
                category=args.category, amount_cents=args.amount_cents,
                request_key=args.request_key, calc_note=args.calc_note,
                segment_id=_uuid(args.segment_id) if args.segment_id else None,
                material_id=_uuid(args.material_id) if args.material_id else None,
                annotation_id=_uuid(args.annotation_id) if args.annotation_id else None)
            return claims.add_item(session, payload, actor="agent", entry="cli")
        if args.command == "claim-update-item":
            from . import claims
            from .schemas import ClaimItemChanges, UpdateClaimItemInput
            fields = {}
            if args.title is not None:
                fields["title"] = args.title
            if args.category is not None:
                fields["category"] = args.category
            if args.amount_cents is not None:
                fields["amount_cents"] = args.amount_cents
            if args.clear_calc_note:
                fields["calc_note"] = None
            elif args.calc_note is not None:
                fields["calc_note"] = args.calc_note
            sources = {"segment_id": args.segment_id, "material_id": args.material_id,
                       "annotation_id": args.annotation_id}
            if args.clear_source:
                fields.update({name: None for name in sources})
            elif any(value is not None for value in sources.values()):
                fields.update({name: _uuid(value) if value else None
                               for name, value in sources.items()})
            payload = UpdateClaimItemInput(
                case_id=_uuid(args.case_id), item_id=_uuid(args.item_id),
                expected_revision=args.expected_revision, changes=ClaimItemChanges(**fields),
                request_key=args.request_key)
            return claims.update_item(session, payload, actor="agent", entry="cli")
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"未知命令: {args.command}")
    finally:
        session.close()


def _get_original(session, case_id: uuid.UUID, material_id: uuid.UUID) -> dict[str, Any]:
    from . import models

    material = session.get(models.Material, material_id)
    if material is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "材料不存在",
                             {"material_id": str(material_id)})
    if material.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "材料不属于该案件",
                             {"material_id": str(material_id)})
    if material.original_file_version_id is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "材料尚无原件版本",
                             {"material_id": str(material_id)})
    version = session.get(models.FileVersion, material.original_file_version_id)
    stored = session.get(models.StoredObject, version.stored_object_id)
    return {
        "file_version_id": str(version.id),
        "integrity_status": stored.integrity_status if stored else "missing",
        "browse_url": f"/api/v1/cases/{case_id}/file-versions/{version.id}/preview",
    }


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if hasattr(stream, "reconfigure") and (sys.platform == "win32" or encoding != "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run-worker":
            count = worker.run_worker(get_engine(), _storage(), once=True)
            print(_dump({"processed_jobs": count}))
            return 0
        result = dispatch(args)
        print(_dump(result))
        return 0
    except WorkbenchError as exc:
        print(_dump({"code": exc.code.value, "message": exc.message,
                     "details": exc.details}), file=sys.stderr)
        return 2
    except ValidationError as exc:
        # 无效入参（如非整数分、越界金额）在入口就给出结构化错误，
        # 不能落进下面的 internal_error，也不能输出 traceback。
        print(_dump({"code": ErrorCode.INVALID_INPUT.value,
                     "message": "命令参数不合法",
                     "details": {"errors": json.loads(exc.json(include_url=False))}}),
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary must not leak a traceback as JSON
        logger.error("cli failure: %s", type(exc).__name__, exc_info=exc)
        print(_dump({"code": ErrorCode.INTERNAL_ERROR.value,
                     "message": "命令执行失败"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
