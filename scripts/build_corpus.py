"""Build synthetic TEST-ONLY corpora for acceptance and performance runs.

All content is generated here; no real case material is ever read. Two entry
points:
  * build_functional_corpus  - the mixed-format sample set (TXT/DOCX/XLSX/PDF/images)
  * build_performance_corpus - ~1000 files / ~10000 pages of varied synthetic text
"""
from __future__ import annotations

import os
import random
import zipfile
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

_WIN_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_CANDIDATES = (
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    str(_WIN_FONTS / "msyh.ttc"),
    str(_WIN_FONTS / "simhei.ttf"),
    str(_WIN_FONTS / "simsun.ttc"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)

CLAUSE_TEMPLATES = (
    "甲方应于约定日期前向乙方支付人民币{amount}元，逾期按日万分之五计息。",
    "乙方应在收到通知后{amount}日内完成交付，交付地点由双方另行确认。",
    "本合同项下争议由合同签订地人民法院管辖，适用中华人民共和国法律。",
    "任何一方违约，应向守约方赔偿由此造成的直接损失，包括合理律师费。",
    "保密义务在本合同终止后继续有效，期限为{amount}年。",
    "附件与本合同具有同等效力；附件与正文不一致时，以正文为准。",
    "乙方保证交付物不侵犯第三方知识产权，如有侵权由乙方承担全部责任。",
    "经双方书面同意，可以变更本合同条款；口头变更不发生效力。",
)
PARTIES = ("甲方", "乙方", "丙方", "丁公司", "某某事务所", "担保人")
IDS = ("HT-{n:03d}", "LWB/{year}/{n:04d}", "NO-{n:05d}", "CASE-{n:04d}-A")


def _font(size: int = 22):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _sentences(rng: random.Random, count: int, *, with_numbers: bool = True) -> list[str]:
    out = []
    for _ in range(count):
        template = rng.choice(CLAUSE_TEMPLATES)
        amount = rng.choice([100, 1000, 5000, 10000, 120000, 3500000])
        sentence = template.format(amount=f"{amount:,}" if rng.random() < 0.3 else amount)
        if with_numbers and rng.random() < 0.5:
            sentence += f" 编号 {rng.choice(IDS).format(n=rng.randint(1, 999), year=2024)}。"
        if with_numbers and rng.random() < 0.4:
            sentence += f" 签订日期 2024年{rng.randint(1, 12)}月{rng.randint(1, 28)}日。"
        out.append(sentence)
    return out


def build_functional_corpus(root: Path) -> dict:
    """Mixed formats, duplicates, a corrupt file, near-amount values, nested folders."""
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261110)
    layout: dict[str, int] = {}

    # 1. Plain text, two encodings.
    (root / "虚构合同-utf8.txt").write_text(
        "TEST-ONLY 虚构合同\n\n" + "\n\n".join(_sentences(rng, 12)) + "\n", encoding="utf-8")
    (root / "虚构合同-gb18030.txt").write_bytes(
        ("TEST-ONLY 虚构合同（GB18030）\n\n" + "\n\n".join(_sentences(rng, 8)))
        .encode("gb18030"))
    layout["txt"] = 2

    # 2. DOCX with a nested table.
    document = Document()
    document.add_heading("TEST-ONLY 虚构合同", level=1)
    for line in _sentences(rng, 6):
        document.add_paragraph(line)
    table = document.add_table(rows=3, cols=3)
    for row_index, row in enumerate(table.rows):
        for column_index, cell in enumerate(row.cells):
            cell.text = f"单元格{row_index}-{column_index} 金额{1000 * (row_index + 1)}元"
    nested = table.cell(0, 0).add_table(rows=2, cols=2)
    nested.cell(0, 0).text = "嵌套表格 A1 编号 HT-777"
    document.save(root / "虚构合同.docx")
    layout["docx"] = 1

    # 3. XLSX with formulas, a hidden sheet and cached values missing on one cell.
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet.append(["项目", "金额", "日期", "编号"])
    for index in range(1, 13):
        sheet.append([f"项目{index}", 100 * index, f"2024-{1 + index % 12:02d}-{1 + index % 28:02d}",
                      f"HT-{index:03d}"])
    sheet["B15"] = "=SUM(B2:B13)"
    hidden = workbook.create_sheet("隐藏页")
    hidden.sheet_state = "hidden"
    hidden["A1"] = "TEST-ONLY 隐藏工作表内容 金额 5000元"
    workbook.save(root / "虚构明细.xlsx")
    layout["xlsx"] = 1

    # 4. Native-text PDF, multi page.
    pdf_path = root / "虚构合同.pdf"
    pdf = canvas.Canvas(str(pdf_path), pagesize=A4)
    # The task package asks for a 40-page mixed PDF; page count also exercises
    # per-page checkpointing rather than a single trivial unit.
    for page in range(1, 41):
        pdf.setFont("Helvetica", 11)
        pdf.drawString(60, 800, f"TEST-ONLY contract page {page}")
        y = 770
        for line in _sentences(rng, 8):
            pdf.drawString(60, y, line.encode("ascii", "replace").decode("ascii")[:95])
            y -= 18
        pdf.showPage()
    pdf.save()
    layout["pdf_native"] = 1

    # 5. Scanned image and a scanned-page PDF (needs OCR).
    image = Image.new("RGB", (1000, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 60), "TEST-ONLY 扫描件", font=_font(28), fill="black")
    draw.text((40, 140), "合同金额 5000 元", font=_font(24), fill="black")
    draw.text((40, 220), "签订日期 2024年3月5日", font=_font(24), fill="black")
    image.save(root / "虚构扫描件.png")
    layout["image"] = 1

    scanned_pdf = root / "虚构扫描.pdf"
    scanned = canvas.Canvas(str(scanned_pdf), pagesize=A4)
    scanned.drawImage(str(root / "虚构扫描件.png"), 40, 500, width=520, height=208)
    scanned.showPage()
    scanned.save()
    layout["pdf_scanned"] = 1

    # 6. Duplicate content in a different folder, plus a nested subfolder.
    nested_dir = root / "子目录" / "再一层"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "虚构合同-utf8.txt").write_text(
        (root / "虚构合同-utf8.txt").read_text(encoding="utf-8"), encoding="utf-8")
    payload = "TEST-ONLY duplicate payload 金额 1000元"
    (nested_dir / "虚构重复副本.txt").write_text(payload, encoding="utf-8")
    (root / "虚构重复原件.txt").write_text(payload, encoding="utf-8")
    layout["duplicates"] = 3

    # 7. Corrupt files that must fail visibly, never be silently skipped.
    (root / "虚构损坏.docx").write_bytes(b"PK\x03\x04 not really a docx")
    broken = root / "虚构损坏.xlsx"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook>broken")
    layout["corrupt"] = 2

    # 8. Empty file.
    (root / "虚构空文件.txt").write_bytes(b"")
    layout["empty"] = 1

    # 9. System junk that must be skipped and recorded.
    (root / ".DS_Store").write_bytes(b"junk")
    layout["junk"] = 1

    # 10. Near-but-different amounts for the amount-mode check.
    (root / "虚构金额对照.txt").write_text(
        "TEST-ONLY 金额对照\n\n"
        "第一笔 100元。\n\n第二笔 1000元。\n\n第三笔 1万元。\n\n"
        "第四笔 10000元。\n\n第五笔 100 USD。\n\n第六笔 100 CNY。\n",
        encoding="utf-8")
    layout["amounts"] = 1
    return layout


