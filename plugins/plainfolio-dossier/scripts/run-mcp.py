#!/usr/bin/env python3
"""便携启动器：找到已安装的 `plainfolio_dossier`，把进程交给 MCP 服务。

只用标准库，因此可以在这台机器上先跑起来再装依赖。定位顺序：

1. ``PLAINFOLIO_DOSSIER_BIN``：显式指定的解释器或 ``plainfolio-dossier-mcp`` 可执行文件；
2. ``PATH`` 中的 ``plainfolio-dossier-mcp``（``pip install`` 后的标准入口）；
3. 插件所在仓库的 ``backend/.venv`` 或 ``packages/plainfolio-dossier/.venv``，
   配合 ``PYTHONPATH=<仓库>/packages/plainfolio-dossier/src``；
4. 当前解释器，前提是它已经装过 ``plainfolio_dossier``。

``--print-plan`` 只打印将要执行的命令（JSON），**不启动服务**，用于排查
"MCP 客户端连不上"到底是解释器没装还是路径没找到。

stdout 只属于 MCP 协议帧：任何诊断信息都写 stderr。正常路径用 ``os.execve``
直接替换进程，连一个字节的中转输出都不会有。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BIN_ENV = "PLAINFOLIO_DOSSIER_BIN"
ENTRY_NAME = "plainfolio-dossier-mcp"
SERVER_MODULE = "plainfolio_dossier.mcp_server"
PACKAGE_RELATIVE = Path("packages") / "plainfolio-dossier"
PLUGIN_RELATIVE = Path("plugins") / "plainfolio-dossier"


def _say(message: str) -> None:
    sys.stderr.write(f"plainfolio-dossier-mcp: {message}\n")
    sys.stderr.flush()


def _repo_root(script_path: Path) -> Path | None:
    """The checkout this launcher lives in (…/plugins/plainfolio-dossier/scripts)."""
    for candidate in script_path.resolve().parents:
        if (candidate / PACKAGE_RELATIVE / "src" / "plainfolio_dossier" / "__init__.py").is_file():
            return candidate
    return None


def _venv_python(root: Path) -> Path | None:
    suffix = ".exe" if os.name == "nt" else ""
    for relative in (
        Path("backend") / ".venv" / "bin" / f"python{suffix}",
        Path("backend") / ".venv" / "Scripts" / f"python{suffix}",
        PACKAGE_RELATIVE / ".venv" / "bin" / f"python{suffix}",
        PACKAGE_RELATIVE / ".venv" / "Scripts" / f"python{suffix}",
    ):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def _base_env(env: dict[str, str]) -> dict[str, str]:
    result = dict(env)
    result.setdefault("PYTHONUTF8", "1")
    result.setdefault("PYTHONIOENCODING", "utf-8")
    return result


def _with_pythonpath(env: dict[str, str], path: Path) -> dict[str, str]:
    result = _base_env(env)
    existing = result.get("PYTHONPATH", "")
    parts = [str(path)] + [item for item in existing.split(os.pathsep) if item]
    result["PYTHONPATH"] = os.pathsep.join(parts)
    return result


def resolve_plan(script_path: Path, env: dict[str, str],
                 *, which_fn=None, module_available_fn=None) -> dict | None:
    """First usable launch plan, in the documented order."""
    which = which_fn or shutil.which

    explicit = (env.get(BIN_ENV) or "").strip()
    if explicit:
        target = Path(explicit).expanduser()
        if target.is_file():
            looks_like_python = "python" in target.name.lower()
            command = [str(target)] + (["-m", SERVER_MODULE] if looks_like_python else [])
            return {
                "source": f"{BIN_ENV}",
                "command": command,
                "cwd": None,
                "env": _base_env(env),
            }
        _say(f"{BIN_ENV} 指向的文件不存在：{explicit}")

    on_path = which(ENTRY_NAME)
    if on_path:
        return {
            "source": "PATH",
            "command": [on_path],
            "cwd": None,
            "env": _base_env(env),
        }

    root = _repo_root(script_path)
    if root is not None:
        python = _venv_python(root)
        src = root / PACKAGE_RELATIVE / "src"
        if python is not None:
            return {
                "source": "仓库虚拟环境",
                "command": [str(python), "-m", SERVER_MODULE],
                "cwd": None,
                "env": _with_pythonpath(env, src),
            }

    available = module_available_fn or _current_interpreter_has_package
    if available():
        return {
            "source": "当前解释器",
            "command": [sys.executable, "-m", SERVER_MODULE],
            "cwd": None,
            "env": _base_env(env),
        }
    return None


def _current_interpreter_has_package() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("plainfolio_dossier") is not None
    except (ImportError, ValueError):  # pragma: no cover - 损坏的安装
        return False


def _default_exec(plan: dict) -> int:
    command = list(plan["command"])
    env = dict(plan["env"])
    cwd = plan.get("cwd")
    if cwd:
        os.chdir(cwd)
    if os.name == "nt":
        sys.stdout.flush()
        sys.stderr.flush()
        return subprocess.run(command, env=env).returncode
    if hasattr(os, "execve"):
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(command[0], command, env)
    raise RuntimeError("os.execve unavailable")  # pragma: no cover


def _plan_payload(plan: dict) -> dict:
    return {
        "source": plan["source"],
        "command": list(plan["command"]),
        "cwd": plan.get("cwd"),
        "pythonpath": plan["env"].get("PYTHONPATH"),
    }


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None,
         script_path: Path | None = None, exec_fn=None,
         which_fn=None, module_available_fn=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    unknown = [item for item in arguments if item != "--print-plan"]
    if unknown:
        _say(f"未知参数：{' '.join(unknown)}；只支持 --print-plan")
        return 2

    environment = dict(os.environ if env is None else env)
    script = Path(script_path) if script_path is not None else Path(__file__)
    plan = resolve_plan(
        script, environment, which_fn=which_fn, module_available_fn=module_available_fn,
    )

    if "--print-plan" in arguments:
        if plan is None:
            _say("没有找到可用的解释器或入口；先 pip install plainfolio-dossier，"
                 f"或设置 {BIN_ENV}")
            return 2
        sys.stdout.write(json.dumps(_plan_payload(plan), ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0

    if plan is None:
        _say("未找到 plainfolio_dossier：请先 pip install plainfolio-dossier，"
             f"或在 {BIN_ENV} 中指定解释器/入口")
        _say(f"已尝试：{BIN_ENV} → PATH({ENTRY_NAME}) → 仓库虚拟环境 → 当前解释器")
        return 2

    executor = exec_fn or (lambda resolved: _default_exec(resolved))
    return int(executor(plan) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
