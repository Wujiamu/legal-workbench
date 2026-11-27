"""零数据库独立包验收：方案 A 围栏、五格式解析、OCR 降级、精确值/暗线、CLI、真实 stdio MCP、插件包。

这些测试不连数据库、不启动工作台。样本分两类：

* 临时目录里的合成材料（TEST-ONLY），用于边界与降级分支；
* 仓库 ``acceptance_simulation/dossier`` 的真实虚构案卷，用于 CLI 与 Markdown 导出的
  端到端证据（其中金额、USCC、手机号、邮箱的期望值写在 ``ANSWER_KEY.md``）。

OCR 一律不作为断言前提：需要 OCR 的页面在缺模型时**必须**以结构化失败出现，
而不是让整份文档消失。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
from docx import Document
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas as rl_canvas

from plainfolio_parse import (
    ParseConfig,
    ParseError,
    parse_file,
    preview_table,
    resolve_readable_dir,
    resolve_readable_file,
    resolve_writable_output,
    scan_identifier_bridges,
)
from plainfolio_parse.config import default_parse_config
from plainfolio_parse.exact_values import extract_exact_values, group_amounts
from plainfolio_parse.formats.ocr import ocr_availability, verify_models
from plainfolio_parse.identifiers import extract_identifiers, extract_org_names
from plainfolio_parse.markdown_export import render_parse_result_markdown
from plainfolio_parse.path_fence import PLAINFOLIO_ALLOWED_ROOTS_ENV, allowed_roots
from plainfolio_parse.textnorm import normalize_with_map, original_index

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "packages" / "plainfolio-parse"
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plainfolio-parse"
LAUNCHER = PLUGIN_ROOT / "scripts" / "run-mcp.py"
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
ACCEPTANCE = REPO_ROOT / "acceptance_simulation" / "dossier"

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
FORBIDDEN_MARKERS = ("/Users/", "C:\\Users\\", "C:/Users/")

MCP_TOOLS = [
    "parse_file", "parse_folder", "parse_policy", "preview_table", "scan_exact_values",
    "scan_identifier_bridges",
]

# 来自 acceptance_simulation/ANSWER_KEY.md 的真实虚构值（校验位真实通过）
USCC_HUACHEN = "91310115MA1K9X284A"
USCC_QIHANG = "91320506MA1P7L392C"
MOBILE = "13918826655"
EMAIL = "legal.contract@huachen-sim.example"
BANK_ACCOUNT = "6225880123456789012"
CITIZEN_ID_DECOY = "31011519880512123X"
LOGISTICS_DECOY = "9000123456789012"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:\\Windows\\Fonts\\msyh.ttc",
)
FONT = next((path for path in _FONT_CANDIDATES if Path(path).is_file()), "")
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


# --- 合成材料 ---------------------------------------------------------------


def _pdf_bytes(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    pdf = rl_canvas.Canvas(buf, pagesize=A4)
    for index, line in enumerate(lines):
        pdf.setFont("STSong-Light", 12)
        pdf.drawString(60, 760 - index * 24, line)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _scanned_pdf_bytes(text: str) -> bytes:
    """一页只有图片的 PDF：文字层为空，必须 OCR 才能读。"""
    image = Image.new("RGB", (1240, 620), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 56) if FONT else ImageFont.load_default()
    draw.text((40, 60), text, font=font, fill="black")
    buf = io.BytesIO()
    pdf = rl_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height * 0.55)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("TEST-ONLY 采购合同（虚构）")
    document.add_paragraph(
        "甲方：上海华辰智能制造有限公司（统一社会信用代码 91310115MA1K9X284A）")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "标的"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "成套设备"
    table.cell(1, 1).text = "CNY 8,500,000.00"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "往来明细"
    first.append(["日期", "币种", "金额"])
    first.append(["2026-01-05", "CNY", 3500000.0])
    first.append(["2026-01-06", "USD", 50000.0])
    second = workbook.create_sheet("汇总")
    second["A1"] = "合计"
    second["B1"] = "=SUM(往来明细!C2:C3)"
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 480), (212, 226, 236)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """A fenced working directory: CWD-relative roots, no inherited env override."""
    root = (tmp_path / "work").resolve()
    root.mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.delenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, raising=False)
    monkeypatch.delenv("PLAINFOLIO_OCR_MODEL_DIR", raising=False)
    monkeypatch.delenv("LWB_OCR_MODEL_DIR", raising=False)
    return root


def _materials(root: Path) -> dict[str, Path]:
    files = {
        "text": root / "合同说明.txt",
        "docx": root / "起诉状.docx",
        "xlsx": root / "台账.xlsx",
        "pdf": root / "合同.pdf",
        "png": root / "拍照件.png",
        "scan_pdf": root / "扫描件.pdf",
    }
    files["text"].write_text("TEST-ONLY 第一段 金额 CNY 1,234.56元 日期 2026年1月5日\n\n第二段", encoding="utf-8")
    files["docx"].write_bytes(_docx_bytes())
    files["xlsx"].write_bytes(_xlsx_bytes())
    files["pdf"].write_bytes(_pdf_bytes([
        "TEST-ONLY 采购合同正文 合同金额 CNY 8,500,000.00元",
        "甲方 上海华辰智能制造有限公司 统一社会信用代码 91310115MA1K9X284A",
    ]))
    files["png"].write_bytes(_png_bytes())
    files["scan_pdf"].write_bytes(_scanned_pdf_bytes("扫描件 合同金额100.10元"))
    (root / ".DS_Store").write_bytes(b"hidden")
    return files


def _cli(root: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    for name in (PLAINFOLIO_ALLOWED_ROOTS_ENV, "PLAINFOLIO_OCR_MODEL_DIR", "LWB_OCR_MODEL_DIR"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(PACKAGE_ROOT / "src")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "plainfolio_parse.cli", *args],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=300,
    )


# --- 1. 方案 A 文件访问围栏 -------------------------------------------------


def test_fence_defaults_to_the_working_directory(workdir):
    assert allowed_roots() == (workdir,)

    inside = workdir / "材料.txt"
    inside.write_text("TEST-ONLY", encoding="utf-8")
    assert resolve_readable_file("材料.txt") == inside
    assert resolve_readable_dir(".") == workdir

    outside = workdir.parent / "外部材料.txt"
    outside.write_text("TEST-ONLY", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        resolve_readable_file(str(outside))
    assert caught.value.code == "forbidden"


def test_fence_rejects_parent_traversal(workdir):
    (workdir.parent / "外面.txt").write_text("TEST-ONLY", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        resolve_readable_file("../外面.txt")
    assert caught.value.code == "forbidden"

    # 词法归一后落在根内的 .. 不算穿越
    nested = workdir / "a" / "b"
    nested.mkdir(parents=True)
    (workdir / "内.txt").write_text("TEST-ONLY", encoding="utf-8")
    assert resolve_readable_file("a/b/../../内.txt") == workdir / "内.txt"


def test_fence_rejects_symlinks_even_when_they_point_inside(workdir):
    target = workdir / "真材料.txt"
    target.write_text("TEST-ONLY", encoding="utf-8")
    link = workdir / "链接.txt"
    link.symlink_to(target)
    with pytest.raises(ParseError) as caught:
        resolve_readable_file("链接.txt")
    assert caught.value.code == "forbidden"
    assert "符号链接" in caught.value.message


def test_fence_honours_the_allowed_roots_override(workdir, tmp_path, monkeypatch):
    other = (tmp_path / "别处").resolve()
    other.mkdir()
    (other / "材料.txt").write_text("TEST-ONLY", encoding="utf-8")

    with pytest.raises(ParseError):
        resolve_readable_file(other / "材料.txt")

    monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, str(other))
    assert allowed_roots() == (other,)
    assert resolve_readable_file(other / "材料.txt") == other / "材料.txt"

    monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, f"{other}{os.pathsep}{workdir}")
    assert set(allowed_roots()) == {other, workdir}

    monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, str(tmp_path / "不存在"))
    with pytest.raises(ParseError) as caught:
        allowed_roots()
    assert caught.value.code == "invalid_input"
    assert "不存在" in caught.value.message


def test_fence_handles_macos_tmp_alias_and_rejects_intermediate_dir_symlink(workdir, monkeypatch):
    real_dir = workdir / "真实子目录"
    real_dir.mkdir()
    real_file = real_dir / "证据.txt"
    real_file.write_text("TEST-ONLY", encoding="utf-8")
    link_dir = workdir / "链接子目录"
    try:
        link_dir.symlink_to(real_dir)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    str_work = str(workdir)
    alias_path = (
        Path("/tmp" + str_work.removeprefix("/private/tmp")) / "链接子目录" / "证据.txt"
        if sys.platform == "darwin" and str_work.startswith("/private/tmp")
        else link_dir / "证据.txt"
    )
    with pytest.raises(ParseError) as caught:
        resolve_readable_file(alias_path, allowed_roots())
    assert caught.value.code == "forbidden"

    if sys.platform == "darwin" and str_work.startswith("/private/tmp"):
        alias_root = Path("/tmp" + str_work.removeprefix("/private/tmp")) / "真实子目录"
        monkeypatch.setenv(PLAINFOLIO_ALLOWED_ROOTS_ENV, str(alias_root))
        assert allowed_roots() == (real_dir,)
        monkeypatch.setenv(
            PLAINFOLIO_ALLOWED_ROOTS_ENV,
            str(Path("/tmp" + str_work.removeprefix("/private/tmp")) / "链接子目录"),
        )
        with pytest.raises(ParseError) as caught_root:
            allowed_roots()
        assert caught_root.value.code == "invalid_input"


def test_output_refuses_to_overwrite_an_input_file(workdir):
    source = workdir / "原件.txt"
    source.write_text("TEST-ONLY", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        resolve_writable_output("原件.txt", forbidden_inputs=[source])
    assert caught.value.code == "forbidden"
    assert caught.value.message == "输出路径不得覆盖输入源文件"

    # 同一文件的不同写法也必须拦住
    with pytest.raises(ParseError) as caught:
        resolve_writable_output(str(source), forbidden_inputs=[workdir / "同类.txt" / ".." / "原件.txt"])
    assert caught.value.code == "forbidden"

    assert resolve_writable_output("导出/结果.md", forbidden_inputs=[source]).name == "结果.md"


# --- 2. TXT / DOCX / XLSX / PDF 解析与 Markdown 锚点 ------------------------


def test_text_parse_blocks_lines_and_normalization_offsets(workdir):
    path = workdir / "说明.txt"
    path.write_text("第一段\n第二行\n\n第二段", encoding="utf-8")
    result = parse_file(path)
    assert result.status == "complete"
    assert [unit.text for unit in result.units] == ["第一段\n第二行", "第二段"]
    assert result.units[0].locator == {"type": "text", "block": 0, "line_start": 1, "line_end": 2}

    normalized, runs = normalize_with_map("甲\u3000\u3000乙\n丙")
    assert normalized == "甲 乙 丙"
    assert original_index(runs, 2) == 3
    assert original_index(runs, 4) == 5

    fullwidth = workdir / "全角.txt"
    fullwidth.write_text("金额 ８５００ 元，编号 HT－001", encoding="utf-8")
    assert "8500" in normalize_with_map(parse_file(fullwidth).units[0].text)[0]
    assert "HT-001" in normalize_with_map(parse_file(fullwidth).units[0].text)[0]


def test_docx_paragraphs_and_table_cells(workdir):
    path = workdir / "起诉状.docx"
    path.write_bytes(_docx_bytes())
    result = parse_file(path)
    assert result.status == "complete"
    texts = [unit.text for unit in result.units]
    assert "TEST-ONLY 采购合同（虚构）" in texts
    assert "CNY 8,500,000.00" in texts
    keys = {unit.unit_key for unit in result.units}
    assert "docx:p:0" in keys
    assert any(key.startswith("docx:t:0:1:1") for key in keys)


def test_xlsx_sheets_rows_and_formula_cache_warning(workdir):
    path = workdir / "台账.xlsx"
    path.write_bytes(_xlsx_bytes())
    result = parse_file(path)
    sheets = {unit.locator.get("sheet") for unit in result.units if unit.locator.get("type") == "xlsx"}
    assert sheets == {"往来明细", "汇总"}
    row = next(unit for unit in result.units if unit.locator.get("sheet") == "往来明细"
               and unit.locator.get("row") == 2)
    assert "2026-01-05" in row.text
    formula = next(unit for unit in result.units if unit.locator.get("sheet") == "汇总")
    assert "=SUM(" in formula.text
    assert any("缓存" in warning for warning in formula.warnings)


def test_pdf_native_text_parsing(workdir):
    path = workdir / "合同.pdf"
    path.write_bytes(_pdf_bytes([
        "TEST-ONLY 采购合同正文 合同金额 CNY 8,500,000.00元",
        "甲方 上海华辰智能制造有限公司 统一社会信用代码 91310115MA1K9X284A",
    ]))
    result = parse_file(path)
    assert result.status == "complete"
    assert len(result.units) == 1
    unit = result.units[0]
    assert unit.locator["page"] == 1
    assert unit.method == "native"
    assert "8,500,000.00" in unit.text
    assert "上海华辰智能制造有限公司" in unit.text
    assert result.config["version"] == "pdf-quality-1.0"


def test_markdown_export_keeps_page_and_sheet_anchors(workdir):
    pdf = parse_file(workdir / "合同.pdf") if (workdir / "合同.pdf").exists() else None
    if pdf is None:
        (workdir / "合同.pdf").write_bytes(_pdf_bytes(["TEST-ONLY 合同正文 金额 CNY 100元"]))
        pdf = parse_file(workdir / "合同.pdf")
    markdown = render_parse_result_markdown(pdf, source_name="合同.pdf")
    assert markdown.startswith("---\n")
    for field in ("source:", "status:", "parser_version:", "unit_count:", "warnings:"):
        assert field in markdown
    assert "<!-- unit: pdf:1 -->" in markdown
    assert "## 第 1 页" in markdown

    xlsx = parse_file(workdir / "台账.xlsx") if (workdir / "台账.xlsx").exists() else None
    if xlsx is None:
        (workdir / "台账.xlsx").write_bytes(_xlsx_bytes())
        xlsx = parse_file(workdir / "台账.xlsx")
    sheet_markdown = render_parse_result_markdown(xlsx, source_name="台账.xlsx")
    assert "## 工作表：往来明细" in sheet_markdown
    assert "## 工作表：汇总" in sheet_markdown


# --- 3. 缺 OCR 时的结构化降级 ----------------------------------------------


def test_policy_reports_missing_ocr_reason_without_crashing(workdir):
    availability = ocr_availability(ParseConfig())
    assert availability["available"] is False
    assert availability["missing_policy"] == "degrade"
    assert availability["reason"]
    assert availability["model_dir"] is None

    configured = ocr_availability(ParseConfig(ocr_model_dir=workdir / "没有模型"))
    assert configured["available"] is False
    assert "模型" in configured["reason"]
    assert configured["model_dir"].endswith("rapidocr")


def test_scanned_page_degrades_to_failed_unit_and_keeps_page_info(workdir):
    path = workdir / "扫描件.pdf"
    path.write_bytes(_scanned_pdf_bytes("扫描件 合同金额100.10元"))
    result = parse_file(path)  # 默认 degrade，无模型目录
    assert result.status == "partial"
    assert len(result.units) == 1
    unit = result.units[0]
    assert unit.unit_key == "pdf:1"
    assert unit.status == "failed"
    assert unit.error.startswith("ocr_unavailable:")
    assert unit.locator["page"] == 1
    assert any("第 1 页失败" in warning for warning in result.warnings)


def test_image_without_ocr_reports_reason_but_text_formats_stay_complete(workdir):
    _materials(workdir)
    image_result = parse_file(workdir / "拍照件.png")
    assert image_result.status == "partial"
    assert image_result.units[0].status == "failed"
    assert image_result.units[0].error.startswith("ocr_unavailable:")

    for name in ("合同说明.txt", "起诉状.docx", "台账.xlsx", "合同.pdf"):
        path = workdir / name
        if not path.exists():
            continue
        assert parse_file(path).status == "complete", name

    disabled = parse_file(workdir / "拍照件.png", ParseConfig(enable_ocr=False))
    assert disabled.units[0].status == "failed"
    assert "关闭 OCR" in disabled.units[0].error


def test_strict_policy_raises_instead_of_degrading(workdir):
    path = workdir / "扫描件.pdf"
    if not path.exists():
        path.write_bytes(_scanned_pdf_bytes("扫描件 合同金额100.10元"))
    strict = ParseConfig(ocr_missing_policy="error")
    with pytest.raises(ParseError) as caught:
        parse_file(path, strict)
    assert caught.value.details["reason"] == "ocr_unavailable"

    with pytest.raises(ParseError):
        verify_models(workdir / "没有模型")


# --- 4. 精确值、暗线与表格预览 ---------------------------------------------


def test_exact_values_keep_currencies_apart():
    text = ("合同价款 CNY 8,500,000.00 元，另约定 USD 50,000.00 美元；"
            "保证金 12.8万元；签署日 2026年1月5日。")
    hits = extract_exact_values(text)
    totals = group_amounts(hits)
    assert totals["CNY"]["total"] == "8628000.00"
    assert totals["USD"]["total"] == "50000.00"
    assert totals["CNY"]["count"] == 2 and totals["USD"]["count"] == 1
    dates = [hit.canonical["date"] for hit in hits if hit.kind == "date"]
    assert dates == ["2026-01-05"]


def test_tabular_cell_currency_pairs_with_adjacent_amount():
    text = "A2: 2026-01-05\nB2: 3017\nC2: =D2 => CNY\nD2: 3,500,000.00"
    hits = [hit for hit in extract_exact_values(text) if hit.kind == "amount"]
    assert any(hit.amount is not None and str(hit.amount) == "3500000.00"
               and hit.currency == "CNY" for hit in hits)


def test_identifiers_reject_citizen_id_and_logistics_number():
    text = (f"经办人身份证号 {CITIZEN_ID_DECOY}，物流单号 {LOGISTICS_DECOY}，"
            f"信用代码 {USCC_QIHANG}，手机 {MOBILE}，邮箱 {EMAIL}，"
            f"开户银行 招商银行 账号 {BANK_ACCOUNT}。")
    kinds = {(hit.kind, hit.value) for hit in extract_identifiers(text)}
    assert ("uscc", USCC_QIHANG) in kinds
    assert ("cn_mobile", MOBILE) in kinds
    assert ("email", EMAIL) in kinds
    assert ("bank_account", BANK_ACCOUNT) in kinds
    assert all(value != CITIZEN_ID_DECOY for _kind, value in kinds)
    assert all(value != LOGISTICS_DECOY for _kind, value in kinds)


def test_org_names_use_strict_suffixes_only():
    names = [org.name for org in extract_org_names(
        "原告：上海华辰智能制造有限公司（统一社会信用代码 91310115MA1K9X284A）\n"
        "被告：苏州启航建设工程有限公司\n本院认为上述公司应承担责任")]
    assert "上海华辰智能制造有限公司" in names
    assert "苏州启航建设工程有限公司" in names
    assert all("上述公司" != name for name in names)


def test_scan_identifier_bridges_finds_cross_file_collisions(workdir):
    first = workdir / "第一份.txt"
    first.write_text(f"联系人 {MOBILE}，统一社会信用代码 {USCC_QIHANG}，账户 {BANK_ACCOUNT} 招商银行 账号",
                     encoding="utf-8")
    second = workdir / "第二份.txt"
    second.write_text(f"回款至 {MOBILE}；邮箱 {EMAIL}；社会信用代码 {USCC_QIHANG}",
                      encoding="utf-8")
    third = workdir / "第三份.txt"
    third.write_text(f"独立材料，仅手机 {MOBILE}", encoding="utf-8")

    report = scan_identifier_bridges([first, second, third])
    assert report["file_count"] == 3
    by_value = {(bridge["kind"], bridge["value"]): bridge for bridge in report["bridges"]}
    assert by_value[("cn_mobile", MOBILE)]["file_count"] == 3
    assert by_value[("uscc", USCC_QIHANG)]["file_count"] == 2
    assert ("email", EMAIL) not in by_value  # 只出现在一份文件里，不算暗线
    assert by_value[("cn_mobile", MOBILE)]["occurrences"][0]["file"].endswith("第一份.txt")
    assert report["failed_files"] == []


def test_preview_table_reads_utf8_bom_gb18030_and_xlsx(workdir):
    csv_utf8 = workdir / "utf8.csv"
    csv_utf8.write_text("\ufeff日期,对方名称,金额\n2026-03-01,TEST-ONLY 甲公司,123.45\n",
                        encoding="utf-8")
    preview = preview_table(csv_utf8)
    assert preview["headers"] == ["日期", "对方名称", "金额"]
    assert preview["sample_rows"] == [["2026-03-01", "TEST-ONLY 甲公司", "123.45"]]
    assert preview["total_rows"] == 1
    assert preview["sheets"] == ["CSV"]

    csv_gb = workdir / "gb.csv"
    csv_gb.write_bytes("日期,对方名称,金额\n2026-03-02,TEST-ONLY 乙公司,¥500.00\n".encode("gb18030"))
    gb_preview = preview_table(csv_gb)
    assert gb_preview["sample_rows"] == [["2026-03-02", "TEST-ONLY 乙公司", "¥500.00"]]

    workbook = Workbook()
    workbook.active.title = "封面"  # 空工作表：自动选中的应是第一张非空表
    flow = workbook.create_sheet("银行流水")
    flow.append(["交易日期", "", "方向", "金额", "备注"])
    flow.append(["2026-01-05", "TEST-ONLY 对方1有限公司", "支出", "100.50", "摘要"])
    path = workdir / "流水.xlsx"
    workbook.save(path)
    xlsx_preview = preview_table(path, header_row=1)
    assert xlsx_preview["selected_sheet"] == "银行流水"
    assert xlsx_preview["headers"] == ["交易日期", "列2", "方向", "金额", "备注"]
    assert xlsx_preview["total_rows"] == 1

    with pytest.raises(ParseError) as caught:
        preview_table(csv_utf8, header_row=0)
    assert caught.value.code == "invalid_input"


# --- 5. 真实样本上的 CLI ----------------------------------------------------


def _acceptance_file(*parts: str) -> str:
    return str((ACCEPTANCE.joinpath(*parts)).relative_to(REPO_ROOT))


@pytest.mark.skipif(not ACCEPTANCE.is_dir(), reason="需要 acceptance_simulation 样本")
def test_cli_parse_file_json_and_markdown_outputs():
    txt = _acceptance_file("04_履约往来与暗线材料", "08_工商变更与关联核查底档.txt")
    result = _cli(REPO_ROOT, "parse-file", "--path", txt)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "complete"
    assert payload["unit_count"] >= 1
    assert any("91320506MA1P7L392C" in unit["text"] for unit in payload["units"])
    assert payload["ocr"]["available"] in (True, False)

    markdown = _cli(REPO_ROOT, "parse-file", "--path", txt, "--format", "md")
    assert markdown.returncode == 0, markdown.stderr
    assert markdown.stdout.startswith("---\nsource:")
    assert "## 段落单元" in markdown.stdout
    # Markdown 分支不得向 stdout 混入 JSON 摘要
    assert "\"units\"" not in markdown.stdout


@pytest.mark.skipif(not ACCEPTANCE.is_dir(), reason="需要 acceptance_simulation 样本")
def test_cli_parse_folder_exports_markdown_without_overwriting_inputs(tmp_path):
    source = tmp_path / "案卷"
    source.mkdir()
    shutil.copy(ACCEPTANCE / "01_诉讼文书" / "01_民事起诉状.docx", source / "01_起诉状.docx")
    shutil.copy(ACCEPTANCE / "04_履约往来与暗线材料" / "08_工商变更与关联核查底档.txt",
                source / "08_底档.txt")
    (source / ".hidden.txt").write_text("隐藏文件不得被解析", encoding="utf-8")

    output_dir = tmp_path / "笔记"
    result = _cli(tmp_path, "parse-folder", "--dir", "案卷", "--format", "md",
                  "--output-dir", "笔记")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["file_count"] == 2
    assert (output_dir / "01_起诉状.md").is_file()
    assert (output_dir / "08_底档.md").is_file()
    assert not any(path.name == ".hidden.md" for path in output_dir.iterdir())
    assert "## 段落单元" in (output_dir / "08_底档.md").read_text(encoding="utf-8")

    # 输出与输入同一路径（哪怕只是把源文件当输出）→ 必须拒绝，且不留半个产物
    clash = _cli(tmp_path, "parse-file", "--path", "案卷/08_底档.txt",
                 "--format", "md", "--output", "案卷/08_底档.txt")
    assert clash.returncode == 2
    assert clash.stdout == ""
    assert json.loads(clash.stderr)["message"] == "输出路径不得覆盖输入源文件"
    assert (tmp_path / "案卷" / "08_底档.txt").read_text(encoding="utf-8").startswith(
        "【TEST-ONLY")

    json_run = _cli(tmp_path, "parse-folder", "--dir", "案卷")
    assert json_run.returncode == 0, json_run.stderr
    payload = json.loads(json_run.stdout)
    assert payload["file_count"] == 2
    assert {item["status"] for item in payload["files"]} == {"complete"}


@pytest.mark.skipif(not ACCEPTANCE.is_dir(), reason="需要 acceptance_simulation 样本")
def test_cli_scan_values_and_bridges_on_real_samples():
    values = _cli(REPO_ROOT, "scan-values", "--path",
                  _acceptance_file("04_履约往来与暗线材料", "08_工商变更与关联核查底档.txt"))
    assert values.returncode == 0, values.stderr
    payload = json.loads(values.stdout)
    assert payload["amount_totals"]["CNY"]["total"] == "8500000.00"
    assert payload["date_counts"].get("2025-09-18") == 1

    bridges = _cli(REPO_ROOT, "scan-bridges", "--path", "acceptance_simulation/dossier")
    assert bridges.returncode == 0, bridges.stderr
    report = json.loads(bridges.stdout)
    found = {(bridge["kind"], bridge["value"]): bridge for bridge in report["bridges"]}
    uscc = found[("uscc", USCC_QIHANG)]
    assert uscc["file_count"] == 3
    assert any(path.endswith("01_民事起诉状.docx") for path in uscc["files"])
    assert any(path.endswith("08_工商变更与关联核查底档.txt") for path in uscc["files"])
    assert ("cn_mobile", MOBILE) in found
    assert ("email", EMAIL) in found
    assert ("bank_account", BANK_ACCOUNT) in found
    assert all(value != CITIZEN_ID_DECOY for _kind, value in found)
    assert all(value != LOGISTICS_DECOY for _kind, value in found)


@pytest.mark.skipif(not ACCEPTANCE.is_dir(), reason="需要 acceptance_simulation 样本")
def test_cli_preview_table_on_the_multi_header_statement():
    result = _cli(REPO_ROOT, "preview-table", "--path",
                  _acceptance_file("03_财务流水与对账", "06_非标银行对账单_多表头混列.csv"),
                  "--header-row", "1")
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview["selected_sheet"] == "CSV"
    assert preview["headers"]
    assert preview["total_rows"] >= 20
    assert len(preview["sample_rows"]) == 20


def test_cli_reports_business_errors_with_exit_code_two(tmp_path, workdir):
    outside = tmp_path / "外面.txt"
    outside.write_text("TEST-ONLY", encoding="utf-8")
    escaped = _cli(workdir, "parse-file", "--path", str(outside))
    assert escaped.returncode == 2
    assert escaped.stdout == ""
    payload = json.loads(escaped.stderr)
    assert payload["code"] == "forbidden"

    missing = _cli(workdir, "parse-file", "--path", "不存在.txt")
    assert missing.returncode == 2
    assert json.loads(missing.stderr)["code"] == "not_found"

    unsupported = workdir / "图片.bmp.bin"
    unsupported.write_bytes(b"\x00\x01")
    bad = _cli(workdir, "parse-file", "--path", "图片.bmp.bin")
    assert bad.returncode == 2
    assert json.loads(bad.stderr)["code"] == "unsupported_format"


# --- 6. 真实 stdio MCP ------------------------------------------------------


def _stdio_params(root: Path) -> StdioServerParameters:
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")}
    for name in (PLAINFOLIO_ALLOWED_ROOTS_ENV, "PLAINFOLIO_OCR_MODEL_DIR", "LWB_OCR_MODEL_DIR"):
        env.pop(name, None)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "plainfolio_parse.mcp_server"],
        cwd=str(root), env=env,
    )


def _tool_payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    return json.loads(result.content[0].text)


def test_stdio_mcp_exposes_six_tools_and_parses(tmp_path):
    case_dir = (tmp_path / "case").resolve()
    case_dir.mkdir()
    _materials(case_dir)
    (case_dir / "对账单.csv").write_text("日期,对方,金额\n2026-01-05,TEST-ONLY 甲,100.00\n",
                                        encoding="utf-8")

    async def body():
        async with stdio_client(_stdio_params(case_dir)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                policy = await session.call_tool("parse_policy", {})
                parsed = await session.call_tool("parse_file", {"path": "合同说明.txt"})
                folder = await session.call_tool("parse_folder", {"directory": "."})
                values = await session.call_tool("scan_exact_values",
                                                 {"paths": ["合同说明.txt", "合同.pdf"]})
                bridges = await session.call_tool(
                    "scan_identifier_bridges", {"paths": ["合同.pdf", "起诉状.docx"]})
                table = await session.call_tool("preview_table", {"path": "对账单.csv"})
                escaped = await session.call_tool("parse_file", {"path": "../外面.txt"})
                return tools, policy, parsed, folder, values, bridges, table, escaped

    tools, policy, parsed, folder, values, bridges, table, escaped = anyio.run(body)
    assert tools == MCP_TOOLS
    policy_payload = _tool_payload(policy)
    assert str(case_dir) in policy_payload["allowed_roots"]
    assert "ocr" in policy_payload

    parsed_payload = _tool_payload(parsed)
    assert parsed_payload["status"] == "complete"
    assert "CNY 1,234.56" in parsed_payload["units"][0]["text"]

    folder_payload = _tool_payload(folder)
    assert folder_payload["file_count"] >= 5
    statuses = {Path(item["source"]).name: item["status"] for item in folder_payload["files"]}
    assert statuses["合同.pdf"] == "complete"
    assert statuses["拍照件.png"] == "partial"  # 无模型目录 → 结构化失败，不是崩溃
    assert not any(name.startswith(".") for name in statuses)

    values_payload = _tool_payload(values)
    assert "CNY" in values_payload["amount_totals"]
    assert values_payload["date_counts"].get("2026-01-05") == 1

    bridges_payload = _tool_payload(bridges)
    found = {(bridge["kind"], bridge["value"]) for bridge in bridges_payload["bridges"]}
    assert ("uscc", USCC_HUACHEN) in found

    assert _tool_payload(table)["headers"] == ["日期", "对方", "金额"]
    assert getattr(escaped, "isError", False) or "forbidden" in json.dumps(
        _tool_payload(escaped), ensure_ascii=False)


# --- 7. 插件包与市场登记 ----------------------------------------------------


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_package_imports_no_database_or_http_stack():
    banned = ("sqlalchemy", "psycopg", "fastapi", "alembic", "uvicorn")
    for path in sorted((PACKAGE_ROOT / "src" / "plainfolio_parse").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in source, f"{path.name} 引入了 {name}"
            assert f"from {name}" not in source, f"{path.name} 引入了 {name}"

    probe = subprocess.run(
        [sys.executable, "-c",
         "import json, sys, plainfolio_parse; "
         "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
         "('sqlalchemy','psycopg','fastapi','rapidocr_onnxruntime','alembic','uvicorn'))))"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")},
    )
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == []


def test_plugin_manifest_declares_a_portable_parse_server():
    manifest = _read_json(PLUGIN_ROOT / "plugin.json")
    assert manifest["$schema"] == PLUGIN_SCHEMA
    assert manifest["name"] == "plainfolio-parse"
    assert manifest["version"] == "0.1.0"
    assert manifest["description"].strip()
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (PLUGIN_ROOT / "skills" / "plainfolio-parse" / "SKILL.md").is_file()
    assert (PLUGIN_ROOT / ".mcp.json").is_file()
    openai = manifest["extensions"]["com.openai"]
    for key in ("category", "displayName", "shortDescription", "longDescription"):
        assert openai[key].strip(), key
    assert (PLUGIN_ROOT / openai["onboardingSkill"]).is_file()

    skill = (PLUGIN_ROOT / "skills" / "plainfolio-parse" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\n")
    assert "name: plainfolio-parse" in skill
    assert "PLAINFOLIO_ALLOWED_ROOTS" in skill
    assert "原件只读" in skill


def test_both_mcp_manifests_launch_the_portable_wrapper():
    portable = _read_json(PLUGIN_ROOT / "mcp.json")
    assert portable["$schema"] == MCP_SCHEMA
    server = portable["mcpServers"]["plainfolio-parse"]
    assert server["type"] == "stdio"
    assert server["command"] == "python3"
    assert server["args"] == ["${PLUGIN_ROOT}/scripts/run-mcp.py"]
    assert not {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(server.get("env", {}))

    codex = _read_json(PLUGIN_ROOT / ".mcp.json")["mcpServers"]["plainfolio-parse"]
    assert {key: server.get(key) for key in ("command", "args", "env")} == {
        key: codex.get(key) for key in ("command", "args", "env")}
    assert LAUNCHER.is_file()


def test_plugin_ships_no_machine_specific_path():
    files = sorted(
        path for path in PLUGIN_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    assert files
    offenders: list[str] = []
    for path in [*files, MARKETPLACE]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in FORBIDDEN_MARKERS:
            if marker in text:
                offenders.append(f"{path.name} -> {marker}")
    assert offenders == []


def test_launcher_print_plan_resolves_a_real_interpreter():
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "--print-plan"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1
    plan = json.loads(result.stdout)
    assert plan["source"] in {"PLAINFOLIO_PARSE_BIN", "PATH", "仓库虚拟环境", "当前解释器"}
    assert plan["cwd"] is None
    assert Path(plan["command"][0]).exists()
    assert plan["command"][-2:] == ["-m", "plainfolio_parse.mcp_server"]

    unknown = subprocess.run(
        [sys.executable, str(LAUNCHER), "--serve"],
        capture_output=True, text=True, timeout=120,
    )
    assert unknown.returncode == 2
    assert unknown.stdout == ""
    assert "未知参数" in unknown.stderr


def test_marketplace_indexes_the_parse_plugin_after_plainfolio():
    catalog = _read_json(MARKETPLACE)
    names = [entry["name"] for entry in catalog["plugins"]]
    assert names[0] == "plainfolio"
    assert catalog["plugins"][0]["version"] == "0.1.0"
    assert "plainfolio-parse" in names
    assert len(names) == len(set(names))

    entry = next(item for item in catalog["plugins"] if item["name"] == "plainfolio-parse")
    assert entry["version"] == _read_json(PLUGIN_ROOT / "plugin.json")["version"] == "0.1.0"
    assert entry["source"] == {"source": "local", "path": "./plugins/plainfolio-parse"}
    resolved = (MARKETPLACE.parents[2] / entry["source"]["path"]).resolve()
    assert resolved == PLUGIN_ROOT.resolve()
    assert (resolved / "plugin.json").is_file()
