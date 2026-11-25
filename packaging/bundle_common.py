"""Shared helpers for the desktop packaging scripts.

These scripts run on a build machine (possibly a different OS from the one they
package for), so they use the standard library only and never import the
application package: a Windows build host must not need a working PostgreSQL to
verify that the payload is complete.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import re
import sys

APP_NAME = "PlainFolio"
MANIFEST_NAME = "bundle-manifest.json"
MANIFEST_SCHEMA = "plainfolio-bundle/1"

#: Every PostgreSQL binary the launcher or the backup path can call.
PG_REQUIRED_TOOLS = ("initdb", "pg_ctl", "postgres", "psql", "createdb",
                     "pg_dump", "pg_restore")

#: Files that must exist for a bundle to start at all.
CODE_REQUIRED = (
    "frontend/dist/index.html",
    "backend/alembic.ini",
    "backend/migrations/env.py",
    "plugins/plainfolio/plugin.json",
    "plugins/plainfolio/scripts/run-mcp.py",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def platform_slug(platform_name: str | None = None) -> str:
    name = sys.platform if platform_name is None else platform_name
    if name == "darwin":
        return "macos"
    if name in ("win32", "nt", "windows"):
        return "windows"
    return "linux"


def is_windows(platform_name: str | None = None) -> bool:
    name = sys.platform if platform_name is None else platform_name
    return name in ("win32", "nt", "windows")


def pg_tool_names(platform_name: str | None = None) -> tuple[str, ...]:
    suffix = ".exe" if is_windows(platform_name) else ""
    return tuple(f"{tool}{suffix}" for tool in PG_REQUIRED_TOOLS)


def read_version(source_root: Path | str = REPO_ROOT) -> str:
    """Application version from ``backend/pyproject.toml`` (no toml dependency)."""
    pyproject = Path(source_root) / "backend" / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


_REVISION = re.compile(r"(?m)^\s*revision\s*=\s*[\"']([^\"']+)[\"']")
_DOWN_REVISION = re.compile(r"(?m)^\s*down_revision\s*=\s*(.+)$")


def heads(versions_dir: Path | str) -> list[str]:
    """Revisions that no other file names as ``down_revision``."""
    directory = Path(versions_dir)
    if not directory.is_dir():
        return []
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(directory.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        found = _REVISION.search(text)
        if not found:
            continue
        revisions.add(found.group(1))
        down = _DOWN_REVISION.search(text)
        if down:
            parents.update(re.findall(r"[\"']([^\"']+)[\"']", down.group(1)))
    return sorted(revisions - parents)


def migration_head(versions_dir: Path | str) -> str | None:
    """Head revision of a migration directory, read without importing Alembic.

    More than one head means the chain is branched and the bundle must not ship.
    """
    found = heads(versions_dir)
    return found[0] if len(found) == 1 else None


def directory_files(directory: Path | str) -> list[Path]:
    """Every regular file under ``directory``, ignoring bytecode caches."""
    root = Path(directory)
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


def file_entries(directory: Path | str, *, relative_to: Path | str | None = None) -> list[dict]:
    """``[{path, size, sha256}]`` for a directory tree, for the manifest."""
    root = Path(directory)
    base = Path(relative_to) if relative_to is not None else root
    entries = []
    for path in directory_files(root):
        entries.append({
            "path": path.relative_to(base).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return entries


def dump_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def machine_arch() -> str:
    """Normalised CPU architecture for artifact names."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine or "unknown"


# --- Inno Setup template checks -------------------------------------------

ISS_SECTIONS = re.compile(r"(?m)^\s*\[([^\]]+)\]\s*$")


def iss_sections(text: str) -> dict[str, list[str]]:
    """Split an ``.iss`` file into ``{section: [lines]}`` for static checks."""
    text = text.lstrip("\ufeff")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        found = ISS_SECTIONS.match(line)
        if found:
            name = found.group(1).strip().lower()
            current = name
            sections.setdefault(name, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def iss_directive(lines: list[str], key: str) -> str | None:
    pattern = re.compile(rf"(?i)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$")
    for line in lines:
        found = pattern.match(line)
        if found:
            return found.group(1)
    return None


def _code_lines(lines: list[str]) -> list[str]:
    """Drop ``;`` comments: documentation must never trip a safety rule."""
    return [line for line in lines if not line.lstrip().startswith(";")]


def validate_iss(text: str) -> list[str]:
    """Structural and safety checks for the installer template.

    The safety checks are the important half: an uninstaller that deletes
    ``%LOCALAPPDATA%\\PlainFolio`` would destroy the user's case database, which
    is the one thing this product must never do.
    """
    problems: list[str] = []
    sections = iss_sections(text)
    for required in ("setup", "files", "icons", "languages"):
        if required not in sections:
            problems.append(f"缺少 [{required}] 段")
    setup = sections.get("setup", [])
    directory = iss_directive(setup, "DefaultDirName") or ""
    if "{localappdata}" not in directory.lower():
        problems.append("DefaultDirName 必须位于 {localappdata}（免管理员安装）")
    privileges = (iss_directive(setup, "PrivilegesRequired") or "").lower()
    if privileges != "lowest":
        problems.append("PrivilegesRequired 必须为 lowest")
    if not iss_directive(setup, "AppId"):
        problems.append("[Setup] 缺少 AppId（升级覆盖安装依赖它保持稳定）")
    languages = " ".join(_code_lines(sections.get("languages", [])))
    if "chinesesimplified" not in languages.lower():
        problems.append("[Languages] 缺少简体中文（chinesesimplified）")
    icons = " ".join(_code_lines(sections.get("icons", [])))
    if "legal-workbench.ico" not in icons.lower():
        problems.append("[Icons] 未使用 scripts/legal-workbench.ico")
    files = " ".join(_code_lines(sections.get("files", [])))
    if "{app}" not in files:
        problems.append("[Files] 未把程序文件安装到 {app}")
    tasks = " ".join(_code_lines(sections.get("tasks", [])))
    if "path" not in tasks.lower():
        problems.append("[Tasks] 缺少可选的“加入 PATH”任务")

    # Never delete anything outside the install directory.
    uninstall = " ".join(_code_lines(sections.get("uninstalldelete", [])))
    lowered = uninstall.lower()
    for forbidden in ("{localappdata}\\plainfolio", "{localappdata}/plainfolio",
                      "%localappdata%\\plainfolio", "var\\data", "var/data",
                      "{userappdata}", "{commondocs}", "{docs}"):
        if forbidden in lowered:
            problems.append(f"[UninstallDelete] 不得删除用户数据目录（命中 {forbidden}）")
    return problems


def parse_define(text: str, name: str) -> str | None:
    """Default value of an ISPP ``#define`` (``#define Name "value"``)."""
    pattern = re.compile(rf"(?m)^\s*#define\s+{re.escape(name)}\s+\"?([^\"\r\n]+)\"?\s*$")
    found = pattern.search(text)
    return found.group(1).strip() if found else None
