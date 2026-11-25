"""Portable agent plugin package: manifests, path hygiene, launcher behaviour.

The package must work from any checkout on any machine, so these tests treat
"no machine-specific path" and "stdout stays a pure MCP channel" as hard
requirements rather than style preferences.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import types

import pytest

from legal_workbench import config

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plainfolio"
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
LAUNCHER = PLUGIN_ROOT / "scripts" / "run-mcp.py"
SKILLS_ROOT = PLUGIN_ROOT / "skills"

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

# A plugin must never carry a developer's home directory or the internal
# end-to-end acceptance skill (that one is for this repository only).
FORBIDDEN_MARKERS = (
    "/Users/",
    "C:\\Users\\",
    "C:/Users/",
    "legal-workbench-e2e-acceptance",
)


def _load_script(name: str, path: Path):
    """Load a standalone script without leaving bytecode inside the package.

    ``dataclasses`` resolves postponed annotations through ``sys.modules``, so the
    module has to be registered while it executes; compiling the source directly
    (instead of ``SourceFileLoader``) also avoids writing ``__pycache__`` into the
    shipped plugin directory.
    """
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def _launcher():
    return _load_script("plainfolio_run_mcp", LAUNCHER)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _workbench(root: Path, *, venv: bool = True) -> Path:
    """A minimal directory that the launcher accepts as a workbench checkout."""
    module_dir = root / "backend" / "src" / "legal_workbench"
    module_dir.mkdir(parents=True)
    (module_dir / "mcp_server.py").write_text("# mock MCP server\n", encoding="utf-8")
    (root / "scripts").mkdir(parents=True)
    if venv:
        python = root / "backend" / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
    return root


def _isolated_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Launcher environment that cannot fall back to the developer's pointer."""
    env = {"LWB_INSTANCE_CONFIG": str(tmp_path / "missing-instance.json")}
    env.update(extra)
    return env


# --- 清单与插件市场 -------------------------------------------------------

def test_plugin_manifest_declares_portable_components():
    manifest = _read_json(PLUGIN_ROOT / "plugin.json")
    assert manifest["$schema"] == PLUGIN_SCHEMA
    assert manifest["name"] == "plainfolio"
    assert manifest["version"] == "0.1.0"
    assert manifest["description"].strip()
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (PLUGIN_ROOT / manifest["skills"]).is_dir()
    assert (PLUGIN_ROOT / manifest["mcpServers"]).is_file()
    openai = manifest["extensions"]["com.openai"]
    assert openai["category"]
    assert openai["displayName"]
    assert (PLUGIN_ROOT / openai["onboardingSkill"]).is_file()


@pytest.mark.parametrize("name", ["mcp.json", ".mcp.json"])
def test_mcp_manifests_launch_the_portable_wrapper(name):
    data = _read_json(PLUGIN_ROOT / name)
    server = data["mcpServers"]["plainfolio"]
    assert server["command"] == "python3"
    assert server["args"] == ["${PLUGIN_ROOT}/scripts/run-mcp.py"]
    assert (PLUGIN_ROOT / "scripts" / "run-mcp.py").is_file()
    assert set(server) <= {"type", "command", "args", "env", "cwd"}


