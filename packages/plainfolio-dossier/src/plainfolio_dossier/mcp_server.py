"""FastMCP stdio 服务：4 个工具覆盖"确认围栏 → 生成草案 → 预检 → 装卷"。

硬约束：**stdout 只属于 MCP 协议帧**。任何日志、告警、进度都必须写 stderr——
一次误打到 stdout 的 print 就会让客户端解析失败，表现为"服务启动后立刻断开"，
这是最难现场定位的一类故障。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

try:  # mcp 1.x（工作台与本包锁定的协议实现）
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - 只在依赖漂移时触发
    # mcp 2.x 把 FastMCP 改名为 MCPServer 并改了注册语义。若不在这里说清楚，
    # 客户端只会看到"服务启动后立刻断开"，而真正的原因藏在子进程 stderr 里。
    sys.stderr.write(
        "plainfolio-dossier-mcp: 需要 MCP Python SDK 1.x（mcp.server.fastmcp）。\n"
        "当前环境装的是 mcp 2.x：请执行 `pip install 'mcp>=1.30.0,<2'` 后重试。\n"
    )
    raise SystemExit(2) from exc

from . import path_fence
from .builder import build_dossier
from .errors import DossierError
from .manifest import (
    atomic_write_bytes,
    init_manifest,
    load_manifest,
    validate_manifest,
)

SERVER_NAME = "plainfolio-dossier"

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


@mcp.tool()
def dossier_policy() -> dict[str, Any]:
    """报告当前文件访问围栏：允许的根目录、来源（环境变量/当前目录）与红线。"""
    raw = os.environ.get(path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV) or ""
    return {
        "allowed_roots": [str(root) for root in _roots()],
        "source": "env" if raw.strip() else "cwd",
        "env_var": path_fence.PLAINFOLIO_ALLOWED_ROOTS_ENV,
        "path_separator": os.pathsep,
        "cwd": str(Path.cwd()),
        "rules": [
            "只允许访问上述根目录及其子目录；其他路径一律拒绝",
            "拒绝任何经由符号链接或 NTFS Junction 的访问",
            "原件只读：输出 ZIP 不得与任何输入材料同名同路径",
        ],
    }


@mcp.tool()
def dossier_init_manifest(
    source_dir: str,
    output_path: str | None = None,
    case_title: str = "未命名案件",
    case_number: str | None = None,
    plaintiff: str | None = None,
    defendant: str | None = None,
) -> dict[str, Any]:
    """扫描一个材料目录，生成待律师人工确认的清单草案（JSON）。

    草案只是起点：必须由承办人核对分组、证据名称与证明目的后，才进入装卷。
    """
    draft = init_manifest(
        source_dir,
        output_path,
        case_title=case_title,
        case_number=case_number,
        plaintiff=plaintiff,
        defendant=defendant,
        roots=_roots(),
    )
    item_count = sum(len(group["items"]) for group in draft["groups"])
    binding_count = sum(len(item["bindings"]) for group in draft["groups"] for item in group["items"])
    return {
        "output_path": output_path,
        "group_count": len(draft["groups"]),
        "item_count": item_count,
        "binding_count": binding_count,
        "next_step": "请承办人逐条确认为证据后，再调用 dossier_validate 与 dossier_build",
        "spec": draft,
    }


@mcp.tool()
def dossier_validate(manifest_path: str) -> dict[str, Any]:
    """只读预检清单：文件缺失/越权、页码倒挂或越界、空分组与未绑定条目。"""
    return validate_manifest(manifest_path, roots=_roots())


@mcp.tool()
def dossier_build(manifest_path: str, output_path: str) -> dict[str, Any]:
    """按清单装卷并写出 ZIP（原子写入，绝不覆盖输入材料）。"""
    roots = _roots()
    spec, manifest_file, binding_files = load_manifest(manifest_path, roots=roots)
    output = path_fence.resolve_writable_output(
        output_path,
        roots,
        forbidden_inputs=[manifest_file, *binding_files],
    )
    result = build_dossier(spec, allowed_roots=roots)
    atomic_write_bytes(output, result.zip_bytes)
    return {
        "output_path": str(output),
        "filename": result.filename,
        "item_count": result.item_count,
        "total_body_pages": result.total_body_pages,
        "total_pages": result.total_pages,
        "warnings": result.warnings,
        "size_bytes": len(result.zip_bytes),
    }


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
    except DossierError as exc:  # pragma: no cover - 启动期围栏错误
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