def build_performance_corpus(root: Path, *, files: int = 1000, pages_per_file: int = 10,
                             seed: int = 20261111) -> dict:
    """~files x pages_per_file pages of distinct synthetic text, mixed formats."""
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    written = 0
    total_pages = 0
    for index in range(files):
        folder = root / f"批次{index // 100:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        bucket = index % 10
        name = f"虚构材料{index:05d}"
        if bucket < 6:  # 60% plain text, one page per "page"
            lines = []
            for page in range(pages_per_file):
                lines.append(f"第{page + 1}页 案卷{index:05d} 页码{page + 1}")
                lines.extend(_sentences(rng, 4))
                lines.append("")
            (folder / f"{name}.txt").write_text("\n".join(lines), encoding="utf-8")
            total_pages += pages_per_file
        elif bucket < 8:  # 20% native PDF
            pdf = canvas.Canvas(str(folder / f"{name}.pdf"), pagesize=A4)
            for page in range(pages_per_file):
                pdf.setFont("Helvetica", 10)
                pdf.drawString(50, 800, f"TEST-ONLY file {index} page {page + 1}")
                y = 770
                for line in _sentences(rng, 6):
                    pdf.drawString(50, y, line.encode("ascii", "replace").decode("ascii")[:100])
                    y -= 16
                pdf.showPage()
            pdf.save()
            total_pages += pages_per_file
        elif bucket == 8:  # 10% DOCX
            document = Document()
            document.add_heading(f"TEST-ONLY 案卷 {index}", level=1)
            for _ in range(pages_per_file * 4):
                document.add_paragraph(rng.choice(_sentences(rng, 1)))
            document.save(folder / f"{name}.docx")
            total_pages += pages_per_file
        else:  # 10% XLSX, one sheet per page
            workbook = Workbook()
            workbook.remove(workbook.active)
            for page in range(pages_per_file):
                sheet = workbook.create_sheet(f"表{page + 1}")
                sheet.append(["项目", "金额", "编号"])
                for row in range(20):
                    sheet.append([f"项目{row}", 100 * (row + 1), f"HT-{index:05d}-{row:03d}"])
            workbook.save(folder / f"{name}.xlsx")
            total_pages += pages_per_file
        written += 1
    return {"files": written, "pages": total_pages}