def test_portable_mcp_manifest_matches_the_agent_plugins_schema():
    data = _read_json(PLUGIN_ROOT / "mcp.json")
    assert data["$schema"] == MCP_SCHEMA
    server = data["mcpServers"]["plainfolio"]
    assert server["type"] == "stdio"
    # The portable schema reserves PLUGIN_ROOT / PLUGIN_DATA for the client.
    assert not {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(server.get("env", {}))


def test_both_mcp_manifests_declare_the_same_server():
    portable = _read_json(PLUGIN_ROOT / "mcp.json")["mcpServers"]["plainfolio"]
    codex = _read_json(PLUGIN_ROOT / ".mcp.json")["mcpServers"]["plainfolio"]
    keys = ("command", "args", "env")
    assert {key: portable.get(key) for key in keys} == {key: codex.get(key) for key in keys}


def test_marketplace_catalog_points_at_the_local_plugin():
    catalog = _read_json(MARKETPLACE)
    assert catalog["name"].strip()
    entries = {entry["name"]: entry for entry in catalog["plugins"]}
    entry = entries["plainfolio"]
    assert entry["source"] == {"source": "local", "path": "./plugins/plainfolio"}
    assert entry["version"] == _read_json(PLUGIN_ROOT / "plugin.json")["version"]
    # The path is relative to the marketplace root (the repository root).
    resolved = (MARKETPLACE.parents[2] / entry["source"]["path"]).resolve()
    assert resolved == PLUGIN_ROOT.resolve()
    assert (resolved / "plugin.json").is_file()


# --- 便携性与内容边界 -----------------------------------------------------

def test_plugin_package_contains_no_machine_specific_paths():
    files = sorted(
        path for path in PLUGIN_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    assert files, "plugin package is empty"
    offenders: list[str] = []
    for path in [*files, MARKETPLACE]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in FORBIDDEN_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {marker}")
    assert offenders == []


def test_internal_acceptance_skill_is_not_shipped():
    shipped = sorted(path.name for path in SKILLS_ROOT.iterdir() if path.is_dir())
    assert shipped == ["chart-substantive-review", "nonstandard-table-script-sop",
                       "plainfolio-workbench"]
    assert not (SKILLS_ROOT / "legal-workbench-e2e-acceptance").exists()


@pytest.mark.parametrize("name", ["chart-substantive-review", "nonstandard-table-script-sop",
                                  "plainfolio-workbench"])
def test_every_shipped_skill_has_matching_frontmatter(name):
    text = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    header = text.split("---", 2)[1]
    fields = {line.split(":", 1)[0].strip() for line in header.splitlines() if ":" in line}
    assert {"name", "description"} <= fields
    assert f"name: {name}" in header
    description = header.split("description:", 1)[1].strip()
    assert len(description) > 20


@pytest.mark.parametrize("name", ["chart-substantive-review", "nonstandard-table-script-sop"])
def test_mirrored_skills_do_not_drift_from_the_repository_copies(name):
    """三份逐字一致：插件包、仓库 .agents 与文档镜像。

    围栏措辞只要有一份漏改，装上插件的客户端就会少一层软保护，因此这里按
    字节比较而不是按关键词抽查。
    """
    shipped = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    for mirror in (REPO_ROOT / ".agents" / "skills" / name / "SKILL.md",
                   REPO_ROOT / "docs" / "skills" / name / "SKILL.md"):
        assert mirror.is_file(), f"缺少镜像：{mirror}"
        assert shipped == mirror.read_text(encoding="utf-8"), f"与 {mirror} 不一致"


@pytest.mark.parametrize("name", ["legal-workbench-cli", "legal-workbench-e2e-acceptance"])
def test_repository_only_skills_keep_their_two_copies_in_sync(name):
    """仓库内部 Skill（不分发）在 .agents 与 docs 之间必须一致。"""
    agents = REPO_ROOT / ".agents" / "skills" / name / "SKILL.md"
    docs = REPO_ROOT / "docs" / "skills" / name / "SKILL.md"
    assert agents.is_file() and docs.is_file()
    assert agents.read_text(encoding="utf-8") == docs.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["legal-workbench-cli", "legal-workbench-e2e-acceptance"])
def test_repository_only_skills_also_carry_the_authorisation_fence(name):
    """连仓库内部验收 SOP 也不能「顺手连一下」——它同样会碰真实案卷。"""
    text = (REPO_ROOT / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2]
    assert "授权前置与硬围栏纪律" in body, name
    for needle in ("agent-grant", "deny_all", "forbidden"):
        assert needle in body, f"{name} 缺少 {needle}"


# 三个业务 Skill 都必须把「先申请授权、禁止默认连接」放在最前面。
FENCE_KEYWORDS = ("授权", "禁止", "deny_all")


@pytest.mark.parametrize("name", ["chart-substantive-review", "nonstandard-table-script-sop",
                                  "plainfolio-workbench"])
def test_every_shipped_skill_front_loads_the_authorisation_fence(name):
    text = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2]
    assert "授权前置与硬围栏纪律" in body, name
    for needle in ("agent-grant", "agent-revoke", "agent-lock", "deny_all", "forbidden"):
        assert needle in body, f"{name} 缺少 {needle}"
    # 必须先申请授权再动手：禁止默认自动连接/探测/列举。
    assert "不得" in body and ("list_cases" in body or "list-cases" in body)
    # 用完即锁。
    assert "收回" in body and "agent-revoke" in body
    # 这一段必须排在「执行步骤/四步执行规程」之前，否则等于没写。
    fence_at = body.index("授权前置与硬围栏纪律")
    for later in ("四步执行规程", "执行步骤", "## 一、隐私边界"):
        if later in body:
            assert fence_at < body.index(later), f"{name}: 围栏段落必须前置（早于 {later}）"


