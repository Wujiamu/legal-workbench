"""把解析结果渲染成 Obsidian / 外部 Agent 友好的 Markdown。

约定：

* 文首 YAML frontmatter 固定字段：``source`` / ``status`` / ``parser_version`` /
  ``unit_count`` / ``warnings``——外部 Agent 只看这 5 个字段就能判断能不能用；
* 逐单元一个二级标题锚点，页码 / 工作表 / 段落位置必须留在标题里，便于从
  ``[[文件#第 3 页]]`` 直接跳转；
* 每个单元前保留 ``<!-- unit: … -->`` 注释锚，标题改写不会破坏反向链接。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from .formats.base import ParseResult, ParsedUnit


def _yaml_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _unit_title(unit: ParsedUnit) -> str:
    locator = unit.locator or {}
    kind = locator.get("type")
    if kind == "pdf":
        page = locator.get("page")
        title = f"第 {page} 页" if isinstance(page, int) else "PDF 单元"
    elif kind == "xlsx":
        sheet = locator.get("sheet") or "未知工作表"
        row = locator.get("row")
        title = f"工作表：{sheet}" + (f" · 第 {row} 行" if isinstance(row, int) else "")
    elif kind == "docx":
        if "table_path" in locator:
            path = ".".join(str(part) for part in locator.get("table_path") or [])
            title = (f"表格 {path or '1'} · {locator.get('cell', '')}".strip()
                     if locator.get("cell") else f"表格 {path or '1'}")
        else:
            paragraph = locator.get("paragraph")
            title = (f"段落 {int(paragraph) + 1}" if isinstance(paragraph, int) else "段落单元")
    elif kind == "text":
        start, end = locator.get("line_start"), locator.get("line_end")
        block = locator.get("block")
        if isinstance(start, int) and isinstance(end, int):
            title = f"段落单元 {int(block) + 1 if isinstance(block, int) else 1}（第 {start}–{end} 行）"
        else:
            title = "段落单元"
    elif kind == "image":
        frame = locator.get("frame")
        title = f"图片第 {int(frame) + 1} 帧" if isinstance(frame, int) else "图片单元"
    else:
        title = f"单元 {unit.unit_key}"
    return title


def _unit_meta(unit: ParsedUnit) -> str:
    bits = [f"method={unit.method}", f"status={unit.status}"]
    if unit.error:
        bits.append(f"error={unit.error}")
    if unit.warnings:
        bits.append("warnings=" + "; ".join(unit.warnings))
    return " · ".join(bits)


def _exact_value_table(hits: Sequence[Any]) -> list[str]:
    rows = []
    for hit in hits:
        kind = getattr(hit, "kind", "")
        label = {"amount": "金额", "date": "日期", "identifier": "编号"}.get(kind, kind)
        value = getattr(hit, "raw_text", "")
        currency = getattr(hit, "currency", None)
        canonical = getattr(hit, "canonical", {}) or {}
        if kind == "date":
            value = str(canonical.get("date", value))
        elif kind == "amount":
            value = f"{canonical.get('value', value)} {currency or ''}".strip()
        rows.append((label, value))
    if not rows:
        return []
    lines = ["## 精确值", "", "| 类型 | 值 |", "|---|---|"]
    lines.extend(f"| {label} | {value} |" for label, value in rows)
    lines.append("")
    return lines


def render_parse_result_markdown(
    result: ParseResult, *, source_name: str,
    exact_values: Iterable[Any] | None = None,
) -> str:
    """Render one parse result as Markdown with a stable YAML frontmatter."""
    lines: list[str] = ["---"]
    lines.append(f"source: {_yaml_value(source_name)}")
    lines.append(f"status: {_yaml_value(result.status)}")
    lines.append(f"parser_version: {_yaml_value(result.parser_version)}")
    lines.append(f"unit_count: {_yaml_value(len(result.units))}")
    lines.append(f"warnings: {_yaml_value(list(result.warnings))}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {source_name}")
    lines.append("")
    if result.warnings:
        lines.append("> 解析告警：" + "；".join(result.warnings))
        lines.append("")

    if exact_values is not None:
        lines.extend(_exact_value_table(list(exact_values)))

    for unit in result.units:
        lines.append(f"<!-- unit: {unit.unit_key} -->")
        lines.append(f"## {_unit_title(unit)}")
        lines.append("")
        lines.append(f"*{_unit_meta(unit)}*")
        lines.append("")
        text = (unit.text or "").strip()
        lines.append(text if text else "_（无文本）_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
