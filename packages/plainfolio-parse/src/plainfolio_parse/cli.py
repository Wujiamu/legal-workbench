"""命令行入口：stdout 只放一行 JSON（唯一例外见下），日志与错误全部走 stderr。

stdout 契约：

* ``policy`` / ``parse-file --format json`` / ``parse-folder`` / ``scan-values`` /
  ``scan-bridges`` / ``preview-table`` → 一行 JSON；
* ``parse-file --format md`` **且未指定** ``--output`` → 直接输出 Markdown 正文
  （这是唯一不是 JSON 的情形，供人直接看或重定向进笔记库）；
* ``parse-file --format md --output 文件.md`` → Markdown 写入文件，stdout 仍是一行
  的 JSON 摘要（``output_path`` / ``status`` / ``unit_count`` / ``warnings``）。

退出码：``0`` 成功、``2`` 业务错误（:class:`ParseError`）、``1`` 未预期错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import path_fence
from .config import (
    PLAINFOLIO_OCR_MODEL_DIR_ENV,
    WORKBENCH_OCR_MODEL_DIR_ENV,
    ParseConfig,
    default_parse_config,
)
from .errors import ParseError
from .exact_values import ExactValueHit, extract_exact_values, group_amounts, group_dates
from .formats.base import ParseResult
from .formats.dispatch import SUPPORTED_EXTENSIONS, is_supported, parse_file
from .formats.ocr import ocr_availability
from .identifiers import scan_identifier_bridges
from .markdown_export import render_parse_result_markdown
from .tables import preview_table

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BUSINESS = 2

PARSER_VERSION = "plainfolio-parse/0.1.0"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _fail(message: str) -> None:
    sys.stderr.write(f"plainfolio-parse: {message}\n")
    sys.stderr.flush()


def _write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 ``os.replace``，不留半份产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _config_from_args(args: argparse.Namespace) -> ParseConfig:
    config = default_parse_config()
    model_dir = getattr(args, "ocr_model_dir", None)
    if model_dir:
        config.ocr_model_dir = Path(model_dir).expanduser()
    if getattr(args, "no_ocr", False):
        config.enable_ocr = False
    return config


def _policy_payload() -> dict:
    raw_roots = os.environ.get(path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV) or ""
    roots = path_fence.allowed_roots()
    config = default_parse_config()
    return {
        "allowed_roots": [str(root) for root in roots],
        "source": "env" if raw_roots.strip() else "cwd",
        "env_var": path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV,
        "path_separator": os.pathsep,
        "cwd": str(Path.cwd()),
        "ocr": ocr_availability(config),
        "ocr_model_dir_env": [PLAINFOLIO_OCR_MODEL_DIR_ENV, WORKBENCH_OCR_MODEL_DIR_ENV],
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "parser_version": PARSER_VERSION,
        "notes": (
            "默认只允许读取当前工作目录及其子目录；需要处理其他目录时，把目录写进 "
            f"{path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV}（{os.pathsep} 分隔）。"
            "原件只读：输出路径不得与任何输入文件相同。"
            "OCR 需要额外安装 plainfolio-parse[ocr] 并配置 "
            f"{PLAINFOLIO_OCR_MODEL_DIR_ENV}；缺失时纯文本/原生文字层 PDF 照常解析，"
            "需要 OCR 的页面或图片标为 failed 并写明 ocr_unavailable 原因。"
        ),
    }


def _cmd_policy(_args: argparse.Namespace) -> int:
    _emit(_policy_payload())
    return EXIT_OK


def _unit_payload(result: ParseResult) -> list[dict]:
    return [
        {
            "unit_key": unit.unit_key,
            "locator": unit.locator,
            "method": unit.method,
            "status": unit.status,
            "error": unit.error,
            "warnings": list(unit.warnings),
            "text": unit.text,
        }
        for unit in result.units
    ]


def _result_payload(path: Path, result: ParseResult, config: ParseConfig) -> dict:
    return {
        "source": str(path),
        "status": result.status,
        "parser_version": result.parser_version,
        "unit_count": len(result.units),
        "warnings": list(result.warnings),
        "config": result.config,
        "ocr": ocr_availability(config),
        "units": _unit_payload(result),
    }


def _cmd_parse_file(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    roots = path_fence.allowed_roots()
    path = path_fence.resolve_readable_file(args.path, roots)
    force_pages = {int(item) for item in (args.force_ocr_pages or [])}
    result = parse_file(path, config, force_pages or None)

    if args.format == "md":
        markdown = render_parse_result_markdown(
            result, source_name=path.name, exact_values=_all_hits(result))
        if args.output is None:
            sys.stdout.write(markdown)
            sys.stdout.flush()
            return EXIT_OK
        output = path_fence.resolve_writable_output(args.output, roots,
                                                   forbidden_inputs=[path])
        _write_text(output, markdown)
        _emit({"output_path": str(output), "format": "md", "status": result.status,
               "unit_count": len(result.units), "warnings": list(result.warnings)})
        return EXIT_OK

    payload = _result_payload(path, result, config)
    if args.output is None:
        _emit(payload)
        return EXIT_OK
    output = path_fence.resolve_writable_output(args.output, roots, forbidden_inputs=[path])
    _write_text(output, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    _emit({"output_path": str(output), "format": "json", "status": result.status,
           "unit_count": len(result.units), "warnings": list(result.warnings)})
    return EXIT_OK


def _all_hits(result: ParseResult) -> list[ExactValueHit]:
    hits: list[ExactValueHit] = []
    for unit in result.units:
        if unit.text:
            hits.extend(extract_exact_values(unit.text))
    return hits


def _scan_directory(root: Path, *, recursive: bool) -> list[Path]:
    """受支持的文件，按路径排序；隐藏文件与隐藏目录一律跳过。"""
    files: list[Path] = []
    walker = root.rglob("*") if recursive else root.glob("*")
    for path in sorted(walker):
        relative_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if is_supported(path):
            files.append(path)
    return files


def _safe_stem(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    stemmed = relative.with_suffix("")
    parts = [part for part in stemmed.parts if part not in ("", ".")]
    return "__".join(parts) or stemmed.name or path.stem


def _cmd_parse_folder(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    roots = path_fence.allowed_roots()
    directory = path_fence.resolve_readable_dir(args.dir, roots)
    files = _scan_directory(directory, recursive=not args.no_recursive)
    if not files:
        raise ParseError("not_found", "目录中没有受支持的受解析文件",
                         {"directory": str(directory),
                          "supported_extensions": sorted(SUPPORTED_EXTENSIONS)})

    if args.format == "md" and args.output_dir is None:
        raise ParseError(
            "invalid_input",
            "parse-folder --format md 必须指定 --output-dir（逐份写 Markdown，"
            "stdout 保持一行 JSON 摘要）",
        )

    entries: list[dict] = []
    for path in files:
        try:
            result = parse_file(path, config)
        except ParseError as exc:
            entries.append({"source": str(path), "status": "failed",
                            "error": {"code": exc.code, "message": exc.message,
                                      "details": exc.details}})
            continue
        entries.append(_result_payload(path, result, config))

    summary = {
        "directory": str(directory),
        "file_count": len(entries),
        "complete": sum(1 for item in entries if item.get("status") == "complete"),
        "partial": sum(1 for item in entries if item.get("status") == "partial"),
        "failed": sum(1 for item in entries if item.get("status") == "failed"),
        "ocr": ocr_availability(config),
        "files": entries,
    }

    if args.format == "md":
        outputs: list[dict] = []
        for path, entry in zip([item for item in files], entries):
            if entry.get("status") == "failed" and "units" not in entry:
                outputs.append({"source": str(path), "written": None, "status": "failed",
                                "error": entry.get("error")})
                continue
            result = ParseResult(
                units=[_unit_from_payload(unit) for unit in entry["units"]],
                parser_version=entry["parser_version"],
                config=entry["config"],
                status=entry["status"],
                warnings=entry["warnings"],
            )
            markdown = render_parse_result_markdown(
                result, source_name=path.name,
                exact_values=_all_hits(result))
            target = Path(args.output_dir) / f"{_safe_stem(directory, path)}.md"
            output = path_fence.resolve_writable_output(target, roots,
                                                        forbidden_inputs=files)
            _write_text(output, markdown)
            outputs.append({"source": str(path), "written": str(output),
                            "status": result.status, "unit_count": len(result.units)})
        _emit({"directory": str(directory), "format": "md",
               "file_count": len(outputs), "outputs": outputs})
        return EXIT_OK

    _emit(summary)
    return EXIT_OK


def _unit_from_payload(payload: dict):
    from .formats.base import ParsedUnit

    return ParsedUnit(
        unit_key=payload["unit_key"], locator=payload["locator"], text=payload["text"],
        method=payload["method"], status=payload["status"],
        warnings=list(payload.get("warnings") or []), error=payload.get("error"),
    )


def _expand_inputs(raw_paths: list[str], roots: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw).expanduser()
        resolved = (path_fence.resolve_readable_dir(raw, roots)
                    if candidate.is_dir() else path_fence.resolve_readable_file(raw, roots))
        if resolved.is_dir():
            files.extend(_scan_directory(resolved, recursive=True))
        else:
            files.append(resolved)
    return list(dict.fromkeys(files))


def _cmd_scan_values(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    roots = path_fence.allowed_roots()
    files = _expand_inputs(args.path, roots)
    if not files:
        raise ParseError("not_found", "没有可扫描的受支持文件", {"paths": args.path})

    hits: list[dict] = []
    all_hits: list[ExactValueHit] = []
    file_rows: list[dict] = []
    for path in files:
        try:
            result = parse_file(path, config)
        except ParseError as exc:
            file_rows.append({"source": str(path), "status": "failed",
                              "error": {"code": exc.code, "message": exc.message}})
            continue
        file_hits = 0
        for unit in result.units:
            if not unit.text:
                continue
            for hit in extract_exact_values(unit.text):
                file_hits += 1
                all_hits.append(hit)
                hits.append({"source": str(path), "unit_key": unit.unit_key,
                             **hit.to_dict()})
        file_rows.append({"source": str(path), "status": result.status,
                          "unit_count": len(result.units), "value_count": file_hits})

    _emit({
        "file_count": len(files),
        "files": file_rows,
        "amount_totals": group_amounts(all_hits),
        "date_counts": group_dates(all_hits),
        "value_count": len(hits),
        "values": hits,
    })
    return EXIT_OK


def _cmd_scan_bridges(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    roots = path_fence.allowed_roots()
    files = _expand_inputs(args.path, roots)
    if not files:
        raise ParseError("not_found", "没有可扫描的受支持文件", {"paths": args.path})
    _emit(scan_identifier_bridges(files, config=config))
    return EXIT_OK


def _cmd_preview_table(args: argparse.Namespace) -> int:
    roots = path_fence.allowed_roots()
    path = path_fence.resolve_readable_file(args.path, roots)
    _emit(preview_table(path, sheet_name=args.sheet, header_row=args.header_row,
                        sample_rows=args.sample_rows))
    return EXIT_OK


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ocr-model-dir", default=None,
                        help=f"OCR 模型根目录（其下 rapidocr/ 含 manifest.json 与 onnx）")
    parser.add_argument("--no-ocr", action="store_true", help="本次解析完全跳过 OCR")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plainfolio-parse",
        description="素章（PlainFolio）零数据库离线阅卷解析：本地文件 → 结构化 JSON / Obsidian Markdown。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    policy = sub.add_parser("policy", help="打印当前文件访问围栏与 OCR 可用性（只读）")
    policy.set_defaults(handler=_cmd_policy)

    parse_one = sub.add_parser("parse-file", help="解析单个文件（stdout 一行 JSON；md 见说明）")
    parse_one.add_argument("--path", required=True)
    parse_one.add_argument("--format", choices=("json", "md"), default="json")
    parse_one.add_argument("--output", default=None,
                           help="写入文件；md 未指定时直接输出 Markdown 正文")
    parse_one.add_argument("--force-ocr-pages", nargs="*", type=int, default=None,
                           help="仅 PDF：强制这些页走 OCR（1 起）")
    _add_config_args(parse_one)
    parse_one.set_defaults(handler=_cmd_parse_file)

    folder = sub.add_parser("parse-folder", help="批量解析目录（跳过隐藏文件与隐藏目录）")
    folder.add_argument("--dir", required=True)
    folder.add_argument("--format", choices=("json", "md"), default="json")
    folder.add_argument("--output-dir", default=None,
                        help="md 输出目录（必须；逐份写 <相对路径>.md，绝不覆盖输入文件）")
    folder.add_argument("--no-recursive", action="store_true", help="只扫描顶层，不递归")
    _add_config_args(folder)
    folder.set_defaults(handler=_cmd_parse_folder)

    values = sub.add_parser("scan-values", help="扫描精确金额（分币种）与日期")
    values.add_argument("--path", nargs="+", required=True, help="文件或目录，可多个")
    _add_config_args(values)
    values.set_defaults(handler=_cmd_scan_values)

    bridges = sub.add_parser("scan-bridges", help="跨文件强指纹碰撞排查（手机号/USCC/银行账号/邮箱）")
    bridges.add_argument("--path", nargs="+", required=True, help="文件或目录，可多个")
    _add_config_args(bridges)
    bridges.set_defaults(handler=_cmd_scan_bridges)

    table = sub.add_parser("preview-table", help="表格列名与前 N 行预览（.xlsx / .csv）")
    table.add_argument("--path", required=True)
    table.add_argument("--sheet", default=None)
    table.add_argument("--header-row", type=int, default=1)
    table.add_argument("--sample-rows", type=int, default=20)
    table.set_defaults(handler=_cmd_preview_table)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 自己处理 -h 与参数错误
        return int(exc.code or EXIT_OK)

    try:
        return args.handler(args)
    except ParseError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return EXIT_BUSINESS
    except ValueError as exc:  # 配置值非法（如负数超时）
        sys.stderr.write(json.dumps({"code": "invalid_input", "message": str(exc),
                                     "details": {}}, ensure_ascii=False) + "\n")
        sys.stderr.flush()
        return EXIT_BUSINESS
    except Exception as exc:  # noqa: BLE001 - 兜底必须给非 0 退出码，且不污染 stdout
        _fail(f"未预期错误：{type(exc).__name__}: {exc}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