def test_workbench_skill_states_privacy_boundary_and_human_gate():
    text = (SKILLS_ROOT / "plainfolio-workbench" / "SKILL.md").read_text(encoding="utf-8")
    # Privacy: offline by itself, but tool output enters the connected model.
    assert "完全离线" in text
    assert "上下文" in text
    # Human gate: agents only submit proposals.
    assert "待确认" in text
    assert "原件" in text
    for tool in ("list_cases", "search_case", "read_source", "save_analysis",
                 "export-chart-audit-bundle", "export_chart_audit_bundle"):
        assert tool in text


def test_plugin_readme_covers_the_documented_clients():
    text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("codex plugin marketplace add", "dsh-mcp-client", "Claude Desktop",
                   "Antigravity", "LWB_HOME", "LWB_MCP_AUTO_START_DB",
                   "register-instance", "隐私"):
        assert needle in text, needle
    # DSH 侧的唯一接入形态（@deepseek-ai/dsh@0.2.1-rc.2）：通用 MCP 客户端配置。
    # 不再有 Cordis 补丁层（plugins/dsh-plainfolio 已废弃移除）。
    assert "cordis.patch.yml" not in text


def test_plugin_readme_documents_the_default_deny_fence_first():
    """默认锁死＋单案授权是接入的第一条安全流程，必须在 README 里写清楚。"""
    text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("deny_all", "agent-grant", "agent-revoke", "agent-lock",
                   "agent-access.json", "allowlist"):
        assert needle in text, needle
    # 门禁说明必须排在「接入示例」之前：先讲清权限，再讲怎么连。
    assert text.index("deny_all") < text.index("## 接入示例")


def test_plugin_readme_no_longer_advertises_a_resident_cordis_plugin():
    """路线 2：常驻宿主插件与高保密案件隔离原则冲突，已移除。

    README 仍可提及 DSH 的通用 MCP 用法（按需临时接入），但不得再指向一个
    仓库里已不存在的插件包，也不得把「装包 + 补丁层」写成推荐路径。
    """
    text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "dsh-agents-plugins-bridge" not in text
    # 历史包名可以出现，但只能作为「已废弃移除」的说明，不得再给出安装命令。
    if "dsh-plainfolio" in text:
        assert "废弃" in text and "移除" in text
        assert "dsh plugin --profile web add" not in text
    assert "dsh plugin --profile web add" not in text
    assert "--patch" not in text
    assert not (REPO_ROOT / "plugins" / "dsh-plainfolio").exists()


# --- 实例发现（config） ---------------------------------------------------

def test_instance_config_path_honours_the_override(monkeypatch, tmp_path):
    target = tmp_path / "state" / "instance.json"
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(target))
    assert config.instance_config_path() == target


def test_instance_config_path_defaults_to_the_user_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("LWB_INSTANCE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert config.instance_config_path() == tmp_path / ".plainfolio" / "instance.json"


def test_resolve_home_dir_prefers_env_then_pointer(monkeypatch, tmp_path):
    env_home = tmp_path / "env-home"
    env_home.mkdir()
    pointer_home = tmp_path / "pointer-home"
    pointer_home.mkdir()
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(pointer_home)}), encoding="utf-8")
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))

    monkeypatch.setenv("LWB_HOME", str(env_home))
    assert config.resolve_home_dir() == env_home.resolve()

    monkeypatch.delenv("LWB_HOME")
    assert config.resolve_home_dir() == pointer_home.resolve()


