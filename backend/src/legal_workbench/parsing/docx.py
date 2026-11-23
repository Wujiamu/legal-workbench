"""DOCX parsing in document order, including nested tables and coverage warnings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from ..errors import ErrorCode, WorkbenchError
from .base import ParseResult, ParsedUnit, make_result

PARSER = "docx-1.0"


def parse_docx(path: Path) -> ParseResult:
    try:
        document = Document(str(path))
    except Exception as exc:  # python-docx raises several unrelated types for bad ZIPs
        raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "DOCX 无法打开（可能损坏或加密）",
                             {"error": type(exc).__name__}) from exc
    units: list[ParsedUnit] = []
    warnings: list[str] = []
    paragraph_index = 0
    table_index = 0
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            if paragraph.text.strip():
                units.append(ParsedUnit(
                    unit_key=f"docx:p:{paragraph_index}",
                    locator={"type": "docx", "paragraph": paragraph_index},
                    text=paragraph.text,
                ))
            paragraph_index += 1
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            _emit_table(table, [table_index], units, set())
            table_index += 1
    if document.inline_shapes:
        warnings.append("嵌入图片未识别（本格式不解析图像）")
    if _has_header_footer_text(document):
        warnings.append("页眉/页脚文本未纳入提取")
    if document.element.body.findall(".//" + qn("w:txbxContent")):
        warnings.append("文本框内容未纳入提取")
    if not units:
        units = [ParsedUnit(unit_key="docx:0", locator={"type": "docx"},
                            text="", status="no_text_detected")]
    result = make_result(units, PARSER, {"parts": ["body", "tables"]}, warnings)
    if warnings and result.status == "complete":
        result.status = "partial"
    return result


def _emit_table(table: Table, path: list[int], units: list[ParsedUnit],
                seen: set[int]) -> None:
    for row_index, row in enumerate(table.rows):
        for col_index, cell in enumerate(row.cells):
            key = id(cell._tc)
            if key in seen:
                continue
            seen.add(key)
            text = "\n".join(p.text for p in cell.paragraphs).strip()
            if text:
                units.append(ParsedUnit(
                    unit_key="docx:t:" + ".".join(str(p) for p in path) + f":{row_index}:{col_index}",
                    locator={"type": "docx", "table_path": path, "row": row_index,
                             "col": col_index, "cell": f"R{row_index}C{col_index}"},
                    text=text,
                ))
            for nested_index, nested in enumerate(cell.tables):
                _emit_table(nested, [*path, row_index, col_index, nested_index], units, seen)


def _has_header_footer_text(document) -> bool:
    for section in document.sections:
        for part in (section.header, section.footer):
            if part is None:
                continue
            if any(p.text.strip() for p in part.paragraphs):
                return True
    return False
