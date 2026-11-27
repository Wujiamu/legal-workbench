"""模块化拆分与按需分发（Phase 1）：模块解析、HTTP 开关、MCP 工具裁剪与插件包。

三层行为，各自有独立的失败方式，所以分开断言：

1. **HTTP**：模块关闭时对应路由返回 ``not_found``（不是 ``forbidden``——裁剪后
   本机根本没有这个功能面），开启后恢复；``chart_export`` 属于"关系图或诉请金额
   任一启用即可"的并集语义。
2. **MCP**：``TOOL_MODULES`` 必须 100% 覆盖注册工具（漏登记＝裁剪后静默少工具），
   ``build_server`` 只注册启用模块的工具，且复制出去的工具仍带 ``@_case_guard``；
   真实 stdio 握手验证客户端看到的工具清单。
3. **插件包**：``plugins/plainfolio-legal-sops`` 是零代码、零数据库依赖的独立
   方法论插件，清单、frontmatter、无本机路径和插件市场索引都要成立。

数据库用 conftest 重建的 ``legal_workbench_test``；未授权状态由会话级
``LWB_AGENT_ACCESS_FILE`` 指到不存在的文件保证（默认锁死）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from legal_workbench import mcp_server
from legal_workbench.api.deps import require_module
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.modules import (ALL_MODULES, CORE_MODULE, OPTIONAL_MODULES,
                                     is_module_enabled, parse_enabled_modules)

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
SOPS_PLUGIN = REPO_ROOT / "plugins" / "plainfolio-legal-sops"
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

# GET 路由 → 需要的模块。全部是只读入口，禁用时必须 404 且不触库。
GUARDED_GETS = {
    "progress": "/api/v1/progress/rules",
    "graph": "/api/v1/graph/rules",
    "notes": "/api/v1/cases/{case_id}/annotations",
    "evidence": "/api/v1/cases/{case_id}/evidence-catalog",
    "claims": "/api/v1/cases/{case_id}/claims",
}


# --- 1. 模块解析 -----------------------------------------------------------

def test_parse_defaults_to_every_module():
    assert parse_enabled_modules(None) == ALL_MODULES
    assert parse_enabled_modules("") == ALL_MODULES
    assert parse_enabled_modules("   ") == ALL_MODULES
    for text in ("all", "ALL", " All "):
        assert parse_enabled_modules(text) == ALL_MODULES


def test_parse_keeps_core_first_and_orders_stably():
    text = " Time,notes ,, GRAPH "
    assert parse_enabled_modules(text) == ("core", "notes", "graph", "time")
    # 重复项去重、大小写归一。
    assert parse_enabled_modules("claims,CLAIMS,claims") == ("core", "claims")
    # 显式只写 core 时不额外加可选模块。
    assert parse_enabled_modules("core") == (CORE_MODULE,)
    assert parse_enabled_modules("notes")[0] == CORE_MODULE


def test_parse_rejects_an_unknown_module():
    with pytest.raises(WorkbenchError) as caught:
        parse_enabled_modules("core,timeline")
    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert "timeline" in caught.value.message
    assert caught.value.details == {"module": "timeline"}


def test_all_and_optionals_are_the_documented_names():
    assert CORE_MODULE == "core"
    assert OPTIONAL_MODULES == ("notes", "evidence", "graph", "claims", "progress", "time")
    assert ALL_MODULES == ("core", *OPTIONAL_MODULES)


def test_is_module_enabled_reads_settings_or_an_explicit_list(monkeypatch):
    monkeypatch.setenv("LWB_ENABLED_MODULES", "core,notes")
    assert is_module_enabled("notes") is True
    assert is_module_enabled("graph") is False
    # 显式传入优先于设置，便于调用方在纯函数里判断。
    assert is_module_enabled("graph", ("graph",)) is True
    assert is_module_enabled("graph", ()) is False


# --- 2. HTTP 开关 ----------------------------------------------------------

def test_modules_endpoint_reports_the_configured_surface(api, monkeypatch):
    monkeypatch.delenv("LWB_ENABLED_MODULES", raising=False)
    default = api.get("/api/v1/modules")
    assert default.status_code == 200
    assert default.json() == {"enabled_modules": list(ALL_MODULES),
                              "all_modules": list(ALL_MODULES)}

    monkeypatch.setenv("LWB_ENABLED_MODULES", "core,graph")
    narrowed = api.get("/api/v1/modules").json()
    assert narrowed["enabled_modules"] == ["core", "graph"]
    # all_modules 是完整清单，前端据此知道"还能开什么"。
    assert narrowed["all_modules"] == list(ALL_MODULES)


@pytest.mark.parametrize("module,path", sorted(GUARDED_GETS.items()))
def test_a_disabled_module_answers_not_found(api, monkeypatch, module, path):
    monkeypatch.setenv("LWB_ENABLED_MODULES", "core")
    url = path.replace("{case_id}", str(uuid.uuid4()))
    response = api.get(url, params={"work_date": "2026-11-27"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert module in body["message"]
    assert body["details"] == {"modules": [module]}


def test_the_time_route_is_guarded_too(api, monkeypatch):
    monkeypatch.setenv("LWB_ENABLED_MODULES", "core")
    url = f"/api/v1/cases/{uuid.uuid4()}/time"
    response = api.get(url, params={"work_date": "2026-11-27"})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "time" in response.json()["message"]


def test_core_routes_survive_a_trimmed_install(api, monkeypatch):
    monkeypatch.setenv("LWB_ENABLED_MODULES", "core")
    assert api.get("/api/v1/cases", params={"limit": 5}).status_code == 200
    assert api.get("/api/v1/health").status_code == 200
    # 规则集属于各自模块，因此随模块一起消失。
    assert api.get("/api/v1/progress/rules").status_code == 404


def test_an_enabled_module_serves_its_routes_again(api, monkeypatch):
    monkeypatch.setenv("LWB_ENABLED_MODULES", "all")
    assert api.get("/api/v1/progress/rules").status_code == 200
    assert api.get("/api/v1/graph/rules").status_code == 200


def test_a_case_scoped_module_serves_the_case_once_enabled(api, db, monkeypatch):
    from legal_workbench import cases

    case_id = cases.create_case(db, title="TEST-ONLY 模块开关案件").case_id
    db.commit()

    monkeypatch.setenv("LWB_ENABLED_MODULES", "core,time,claims,evidence,notes")
    params = {"work_date": "2026-11-27"}
    assert api.get(f"/api/v1/cases/{case_id}/time", params=params).status_code == 200
    assert api.get(f"/api/v1/cases/{case_id}/claims").status_code == 200
    assert api.get(f"/api/v1/cases/{case_id}/evidence-catalog").status_code == 200
    assert api.get(f"/api/v1/cases/{case_id}/annotations").status_code == 200

    monkeypatch.setenv("LWB_ENABLED_MODULES", "core")
    assert api.get(f"/api/v1/cases/{case_id}/claims").status_code == 404


def test_chart_export_is_an_alternative_of_graph_and_claims(api, monkeypatch):
    """关系图或诉请金额任一启用即可导出：两者都关时才 404。"""
    case_id = str(uuid.uuid4())
    params = {"chart_type": "graph", "target_id": str(uuid.uuid4()), "profile": "court"}

    monkeypatch.setenv("LWB_ENABLED_MODULES", "core")
    disabled = api.get(f"/api/v1/cases/{case_id}/charts/audit-bundle", params=params)
    assert disabled.status_code == 404
    assert disabled.json()["code"] == "not_found"
    assert disabled.json()["details"] == {"modules": ["graph", "claims"]}

    for enabled in ("core,graph", "core,claims"):
        monkeypatch.setenv("LWB_ENABLED_MODULES", enabled)
        response = api.get(f"/api/v1/cases/{case_id}/charts/audit-bundle", params=params)
        # 案件不存在属于业务 404；这里只断言不再是"模块未启用"。
        assert "模块未启用" not in response.json().get("message", ""), enabled


def test_the_dependency_factory_itself_enforces_the_union(monkeypatch):
    monkeypatch.setenv("LWB_ENABLED_MODULES", "core,claims")
    require_module("graph", "claims")()  # 不抛错

    monkeypatch.setenv("LWB_ENABLED_MODULES", "core")
    with pytest.raises(WorkbenchError) as caught:
        require_module("graph", "claims")()
    assert caught.value.code is ErrorCode.NOT_FOUND
    assert caught.value.http_status == 404


# --- 3. MCP 工具裁剪 -------------------------------------------------------

def test_tool_modules_covers_every_registered_tool_exactly_once():
    tools = mcp_server.server._tool_manager.list_tools()
    assert len(tools) == 57, f"预期 57 个工具，实际 {len(tools)}"
    python_names = {tool.fn.__name__ for tool in tools}
    assert set(mcp_server.TOOL_MODULES) == python_names
    # 唯一的注册名差异是审计包（对外用连字符），映射仍以函数名登记。
    assert {tool.name for tool in tools} - python_names == {"export-chart-audit-bundle"}

    expected_sizes = {"core": 12, "notes": 5, "progress": 14, "graph": 13,
                      "evidence": 6, "claims": 6}
    counts = {name: 0 for name in expected_sizes}
    for modules in mcp_server.TOOL_MODULES.values():
        assert modules, "每个工具至少要归属一个模块"
        assert set(modules) <= set(ALL_MODULES)
        if len(modules) == 1:
            counts[modules[0]] += 1
    assert counts == expected_sizes
    assert mcp_server.TOOL_MODULES["export_chart_audit_bundle"] == ("graph", "claims")
    assert sum(expected_sizes.values()) + 1 == 57


def _tool_names(target) -> set[str]:
    return {tool.name for tool in target._tool_manager.list_tools()}


def test_build_server_keeps_only_the_enabled_modules():
    srv = mcp_server.build_server(["core", "evidence"])
    registered = {tool.fn.__name__ for tool in srv._tool_manager.list_tools()}
    expected = {name for name, modules in mcp_server.TOOL_MODULES.items()
                if set(modules) & {"core", "evidence"}}
    assert registered == expected
    assert len(registered) == 18
    for absent in ("get_progress", "progress_add_node", "get_graph", "graph_add_entity",
                   "get_claims", "claim_add_item", "save_analysis", "annotate_file"):
        assert absent not in registered, absent
    assert {"get_evidence_catalog", "evidence_add_item", "search_case"} <= registered
    # 审计包需要关系图或诉请金额，两者都没开时不存在。
    assert "export-chart-audit-bundle" not in _tool_names(srv)
    # 复制到新服务上的工具仍带围栏，且审计在新服务上同样通过。
    assert mcp_server._fence_audit(srv) == []


def test_the_narrowed_server_still_refuses_an_unauthorised_case():
    srv = mcp_server.build_server(["core", "evidence"])
    tool = {item.name: item for item in srv._tool_manager.list_tools()}["search_case"]
    assert getattr(tool.fn, "__case_guard__", False)
    with pytest.raises(WorkbenchError) as caught:
        tool.fn(case_id=str(uuid.uuid4()), query="合同")
    assert caught.value.code is ErrorCode.FORBIDDEN
    assert "agent-grant" in caught.value.message


def test_the_full_build_is_the_unchanged_default_server():
    assert mcp_server.build_server() is mcp_server.server
    assert mcp_server.build_server(ALL_MODULES) is mcp_server.server
    # 少一个模块就必须真的裁剪，而不是悄悄回退全量。
    assert mcp_server.build_server(["core"]) is not mcp_server.server


def test_build_server_rejects_an_unregistered_tool(monkeypatch):
    """反向验证：新增工具漏登记时，裁剪必须报错而不是静默漏掉。"""

    def sneaky_probe(query: str) -> dict[str, object]:
        return {"query": query}

    mcp_server.server.add_tool(sneaky_probe, name="sneaky_probe")
    try:
        with pytest.raises(KeyError):
            mcp_server.build_server(["core"])
    finally:
        mcp_server.server.remove_tool("sneaky_probe")
    assert mcp_server.build_server(["core"]) is not mcp_server.server


def _stdio_params(test_database, modules: str | None) -> StdioServerParameters:
    env = {**os.environ, "PYTHONPATH": str(BACKEND / "src"),
           "LWB_DATABASE_URL": test_database[1], "LWB_MIGRATION_URL": test_database[0]}
    env.pop("LWB_MCP_ACCESS_MODE", None)
    env.pop("LWB_MCP_ALLOWED_CASES", None)
    args = ["-m", "legal_workbench.mcp_server"]
    if modules is not None:
        args += ["--modules", modules]
    return StdioServerParameters(command=sys.executable, args=args, cwd=str(BACKEND), env=env)


def test_stdio_handshake_lists_only_the_trimmed_tools(test_database):
    """真实 stdio：`--modules core,evidence` 的客户端只看到 18 个工具。"""

    async def body():
        async with stdio_client(_stdio_params(test_database, "core,evidence")) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                blocked = await session.call_tool("search_case",
                                                  {"case_id": str(uuid.uuid4()), "query": "合同"})
                return sorted(tool.name for tool in tools.tools), blocked

    names, blocked = anyio.run(body)
    assert names == sorted(
        tool.name for tool in mcp_server.build_server(["core", "evidence"])
        ._tool_manager.list_tools())
    assert len(names) == 18
    assert "progress_add_node" not in names
    assert "export-chart-audit-bundle" not in names
    # 裁剪不会削弱围栏：未授权案件仍是 forbidden。
    assert blocked.isError
    assert "forbidden" in blocked.content[0].text
    assert "agent-grant" in blocked.content[0].text


def test_stdio_defaults_to_the_configured_mcp_modules(test_database, monkeypatch):
    """`LWB_MCP_MODULES` 在没传 `--modules` 时决定工具集。"""

    async def body():
        async with stdio_client(_stdio_params(test_database, None)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return sorted(tool.name for tool in (await session.list_tools()).tools)

    monkeypatch.setenv("LWB_MCP_MODULES", "core")
    assert len(anyio.run(body)) == 12


# --- 4. 独立通用法律 SOP 插件包 --------------------------------------------

def test_legal_sops_manifest_is_a_standalone_methodology_plugin():
    manifest = json.loads((SOPS_PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == PLUGIN_SCHEMA
    assert manifest["name"] == "plainfolio-legal-sops"
    assert manifest["version"] == "0.1.0"
    assert manifest["license"] == "Apache-2.0"
    assert manifest["description"].strip()
    assert manifest["skills"] == "./skills/"
    assert (SOPS_PLUGIN / manifest["skills"]).is_dir()
    # 零代码、零数据库：没有 MCP 服务、没有脚本目录。
    assert "mcpServers" not in manifest
    openai = manifest["extensions"]["com.openai"]
    assert openai["category"] and openai["displayName"]
    assert (SOPS_PLUGIN / openai["onboardingSkill"]).is_file()


def test_legal_sops_ships_the_two_methodology_skills_with_frontmatter():
    skills = sorted(path for path in (SOPS_PLUGIN / "skills").iterdir() if path.is_dir())
    assert [path.name for path in skills] == ["legal-chart-substantive-review",
                                              "legal-table-script-sop"]
    for path in skills:
        text = (path / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), path
        header = text.split("---", 2)[1]
        assert f"name: {path.name}" in header
        description = header.split("description:", 1)[1].strip()
        assert len(description) > 20, path
        body = text.split("---", 2)[2]
        # 通用版不依赖素章底座：可选联动必须单列并明确"未接入时照旧执行"。
        assert "可选" in body and "素章" in body, path
        # 方法论本体要能独立使用：脚本规程必须写死确定性算账与异常清单要求。
        if path.name == "legal-table-script-sop":
            for needle in ("Decimal", "ROUND_HALF_UP", "skipped_rows"):
                assert needle in body, needle


def test_legal_sops_carries_no_machine_specific_path_or_secret():
    files = sorted(path for path in SOPS_PLUGIN.rglob("*") if path.is_file())
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("/Users/", "C:\\Users\\", "C:/Users/", "postgresql://", "lwb_app"):
            assert marker not in text, f"{path.name} 含 {marker}"


def test_marketplace_indexes_the_new_plugin_after_plainfolio():
    catalog = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    names = [entry["name"] for entry in catalog["plugins"]]
    assert names[0] == "plainfolio"
    assert "plainfolio-legal-sops" in names
    assert len(names) == len(set(names))

    entry = next(item for item in catalog["plugins"] if item["name"] == "plainfolio-legal-sops")
    assert entry["source"] == {"source": "local", "path": "./plugins/plainfolio-legal-sops"}
    resolved = (MARKETPLACE.parents[2] / entry["source"]["path"]).resolve()
    assert resolved == SOPS_PLUGIN.resolve()
    assert (resolved / "plugin.json").is_file()


def test_config_and_modules_import_without_pydantic_for_scripts_lwb():
    """scripts/lwb 在系统裸 python3 下运行，config -> modules 导入期绝不能拖入 pydantic。"""
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['pydantic'] = None; "
            "from legal_workbench import config, modules; "
            "assert modules.parse_enabled_modules('core,graph') == ('core', 'graph')",
        ],
        cwd=str(REPO_ROOT / "backend" / "src"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