def test_resolve_home_dir_falls_back_to_root_without_any_pointer(monkeypatch, tmp_path):
    monkeypatch.delenv("LWB_HOME", raising=False)
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(tmp_path / "missing.json"))
    assert config.resolve_home_dir() == config.ROOT


def test_resolve_home_dir_ignores_a_pointer_that_names_no_directory(monkeypatch, tmp_path):
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(tmp_path / "gone")}), encoding="utf-8")
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))
    monkeypatch.delenv("LWB_HOME", raising=False)
    assert config.resolve_home_dir() == config.ROOT


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"home": ""}', '{"home": 42}'])
def test_resolve_home_dir_survives_an_unusable_pointer(monkeypatch, tmp_path, payload):
    pointer = tmp_path / "instance.json"
    pointer.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))
    monkeypatch.delenv("LWB_HOME", raising=False)
    assert config.resolve_home_dir() == config.ROOT


def test_resolve_home_dir_still_honours_a_patched_root(monkeypatch, tmp_path):
    """test_windows_compat patches config.ROOT and expects load_settings to obey."""
    fake_root = tmp_path / "checkout"
    fake_root.mkdir()
    monkeypatch.setattr(config, "ROOT", fake_root)
    monkeypatch.delenv("LWB_HOME", raising=False)
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(tmp_path / "missing.json"))
    assert config.resolve_home_dir() == fake_root


def test_write_instance_pointer_records_paths_and_timestamp_only(tmp_path):
    home = tmp_path / "workbench"
    home.mkdir()
    target = tmp_path / "state" / "instance.json"
    written = config.write_instance_pointer(home, home, config_path=target)
    assert written == target

    text = target.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert set(payload) == {"home", "project_root", "updated_at"}
    assert payload["home"] == str(home.resolve())
    assert payload["project_root"] == str(home.resolve())
    # 2026-11-24T12:00:00Z
    assert payload["updated_at"].endswith("Z") and len(payload["updated_at"]) == 20

    for secret in ("password", "PASSWORD", "postgresql://", "postgres://", "lwb_app",
                   "lwb_super", "lwb_migrator", "DATABASE_URL", "://"):
        assert secret not in text
    if os.name == "posix":
        assert target.stat().st_mode & 0o077 == 0


def test_write_instance_pointer_replaces_the_previous_pointer(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    target = tmp_path / "state" / "instance.json"

    config.write_instance_pointer(first, first, config_path=target)
    config.write_instance_pointer(second, second, config_path=target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["home"] == str(second.resolve())
    # Atomic write: no temp file is left behind next to the pointer.
    assert sorted(path.name for path in target.parent.iterdir()) == ["instance.json"]


def test_write_instance_pointer_rejects_a_missing_directory(tmp_path):
    with pytest.raises(ValueError):
        config.write_instance_pointer(tmp_path / "nope", config_path=tmp_path / "i.json")


def test_load_settings_reads_env_from_the_resolved_home(monkeypatch, tmp_path):
    home = tmp_path / "workbench"
    home.mkdir()
    (home / ".env").write_text("LWB_DATA_ROOT=managed-data\nLWB_PORT=9999\n", encoding="utf-8")
    monkeypatch.setenv("LWB_HOME", str(home))
    monkeypatch.delenv("LWB_DATA_ROOT", raising=False)
    monkeypatch.delenv("LWB_PORT", raising=False)

    settings = config.load_settings()
    assert settings.data_root == home / "managed-data"
    assert settings.port == 9999


def test_load_settings_follows_the_instance_pointer(monkeypatch, tmp_path):
    home = tmp_path / "registered"
    home.mkdir()
    (home / ".env").write_text("LWB_PORT=8123\n", encoding="utf-8")
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(home)}), encoding="utf-8")
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))
    monkeypatch.delenv("LWB_HOME", raising=False)
    monkeypatch.delenv("LWB_PORT", raising=False)

    assert config.load_settings().port == 8123


