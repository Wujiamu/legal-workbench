"""命令行入口：stdout 只有一行 JSON，日志与错误全部走 stderr。

退出码：``0`` 成功、``2`` 业务错误（:class:`DossierError`）、``1`` 未预期错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import path_fence
from .builder import build_dossier
from .errors import DossierError
from .manifest import (
    atomic_write_bytes,
    init_manifest,
    load_manifest,
    validate_manifest,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BUSINESS = 2


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _fail(message: str) -> None:
    sys.stderr.write(f"plainfolio-dossier: {message}\n")
    sys.stderr.flush()


def _roots() -> tuple[Path, ...]:
    return path_fence.allowed_roots()


def _policy_payload() -> dict:
    raw = os.environ.get(path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV) or ""
    roots = _roots()
    return {
        "allowed_roots": [str(root) for root in roots],
        "source": "env" if raw.strip() else "cwd",
        "env_var": path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV,
        "path_separator": os.pathsep,
        "cwd": str(Path.cwd()),
        "notes": (
            "默认只允许当前工作目录及其子目录；需要处理其他目录时，把目录写进 "
            f"{path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV}（{os.pathsep} 分隔）。"
            "原件只读：输出路径不得与任何输入材料相同。"
        ),
    }


def _cmd_policy(_args: argparse.Namespace) -> int:
    _emit(_policy_payload())
    return EXIT_OK


def _cmd_init_manifest(args: argparse.Namespace) -> int:
    roots = _roots()
    draft = init_manifest(
        args.source_dir,
        args.output,
        case_title=args.case_title,
        case_number=args.case_number,
        plaintiff=args.plaintiff,
        defendant=args.defendant,
        roots=roots,
    )
    item_count = sum(len(group["items"]) for group in draft["groups"])
    binding_count = sum(len(item["bindings"]) for group in draft["groups"] for item in group["items"])
    _emit({
        "output_path": str(Path(args.output).expanduser()) if args.output else None,
        "group_count": len(draft["groups"]),
        "item_count": item_count,
        "binding_count": binding_count,
        "next_step": "请承办人逐条确认分组、证据名称与证明目的后再执行 build",
        "spec": draft,
    })
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    result = validate_manifest(args.manifest, roots=_roots())
    _emit(result)
    return EXIT_OK if result["valid"] else EXIT_BUSINESS


def _cmd_build(args: argparse.Namespace) -> int:
    roots = _roots()
    spec, manifest_path, binding_files = load_manifest(args.manifest, roots=roots)
    # 先解析并校验输出路径，再渲染：否则用户要等整卷排完版才被告知"不能覆盖原件"。
    output = path_fence.resolve_writable_output(
        args.output,
        roots,
        forbidden_inputs=[manifest_path, *binding_files],
    )
    result = build_dossier(spec, allowed_roots=roots)
    atomic_write_bytes(output, result.zip_bytes)
    _emit({
        "output_path": str(output),
        "filename": result.filename,
        "item_count": result.item_count,
        "total_body_pages": result.total_body_pages,
        "total_pages": result.total_pages,
        "warnings": result.warnings,
        "size_bytes": len(result.zip_bytes),
    })
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plainfolio-dossier",
        description="素章（PlainFolio）法院自动装卷器：零数据库、零后端，生成证据合订卷宗 ZIP。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    policy = sub.add_parser("policy", help="打印当前生效的文件访问围栏（只读）")
    policy.set_defaults(handler=_cmd_policy)

    init = sub.add_parser("init-manifest", help="扫描材料目录，生成待律师确认的清单草案")
    init.add_argument("--source-dir", required=True, help="材料所在目录（递归扫描，跳过隐藏文件）")
    init.add_argument("--output", default=None, help="草案输出路径（.json）；省略则只打印")
    init.add_argument("--case-title", default="未命名案件")
    init.add_argument("--case-number", default=None)
    init.add_argument("--plaintiff", default=None)
    init.add_argument("--defendant", default=None)
    init.set_defaults(handler=_cmd_init_manifest)

    validate = sub.add_parser("validate", help="只读预检清单（不装卷、不写文件）")
    validate.add_argument("--manifest", required=True, help="清单文件（.json 或 .csv）")
    validate.set_defaults(handler=_cmd_validate)

    build = sub.add_parser("build", help="装卷并写出 ZIP（原子写入，绝不覆盖输入材料）")
    build.add_argument("--manifest", required=True)
    build.add_argument("--output", required=True, help="输出 ZIP 路径")
    build.set_defaults(handler=_cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 自己处理 -h 与参数错误
        return int(exc.code or EXIT_OK)

    try:
        return args.handler(args)
    except DossierError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return EXIT_BUSINESS
    except Exception as exc:  # noqa: BLE001 - 兜底必须给非 0 退出码，且不污染 stdout
        _fail(f"未预期错误：{exc}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
