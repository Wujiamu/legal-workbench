#!/usr/bin/env python3
"""Build ``PlainFolio-Setup-<version>-x64.exe`` (run on a Windows build host).

Pipeline:

1. frontend: reuse ``frontend/dist`` when present, otherwise ``npm run build``;
2. PyInstaller ``onedir`` from ``packaging/plainfolio.spec``
   → ``packaging/dist/PlainFolio/`` (exe + ``_internal``);
3. ``stage_bundle.py`` copies portable PostgreSQL 18 + OCR models into
   ``packaging/dist/PlainFolio/_internal/runtime``;
4. Inno Setup 6 compiles ``packaging/windows/plainfolio.iss`` with
   ``/DAppVersion`` / ``/DSourceDir`` / ``/DOutputDir``.

``--dry-run`` parses the arguments, checks the ``.iss`` template (structure and
the uninstall-safety rules), compiles the spec for syntax, and prints the build
plan as JSON.  It touches nothing and needs neither PyInstaller nor Inno Setup,
so it runs on a macOS development machine and in CI.

Exit codes: 0 ok, 2 invalid input, 3 build step failed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bundle_common as common  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BUILD_FAILED = 3

EXECUTABLE_NAME = "PlainFolio"
SETUP_BASENAME = "PlainFolio-Setup-{version}-x64.exe"
CHINESE_MESSAGES = Path("Languages") / "ChineseSimplified.isl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="组装 Windows 安装包 PlainFolio-Setup-<version>-x64.exe（支持 --dry-run）")
    parser.add_argument("--source-root", default=str(common.REPO_ROOT))
    parser.add_argument("--output-dir", default=str(common.REPO_ROOT / "packaging" / "dist"))
    parser.add_argument("--work-dir", default=str(common.REPO_ROOT / "packaging" / "build"))
    parser.add_argument("--staging-dir", default=str(common.REPO_ROOT / "packaging" / "staging" / "windows"))
    parser.add_argument("--version", default=None)
    parser.add_argument("--iss", default=None, help="安装脚本（默认 packaging/windows/plainfolio.iss）")
    parser.add_argument("--iscc", default=os.environ.get("ISCC"),
                        help="ISCC.exe 路径（默认查 PATH 与 Program Files）")
    parser.add_argument("--pg-dir", default=os.environ.get("LWB_PG_SOURCE_DIR"))
    parser.add_argument("--models-dir", default=os.environ.get("LWB_OCR_SOURCE_DIR"))
    parser.add_argument("--build-frontend", action="store_true",
                        help="即使 frontend/dist 已存在也重新执行 npm run build")
    parser.add_argument("--allow-missing-runtime", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验参数、.iss 与构建计划，不执行任何构建步骤")
    return parser


def find_iscc(explicit: str | None) -> Path | None:
    """Locate ``ISCC.exe``: explicit path → ``ISCC`` env → PATH → Program Files."""
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return Path(found)
    for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
        if candidate.is_file():
            return candidate
    return None


def validate(source_root: Path, *, iss: Path, spec: Path,
             pg_dir: Path | None, models_dir: Path | None,
             allow_missing_runtime: bool) -> list[str]:
    problems: list[str] = []
    if not iss.is_file():
        problems.append(f"安装脚本不存在：{iss}")
    else:
        try:
            text = iss.read_text(encoding="utf-8-sig")
        except OSError as exc:
            problems.append(f"安装脚本无法读取：{exc}")
        else:
            problems.extend(f"plainfolio.iss：{item}" for item in common.validate_iss(text))
    if not spec.is_file():
        problems.append(f"PyInstaller spec 不存在：{spec}")
    else:
        try:
            compile(spec.read_text(encoding="utf-8"), str(spec), "exec")
        except SyntaxError as exc:
            problems.append(f"spec 语法错误：{exc}")
    for relative in common.CODE_REQUIRED:
        if not (source_root / relative).is_file():
            problems.append(f"缺少必需文件：{relative}")
    if not (source_root / "scripts" / "legal-workbench.ico").is_file():
        problems.append("缺少 scripts/legal-workbench.ico（安装向导与快捷方式图标）")
    if not allow_missing_runtime:
        if pg_dir is None:
            problems.append("未提供便携 PostgreSQL 18 目录（--pg-dir 或 LWB_PG_SOURCE_DIR）")
        else:
            missing = [tool for tool in common.pg_tool_names("win32")
                       if not (pg_dir / "bin" / tool).is_file()
                       and not (pg_dir / tool).is_file()]
            if missing:
                problems.append(f"便携 PostgreSQL 缺少：{', '.join(missing)}")
        if models_dir is None:
            problems.append("未提供 OCR 模型目录（--models-dir 或 LWB_OCR_SOURCE_DIR）")
    return problems


def build_plan(source_root: Path, *, version: str, output_dir: Path, work_dir: Path,
               staging_dir: Path, iss: Path, spec: Path, iscc: Path | None,
               pg_dir: Path | None, models_dir: Path | None,
               allow_missing_runtime: bool) -> dict:
    onedir = output_dir / EXECUTABLE_NAME
    chinese = (iscc.parent / CHINESE_MESSAGES) if iscc is not None else None
    return {
        "mode": "build",
        "app": common.APP_NAME,
        "app_version": version,
        "platform": "windows",
        "arch": "x64",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z"),
        "paths": {
            "source_root": str(source_root),
            "spec": str(spec),
            "iss": str(iss),
            "output_dir": str(output_dir),
            "work_dir": str(work_dir),
            "staging_dir": str(staging_dir),
            "onedir": str(onedir),
            "installer": str(output_dir / SETUP_BASENAME.format(version=version)),
            "pg_dir": str(pg_dir) if pg_dir else None,
            "models_dir": str(models_dir) if models_dir else None,
        },
        "stages": [
            "frontend: reuse frontend/dist or run npm run build",
            "pyinstaller onedir (packaging/plainfolio.spec)",
            "stage runtime/postgres + runtime/models into _internal",
            "ISCC /DAppVersion /DSourceDir /DOutputDir packaging/windows/plainfolio.iss",
        ],
        "runtime": {
            "bundled": bool(pg_dir or models_dir),
            "allow_missing_runtime": bool(allow_missing_runtime),
        },
        "tools": {
            "iscc": str(iscc) if iscc else None,
            "chinese_messages": str(chinese) if chinese else None,
            "chinese_messages_present": bool(chinese and chinese.is_file()),
            "pyinstaller": _available("PyInstaller"),
            "npm": shutil.which("npm"),
        },
    }


def _available(module: str) -> bool:
    try:
        __import__(module)
    except Exception:  # noqa: BLE001 - availability probe only
        return False
    return True


def _run(command: list[str], *, cwd: Path) -> None:
    print(f"+ {' '.join(command)}", file=sys.stderr)
    subprocess.run(command, cwd=str(cwd), check=True)


def build_frontend(source_root: Path, *, force: bool) -> None:
    dist = source_root / "frontend" / "dist" / "index.html"
    if dist.is_file() and not force:
        return
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("找不到 npm；请先安装 Node.js 22.22.2+/24.15.0+/26+ 或手工构建前端")
    _run([npm, "run", "build"], cwd=source_root / "frontend")


def stage_runtime(source_root: Path, onedir: Path, *, pg_dir: Path | None,
                  models_dir: Path | None, allow_missing_runtime: bool) -> None:
    internal = onedir / "_internal"
    internal.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(source_root / "packaging" / "stage_bundle.py"),
               "--source-root", str(source_root), "--staging-dir", str(internal),
               "--platform", "windows"]
    if pg_dir is not None:
        command += ["--pg-dir", str(pg_dir)]
    if models_dir is not None:
        command += ["--models-dir", str(models_dir)]
    if allow_missing_runtime:
        command.append("--allow-missing-runtime")
    _run(command, cwd=source_root)


def compile_installer(iscc: Path, iss: Path, *, version: str, onedir: Path,
                      output_dir: Path) -> None:
    if not (iscc.parent / CHINESE_MESSAGES).is_file():
        raise RuntimeError(
            f"Inno Setup 缺少简体中文语言文件：{iscc.parent / CHINESE_MESSAGES}；"
            "请把 ChineseSimplified.isl 放入 Inno Setup 6 的 Languages 目录（见 docs/PACKAGING.md）")
    _run([str(iscc), f"/DAppVersion={version}", f"/DSourceDir={onedir}",
          f"/DOutputDir={output_dir}", str(iss)], cwd=iss.parent)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    source_root = Path(args.source_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    staging_dir = Path(args.staging_dir).expanduser().resolve()
    iss = Path(args.iss).expanduser().resolve() if args.iss else (
        source_root / "packaging" / "windows" / "plainfolio.iss")
    spec = source_root / "packaging" / "plainfolio.spec"
    pg_dir = Path(args.pg_dir).expanduser().resolve() if args.pg_dir else None
    models_dir = Path(args.models_dir).expanduser().resolve() if args.models_dir else None
    version = args.version or common.read_version(source_root)
    iscc = find_iscc(args.iscc)
    # A dry run must work on a machine that has neither the portable PostgreSQL
    # nor the OCR models: it verifies the plan, not the payload.
    allow_missing = args.allow_missing_runtime or args.dry_run

    plan = build_plan(source_root, version=version, output_dir=output_dir,
                      work_dir=work_dir, staging_dir=staging_dir, iss=iss, spec=spec,
                      iscc=iscc, pg_dir=pg_dir, models_dir=models_dir,
                      allow_missing_runtime=allow_missing)
    problems = validate(source_root, iss=iss, spec=spec, pg_dir=pg_dir,
                        models_dir=models_dir, allow_missing_runtime=allow_missing)
    if args.dry_run:
        plan["mode"] = "dry-run"
        if problems:
            plan["problems"] = problems
        print(common.dump_json(plan))
        for problem in problems:
            print(f"build_windows_exe: {problem}", file=sys.stderr)
        return EXIT_INVALID if problems else EXIT_OK
    if problems:
        for problem in problems:
            print(f"build_windows_exe: {problem}", file=sys.stderr)
        return EXIT_INVALID
    if iscc is None:
        print("build_windows_exe: 找不到 ISCC.exe；请安装 Inno Setup 6 或设置 ISCC",
              file=sys.stderr)
        return EXIT_INVALID
    if not _available("PyInstaller"):
        print("build_windows_exe: 需要先在构建机安装 PyInstaller（uv add --dev pyinstaller）",
              file=sys.stderr)
        return EXIT_INVALID

    print(common.dump_json(plan))
    try:
        build_frontend(source_root, force=args.build_frontend)
        _run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
              "--distpath", str(output_dir), "--workpath", str(work_dir), str(spec)],
             cwd=source_root)
        onedir = output_dir / EXECUTABLE_NAME
        if not onedir.is_dir():
            print(f"build_windows_exe: PyInstaller 未生成 {onedir}", file=sys.stderr)
            return EXIT_BUILD_FAILED
        stage_runtime(source_root, onedir, pg_dir=pg_dir, models_dir=models_dir,
                      allow_missing_runtime=allow_missing)
        compile_installer(iscc, iss, version=version, onedir=onedir, output_dir=output_dir)
    except subprocess.CalledProcessError as exc:
        print(f"build_windows_exe: 构建步骤失败：{exc}", file=sys.stderr)
        return EXIT_BUILD_FAILED
    except (OSError, RuntimeError) as exc:
        print(f"build_windows_exe: 构建失败：{exc}", file=sys.stderr)
        return EXIT_BUILD_FAILED
    installer = output_dir / SETUP_BASENAME.format(version=version)
    if not installer.is_file():
        print(f"build_windows_exe: 安装包未生成：{installer}", file=sys.stderr)
        return EXIT_BUILD_FAILED
    print(common.dump_json({**plan, "result": "built", "installer": str(installer)}))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
