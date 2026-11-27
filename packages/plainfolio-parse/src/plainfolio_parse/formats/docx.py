"""DOCX 解析：按文档顺序输出段落与表格（含嵌套表格），并报告未覆盖部分。"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..config import ParseConfig
from ..errors import ParseError
from .base import ParseResult, ParsedUnit, make_result

PARSER = "docx-1.0"


def parse_docx(path: Path, config: ParseConfig | None = None) -> ParseResult:
    try:
        document = Document(str(path))
    except Exception as exc:  # python-docx raises several unrelated types for bad ZIPs
        raise ParseError("unsupported_format", "DOCX 无法打开（可能损坏或加密）",
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
            _emit_table(table, [table_index], units, {})
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
                seen: dict[int, object]) -> None:
    for row_index, row in enumerate(table.rows):
        for col_index, cell in enumerate(row.cells):
            # 合并单元格会在 ``row.cells`` 里重复出现同一个 ``w:tc``，只应提取一次。
            # 用 ``id()`` 去重时必须同时持有代理对象：lxml 的 Python 代理是按需创建
            # 且可被回收的，只记 id 会让后面**另一个**单元格复用同一个地址而被误判
            # 为"已提取"——那正是"表格最后一个单元格凭空消失"的成因。
            tc = cell._tc
            key = id(tc)
            if key in seen and seen[key] is tc:
                continue
            seen[key] = tc
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
