"""XLSX 解析：值 + 公式缓存、工作表名、A1 地址与隐藏状态，逐行成单元。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..config import ParseConfig
from ..errors import ParseError
from .base import ParseResult, ParsedUnit, make_result

PARSER = "xlsx-1.0"


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def parse_xlsx(path: Path, config: ParseConfig | None = None) -> ParseResult:
    try:
        formulas = load_workbook(str(path), data_only=False, read_only=False)
        values = load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:
        raise ParseError("unsupported_format", "XLSX 无法打开（可能损坏或加密）",
                         {"error": type(exc).__name__}) from exc
    units: list[ParsedUnit] = []
    warnings: list[str] = []
    try:
        for sheet_index, sheet in enumerate(formulas.worksheets):
            value_sheet = values.worksheets[sheet_index]
            hidden = sheet.sheet_state in ("hidden", "veryHidden")
            if getattr(sheet, "_images", None):
                warnings.append(f"工作表 {sheet.title} 含图片未识别")
            if getattr(sheet, "_charts", None):
                warnings.append(f"工作表 {sheet.title} 含图表未识别")
            merged = [str(rng) for rng in sheet.merged_cells.ranges]
            for row_index, row in enumerate(sheet.iter_rows(), start=1):
                value_row = list(value_sheet.iter_rows(
                    min_row=row_index, max_row=row_index, max_col=len(row)
                ))[0] if row else []
                cells: list[dict[str, Any]] = []
                lines: list[str] = []
                unit_warnings: list[str] = []
                for col_index, cell in enumerate(row):
                    formula = cell.value
                    cached_cell = value_row[col_index] if col_index < len(value_row) else None
                    cached = cached_cell.value if cached_cell is not None else None
                    is_formula = isinstance(formula, str) and formula.startswith("=")
                    if is_formula:
                        has_cache = cached is not None
                        if not has_cache:
                            unit_warnings.append(f"{cell.coordinate} 公式缺少缓存值")
                        lines.append(f"{cell.coordinate}: {formula}"
                                     + (f" => {_cell_text(cached)}" if has_cache else " (缓存缺失)"))
                        cells.append({"cell": cell.coordinate, "formula": formula,
                                      "value": _cell_text(cached) if has_cache else None,
                                      "cached": has_cache})
                    elif formula is not None:
                        lines.append(f"{cell.coordinate}: {_cell_text(formula)}")
                        cells.append({"cell": cell.coordinate, "value": _cell_text(formula)})
                if not lines:
                    continue
                units.append(ParsedUnit(
                    unit_key=f"xlsx:{sheet_index}:{row_index}",
                    locator={"type": "xlsx", "sheet": sheet.title, "sheet_index": sheet_index,
                             "row": row_index, "hidden": hidden, "cells": cells,
                             "merged": merged},
                    text="\n".join(lines), warnings=unit_warnings,
                ))
    finally:
        formulas.close()
        values.close()
    if not units:
        units = [ParsedUnit(unit_key="xlsx:0", locator={"type": "xlsx"},
                            text="", status="no_text_detected")]
    result = make_result(units, PARSER, {"read": "values+formula_cache"}, warnings)
    if warnings and result.status == "complete":
        result.status = "partial"
    return result