# --- scripts/lwb register-instance ---------------------------------------

def test_lwb_register_instance_writes_the_pointer(monkeypatch, tmp_path, capsys):
    lwb = _load_script("lwb_register_test", REPO_ROOT / "scripts" / "lwb")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    pointer = tmp_path / "state" / "instance.json"
    monkeypatch.setattr(lwb, "ROOT", checkout)
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))

    assert lwb.register_instance() == 0

    payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert payload["home"] == str(checkout.resolve())
    assert payload["project_root"] == str(checkout.resolve())
    output = capsys.readouterr().out
    assert "实例指针文件" in output
    assert str(pointer) in output


def test_lwb_main_dispatches_register_instance(monkeypatch, tmp_path):
    lwb = _load_script("lwb_main_test", REPO_ROOT / "scripts" / "lwb")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    pointer = tmp_path / "state" / "instance.json"
    monkeypatch.setattr(lwb, "ROOT", checkout)
    monkeypatch.setattr(lwb.os, "chdir", lambda _path: None)
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))
    monkeypatch.setattr(sys, "argv", ["lwb", "register-instance"])

    assert lwb.main() == 0
    assert pointer.is_file()


def test_lwb_usage_lists_register_instance(monkeypatch, capsys):
    lwb = _load_script("lwb_usage_test", REPO_ROOT / "scripts" / "lwb")
    monkeypatch.setattr(sys, "argv", ["lwb", "nonsense"])
    assert lwb.main() == 2
    assert "register-instance" in capsys.readouterr().err


# --- 便携启动器 scripts/run-mcp.py ---------------------------------------

def test_launcher_resolves_env_home_and_dispatches_the_venv(tmp_path, capsys):
    module = _launcher()
    home = _workbench(tmp_path / "workbench").resolve()
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    dirs: list[str] = []

    code = module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(home)), script_path=LAUNCHER,
        exec_fn=lambda path, args, env: calls.append((path, list(args), dict(env))),
        chdir_fn=dirs.append,
    )

    assert code == 0
    path, args, env = calls[0]
    expected_python = str(home / "backend" / ".venv" / "bin" / "python")
    assert path == expected_python
    assert args == [expected_python, "-m", "legal_workbench.mcp_server"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(home / "backend" / "src")
    assert env["LWB_HOME"] == str(home)
    assert env["PYTHONUTF8"] == "1"
    assert dirs == [str(home / "backend")]
    assert capsys.readouterr().out == ""


def test_launcher_uses_instance_pointer_project_root(tmp_path):
    module = _launcher()
    home = _workbench(tmp_path / "registered").resolve()
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(tmp_path / "gone"), "project_root": str(home)}),
                       encoding="utf-8")

    resolution = module.resolve_home(LAUNCHER, {"LWB_INSTANCE_CONFIG": str(pointer)})

    assert resolution.home == home
    assert "project_root" in resolution.source


def test_launcher_falls_back_from_a_bad_project_root_to_home(tmp_path):
    module = _launcher()
    home = _workbench(tmp_path / "registered").resolve()
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(home), "project_root": str(tmp_path / "gone")}),
                       encoding="utf-8")

    resolution = module.resolve_home(LAUNCHER, {"LWB_INSTANCE_CONFIG": str(pointer)})

    assert resolution.home == home
    assert resolution.source.endswith("的 home")
    assert any("project_root=" in attempt for attempt in resolution.attempts)


def test_launcher_falls_through_a_stale_env_to_the_script_checkout(tmp_path):
    module = _launcher()
    resolution = module.resolve_home(
        LAUNCHER, _isolated_env(tmp_path, LWB_HOME=str(tmp_path / "nope")),
    )
    assert resolution.home == REPO_ROOT.resolve()
    assert resolution.source == "脚本所在仓库"
    assert any("LWB_HOME" in attempt for attempt in resolution.attempts)


