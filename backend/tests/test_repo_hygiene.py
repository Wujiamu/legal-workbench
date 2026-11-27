"""Static repository hygiene gates against doc drift, broken links, and version split."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _repo_non_ignored_files() -> set[str]:
    if not (ROOT / ".git").exists():
        pytest.skip("requires a git checkout")
    out = subprocess.check_output(
        ["git", "-c", "core.quotepath=false", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    return set(out.splitlines())


def _normalize_repo_rel(source_file: Path, raw_target: str) -> str | None:
    target = raw_target.strip()
    if not target or target.startswith("#"):
        return None
    if target.startswith(("http://", "https://", "mailto:", "file://", "conversation://")):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    resolved = (source_file.parent / target).resolve()
    try:
        rel = resolved.relative_to(ROOT.resolve())
    except ValueError:
        return f"__OUTSIDE_REPO__/{target}"
    return rel.as_posix()


def test_tracked_markdown_links_resolve_to_tracked_files() -> None:
    tracked = _repo_non_ignored_files()
    md_files = sorted(p for p in tracked if p.endswith(".md"))
    broken: list[str] = []

    for rel_md in md_files:
        path = ROOT / rel_md
        text = path.read_text(encoding="utf-8")
        cleaned = _FENCED_CODE_RE.sub("", text)
        cleaned = _INLINE_CODE_RE.sub("", cleaned)
        for match in _MD_LINK_RE.finditer(cleaned):
            norm = _normalize_repo_rel(path, match.group(1))
            if norm is None:
                continue
            target_path = ROOT / norm
            if norm.startswith("__OUTSIDE_REPO__/") or not target_path.exists():
                broken.append(f"{rel_md} -> {match.group(1)} (missing)")
                continue
            if target_path.is_dir():
                prefix = norm.rstrip("/") + "/"
                if not any(item.startswith(prefix) for item in tracked):
                    broken.append(f"{rel_md} -> {match.group(1)} (dir untracked/ignored)")
            elif norm not in tracked:
                broken.append(f"{rel_md} -> {match.group(1)} (untracked/gitignored)")

    assert not broken, "Broken or gitignored Markdown links:\n" + "\n".join(broken)


def test_package_versions_stay_in_sync() -> None:
    pyproject = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    backend_version = pyproject["project"]["version"]

    frontend_pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    plugin_pkg = json.loads((ROOT / "plugins" / "plainfolio" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    launcher_text = (ROOT / "backend" / "src" / "legal_workbench" / "desktop_launcher.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', launcher_text, re.MULTILINE)
    assert m is not None, "APP_VERSION not found in desktop_launcher.py"

    versions = {
        "backend/pyproject.toml": backend_version,
        "frontend/package.json": frontend_pkg["version"],
        "plugins/plainfolio/plugin.json": plugin_pkg["version"],
        ".agents/plugins/marketplace.json": marketplace["plugins"][0]["version"],
        "desktop_launcher.py": m.group(1),
    }
    assert len(set(versions.values())) == 1, f"Version mismatch across manifests: {versions}"


def test_main_backend_dependencies_are_documented_in_reuse() -> None:
    pyproject = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    reuse_text = (ROOT / "docs" / "REUSE.md").read_text(encoding="utf-8").lower()
    missing: list[str] = []
    for spec in pyproject["project"]["dependencies"]:
        pkg = re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0].lower()
        if pkg and pkg not in reuse_text:
            missing.append(pkg)
    assert not missing, f"Dependencies in pyproject.toml missing from docs/REUSE.md: {missing}"


def test_active_changes_in_next_steps_are_not_closed_or_deprecated() -> None:
    next_steps = (ROOT / "docs" / "NEXT_STEPS.md").read_text(encoding="utf-8")
    active_rows = [line for line in next_steps.splitlines() if line.startswith("| 活跃变更单")]
    assert active_rows, "Expected '活跃变更单' row in docs/NEXT_STEPS.md"

    violations: list[str] = []
    for row in active_rows:
        for match in re.finditer(r"\((changes/[^)#\s]+\.md)\)", row):
            rel = match.group(1)
            header = "\n".join((ROOT / "docs" / rel).read_text(encoding="utf-8").splitlines()[:10])
            if any(marker in header for marker in ("状态：已封板", "状态：**已封板", "状态：已废弃", "状态：**已废弃")):
                violations.append(f"docs/{rel} is closed/deprecated but still listed as active in docs/NEXT_STEPS.md")
    assert not violations, "\n".join(violations)
