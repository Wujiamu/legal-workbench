#!/usr/bin/env python3
"""Portable stdio launcher for the 素章 (PlainFolio) legal workbench MCP server.

The workbench virtual environment is not always where a client looks for it, so
this wrapper locates the installation first and then hands the process over to
the real MCP server.  It uses the Python standard library only (3.8+), so any
client can start it with the system ``python3``.

stdout is reserved for MCP protocol frames.  Every diagnostic — including the
output of an optional database auto-start — goes to stderr, so a chatty
``pg_ctl`` can never corrupt the JSON-RPC stream.

Workbench lookup order
    1. ``LWB_HOME``
    2. ``LWB_INSTANCE_CONFIG`` (default ``~/.plainfolio/instance.json``), reading
       ``project_root`` and then ``home``
    3. the nearest ancestor of this script that looks like a workbench checkout

A directory counts as an installation when it holds the MCP server module (a
source checkout) **or** the desktop binary of a packaged install.  For the
latter this wrapper re-enters that binary with ``--run-mcp``: a frozen build has
no ``backend/.venv`` and no importable source tree to hand over to.

Usage
    python3 run-mcp.py               # serve MCP over stdio
    python3 run-mcp.py --print-plan  # print the resolved launch plan as JSON
    python3 run-mcp.py --help

Exit codes
    0  normal termination
    1  the located workbench could not be launched
    2  no usable workbench installation was found
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Mapping, Sequence

PROGRAM = "plainfolio-mcp"
HOME_ENV = "LWB_HOME"
INSTANCE_ENV = "LWB_INSTANCE_CONFIG"
AUTO_START_ENV = "LWB_MCP_AUTO_START_DB"
INSTANCE_RELATIVE = (".plainfolio", "instance.json")
WORKBENCH_MARKER = ("backend", "src", "legal_workbench", "mcp_server.py")
MODULE_ARGS = ("-m", "legal_workbench.mcp_server")
TRUTHY = frozenset({"1", "true", "yes", "on"})

#: A packaged install is recognised by the desktop binary it ships.
PACKAGED_MODE = "packaged"
PACKAGED_EXE_NAMES = ("PlainFolio", "PlainFolio.exe", "素章", "素章.exe", "法律工作台", "法律工作台.exe")
PACKAGED_APP_NAMES = ("PlainFolio.app", "素章.app", "法律工作台.app")
PACKAGED_ARG = "--run-mcp"

EXIT_LAUNCH_FAILED = 1
EXIT_NO_WORKBENCH = 2

HELP_TEXT = f"""\
{PROGRAM} — 启动素章（PlainFolio）法律工作台的 MCP 服务（stdio）

用法：
  python3 run-mcp.py               启动 MCP 服务（stdout 只输出 MCP 协议）
  python3 run-mcp.py --print-plan  仅打印解析出的启动计划（JSON），不启动
  python3 run-mcp.py --help        显示本帮助

工作台查找顺序：
  1. 环境变量 {HOME_ENV}
  2. 实例指针 {INSTANCE_ENV}（默认 ~/.plainfolio/instance.json）的 project_root / home
  3. 从本脚本位置逐级向上探测仓库根

相关环境变量：
  {HOME_ENV}                 直接指定工作台目录
  {INSTANCE_ENV}        覆盖实例指针文件路径（便于测试与多实例）
  {AUTO_START_ENV}=1     启动 MCP 前先启动 PostgreSQL（源码态跑 scripts/lwb start-db，
                        打包态调用内置程序的 --start-db）
"""


class LaunchError(RuntimeError):
    """The located workbench cannot be started with what this machine has."""


@dataclass(frozen=True)
class Resolution:
    """Result of the workbench lookup, including why earlier candidates failed."""

    home: Path | None
    source: str
    attempts: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchPlan:
    """Exactly what will be executed, and from where."""

    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    home: Path
    source: str
    interpreter: str


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if reconfigure is not None and (sys.platform == "win32" or encoding != "utf8"):
            reconfigure(encoding="utf-8", errors="replace")


def instance_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Pointer file location; ``LWB_INSTANCE_CONFIG`` keeps tests off the real one."""
    values = os.environ if env is None else env
    override = _text(values.get(INSTANCE_ENV))
    if override:
        return Path(override).expanduser()
    return Path.home().joinpath(*INSTANCE_RELATIVE)