def test_launcher_records_every_rejected_candidate(tmp_path, capsys):
    module = _launcher()
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(tmp_path / "a"), "project_root": str(tmp_path / "b")}),
                       encoding="utf-8")
    script = tmp_path / "copy" / "run-mcp.py"
    script.parent.mkdir()
    script.write_text("# copied outside any workbench\n", encoding="utf-8")

    resolution = module.resolve_home(
        script, {"LWB_HOME": str(tmp_path / "nope"), "LWB_INSTANCE_CONFIG": str(pointer)},
    )

    assert resolution.home is None
    joined = "\n".join(resolution.attempts)
    assert "LWB_HOME" in joined
    assert "project_root=" in joined and "home=" in joined
    assert "逐级向上探测" in joined

    code = module.main([], env={"LWB_HOME": str(tmp_path / "nope"),
                                "LWB_INSTANCE_CONFIG": str(pointer)},
                       script_path=script, exec_fn=lambda *_a: pytest.fail("must not launch"))
    assert code == 2
    assert "已尝试" in capsys.readouterr().err


def test_launcher_without_workbench_keeps_stdout_clean(tmp_path, capsys):
    module = _launcher()
    script = tmp_path / "elsewhere" / "run-mcp.py"
    script.parent.mkdir()
    script.write_text("# not a workbench\n", encoding="utf-8")

    code = module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(tmp_path / "nope")),
        script_path=script,
        exec_fn=lambda *_args: pytest.fail("must not launch without a workbench"),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "register-instance" in captured.err
    assert "LWB_HOME" in captured.err
    assert str(tmp_path / "missing-instance.json") in captured.err


def test_launcher_without_interpreter_reports_guidance(tmp_path, capsys):
    module = _launcher()
    home = _workbench(tmp_path / "workbench", venv=False).resolve()

    code = module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(home)), script_path=LAUNCHER,
        exec_fn=lambda *_args: pytest.fail("must not launch without an interpreter"),
        which_fn=lambda _name: None,
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "backend" in captured.err and "lwb-mcp" in captured.err
    assert "register-instance" in captured.err


def test_launcher_falls_back_to_lwb_mcp_on_path(tmp_path):
    module = _launcher()
    home = _workbench(tmp_path / "workbench", venv=False).resolve()
    entry = tmp_path / "bin" / "lwb-mcp"
    entry.parent.mkdir()
    entry.write_text("", encoding="utf-8")
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    dirs: list[str] = []

    code = module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(home)), script_path=LAUNCHER,
        exec_fn=lambda path, args, env: calls.append((path, list(args), dict(env))),
        chdir_fn=dirs.append,
        which_fn=lambda name: str(entry) if name == "lwb-mcp" else None,
    )

    assert code == 0
    path, args, env = calls[0]
    assert path == str(entry) and args == [str(entry)]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(home / "backend" / "src")
    assert dirs == [str(home)]


def test_launcher_auto_starts_the_database_before_dispatch(tmp_path, capsys):
    module = _launcher()
    home = _workbench(tmp_path / "workbench").resolve()
    (home / "scripts" / "lwb").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    order: list[str] = []
    runs: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        order.append("start-db")
        runs.append((list(command), kwargs))
        return SimpleNamespace(returncode=0, stdout="database system is ready\n")

    code = module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(home), LWB_MCP_AUTO_START_DB="1"),
        script_path=LAUNCHER,
        exec_fn=lambda *_args: order.append("exec"),
        run_fn=fake_run,
        chdir_fn=lambda _path: None,
    )

    assert code == 0
    assert order == ["start-db", "exec"]
    command, kwargs = runs[0]
    assert command[0] == str(home / "backend" / ".venv" / "bin" / "python")
    assert command[1] == str(home / "scripts" / "lwb")
    assert command[2] == "start-db"
    # The database log is captured and echoed to stderr, never to stdout.
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "database system is ready" in captured.err


