"""T08: TXT/DOCX/XLSX parsing, locators and coverage warnings (TEST-ONLY samples)."""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Inches
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from PIL import Image

from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import parse_file
from legal_workbench.parsing.text import parse_text
from legal_workbench.parsing.docx import parse_docx
from legal_workbench.parsing.xlsx import parse_xlsx


def _hash(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_txt_utf8_bom_blocks_and_lines(tmp_path):
    path = tmp_path / "合同.txt"
    path.write_bytes("\ufeff第一段\n第二行\n\n第三段".encode("utf-8"))
    before = _hash(path)
    result = parse_text(path)
    assert result.status == "complete"
    assert [u.text for u in result.units] == ["第一段\n第二行", "第三段"]
    assert result.units[0].locator == {"type": "text", "block": 0, "line_start": 1, "line_end": 2}
    assert result.units[1].locator["line_start"] == 4
    assert _hash(path) == before


def test_txt_gb18030_and_invalid_encoding(tmp_path):
    gb = tmp_path / "gb.txt"
    gb.write_bytes("中文合同测试".encode("gb18030"))
    assert parse_text(gb).units[0].text == "中文合同测试"
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\xfd\xfc")
    with pytest.raises(WorkbenchError) as exc:
        parse_text(bad)
    assert exc.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_txt_empty_and_special_characters(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    result = parse_text(empty)
    assert result.units[0].status == "no_text_detected"
    special = tmp_path / "special.txt"
    special.write_text("符号：①“引号”&<> 100%_测试", encoding="utf-8")
    assert "100%_测试" in parse_text(special).units[0].text


def _sample_docx(path: Path) -> None:
    document = Document()
    document.add_paragraph("第一段：TEST-ONLY 合同")
    document.add_paragraph("")
    document.add_paragraph("第二段：金额 100.10 元")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "合并单元格"
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(1, 0).text = "行2列1"
    table.cell(1, 1).text = "行2列2"
    nested = table.cell(1, 1).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "嵌套表格内容"
    document.add_paragraph("表格之后")
    document.sections[0].header.paragraphs[0].text = "页眉 TEST-ONLY"
    image = Image.new("RGB", (10, 10), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    document.add_picture(buffer, width=Inches(0.2))
    document.save(path)


def test_docx_document_order_tables_and_warnings(tmp_path):
    path = tmp_path / "材料.docx"
    _sample_docx(path)
    before = _hash(path)
    result = parse_docx(path)
    texts = [unit.text for unit in result.units]
    assert texts[0] == "第一段：TEST-ONLY 合同"
    assert texts[1] == "第二段：金额 100.10 元"
    assert "合并单元格" in texts and texts.count("合并单元格") == 1
    assert "行2列1" in texts and "行2列2" in texts
    assert "嵌套表格内容" in texts
    assert texts[-1] == "表格之后"
    assert result.units[0].locator == {"type": "docx", "paragraph": 0}
    assert result.units[1].locator == {"type": "docx", "paragraph": 2}
    assert any("页眉/页脚" in warning for warning in result.warnings)
    assert any("嵌入图片" in warning for warning in result.warnings)
    assert result.status == "partial"
    assert _hash(path) == before


def test_docx_broken_zip_rejected(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"PK-not-a-zip")
    with pytest.raises(WorkbenchError) as exc:
        parse_docx(path)
    assert exc.value.code == ErrorCode.UNSUPPORTED_FORMAT


def _inject_cached_value(path: Path, coord: str, value: str) -> None:
    with zipfile.ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    sheet = entries["xl/worksheets/sheet1.xml"].decode("utf-8")
    pattern = re.compile(rf'(<c r="{coord}"[^>]*>)(<f>.*?</f>)(<v>.*?</v>)?(</c>)')
    sheet, count = pattern.subn(rf'\1\2<v>{value}</v>\4', sheet)
    assert count == 1, sheet
    entries["xl/worksheets/sheet1.xml"] = sheet.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in entries.items():
            target.writestr(name, data)


def test_xlsx_values_formulas_hidden_sheets(tmp_path):
    path = tmp_path / "账簿.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet["A1"] = "项目"
    sheet["B1"] = "金额"
    sheet["A2"] = 100
    sheet["A3"] = 1000
    sheet["B2"] = "=SUM(A2:A3)"
    sheet["C2"] = "人民币"
    sheet.merge_cells("A1:B1")
    hidden = workbook.create_sheet("隐藏页")
    hidden["A1"] = "隐藏内容"
    hidden.sheet_state = "hidden"
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=1, min_row=2, max_row=3))
    sheet.add_chart(chart, "E2")
    workbook.save(path)
    _inject_cached_value(path, "B2", "1100")
    before = _hash(path)
    result = parse_xlsx(path)
    by_key = {unit.unit_key: unit for unit in result.units}
    assert "xlsx:0:1" in by_key
    row1 = by_key["xlsx:0:1"]
    assert row1.locator["sheet"] == "明细" and row1.locator["row"] == 1
    assert row1.locator["merged"] == ["A1:B1"]
    assert any(cell["cell"] == "A1" and cell["value"] == "项目" for cell in row1.locator["cells"])
    formula_row = by_key["xlsx:0:2"]
    formula_cell = next(cell for cell in formula_row.locator["cells"] if cell["cell"] == "B2")
    assert formula_cell["formula"] == "=SUM(A2:A3)"
    assert formula_cell["cached"] is True and formula_cell["value"] == "1100"
    hidden_row = by_key["xlsx:1:1"]
    assert hidden_row.locator["hidden"] is True and hidden_row.locator["sheet"] == "隐藏页"
    assert any("图表未识别" in warning for warning in result.warnings)
    assert _hash(path) == before


def test_xlsx_formula_without_cache_is_not_zero(tmp_path):
    path = tmp_path / "公式.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = 5
    sheet["A2"] = "=A1*2"
    workbook.save(path)
    result = parse_xlsx(path)
    row = next(unit for unit in result.units if unit.unit_key == "xlsx:0:2")
    assert "缓存缺失" in row.text and "=> 0" not in row.text
    assert any("公式缺少缓存值" in warning for warning in row.warnings)


def test_xlsx_broken_rejected(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not a zip")
    with pytest.raises(WorkbenchError) as exc:
        parse_xlsx(path)
    assert exc.value.code == ErrorCode.UNSUPPORTED_FORMAT


def test_dispatch_and_unsupported(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("TEST-ONLY", encoding="utf-8")
    assert parse_file(txt).units[0].text == "TEST-ONLY"
    unknown = tmp_path / "a.xyz"
    unknown.write_bytes(b"x")
    with pytest.raises(WorkbenchError) as exc:
        parse_file(unknown)
    assert exc.value.code == ErrorCode.UNSUPPORTED_FORMAT