def read_pointer(path: Path) -> dict[str, object]:
    """Read the pointer file, treating anything unusable as "no pointer"."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_workbench(path: Path | str) -> bool:
    """A directory is a workbench when the MCP server module is really there."""
    return Path(path).joinpath(*WORKBENCH_MARKER).is_file()


def packaged_executable(home: Path | str, pointer: Mapping[str, object] | None = None) -> Path | None:
    """Desktop binary that can serve MCP for a packaged install, if any.

    The instance pointer written by the desktop launcher records ``executable``
    and ``mode``; older or hand-made pointers may only have the directory, so a
    small set of shipped binary names is probed as well.  A pointer that claims
    ``packaged`` but names a missing binary is *not* silently downgraded to the
    probe: the install was moved, and guessing another binary would launch the
    wrong program.
    """
    base = Path(home)
    recorded = _text((pointer or {}).get("executable"))
    if recorded:
        candidate = Path(recorded).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_file():
            return candidate
        if _text((pointer or {}).get("mode")) == PACKAGED_MODE:
            return None
    for name in PACKAGED_EXE_NAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    for app in PACKAGED_APP_NAMES:
        candidate = base / app / "Contents" / "MacOS" / app[: -len(".app")]
        if candidate.is_file():
            return candidate
    return None


def is_instance(path: Path | str, pointer: Mapping[str, object] | None = None) -> bool:
    """A directory this wrapper can actually start (checkout or packaged)."""
    return is_workbench(path) or packaged_executable(path, pointer) is not None


def _candidate(value: object, base: Path) -> Path | None:
    text = _text(value)
    if not text:
        return None
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else base / candidate


def resolve_home(script_path: Path | str | None = None,
                 env: Mapping[str, str] | None = None) -> Resolution:
    """Locate the workbench, recording every candidate that was rejected."""
    values = os.environ if env is None else env
    script = Path(script_path or __file__).resolve()
    attempts: list[str] = []
    # Read the pointer first: it is what tells a packaged directory apart from
    # an arbitrary one, so every candidate below is judged with it in hand.
    pointer_path = instance_config_path(values)
    pointer = read_pointer(pointer_path)

    override = _text(values.get(HOME_ENV))
    if override:
        candidate = _candidate(override, Path.cwd())
        if candidate is not None and is_instance(candidate, pointer):
            return Resolution(candidate.resolve(), f"环境变量 {HOME_ENV}")
        attempts.append(f"环境变量 {HOME_ENV}={override}：不是素章工作台目录")

    if not pointer:
        attempts.append(f"实例指针 {pointer_path}：文件不存在或无法解析")
    else:
        recorded = len(attempts)
        for key in ("project_root", "home"):
            candidate = _candidate(pointer.get(key), Path.cwd())
            if candidate is not None and is_instance(candidate, pointer):
                return Resolution(candidate.resolve(), f"实例指针 {pointer_path} 的 {key}",
                                  tuple(attempts))
            if _text(pointer.get(key)):
                attempts.append(f"实例指针 {pointer_path} 的 {key}="
                                f"{_text(pointer.get(key))}：不是素章工作台目录")
        if len(attempts) == recorded:
            attempts.append(f"实例指针 {pointer_path}：project_root/home 均未指向工作台")

    for parent in script.parents:
        if is_instance(parent, pointer):
            return Resolution(parent.resolve(), "脚本所在仓库", tuple(attempts))
    attempts.append(f"从 {script} 逐级向上探测：未找到 backend/src/legal_workbench")
    return Resolution(None, "", tuple(attempts))


def venv_python(home: Path) -> Path | None:
    """Prefer the workbench's own interpreter; accept a root-level venv too."""
    for candidate in (
        home / "backend" / ".venv" / "bin" / "python",
        home / "backend" / ".venv" / "Scripts" / "python.exe",
        home / ".venv" / "bin" / "python",
        home / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def launch_env(home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for the child: same installation, importable, UTF-8 clean."""
    env = dict(os.environ if base is None else base)
    source_root = str(home / "backend" / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Pin the child to this installation even if the pointer later changes.
    env[HOME_ENV] = str(home)
    return env


def packaged_env(home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a frozen binary: no PYTHONPATH, same pinned home."""
    env = dict(os.environ if base is None else base)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env[HOME_ENV] = str(home)
    return env


def build_plan(home: Path, source: str, env: Mapping[str, str] | None = None,
               which: Callable[[str], str | None] | None = None,
               pointer: Mapping[str, object] | None = None) -> LaunchPlan:
    """Decide what runs the server: a packaged binary, the venv, or PATH."""
    executable = packaged_executable(home, pointer)
    if executable is not None:
        return LaunchPlan((str(executable), PACKAGED_ARG), home, packaged_env(home, env),
                          home, source, "packaged")
    base = launch_env(home, env)
    python = venv_python(home)
    if python is not None:
        return LaunchPlan((str(python), *MODULE_ARGS), home / "backend", base, home,
                          source, "venv")
    finder = shutil.which if which is None else which
    entry = finder("lwb-mcp")
    if entry:
        return LaunchPlan((str(entry),), home, base, home, source, "path")
    raise LaunchError(
        "工作台已定位到 "
        f"{home}，但既没有可用的虚拟环境（backend/.venv 或 .venv），"
        "PATH 中也没有 lwb-mcp 入口。请按工作台 README 安装后端依赖，"
        "或执行 backend/.venv/bin/pip install -e backend（Windows 为 "
        "backend\\.venv\\Scripts\\pip install -e backend）。"
    )


def auto_start_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return _text(values.get(AUTO_START_ENV)).lower() in TRUTHY


def start_database(plan: LaunchPlan, *,
                   run: Callable[..., object] | None = None) -> int:
    """Start PostgreSQL (``scripts/lwb start-db`` or ``<binary> --start-db``)."""
    runner = subprocess.run if run is None else run
    if plan.interpreter == "packaged":
        command = [plan.command[0], "--start-db"]
        print(f"{PROGRAM}: 预启动 PostgreSQL：{plan.command[0]} --start-db", file=sys.stderr)
    else:
        script = plan.home / "scripts" / "lwb"
        if not script.is_file():
            print(f"{PROGRAM}: 未找到 {script}，跳过数据库预启动。", file=sys.stderr)
            return 0
        interpreter = plan.command[0] if plan.interpreter == "venv" else sys.executable
        command = [interpreter, str(script), "start-db"]
        print(f"{PROGRAM}: 预启动 PostgreSQL：{interpreter} scripts/lwb start-db",
              file=sys.stderr)
    completed = runner(
        command, cwd=str(plan.home), env=plan.env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    output = (getattr(completed, "stdout", "") or "").strip()
    if output:
        print(output, file=sys.stderr)
    return int(getattr(completed, "returncode", 1) or 0)


def guidance(resolution: Resolution, env: Mapping[str, str] | None = None,
             extra: str | None = None) -> str:
    """Human-readable, copy-pasteable instructions printed when launching fails."""
    pointer = instance_config_path(env)
    lines = [f"{PROGRAM}: 找不到可用的素章（PlainFolio）法律工作台。", "", "已尝试："]
    lines.extend(f"  - {attempt}" for attempt in resolution.attempts)
    if extra:
        lines.append(f"  - {extra}")
    lines.extend([
        "",
        "请先在本机完成工作台安装与初始化（见工作台 README），然后在工作台目录执行一次：",
        "  scripts/lwb register-instance        # macOS / Linux",
        "  scripts\\lwb.cmd register-instance    # Windows",
        f"该命令把工作台路径与时间戳写入 {pointer}，不写入任何凭据。",
        "",
        f"也可以用环境变量直接指定工作台目录后重试：{HOME_ENV}=/path/to/legal-workbench "
        "python3 run-mcp.py",
    ])
    return "\n".join(lines)


def plan_payload(plan: LaunchPlan) -> dict[str, object]:
    """Diagnostic view of a plan; the environment is deliberately not printed."""
    return {
        "home": str(plan.home),
        "source": plan.source,
        "interpreter": plan.interpreter,
        "command": list(plan.command),
        "cwd": str(plan.cwd),
    }


def main(argv: Sequence[str] | None = None, *,
         env: Mapping[str, str] | None = None,
         script_path: Path | str | None = None,
         exec_fn: Callable[..., object] | None = None,
         run_fn: Callable[..., object] | None = None,
         chdir_fn: Callable[[str], None] | None = None,
         which_fn: Callable[[str], str | None] | None = None) -> int:
    """Resolve, optionally prepare the database, then become the MCP server."""
    _ensure_utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in args):
        print(HELP_TEXT)
        return 0
    unknown = [arg for arg in args if arg != "--print-plan"]
    if unknown:
        print(f"{PROGRAM}: 未知参数：{' '.join(unknown)}", file=sys.stderr)
        print(HELP_TEXT, file=sys.stderr)
        return EXIT_NO_WORKBENCH

    values = dict(os.environ if env is None else env)
    resolution = resolve_home(script_path, values)
    if resolution.home is None:
        print(guidance(resolution, values), file=sys.stderr)
        return EXIT_NO_WORKBENCH
    try:
        plan = build_plan(resolution.home, resolution.source, values, which_fn,
                          read_pointer(instance_config_path(values)))
    except LaunchError as exc:
        print(guidance(resolution, values, extra=str(exc)), file=sys.stderr)
        return EXIT_NO_WORKBENCH

    if "--print-plan" in args:
        print(json.dumps(plan_payload(plan), ensure_ascii=False, indent=2))
        return 0

    if auto_start_enabled(values):
        code = start_database(plan, run=run_fn)
        if code != 0:
            print(f"{PROGRAM}: 警告：scripts/lwb start-db 退出码 {code}；继续启动 MCP，"
                  "工具调用可能因数据库不可用而失败。", file=sys.stderr)

    change_dir = os.chdir if chdir_fn is None else chdir_fn
    try:
        change_dir(str(plan.cwd))
    except OSError as exc:
        print(f"{PROGRAM}: 无法进入工作目录 {plan.cwd}：{exc}", file=sys.stderr)
        return EXIT_LAUNCH_FAILED

    launcher = os.execve if exec_fn is None else exec_fn
    try:
        launcher(plan.command[0], list(plan.command), plan.env)
    except OSError as exc:
        print(f"{PROGRAM}: 启动 MCP 服务失败（{plan.command[0]}）：{exc}", file=sys.stderr)
        return EXIT_LAUNCH_FAILED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