def test_launcher_skips_database_start_unless_requested(tmp_path):
    module = _launcher()
    home = _workbench(tmp_path / "workbench").resolve()
    (home / "scripts" / "lwb").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runs: list[object] = []

    module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(home)), script_path=LAUNCHER,
        exec_fn=lambda *_args: None, run_fn=lambda *a, **k: runs.append(a),
        chdir_fn=lambda _path: None,
    )

    assert runs == []


def test_launcher_continues_after_a_failed_database_start(tmp_path, capsys):
    module = _launcher()
    home = _workbench(tmp_path / "workbench").resolve()
    (home / "scripts" / "lwb").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    launched: list[str] = []

    code = module.main(
        [], env=_isolated_env(tmp_path, LWB_HOME=str(home), LWB_MCP_AUTO_START_DB="true"),
        script_path=LAUNCHER,
        exec_fn=lambda path, *_args: launched.append(path),
        run_fn=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="boom\n"),
        chdir_fn=lambda _path: None,
    )

    assert code == 0
    assert launched == [str(home / "backend" / ".venv" / "bin" / "python")]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "退出码 1" in captured.err


def test_launcher_print_plan_does_not_launch_or_leak_env(tmp_path, capsys):
    module = _launcher()
    home = _workbench(tmp_path / "workbench").resolve()

    code = module.main(
        ["--print-plan"], env=_isolated_env(tmp_path, LWB_HOME=str(home)), script_path=LAUNCHER,
        exec_fn=lambda *_args: pytest.fail("--print-plan must not launch the server"),
    )

    captured = capsys.readouterr()
    assert code == 0
    plan = json.loads(captured.out)
    assert plan["home"] == str(home)
    assert plan["command"][-2:] == ["-m", "legal_workbench.mcp_server"]
    assert plan["cwd"] == str(home / "backend")
    assert "PYTHONPATH" not in captured.out


def test_launcher_rejects_unknown_arguments(tmp_path, capsys):
    module = _launcher()
    code = module.main(["--serve"], env=_isolated_env(tmp_path), script_path=LAUNCHER,
                       exec_fn=lambda *_args: pytest.fail("unknown flags must not launch"))
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "未知参数" in captured.err


def test_launcher_runs_end_to_end_against_a_mock_workbench(tmp_path):
    """A real exec: the launcher must hand over to the workbench's own python."""
    if os.name == "nt":
        pytest.skip("symlinked interpreter fixture is POSIX-only")

    home = tmp_path / "workbench"
    module_dir = home / "backend" / "src" / "legal_workbench"
    module_dir.mkdir(parents=True)
    (module_dir / "mcp_server.py").write_text(
        "import os, sys\n"
        "print('MOCK-MCP-READY', file=sys.stderr)\n"
        "print('CWD=' + os.getcwd(), file=sys.stderr)\n"
        "print('PYTHONPATH=' + os.environ.get('PYTHONPATH', ''), file=sys.stderr)\n"
        "print('LWB_HOME=' + os.environ.get('LWB_HOME', ''), file=sys.stderr)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    python = home / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)

    env = {**os.environ, "LWB_HOME": str(home),
           "LWB_INSTANCE_CONFIG": str(tmp_path / "missing-instance.json")}
    env.pop("LWB_MCP_AUTO_START_DB", None)
    result = subprocess.run(
        [sys.executable, str(LAUNCHER)], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "MOCK-MCP-READY" in result.stderr
    assert f"CWD={home / 'backend'}" in result.stderr
    assert f"LWB_HOME={home}" in result.stderr
    assert str(home / "backend" / "src") in result.stderr


def test_launcher_reports_failure_without_stdout_from_a_real_process(tmp_path):
    # A copy outside the repository: ancestor probing must not find the checkout.
    script = tmp_path / "portable" / "scripts" / "run-mcp.py"
    script.parent.mkdir(parents=True)
    script.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    env = {**os.environ, "LWB_HOME": str(tmp_path / "nope"),
           "LWB_INSTANCE_CONFIG": str(tmp_path / "missing-instance.json")}
    env.pop("LWB_MCP_AUTO_START_DB", None)
    result = subprocess.run(
        [sys.executable, str(script)], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "register-instance" in result.stderr
    assert "scripts" in result.stderr
