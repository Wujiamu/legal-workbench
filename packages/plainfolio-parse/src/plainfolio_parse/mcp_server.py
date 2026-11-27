"""FastMCP stdio 服务：6 个工具覆盖"确认围栏 → 解析单份/整目录 → 扫金额 → 扫暗线 → 看表头"。

硬约束：**stdout 只属于 MCP 协议帧**。任何日志、告警、进度都必须写 stderr——一次
误打到 stdout 的 print 就会让客户端解析失败，表现为"服务启动后立刻断开"。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

try:  # mcp 1.x（本包锁定 <2 的协议实现）
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - 只在依赖漂移时触发
    # mcp 2.x 把 FastMCP 改名为 MCPServer 并改了注册语义。若不在这里说清楚，
    # 客户端只会看到"服务启动后立刻断开"，而真正的原因藏在子进程 stderr 里。
    sys.stderr.write(
        "plainfolio-parse-mcp: 需要 MCP Python SDK 1.x（mcp.server.fastmcp）。\n"
        "当前环境装的是 mcp 2.x：请执行 `pip install 'mcp>=1.30.0,<2'` 后重试。\n"
    )
    raise SystemExit(2) from exc

from . import path_fence
from .config import PLAINFOLIO_OCR_MODEL_DIR_ENV, ParseConfig, default_parse_config
from .errors import ParseError
from .exact_values import extract_exact_values, group_amounts, group_dates
from .formats.dispatch import SUPPORTED_EXTENSIONS, is_supported
from .formats.dispatch import parse_file as _parse_file
from .formats.ocr import ocr_availability
from .identifiers import scan_identifier_bridges as _scan_bridges
from .markdown_export import render_parse_result_markdown
from .tables import preview_table as _preview_table

SERVER_NAME = "plainfolio-parse"

mcp = FastMCP(SERVER_NAME)


def _configure_streams() -> None:
    """Force UTF-8 stderr (Windows consoles default to GBK/cp1252)."""
    for stream in (sys.stderr,):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a decorative stream must never abort startup
            pass
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _roots() -> tuple[Path, ...]:
    return path_fence.allowed_roots()


def _config(ocr_model_dir: str | None, enable_ocr: bool) -> ParseConfig:
    config = default_parse_config()
    if ocr_model_dir:
        config.ocr_model_dir = Path(ocr_model_dir).expanduser()
    if not enable_ocr:
        config.enable_ocr = False
    return config


@mcp.tool()
def parse_policy() -> dict[str, Any]:
    """报告当前文件访问围栏（允许的根目录与来源）与 OCR 可用性（只读）。"""
    raw = os.environ.get(path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV) or ""
    return {
        "allowed_roots": [str(root) for root in _roots()],
        "source": "env" if raw.strip() else "cwd",
        "env_var": path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV,
        "path_separator": os.pathsep,
        "cwd": str(Path.cwd()),
        "ocr_model_dir_env": PLAINFOLIO_OCR_MODEL_DIR_ENV,
        "ocr": ocr_availability(default_parse_config()),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "rules": [
            "只允许读取上述根目录及其子目录；其他路径一律拒绝",
            "拒绝任何经由符号链接或 NTFS Junction 的访问",
            "原件只读：输出路径不得与任何输入文件同名同路径",
            "缺失 OCR 依赖或模型时，纯文本与原生文字层 PDF 照常解析，需要 OCR 的页面标为 failed",
        ],
    }


@mcp.tool()
def parse_file(path: str, output_path: str | None = None,
               ocr_model_dir: str | None = None, enable_ocr: bool = True,
               force_ocr_pages: list[int] | None = None) -> dict[str, Any]:
    """解析单个本地文件，返回逐单元结构（页码/工作表/段落定位 + 文本）。

    指定 ``output_path`` 时额外写出 Obsidian 友好的 Markdown；输出路径不得覆盖输入文件。
    """
    roots = _roots()
    resolved = path_fence.resolve_readable_file(path, roots)
    config = _config(ocr_model_dir, enable_ocr)
    result = _parse_file(resolved, config, set(force_ocr_pages or []) or None)
    payload: dict[str, Any] = {
        "source": str(resolved),
        "status": result.status,
        "parser_version": result.parser_version,
        "unit_count": len(result.units),
        "warnings": list(result.warnings),
        "ocr": ocr_availability(config),
        "units": [
            {"unit_key": unit.unit_key, "locator": unit.locator, "method": unit.method,
             "status": unit.status, "error": unit.error,
             "warnings": list(unit.warnings), "text": unit.text}
            for unit in result.units
        ],
    }
    if output_path:
        hits = []
        for unit in result.units:
            if unit.text:
                hits.extend(extract_exact_values(unit.text))
        markdown = render_parse_result_markdown(result, source_name=resolved.name,
                                               exact_values=hits)
        target = path_fence.resolve_writable_output(output_path, roots,
                                                    forbidden_inputs=[resolved])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
        payload["markdown_path"] = str(target)
    return payload


@mcp.tool()
def parse_folder(directory: str, recursive: bool = True,
                 ocr_model_dir: str | None = None, enable_ocr: bool = True) -> dict[str, Any]:
    """解析一个本地目录（跳过隐藏文件与隐藏目录），逐份返回状态与单元。"""
    roots = _roots()
    root = path_fence.resolve_readable_dir(directory, roots)
    config = _config(ocr_model_dir, enable_ocr)
    files: list[Path] = []
    walker = root.rglob("*") if recursive else root.glob("*")
    for path in sorted(walker):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not path.is_file() or not is_supported(path):
            continue
        files.append(path)
    entries: list[dict[str, Any]] = []
    for path in files:
        try:
            result = _parse_file(path, config)
        except ParseError as exc:
            entries.append({"source": str(path), "status": "failed",
                            "error": {"code": exc.code, "message": exc.message,
                                      "details": exc.details}})
            continue
        entries.append({
            "source": str(path),
            "status": result.status,
            "parser_version": result.parser_version,
            "unit_count": len(result.units),
            "warnings": list(result.warnings),
            "units": [{"unit_key": unit.unit_key, "locator": unit.locator,
                       "method": unit.method, "status": unit.status,
                       "error": unit.error, "text": unit.text}
                      for unit in result.units],
        })
    return {"directory": str(root), "file_count": len(entries),
            "ocr": ocr_availability(config), "files": entries}


@mcp.tool()
def scan_exact_values(paths: list[str], ocr_model_dir: str | None = None,
                      enable_ocr: bool = True) -> dict[str, Any]:
    """扫描精确金额（按币种分别汇总，绝不跨币种相加）与日期。"""
    roots = _roots()
    config = _config(ocr_model_dir, enable_ocr)
    files = _resolve_many(paths, roots)
    hits = []
    all_hits = []
    for path in files:
        result = _parse_file(path, config)
        for unit in result.units:
            if not unit.text:
                continue
            for hit in extract_exact_values(unit.text):
                all_hits.append(hit)
                hits.append({"source": str(path), "unit_key": unit.unit_key,
                             **hit.to_dict()})
    return {"file_count": len(files), "value_count": len(hits),
            "amount_totals": group_amounts(all_hits), "date_counts": group_dates(all_hits),
            "values": hits}


@mcp.tool()
def scan_identifier_bridges(paths: list[str], ocr_model_dir: str | None = None,
                            enable_ocr: bool = True) -> dict[str, Any]:
    """跨文件强指纹碰撞排查：手机号 / USCC / 银行账号 / 邮箱出现在两份以上文件的暗线。"""
    roots = _roots()
    config = _config(ocr_model_dir, enable_ocr)
    files = _resolve_many(paths, roots)
    return _scan_bridges(files, config=config)


@mcp.tool()
def preview_table(path: str, sheet_name: str | None = None, header_row: int = 1,
                  sample_rows: int = 20) -> dict[str, Any]:
    """表格列名与前 N 行预览（.xlsx / .csv）；非标对账单映射前先看清表头。"""
    roots = _roots()
    resolved = path_fence.resolve_readable_file(path, roots)
    return _preview_table(resolved, sheet_name=sheet_name, header_row=header_row,
                          sample_rows=sample_rows)


def _resolve_many(raw_paths: list[str], roots: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            directory = path_fence.resolve_readable_dir(raw, roots)
            walker = directory.rglob("*")
            for path in sorted(walker):
                if any(part.startswith(".") for part in path.relative_to(directory).parts):
                    continue
                if path.is_symlink() or not path.is_file() or not is_supported(path):
                    continue
                files.append(path)
        else:
            files.append(path_fence.resolve_readable_file(raw, roots))
    if not files:
        raise ParseError("not_found", "没有可扫描的受支持文件", {"paths": raw_paths})
    return list(dict.fromkeys(files))


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] not in ("--stdio",):
        sys.stderr.write(
            f"{SERVER_NAME}: 不支持的参数 {arguments}；本服务只提供 stdio 传输\n"
        )
        return 2
    try:
        mcp.run()
    except ParseError as exc:  # pragma: no cover - 启动期围栏错误
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
