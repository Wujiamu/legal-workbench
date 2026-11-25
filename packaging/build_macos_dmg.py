#!/usr/bin/env python3
"""Build ``PlainFolio-<version>-macos-<arch>.dmg`` from this checkout (macOS).

Pipeline (each stage is a separate, inspectable directory):

1. PyInstaller ``onedir`` build from ``packaging/plainfolio.spec``
   → ``packaging/dist/PlainFolio/``;
2. assemble ``PlainFolio.app``: the onedir payload into ``Contents/MacOS``,
   ``Info.plist``, the existing ``AppIcon.icns``, and the external runtime
   (portable PostgreSQL 18 + OCR models) into ``Contents/MacOS/_internal/runtime``
   so ``sys._MEIPASS`` finds it;
3. ``hdiutil create`` a compressed UDZO image with an ``/Applications`` symlink.

``--dry-run`` performs every validation and prints the exact build plan as JSON
without invoking PyInstaller or hdiutil, so it is safe on any machine and in CI.

Exit codes: 0 ok, 2 invalid input, 3 build step failed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bundle_common as common  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BUILD_FAILED = 3

APP_BUNDLE = "PlainFolio.app"
EXECUTABLE_NAME = "PlainFolio"
BUNDLE_ID = "local.plainfolio.desktop"
DEFAULT_ICON = Path("法律工作台.app") / "Contents" / "Resources" / "AppIcon.icns"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="组装 PlainFolio.app 并生成 macOS .dmg（支持 --dry-run）")
    parser.add_argument("--source-root", default=str(common.REPO_ROOT))
    parser.add_argument("--output-dir", default=str(common.REPO_ROOT / "packaging" / "dist"))
    parser.add_argument("--work-dir", default=str(common.REPO_ROOT / "packaging" / "build"))
    parser.add_argument("--staging-dir", default=str(common.REPO_ROOT / "packaging" / "staging" / "macos"))
    parser.add_argument("--version", default=None)
    parser.add_argument("--icon", default=None,
                        help=f"应用图标（默认 {DEFAULT_ICON.as_posix()}）")
    parser.add_argument("--pg-dir", default=os.environ.get("LWB_PG_SOURCE_DIR"))
    parser.add_argument("--models-dir", default=os.environ.get("LWB_OCR_SOURCE_DIR"))
    parser.add_argument("--allow-missing-runtime", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验输入并打印构建计划，不调用 PyInstaller / hdiutil")
    return parser


def validate(source_root: Path, *, icon: Path, spec: Path,
             pg_dir: Path | None, models_dir: Path | None,
             allow_missing_runtime: bool) -> list[str]:
    problems: list[str] = []
    if not icon.is_file():
        problems.append(f"应用图标不存在：{icon}")
    if not spec.is_file():
        problems.append(f"PyInstaller spec 不存在：{spec}")
    else:
        try:
            source = spec.read_text(encoding="utf-8")
            compile(source, str(spec), "exec")
        except SyntaxError as exc:
            problems.append(f"spec 语法错误：{exc}")
        else:
            for marker in ("Analysis(", "COLLECT(", "desktop_launcher.py"):
                if marker not in source:
                    problems.append(f"spec 缺少 {marker}（必须是 onedir 构建）")
    for relative in common.CODE_REQUIRED:
        if not (source_root / relative).is_file():
            problems.append(f"缺少必需文件：{relative}")
    if not (source_root / "backend" / "migrations" / "versions").is_dir():
        problems.append("缺少 backend/migrations/versions")
    if not allow_missing_runtime:
        if pg_dir is None:
            problems.append("未提供便携 PostgreSQL 18 目录（--pg-dir 或 LWB_PG_SOURCE_DIR）")
        else:
            missing = [tool for tool in common.pg_tool_names("darwin")
                       if not (pg_dir / "bin" / tool).is_file()
                       and not (pg_dir / tool).is_file()]
            if missing:
                problems.append(f"便携 PostgreSQL 缺少：{', '.join(missing)}")
        if models_dir is None:
            problems.append("未提供 OCR 模型目录（--models-dir 或 LWB_OCR_SOURCE_DIR）")
    return problems


def build_plan(source_root: Path, *, version: str, output_dir: Path, work_dir: Path,
               staging_dir: Path, icon: Path, spec: Path,
               pg_dir: Path | None, models_dir: Path | None,
               allow_missing_runtime: bool) -> dict:
    arch = common.machine_arch()
    dmg = output_dir / f"PlainFolio-{version}-macos-{arch}.dmg"
    return {
        "mode": "build",
        "app": common.APP_NAME,
        "app_version": version,
        "platform": "macos",
        "arch": arch,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z"),
        "paths": {
            "source_root": str(source_root),
            "spec": str(spec),
            "icon": str(icon),
            "output_dir": str(output_dir),
            "work_dir": str(work_dir),
            "staging_dir": str(staging_dir),
            "app_bundle": str(staging_dir / APP_BUNDLE),
            "dmg": str(dmg),
            "pg_dir": str(pg_dir) if pg_dir else None,
            "models_dir": str(models_dir) if models_dir else None,
        },
        "stages": [
            "pyinstaller onedir (packaging/plainfolio.spec)",
            f"assemble {APP_BUNDLE} (Contents/MacOS + Info.plist + AppIcon.icns)",
            "stage runtime/postgres + runtime/models into _internal",
            "hdiutil create -format UDZO (with /Applications symlink)",
        ],
        "runtime": {
            "bundled": bool(pg_dir or models_dir),
            "allow_missing_runtime": bool(allow_missing_runtime),
        },
        "tools": {
            "hdiutil": shutil.which("hdiutil"),
            "pyinstaller": _pyinstaller_available(),
        },
    }


def _pyinstaller_available() -> bool:
    try:
        import PyInstaller  # noqa: F401
    except Exception:  # noqa: BLE001 - availability probe only
        return False
    return True


def _run(command: list[str], *, cwd: Path) -> None:
    print(f"+ {' '.join(command)}", file=sys.stderr)
    subprocess.run(command, cwd=str(cwd), check=True)


def info_plist(version: str) -> bytes:
    payload = {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": common.APP_NAME,
        "CFBundleDisplayName": "素章 PlainFolio",
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
    }
    return plistlib.dumps(payload)


def assemble_app(source_root: Path, onedir: Path, staging_dir: Path, *,
                 version: str, icon: Path) -> Path:
    """Build ``PlainFolio.app`` around the PyInstaller onedir output."""
    app = staging_dir / APP_BUNDLE
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copytree(onedir, macos, dirs_exist_ok=True)
    executable = macos / EXECUTABLE_NAME
    if executable.is_file():
        executable.chmod(0o755)
    (app / "Contents" / "Info.plist").write_bytes(info_plist(version))
    shutil.copy2(icon, resources / "AppIcon.icns")
    return app


def stage_runtime(app: Path, *, source_root: Path, pg_dir: Path | None,
                  models_dir: Path | None, allow_missing_runtime: bool) -> None:
    """Copy PostgreSQL 18 + OCR models where ``sys._MEIPASS`` will look for them."""
    internal = app / "Contents" / "MacOS" / "_internal"
    internal.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(source_root / "packaging" / "stage_bundle.py"),
               "--source-root", str(source_root), "--staging-dir", str(internal),
               "--platform", "macos"]
    if pg_dir is not None:
        command += ["--pg-dir", str(pg_dir)]
    if models_dir is not None:
        command += ["--models-dir", str(models_dir)]
    if allow_missing_runtime:
        command.append("--allow-missing-runtime")
    _run(command, cwd=source_root)


def create_dmg(staging_dir: Path, dmg: Path, *, volume_name: str = common.APP_NAME) -> None:
    applications = staging_dir / "Applications"
    if applications.is_symlink() or applications.is_file():
        applications.unlink()
    elif applications.is_dir():
        shutil.rmtree(applications)
    os.symlink("/Applications", applications)
    dmg.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "hdiutil", "create", "-volname", volume_name,
        "-srcfolder", str(staging_dir), "-ov", "-format", "UDZO", str(dmg),
    ], cwd=staging_dir)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    source_root = Path(args.source_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    staging_dir = Path(args.staging_dir).expanduser().resolve()
    icon = Path(args.icon).expanduser().resolve() if args.icon else (source_root / DEFAULT_ICON)
    spec = source_root / "packaging" / "plainfolio.spec"
    pg_dir = Path(args.pg_dir).expanduser().resolve() if args.pg_dir else None
    models_dir = Path(args.models_dir).expanduser().resolve() if args.models_dir else None
    version = args.version or common.read_version(source_root)
    # A dry run must work on a machine that has neither the portable PostgreSQL
    # nor the OCR models: it verifies the plan, not the payload.
    allow_missing = args.allow_missing_runtime or args.dry_run

    plan = build_plan(source_root, version=version, output_dir=output_dir,
                      work_dir=work_dir, staging_dir=staging_dir, icon=icon, spec=spec,
                      pg_dir=pg_dir, models_dir=models_dir,
                      allow_missing_runtime=allow_missing)
    problems = validate(source_root, icon=icon, spec=spec, pg_dir=pg_dir,
                        models_dir=models_dir, allow_missing_runtime=allow_missing)
    if args.dry_run:
        plan["mode"] = "dry-run"
        if problems:
            plan["problems"] = problems
        print(common.dump_json(plan))
        for problem in problems:
            print(f"build_macos_dmg: {problem}", file=sys.stderr)
        return EXIT_INVALID if problems else EXIT_OK
    if problems:
        for problem in problems:
            print(f"build_macos_dmg: {problem}", file=sys.stderr)
        return EXIT_INVALID
    if not _pyinstaller_available():
        print("build_macos_dmg: 需要先在构建机安装 PyInstaller（uv add --dev pyinstaller）",
              file=sys.stderr)
        return EXIT_INVALID
    if shutil.which("hdiutil") is None:
        print("build_macos_dmg: 找不到 hdiutil，只能在 macOS 上生成 .dmg", file=sys.stderr)
        return EXIT_INVALID

    print(common.dump_json(plan))
    try:
        _run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
              "--distpath", str(output_dir), "--workpath", str(work_dir), str(spec)],
             cwd=source_root)
        onedir = output_dir / EXECUTABLE_NAME
        if not onedir.is_dir():
            print(f"build_macos_dmg: PyInstaller 未生成 {onedir}", file=sys.stderr)
            return EXIT_BUILD_FAILED
        app = assemble_app(source_root, onedir, staging_dir, version=version, icon=icon)
        stage_runtime(app, source_root=source_root, pg_dir=pg_dir,
                      models_dir=models_dir, allow_missing_runtime=allow_missing)
        create_dmg(staging_dir, Path(plan["paths"]["dmg"]))
    except subprocess.CalledProcessError as exc:
        print(f"build_macos_dmg: 构建步骤失败：{exc}", file=sys.stderr)
        return EXIT_BUILD_FAILED
    except OSError as exc:
        print(f"build_macos_dmg: 构建失败：{exc}", file=sys.stderr)
        return EXIT_BUILD_FAILED
    print(common.dump_json({**plan, "result": "built", "dmg": plan["paths"]["dmg"]}))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
