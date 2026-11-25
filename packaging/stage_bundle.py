#!/usr/bin/env python3
"""Assemble and verify the runtime payload that ships next to the frozen app.

The frozen executable contains the Python code; the large external inputs —
portable PostgreSQL 18 and the OCR models — are copied in beside it by this
script, which also writes ``bundle-manifest.json`` with a SHA-256 for every
critical file.  That manifest is what a support engineer reads to answer "is
this install complete and which build is it?" without launching anything.

Two modes:

``--dry-run``
    Validate every input and print the manifest that *would* be written.
    Nothing is created, so a macOS build host can verify a Windows payload plan
    and CI can gate a release on it.

normal
    Copy the payload into ``--staging-dir`` and write the manifest there.

External inputs are never downloaded: ``--pg-dir`` / ``LWB_PG_SOURCE_DIR`` and
``--models-dir`` / ``LWB_OCR_SOURCE_DIR`` name directories the build machine
already has.  ``--allow-missing-runtime`` relaxes only those two checks, so the
code and frontend payload can be validated before the binaries are staged.

Exit codes: 0 ok, 2 validation failed, 3 copy failed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bundle_common as common  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_COPY_FAILED = 3


def _pg_layout(pg_dir: Path) -> tuple[Path, Path]:
    """Return ``(source_root, destination_relative)`` for the PostgreSQL payload.

    A portable distribution is a directory with ``bin/``, ``lib/`` and
    ``share/`` — ``initdb`` needs ``share/`` for its SQL scripts, so shipping
    ``bin`` alone would produce an install that cannot create a cluster.  A bare
    ``bin`` directory is still accepted and placed where the launcher looks.
    """
    if (pg_dir / "bin").is_dir():
        return pg_dir, Path("runtime") / "postgres"
    return pg_dir, Path("runtime") / "postgres" / "bin"


def _models_layout(models_dir: Path) -> tuple[Path, Path]:
    """Return ``(source_dir, destination_relative)`` for the OCR models.

    The engine looks in ``<ocr_model_dir>/rapidocr``, and ``ocr_model_dir`` is
    ``runtime/models`` inside a bundle.  A build machine may hand over either the
    models root (``…/models`` containing ``rapidocr/``) or the rapidocr directory
    itself; both are normalised to the layout the runtime expects.
    """
    if (models_dir / "manifest.json").is_file():
        return models_dir, Path("runtime") / "models" / "rapidocr"
    return models_dir / "rapidocr", Path("runtime") / "models" / "rapidocr"


def verify(source_root: Path, *, platform_name: str | None = None,
           pg_dir: Path | None = None, models_dir: Path | None = None,
           allow_missing_runtime: bool = False) -> list[str]:
    """Every reason this payload must not ship (empty list means valid)."""
    problems: list[str] = []
    for relative in common.CODE_REQUIRED:
        if not (source_root / relative).is_file():
            problems.append(f"缺少必需文件：{relative}")
    versions = source_root / "backend" / "migrations" / "versions"
    if not versions.is_dir():
        problems.append("缺少 backend/migrations/versions 目录")
    else:
        found = common.heads(versions)
        if not found:
            problems.append("backend/migrations/versions 中没有迁移脚本")
        elif len(found) > 1:
            problems.append(f"迁移链存在多个 head（{', '.join(found)}），拒绝打包")
    if not (source_root / "plugins" / "plainfolio" / "scripts" / "run-mcp.py").is_file():
        problems.append("缺少 plugins/plainfolio/scripts/run-mcp.py")

    if pg_dir is None:
        if not allow_missing_runtime:
            problems.append("未提供便携 PostgreSQL 18 目录（--pg-dir 或 LWB_PG_SOURCE_DIR）")
    else:
        _, destination = _pg_layout(pg_dir)
        for tool in common.pg_tool_names(platform_name):
            candidate = pg_dir / "bin" / tool
            if not candidate.is_file():
                candidate = pg_dir / tool
            if not candidate.is_file():
                problems.append(f"便携 PostgreSQL 缺少必需程序：{tool}（{destination}）")

    if models_dir is None:
        if not allow_missing_runtime:
            problems.append("未提供 OCR 模型目录（--models-dir 或 LWB_OCR_SOURCE_DIR）")
    else:
        models_source, _ = _models_layout(models_dir)
        if not (models_source / "manifest.json").is_file():
            problems.append(f"OCR 模型目录缺少 manifest.json（{models_source}）")
        if not any(models_source.glob("*.onnx")):
            problems.append(f"OCR 模型目录缺少 .onnx 模型文件（{models_source}）")

    if not (source_root / "frontend" / "dist" / "assets").is_dir():
        problems.append("frontend/dist/assets 不存在（前端尚未构建）")
    return problems


def build_manifest(source_root: Path, *, version: str, platform_name: str | None = None,
                   pg_dir: Path | None = None, models_dir: Path | None = None,
                   allow_missing_runtime: bool = False) -> dict:
    """Structured description of the payload, including per-file hashes."""
    slug = common.platform_slug(platform_name)
    dist = source_root / "frontend" / "dist"
    migrations = source_root / "backend" / "migrations"
    plugins = source_root / "plugins" / "plainfolio"
    manifest: dict = {
        "schema": common.MANIFEST_SCHEMA,
        "app": common.APP_NAME,
        "app_version": version,
        "platform": slug,
        "arch": common.machine_arch(),
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "allow_missing_runtime": bool(allow_missing_runtime),
        "components": {
            "frontend_dist": {
                "path": "frontend/dist",
                "index_sha256": common.sha256_file(dist / "index.html")
                if (dist / "index.html").is_file() else None,
                "file_count": len(common.directory_files(dist)) if dist.is_dir() else 0,
            },
            "alembic_ini": {
                "path": "backend/alembic.ini",
                "sha256": common.sha256_file(source_root / "backend" / "alembic.ini")
                if (source_root / "backend" / "alembic.ini").is_file() else None,
            },
            "migrations": {
                "path": "backend/migrations",
                "head": common.migration_head(migrations / "versions"),
                "files": common.file_entries(migrations, relative_to=source_root / "backend")
                if migrations.is_dir() else [],
            },
            "plugins": {
                "path": "plugins/plainfolio",
                "files": common.file_entries(plugins, relative_to=source_root)
                if plugins.is_dir() else [],
            },
            "postgres": {"path": "runtime/postgres", "tools": [], "included": False},
            "models": {"path": "runtime/models", "files": [], "included": False},
        },
    }
    if pg_dir is not None:
        _, destination = _pg_layout(pg_dir)
        tools = []
        for tool in common.pg_tool_names(platform_name):
            candidate = pg_dir / "bin" / tool
            if not candidate.is_file():
                candidate = pg_dir / tool
            if candidate.is_file():
                tools.append({"name": tool, "sha256": common.sha256_file(candidate),
                              "size": candidate.stat().st_size})
        manifest["components"]["postgres"] = {
            "path": destination.as_posix(), "tools": tools, "included": True,
        }
    if models_dir is not None:
        models_source, models_destination = _models_layout(models_dir)
        manifest["components"]["models"] = {
            "path": models_destination.as_posix(),
            "files": common.file_entries(models_source),
            "included": True,
        }
    return manifest


def stage(source_root: Path, staging_dir: Path, *, pg_dir: Path | None = None,
          models_dir: Path | None = None, manifest: dict | None = None) -> list[Path]:
    """Copy the payload into ``staging_dir``; returns the paths created."""
    created: list[Path] = []
    staging_dir.mkdir(parents=True, exist_ok=True)

    dist_target = staging_dir / "frontend" / "dist"
    shutil.copytree(source_root / "frontend" / "dist", dist_target, dirs_exist_ok=True)
    created.append(dist_target)

    backend_target = staging_dir / "backend"
    backend_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / "backend" / "alembic.ini", backend_target / "alembic.ini")
    shutil.copytree(source_root / "backend" / "migrations", backend_target / "migrations",
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    created.append(backend_target)

    plugins_target = staging_dir / "plugins" / "plainfolio"
    shutil.copytree(source_root / "plugins" / "plainfolio", plugins_target,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    created.append(plugins_target)

    if pg_dir is not None:
        source, relative = _pg_layout(pg_dir)
        target = staging_dir / relative
        shutil.copytree(source, target, dirs_exist_ok=True)
        created.append(target)
    if models_dir is not None:
        models_source, models_destination = _models_layout(models_dir)
        target = staging_dir / models_destination
        shutil.copytree(models_source, target, dirs_exist_ok=True)
        created.append(target)

    if manifest is not None:
        path = staging_dir / common.MANIFEST_NAME
        path.write_text(common.dump_json(manifest), encoding="utf-8")
        created.append(path)
    return created


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="组装并校验桌面安装包的运行期载荷（便携 PostgreSQL 18 + OCR 模型 + 前端）")
    parser.add_argument("--source-root", default=str(common.REPO_ROOT),
                        help="仓库根目录（默认：本脚本所在仓库）")
    parser.add_argument("--staging-dir", default=str(common.REPO_ROOT / "packaging" / "staging"),
                        help="载荷输出目录（默认 packaging/staging）")
    parser.add_argument("--platform", default=common.platform_slug(),
                        choices=("macos", "windows", "linux"),
                        help="目标平台（决定 .exe 后缀等，默认：当前平台）")
    parser.add_argument("--version", default=None, help="应用版本（默认读取 backend/pyproject.toml）")
    parser.add_argument("--pg-dir", default=os.environ.get("LWB_PG_SOURCE_DIR"),
                        help="便携 PostgreSQL 18 目录（含 bin/，或直接是 bin/）")
    parser.add_argument("--models-dir", default=os.environ.get("LWB_OCR_SOURCE_DIR"),
                        help="OCR 模型目录（含 manifest.json 与 .onnx）")
    parser.add_argument("--allow-missing-runtime", action="store_true",
                        help="只校验代码与前端框架，允许缺少便携 PG18 / OCR 二进制")
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印清单，不写任何文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    source_root = Path(args.source_root).expanduser().resolve()
    staging_dir = Path(args.staging_dir).expanduser().resolve()
    pg_dir = Path(args.pg_dir).expanduser().resolve() if args.pg_dir else None
    models_dir = Path(args.models_dir).expanduser().resolve() if args.models_dir else None
    version = args.version or common.read_version(source_root)
    platform_name = "win32" if args.platform == "windows" else (
        "darwin" if args.platform == "macos" else "linux")

    problems = verify(source_root, platform_name=platform_name, pg_dir=pg_dir,
                      models_dir=models_dir,
                      allow_missing_runtime=args.allow_missing_runtime)
    manifest = build_manifest(source_root, version=version, platform_name=platform_name,
                              pg_dir=pg_dir, models_dir=models_dir,
                              allow_missing_runtime=args.allow_missing_runtime)
    manifest["staging_dir"] = str(staging_dir)
    manifest["dry_run"] = bool(args.dry_run)
    if problems:
        manifest["problems"] = problems
        print(common.dump_json(manifest))
        for problem in problems:
            print(f"stage_bundle: {problem}", file=sys.stderr)
        return EXIT_INVALID
    if args.dry_run:
        print(common.dump_json(manifest))
        return EXIT_OK
    try:
        created = stage(source_root, staging_dir, pg_dir=pg_dir, models_dir=models_dir,
                        manifest=manifest)
    except OSError as exc:
        print(f"stage_bundle: 复制失败：{exc}", file=sys.stderr)
        return EXIT_COPY_FAILED
    manifest["staged_paths"] = [str(path) for path in created]
    print(common.dump_json(manifest))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
