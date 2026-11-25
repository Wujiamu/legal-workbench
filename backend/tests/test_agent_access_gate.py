"""Agent 访问硬围栏：默认锁死、单案白名单、无授权改写入口。

这是安全回归的守护文件。它验证的不是「某个工具能用」，而是「未获授权的调用
一律被挡在数据库之前」，并且**没有**任何 MCP 工具能改动授权策略。

覆盖四层：

1. **默认锁死**：没有策略文件、没有环境变量时，除 `list_cases(limit=0)` 探活与
   两组只读规则外，全部 `case_id` 工具与 `create_case` 都返回 `forbidden`，
   且错误信息给出本机授权命令。
2. **反射审计**：遍历 `server._tool_manager.list_tools()`，对**每一个**带
   `case_id` 参数的工具逐一调用，断言无漏网；同时断言不存在任何
   grant / unlock / allow 类写工具。
3. **单案隔离**：只授权 A 时，A 的读写正常，B 的读写导出全部 `forbidden`，
   `list_cases` 只返回 A 且 `total == 1`（不泄露库里还有 B）。
4. **策略文件与 CLI 闭环**：损坏文件降级 `deny_all`、`0600` 权限、不含凭据；
   `scripts/lwb agent-*` 五个命令的退出码与状态迁移。

真实 PostgreSQL（conftest 重建 `legal_workbench_test`），全部 TEST-ONLY 合成数据。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import uuid
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from legal_workbench import agent_access, cases, mcp_server
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError


BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
LWB_SCRIPT = REPO_ROOT / "scripts" / "lwb"

# 授权相关词根：任何一个出现在工具名里都说明 MCP 侧能改策略，这是红线。
GRANT_WORD_ROOTS = ("grant", "unlock", "allow", "authorize", "authorise", "permit",
                    "revoke", "lock")


# --- 夹具 -----------------------------------------------------------------

@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    """An isolated policy path that does not exist yet (⇒ default deny)."""
    target = tmp_path / "runtime" / "agent-access.json"
    monkeypatch.setenv("LWB_AGENT_ACCESS_FILE", str(target))
    monkeypatch.delenv("LWB_MCP_ALLOWED_CASES", raising=False)
    monkeypatch.delenv("LWB_MCP_ACCESS_MODE", raising=False)
    return target


@pytest.fixture
def two_cases(db):
    """Two committed cases: MCP tools open their own session, so commit first.

    Created through the service (not the raw row factory) so each case also gets
    its default progress fragment — otherwise ``get_progress`` would return an
    empty tree and the "authorised reads work" assertions would pass vacuously.
    """
    first = cases.create_case(db, title="TEST-ONLY 授权案件甲").case_id
    second = cases.create_case(db, title="TEST-ONLY 未授权案件乙").case_id
    db.commit()
    return first, second


def _tool_map() -> dict[str, Any]:
    return {tool.name: tool for tool in mcp_server.server._tool_manager.list_tools()}


def _case_tools() -> dict[str, Any]:
    return {name: tool for name, tool in _tool_map().items()
            if "case_id" in ((tool.parameters or {}).get("properties") or {})}


def _forbidden(fn) -> WorkbenchError:
    with pytest.raises(WorkbenchError) as caught:
        fn()
    assert caught.value.code is ErrorCode.FORBIDDEN, caught.value
    return caught.value


# --- 1. 默认锁死 -----------------------------------------------------------

def test_absent_policy_file_means_deny_all(policy_file):
    assert not policy_file.exists()
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.DENY_ALL
    assert policy.allowed_case_ids == ()
    assert policy.allow_create_case is False
    assert policy.source == agent_access.SOURCE_DEFAULT_DENY
    # 读取策略不会顺手创建文件：源码/生产环境绝不预生成。
    assert not policy_file.exists()


def test_default_lock_blocks_every_representative_tool(policy_file, two_cases):
    """The documented tool families all refuse before touching the database."""
    first, _second = two_cases
    case_id = str(first)
    fake = str(uuid.uuid4())
    calls = {
        "list_cases(limit=20)": lambda: mcp_server.list_cases(limit=20),
        "create_case": lambda: mcp_server.create_case("TEST-ONLY 不该建成"),
        "search_case": lambda: mcp_server.search_case(case_id, "合同"),
        "get_progress": lambda: mcp_server.get_progress(case_id),
        "get_graph": lambda: mcp_server.get_graph(case_id),
        "get_evidence_catalog": lambda: mcp_server.get_evidence_catalog(case_id),
        "get_claims": lambda: mcp_server.get_claims(case_id),
        "export_chart_audit_bundle": lambda: mcp_server.export_chart_audit_bundle(
            case_id, "graph", fake, "court"),
        "save_analysis": lambda: mcp_server.save_analysis(
            case_id, "标题", "正文", "agent_analysis", []),
        "annotate_file": lambda: mcp_server.annotate_file(
            case_id, fake, "transcription", "TEST-ONLY 标注"),
        "read_source": lambda: mcp_server.read_source(case_id, fake),
        "list_materials": lambda: mcp_server.list_materials(case_id),
        "import_materials": lambda: mcp_server.import_materials(
            case_id, "/tmp", "TEST-ONLY", "k1"),
        "progress_add_node": lambda: mcp_server.progress_add_node(
            case_id, "work", "不该建", "k2"),
    }
    for label, call in calls.items():
        error = _forbidden(call)
        # 提示必须能直接照着执行，且不能泄露任何案件标题。
        assert "agent-grant" in error.message, label
        assert "TEST-ONLY" not in error.message, label
        assert "授权案件甲" not in error.message, label


def test_default_lock_allows_only_the_liveness_probe(policy_file, two_cases):
    payload = mcp_server.list_cases(limit=0)
    assert payload == {"items": [], "offset": 0, "limit": 0, "total": 0, "mode": "deny_all"}

    # 默认参数（limit=50）不是探活，必须拒绝：否则「顺手列一下」就把全库泄露。
    error = _forbidden(lambda: mcp_server.list_cases())
    assert "agent-grant" in error.message
    assert "deny_all" in error.message


def test_rule_sets_stay_readable_without_any_grant(policy_file):
    """规则配置不含案件数据，保持只读可用，Agent 才能解释字段含义。"""
    assert mcp_server.get_progress_rules()["rules"]
    assert mcp_server.get_graph_rules()["rules"]


def test_access_policy_self_check_leaks_no_case_details(policy_file, two_cases):
    payload = mcp_server.get_access_policy()
    assert payload["mode"] == "deny_all"
    assert payload["allowed_case_count"] == 0
    assert payload["allow_create_case"] is False
    assert payload["source"] == "default_deny"
    assert "agent-grant" in payload["grant_command"]
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "授权案件甲" not in rendered and "未授权案件乙" not in rendered
    assert str(two_cases[0]) not in rendered


def test_a_malformed_case_id_is_invalid_input_not_forbidden(policy_file):
    with pytest.raises(WorkbenchError) as caught:
        mcp_server.get_progress("not-a-uuid")
    assert caught.value.code is ErrorCode.INVALID_INPUT


# --- 2. 反射审计：无漏网工具、无授权写工具 ---------------------------------

def test_every_case_tool_is_fenced_before_it_runs(policy_file):
    """每个带 case_id 的工具在默认锁死态下都被拦下（静态 + 反射双重守护）。"""
    tools = _case_tools()
    assert len(tools) >= 50, f"预期全部案件工具，实际只找到 {len(tools)} 个"

    unguarded: list[str] = []
    leaked: list[str] = []
    for name, tool in sorted(tools.items()):
        if not getattr(tool.fn, "__case_guard__", False):
            unguarded.append(name)
            continue
        try:
            # 只给 case_id：围栏必须在任何其他参数被解析、任何会话被打开之前触发。
            tool.fn(case_id=str(uuid.uuid4()))
        except WorkbenchError as exc:
            if exc.code is not ErrorCode.FORBIDDEN or "agent-grant" not in exc.message:
                leaked.append(f"{name}: {exc.code} {exc.message}")
        except Exception as exc:  # noqa: BLE001 - 任何非 WorkbenchError 都说明围栏没生效
            leaked.append(f"{name}: {type(exc).__name__} {exc}")
        else:
            leaked.append(f"{name}: 未抛错")

    assert unguarded == [], f"以下工具带 case_id 但没有 @_case_guard：{unguarded}"
    assert leaked == [], f"以下工具绕过了访问围栏：{leaked}"
    assert mcp_server._fence_audit() == []


def test_mcp_exposes_no_policy_mutation_tool(policy_file):
    names = set(_tool_map())
    offenders = [name for name in names
                 if any(root in name.lower() for root in GRANT_WORD_ROOTS)]
    assert offenders == [], f"MCP 不得暴露任何授权/解锁工具：{offenders}"
    assert "get_access_policy" in names
    # 只读自检工具本身不接受任何参数，无法被用来写入。
    assert not ((_tool_map()["get_access_policy"].parameters or {}).get("properties") or {})


def test_the_fence_audit_actually_catches_an_unguarded_tool(policy_file):
    """反向验证：把一个未加围栏的工具注册进去，审计必须报出来。"""
    def sneaky_probe(case_id: str) -> dict[str, object]:
        return {"case_id": case_id}

    mcp_server.server.add_tool(sneaky_probe, name="sneaky_probe")
    try:
        assert "sneaky_probe" in mcp_server._fence_audit()
        with pytest.raises(RuntimeError, match="sneaky_probe"):
            mcp_server._enforce_case_fence()
    finally:
        mcp_server.server.remove_tool("sneaky_probe")
    assert mcp_server._fence_audit() == []


# --- 3. 单案白名单隔离 -----------------------------------------------------

def test_allowlist_opens_only_the_granted_case(policy_file, two_cases):
    first, second = two_cases
    agent_access.grant_case(first)

    # A：读写都正常。
    snapshot = mcp_server.get_progress(str(first))
    assert snapshot["nodes"]
    stage = next(node for node in snapshot["nodes"] if node["kind"] == "stage")
    created = mcp_server.progress_add_node(str(first), "work", "TEST-ONLY 新工作", "g-1",
                                           parent_id=stage["id"])
    assert any(node["title"] == "TEST-ONLY 新工作" for node in created["nodes"])
    assert mcp_server.list_materials(str(first))["case_total"] == 0
    assert mcp_server.search_case(str(first), "合同")["total"] == 0
    assert mcp_server.get_claims(str(first))["groups"] == []

    # B：读、写、导出全部拒绝，且提示里不带 B 的任何信息。
    blocked = {
        "get_progress": lambda: mcp_server.get_progress(str(second)),
        "get_graph": lambda: mcp_server.get_graph(str(second)),
        "get_claims": lambda: mcp_server.get_claims(str(second)),
        "get_evidence_catalog": lambda: mcp_server.get_evidence_catalog(str(second)),
        "search_case": lambda: mcp_server.search_case(str(second), "合同"),
        "save_analysis": lambda: mcp_server.save_analysis(
            str(second), "标题", "正文", "agent_analysis", []),
        "export_chart_audit_bundle": lambda: mcp_server.export_chart_audit_bundle(
            str(second), "claims", str(uuid.uuid4()), "client"),
        "progress_add_node": lambda: mcp_server.progress_add_node(
            str(second), "work", "不该建", "g-2"),
    }
    for label, call in blocked.items():
        error = _forbidden(call)
        assert str(second) in error.message or "agent-grant" in error.message, label
        assert "未授权案件乙" not in error.message, label


def test_allowlist_list_cases_hides_the_rest_of_the_docket(policy_file, two_cases):
    first, second = two_cases
    agent_access.grant_case(first)

    payload = mcp_server.list_cases(limit=20)
    assert [item["case_id"] for item in payload["items"]] == [str(first)]
    assert payload["total"] == 1
    assert payload["mode"] == "allowlist"
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "未授权案件乙" not in rendered
    assert str(second) not in rendered


def test_allowlist_paging_never_reports_the_full_docket_total(policy_file, db):
    """授权 2 个、库里 3 个：total 只能等于 2，分页也只在授权集合内。"""
    granted = [cases.create_case(db, title=f"TEST-ONLY 甲{i}").case_id for i in range(2)]
    hidden = cases.create_case(db, title="TEST-ONLY 未授权").case_id
    db.commit()
    for case_id in granted:
        agent_access.grant_case(case_id)

    page = mcp_server.list_cases(offset=0, limit=1)
    assert page["total"] == 2 and len(page["items"]) == 1
    second_page = mcp_server.list_cases(offset=1, limit=1)
    assert second_page["total"] == 2 and len(second_page["items"]) == 1
    assert {page["items"][0]["case_id"], second_page["items"][0]["case_id"]} == {
        str(item) for item in granted}
    beyond = mcp_server.list_cases(offset=5, limit=1)
    assert beyond["items"] == [] and beyond["total"] == 2
    assert str(hidden) not in json.dumps(page, ensure_ascii=False)


def test_revoking_returns_to_deny_all_and_closes_reads(policy_file, two_cases):
    first, _second = two_cases
    agent_access.grant_case(first)
    assert mcp_server.get_claims(str(first))["groups"] == []

    policy = agent_access.revoke_case(first)
    assert policy.mode == agent_access.DENY_ALL and policy.allowed_case_ids == ()
    _forbidden(lambda: mcp_server.get_claims(str(first)))
    assert mcp_server.list_cases(limit=0)["mode"] == "deny_all"


def test_granting_a_second_case_keeps_the_first(policy_file, two_cases):
    first, second = two_cases
    agent_access.grant_case(first)
    policy = agent_access.grant_case(second)
    assert policy.mode == agent_access.ALLOWLIST
    assert set(policy.allowed_case_ids) == {first, second}
    assert mcp_server.list_cases(limit=20)["total"] == 2


def test_an_authorised_but_missing_case_reports_not_found(policy_file):
    """授权了但库里没有：报 not_found，而不是 forbidden——围栏与存在性各司其职。"""
    ghost = uuid.uuid4()
    agent_access.grant_case(ghost)
    with pytest.raises(WorkbenchError) as caught:
        mcp_server.get_claims(str(ghost))
    assert caught.value.code is ErrorCode.NOT_FOUND


# --- 4. create_case -------------------------------------------------------

def test_create_case_is_refused_under_deny_all_and_plain_allowlist(policy_file, two_cases):
    _forbidden(lambda: mcp_server.create_case("TEST-ONLY 不该建成"))

    agent_access.grant_case(two_cases[0])
    error = _forbidden(lambda: mcp_server.create_case("TEST-ONLY 仍不该建成"))
    assert error.code is ErrorCode.FORBIDDEN

    with get_session_factory()() as session:
        assert session.query(cases.models.Case).count() == 2


def test_create_case_needs_an_explicit_grant(policy_file, db):
    agent_access.allow_all_cases(allow_create_case=True)
    created = mcp_server.create_case("TEST-ONLY 显式允许的新案")
    assert created["case_id"]
    with get_session_factory()() as session:
        row = session.get(cases.models.Case, uuid.UUID(created["case_id"]))
        assert row is not None and row.title == "TEST-ONLY 显式允许的新案"


def test_allow_all_without_create_flag_still_blocks_creation(policy_file):
    agent_access.allow_all_cases()
    # allow_all 保持 cases.list_cases 的原有输出（不带 mode 字段）。
    assert mcp_server.list_cases(limit=5)["total"] == 0
    assert mcp_server.get_access_policy()["mode"] == "allow_all"
    _forbidden(lambda: mcp_server.create_case("TEST-ONLY 不该建成"))


# --- 5. 策略文件：降级、权限、内容 ---------------------------------------

@pytest.mark.parametrize("payload", [
    "",
    "not json at all",
    "[]",
    '{"mode": "bogus"}',
    '{"mode": "allow_all"}',
    '{"mode": "allow_all", "allowed_case_ids": "not-a-list"}',
    '{"mode": "allowlist", "allowed_case_ids": ["not-a-uuid"]}',
    '{"mode": "allowlist", "allowed_case_ids": [123]}',
])
def test_a_broken_policy_file_degrades_to_deny_all(policy_file, payload):
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(payload, encoding="utf-8")
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.DENY_ALL
    _forbidden(lambda: mcp_server.list_cases(limit=20))


def test_an_empty_allowlist_file_is_normalised_to_deny_all(policy_file):
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(json.dumps({"mode": "allowlist", "allowed_case_ids": [],
                                       "allow_create_case": True}), encoding="utf-8")
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.DENY_ALL
    assert policy.allow_create_case is False


def test_policy_file_is_private_and_carries_no_credentials(policy_file):
    case_id = uuid.uuid4()
    agent_access.grant_case(case_id)
    text = policy_file.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert set(payload) == {"mode", "allowed_case_ids", "allow_create_case", "updated_at"}
    assert payload["mode"] == "allowlist"
    assert payload["allowed_case_ids"] == [str(case_id)]
    assert payload["updated_at"].endswith("Z") and len(payload["updated_at"]) == 20
    for secret in ("postgresql://", "postgres://", "PASSWORD", "password", "lwb_app",
                   "lwb_super", "lwb_migrator", "DATABASE_URL", "://"):
        assert secret not in text
    if os.name == "posix":
        assert policy_file.stat().st_mode & 0o077 == 0
        assert policy_file.stat().st_mode & 0o600 == 0o600
    # 原子写入不留临时文件。
    assert [path.name for path in policy_file.parent.iterdir()] == ["agent-access.json"]


def test_lock_all_clears_the_allowlist_and_creation_flag(policy_file):
    agent_access.allow_all_cases(allow_create_case=True)
    assert agent_access.load_access_policy().allow_create_case is True
    policy = agent_access.lock_all()
    assert policy.mode == agent_access.DENY_ALL
    assert policy.allowed_case_ids == ()
    assert policy.allow_create_case is False


def test_granting_after_allow_all_narrows_to_the_named_case(policy_file):
    agent_access.allow_all_cases(allow_create_case=True)
    case_id = uuid.uuid4()
    policy = agent_access.grant_case(case_id)
    assert policy.mode == agent_access.ALLOWLIST
    assert policy.allowed_case_ids == (case_id,)


def test_filter_and_is_allowed_helpers_agree_with_the_gate(policy_file):
    first, second = uuid.uuid4(), uuid.uuid4()
    agent_access.grant_case(first)
    assert agent_access.is_case_allowed(first) is True
    assert agent_access.is_case_allowed(second) is False
    assert agent_access.filter_allowed_cases([second, first, first]) == (first,)


def test_describe_policy_warns_loudly_under_allow_all(policy_file):
    agent_access.allow_all_cases()
    lines = "\n".join(agent_access.describe_policy(agent_access.load_access_policy()))
    assert "allow_all" in lines and "风险" in lines and "离线" in lines


# --- 6. 环境变量覆盖 -------------------------------------------------------

def test_env_allowed_cases_opens_exactly_that_case(policy_file, monkeypatch, two_cases):
    first, second = two_cases
    monkeypatch.setenv("LWB_MCP_ALLOWED_CASES", str(first))
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.ALLOWLIST and policy.source == agent_access.SOURCE_ENV
    assert policy.allowed_case_ids == (first,)
    assert mcp_server.get_claims(str(first))["groups"] == []
    _forbidden(lambda: mcp_server.get_claims(str(second)))


def test_env_allowed_cases_accepts_a_comma_separated_list(policy_file, monkeypatch, two_cases):
    first, second = two_cases
    monkeypatch.setenv("LWB_MCP_ALLOWED_CASES", f" {first} , {second} ")
    policy = agent_access.load_access_policy()
    assert set(policy.allowed_case_ids) == {first, second}


@pytest.mark.parametrize("value", ["not-a-uuid", "11111111-1111-4111-8111-11111111111Z"])
def test_env_allowed_cases_rejects_a_malformed_uuid(policy_file, monkeypatch, value):
    """非法 UUID 必须显式拒绝，绝不静默丢弃（否则操作者以为已放行）。"""
    monkeypatch.setenv("LWB_MCP_ALLOWED_CASES", value)
    with pytest.raises(WorkbenchError) as caught:
        agent_access.load_access_policy()
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert "LWB_MCP_ALLOWED_CASES" in caught.value.message


def test_a_broken_env_still_leaves_the_self_check_readable(policy_file, monkeypatch):
    """环境变量写错时每个取数工具都该拒绝，但自检必须还能解释原因。"""
    monkeypatch.setenv("LWB_MCP_ALLOWED_CASES", "oops-not-a-uuid")
    _forbidden(lambda: mcp_server.get_claims(str(uuid.uuid4())))
    summary = mcp_server.get_access_policy()
    assert summary["mode"] == "deny_all"
    assert summary["source"] == "invalid_configuration"
    assert "LWB_MCP_ALLOWED_CASES" in summary["configuration_error"]
    assert "agent-grant" in summary["grant_command"]


def test_env_mode_rejects_unknown_values(policy_file, monkeypatch):
    monkeypatch.setenv("LWB_MCP_ACCESS_MODE", "open_sesame")
    with pytest.raises(WorkbenchError) as caught:
        agent_access.load_access_policy()
    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_env_allowlist_without_cases_is_a_lock(policy_file, monkeypatch):
    """`LWB_MCP_ACCESS_MODE=allowlist` 且没有案件清单 = 什么都没开，按锁死处理。"""
    monkeypatch.setenv("LWB_MCP_ACCESS_MODE", "allowlist")
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.DENY_ALL
    _forbidden(lambda: mcp_server.list_cases(limit=20))


def test_env_allow_all_is_never_the_default(policy_file, monkeypatch):
    """allow_all 只能由显式环境变量带出，且不会写文件。"""
    assert agent_access.load_access_policy().mode == agent_access.DENY_ALL
    monkeypatch.setenv("LWB_MCP_ACCESS_MODE", "allow_all")
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.ALLOW_ALL and policy.source == agent_access.SOURCE_ENV
    assert not policy_file.exists()


def test_a_locked_policy_file_cannot_be_reopened_by_the_environment(policy_file, monkeypatch):
    """文件锁死优先于环境变量：agent-lock 不能被某个残留 shell 变量撤销。"""
    agent_access.lock_all()
    monkeypatch.setenv("LWB_MCP_ACCESS_MODE", "allow_all")
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.DENY_ALL
    _forbidden(lambda: mcp_server.list_cases(limit=20))


def test_the_environment_still_narrows_a_broader_policy_file(policy_file, monkeypatch,
                                                             two_cases):
    first, second = two_cases
    agent_access.allow_all_cases()
    monkeypatch.setenv("LWB_MCP_ALLOWED_CASES", str(first))
    policy = agent_access.load_access_policy()
    assert policy.mode == agent_access.ALLOWLIST and policy.allowed_case_ids == (first,)
    _forbidden(lambda: mcp_server.get_claims(str(second)))


# --- 7. scripts/lwb 授权命令闭环 ------------------------------------------

def _run_module_cli(*argv: str) -> int:
    """Drive ``legal_workbench.cli`` in-process (the packaged ``--run-cli`` path)."""
    from legal_workbench import cli

    return cli.main(list(argv))


def _load_lwb() -> types.ModuleType:
    """Load ``scripts/lwb`` without leaving bytecode next to the script."""
    module = types.ModuleType("lwb_agent_access_test")
    module.__file__ = str(LWB_SCRIPT)
    sys.modules[module.__name__] = module
    try:
        exec(compile(LWB_SCRIPT.read_text(encoding="utf-8"), str(LWB_SCRIPT), "exec"),
             module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


@pytest.fixture
def lwb(policy_file, monkeypatch, tmp_path):
    module = _load_lwb()
    # 授权命令只读写策略文件，不应改工作目录，也不该真的起数据库。
    monkeypatch.setattr(module.os, "chdir", lambda _path: pytest.fail("授权命令不应 chdir"))
    monkeypatch.setenv("LWB_AGENT_REEXEC", "1")  # 本进程已有依赖，不必再转发
    return module


def _run_lwb(module, monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["lwb", *argv])
    return module.main()


def test_lwb_agent_status_reports_default_deny(lwb, monkeypatch, capsys, policy_file):
    assert _run_lwb(lwb, monkeypatch, "agent-status") == 0
    out = capsys.readouterr().out
    assert "deny_all" in out and "default_deny" in out
    assert "策略文件不存在" in out
    assert str(policy_file) in out
    assert "建案权限：关闭" in out


def test_lwb_agent_grant_then_status_then_revoke(lwb, monkeypatch, capsys, policy_file):
    case_id = str(uuid.uuid4())

    assert _run_lwb(lwb, monkeypatch, "agent-grant", case_id) == 0
    out = capsys.readouterr().out
    assert case_id in out and "allowlist" in out and "agent-revoke" in out
    stored = json.loads(policy_file.read_text(encoding="utf-8"))
    assert stored["mode"] == "allowlist" and stored["allowed_case_ids"] == [case_id]

    assert _run_lwb(lwb, monkeypatch, "agent-status") == 0
    status = capsys.readouterr().out
    assert "allowlist" in status and case_id in status

    assert _run_lwb(lwb, monkeypatch, "agent-revoke", case_id) == 0
    out = capsys.readouterr().out
    assert "自动退回默认锁死" in out
    stored = json.loads(policy_file.read_text(encoding="utf-8"))
    assert stored["mode"] == "deny_all" and stored["allowed_case_ids"] == []


def test_lwb_agent_grant_rejects_a_malformed_uuid(lwb, monkeypatch, capsys, policy_file):
    assert _run_lwb(lwb, monkeypatch, "agent-grant", "not-a-uuid") == 2
    err = capsys.readouterr().err
    assert "不是合法 UUID" in err and "agent-grant" in err
    assert not policy_file.exists()


def test_lwb_agent_grant_requires_exactly_one_argument(lwb, monkeypatch, capsys):
    assert _run_lwb(lwb, monkeypatch, "agent-grant") == 2
    assert _run_lwb(lwb, monkeypatch, "agent-grant", "a", "b") == 2
    assert "agent-grant <case_id>" in capsys.readouterr().err


def test_lwb_agent_lock_closes_everything(lwb, monkeypatch, capsys, policy_file):
    agent_access.grant_case(uuid.uuid4())
    assert _run_lwb(lwb, monkeypatch, "agent-lock") == 0
    assert "deny_all" in capsys.readouterr().out
    stored = json.loads(policy_file.read_text(encoding="utf-8"))
    assert stored["mode"] == "deny_all" and stored["allowed_case_ids"] == []
    assert stored["allow_create_case"] is False


def test_lwb_agent_lock_rejects_extra_arguments(lwb, monkeypatch, capsys):
    assert _run_lwb(lwb, monkeypatch, "agent-lock", "--now") == 2
    assert "agent-lock" in capsys.readouterr().err


def test_lwb_agent_allow_all_requires_the_confirm_flag(lwb, monkeypatch, capsys, policy_file):
    assert _run_lwb(lwb, monkeypatch, "agent-allow-all") == 2
    captured = capsys.readouterr()
    assert "拒绝执行" in captured.err and "--confirm" in captured.err
    assert not policy_file.exists(), "被拒绝时不得写策略文件"

    assert _run_lwb(lwb, monkeypatch, "agent-allow-all", "--confirm") == 0
    out = capsys.readouterr().out
    assert "警告" in out and "allow_all" in out and "离线" in out
    assert "agent-lock" in out
    assert json.loads(policy_file.read_text(encoding="utf-8"))["mode"] == "allow_all"


def test_lwb_agent_allow_all_rejects_other_flags(lwb, monkeypatch, capsys):
    assert _run_lwb(lwb, monkeypatch, "agent-allow-all", "--yes") == 2
    assert "--confirm" in capsys.readouterr().err


def test_lwb_usage_lists_both_command_groups(lwb, monkeypatch, capsys):
    assert _run_lwb(lwb, monkeypatch) == 2
    err = capsys.readouterr().err
    assert "agent-grant" in err and "register-instance" in err

    assert _run_lwb(lwb, monkeypatch, "nonsense") == 2
    assert "agent-lock" in capsys.readouterr().err


def test_lwb_lifecycle_commands_still_reject_stray_arguments(lwb, monkeypatch, capsys):
    """`scripts/lwb start extra` 这类拼错不能被当成合法命令静默执行。"""
    assert _run_lwb(lwb, monkeypatch, "start", "extra") == 2
    assert "用法" in capsys.readouterr().err


def test_the_module_cli_runs_the_same_agent_commands(policy_file, monkeypatch, capsys):
    """打包态没有 scripts/ 目录：`--run-cli agent-*` 必须走同一条实现。

    只验入口是否接通与输出是否可读；状态迁移本身已由上面 `scripts/lwb` 的用例
    逐条覆盖（两者调用同一个 ``agent_access_cli``）。
    """
    case_id = str(uuid.uuid4())
    assert _run_module_cli("agent-status") == 0
    assert "deny_all" in capsys.readouterr().out

    assert _run_module_cli("agent-grant", case_id) == 0
    assert case_id in capsys.readouterr().out
    assert json.loads(policy_file.read_text(encoding="utf-8"))["mode"] == "allowlist"

    assert _run_module_cli("agent-allow-all") == 2
    assert "--confirm" in capsys.readouterr().err

    assert _run_module_cli("agent-lock") == 0
    assert json.loads(policy_file.read_text(encoding="utf-8"))["mode"] == "deny_all"


def test_the_frozen_launcher_route_reaches_the_same_commands(policy_file, monkeypatch, capsys):
    """`<可执行文件> --run-cli agent-status` 是打包态唯一能用的授权入口。"""
    from legal_workbench import desktop_launcher

    assert desktop_launcher._run_cli(["agent-status"]) == 0
    assert "deny_all" in capsys.readouterr().out
    assert not policy_file.exists()


# --- 8. 真实 stdio 握手 ----------------------------------------------------

def _stdio_params(test_database, env_extra: dict[str, str]) -> StdioServerParameters:
    """A real stdio server bound to the throwaway test database.

    The subprocess must never inherit ``LWB_DATABASE_URL`` from the developer's
    ``.env``: that is the business database.
    """
    env = {**os.environ, "PYTHONPATH": str(BACKEND / "src"),
           "LWB_DATABASE_URL": test_database[1], "LWB_MIGRATION_URL": test_database[0]}
    env.pop("LWB_MCP_ACCESS_MODE", None)
    env.pop("LWB_MCP_ALLOWED_CASES", None)
    env.update(env_extra)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "legal_workbench.mcp_server"],
        cwd=str(BACKEND), env=env,
    )


def _call_over_stdio(test_database, env_extra: dict[str, str],
                     calls: list[tuple[str, dict]]) -> tuple[list[str], list]:
    async def body():
        async with stdio_client(_stdio_params(test_database, env_extra)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                results = [await session.call_tool(name, args) for name, args in calls]
                return sorted(tool.name for tool in tools.tools), results

    return anyio.run(body)


def test_stdio_default_lock_blocks_a_real_client(policy_file, test_database):
    """真实 stdio 握手：默认锁死下客户端只能探活，其余一律 forbidden。"""
    case_id = str(uuid.uuid4())
    names, results = _call_over_stdio(
        test_database, {"LWB_AGENT_ACCESS_FILE": str(policy_file)},
        [
            ("list_cases", {"limit": 0}),
            ("get_access_policy", {}),
            ("list_cases", {"limit": 20}),
            ("search_case", {"case_id": case_id, "query": "合同"}),
            ("get_progress", {"case_id": case_id}),
            ("create_case", {"title": "TEST-ONLY 不该建成"}),
        ],
    )
    assert "get_access_policy" in names
    probe, policy, listing, search, progress, creation = results
    assert not probe.isError
    assert json.loads(probe.content[0].text)["mode"] == "deny_all"
    assert not policy.isError
    assert json.loads(policy.content[0].text)["mode"] == "deny_all"
    for label, result in (("list", listing), ("search", search), ("progress", progress),
                          ("create", creation)):
        assert result.isError, label
        assert "forbidden" in result.content[0].text, label
        assert "agent-grant" in result.content[0].text, label


def test_stdio_allowlist_serves_only_the_granted_case(policy_file, two_cases, test_database):
    first, second = two_cases
    agent_access.grant_case(first)
    names, results = _call_over_stdio(
        test_database, {"LWB_AGENT_ACCESS_FILE": str(policy_file)},
        [
            ("list_cases", {"limit": 20}),
            ("get_claims", {"case_id": str(first)}),
            ("get_claims", {"case_id": str(second)}),
        ],
    )
    listing, allowed, blocked = results
    assert not listing.isError, listing.content
    payload = json.loads(listing.content[0].text)
    assert payload["total"] == 1 and payload["mode"] == "allowlist"
    assert payload["items"][0]["case_id"] == str(first)
    assert not allowed.isError, allowed.content
    assert blocked.isError and "forbidden" in blocked.content[0].text


def test_stdio_env_override_is_honoured_by_a_real_client(policy_file, two_cases, test_database):
    first, _second = two_cases
    _names, results = _call_over_stdio(
        test_database,
        {"LWB_AGENT_ACCESS_FILE": str(policy_file), "LWB_MCP_ALLOWED_CASES": str(first)},
        [("get_claims", {"case_id": str(first)})],
    )
    assert not results[0].isError, results[0].content


def test_stdio_reports_a_broken_policy_file_as_deny_all(policy_file, test_database):
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text("{ truncated", encoding="utf-8")
    _names, results = _call_over_stdio(
        test_database, {"LWB_AGENT_ACCESS_FILE": str(policy_file)},
        [("get_access_policy", {}), ("list_cases", {"limit": 5})],
    )
    policy, listing = results
    assert json.loads(policy.content[0].text)["mode"] == "deny_all"
    assert listing.isError and "forbidden" in listing.content[0].text
