"""法院合规自动装卷渲染内核（A4 排版 / 封面 / 动态页码目录 / 分项材料 / ZIP）。

本模块是从工作台 ``legal_workbench.dossier_export`` 迁出的纯渲染实现，不依赖
数据库、HTTP 或受管存储：

* 输入是 :class:`~plainfolio_dossier.models.DossierSpec`（工作台把数据库快照
  转成同一结构，独立模式从 JSON/CSV 清单读出来）；
* 每条绑定材料的磁盘路径由 ``path_resolver``（工作台注入 ``Storage.verify`` +
  ``managed_path``）或 :mod:`plainfolio_dossier.path_fence`（独立模式围栏）给出；
* 输出是 :class:`~plainfolio_dossier.models.DossierResult`，字节完全由调用方
  决定写到哪。

排版口径（迁出前已被工作台测试锁定，迁移后逐字保留）：A4 幅面、左侧 20mm
装订线、封面/目录/分隔页不占正文页码、正文右下角连续页码、右上角严格留白供
法院归档编码、DOCX 目录可编辑、CSV 防公式注入、网上立案分项 PDF 逐项命名。
"""
from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import io
import ntpath
import os
import re
import threading
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import pypdfium2 as pdfium
from docx import Document as DocxDocument
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Mm, Pt, RGBColor
from lxml import etree
from PIL import Image, ImageOps
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from . import path_fence
from .errors import DossierError
from .models import BindingSpec, DossierResult, DossierSpec

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

A4_PORTRAIT = A4
A4_LANDSCAPE = landscape(A4)

# 装订留边：左侧 20mm（高于法院最低 15mm 要求），右下角编页码，右上角严格留白。
LEFT_BIND_MARGIN = 20 * mm
RIGHT_MARGIN = 15 * mm
TOP_MARGIN = 12 * mm
BOTTOM_MARGIN = 15 * mm
FOOTER_Y = 6 * mm

PDF_SUFFIXES = frozenset({".pdf"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"})

ONLINE_WARN_BYTES = 50 * 1024 * 1024
ONLINE_MAX_BYTES = 100 * 1024 * 1024
MAX_IMAGE_FRAMES = 200
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SOFTWARE_NAME = "PlainFolio (素章)"
PACKAGE_NAME = "plainfolio-dossier"

# PDFium 不是线程安全的；工作台进程与独立进程都只在这把锁内渲染。
PDFIUM_LOCK = threading.Lock()

_FONT_LOCK = threading.Lock()
_FONT_PAIR: tuple[str, str] | None = None

_REGULAR_FONT_CANDIDATES = (
    # macOS (TrueType-outline only; skip PostScript CFF TTCs like Hiragino Sans GB)
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Windows
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simfang.ttf",
    r"C:\Windows\Fonts\simkai.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

_BOLD_FONT_CANDIDATES = (
    # macOS
    "/System/Library/Fonts/STHeiti Medium.ttc",
    # Windows
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)

_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


# --- 文件名规范化（与工作台 storage.safe_name 逐字一致） --------------------

def _is_windows_reserved(name: str) -> bool:
    head = name.partition(".")[0].rstrip(" ").upper()
    if head in _WINDOWS_RESERVED_NAMES:
        return True
    return hasattr(ntpath, "isreserved") and ntpath.isreserved(name)


def _truncate_component(name: str, limit: int = 120) -> str:
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 10 and len(ext) + 2 <= limit:
        return stem[: limit - len(ext) - 1] + "." + ext
    return name[:limit]


def safe_name(name: str, fallback: str = "material") -> str:
    """Display-name to a single safe path component (no directories)."""
    name = unicodedata.normalize("NFC", name).replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = _WINDOWS_INVALID_CHARS.sub("_", name)
    name = name.lstrip(". ").rstrip(" .") or fallback
    name = _truncate_component(name, 120).lstrip(". ").rstrip(" .") or fallback
    if _is_windows_reserved(name):
        name = _truncate_component(f"_{name}", 120).lstrip(". ").rstrip(" .") or fallback
    return name


# --- 字体 -------------------------------------------------------------------

def _register_first_ttfont(alias: str, candidates: tuple[str, ...]) -> str | None:
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            if path.lower().endswith(".ttc"):
                pdfmetrics.registerFont(TTFont(alias, path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(alias, path))
            return alias
        except Exception:
            continue
    return None


def _ensure_fonts() -> tuple[str, str]:
    """Register cross-platform CJK regular/bold fonts for ReportLab once."""
    global _FONT_PAIR
    if _FONT_PAIR is not None:
        return _FONT_PAIR
    with _FONT_LOCK:
        if _FONT_PAIR is not None:
            return _FONT_PAIR
        regular = _register_first_ttfont("PfdCjkRegular", _REGULAR_FONT_CANDIDATES)
        if regular is None:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            regular = "STSong-Light"
        bold = _register_first_ttfont("PfdCjkBold", _BOLD_FONT_CANDIDATES) or regular
        pdfmetrics.registerFontFamily(
            regular, normal=regular, bold=bold, italic=regular, boldItalic=bold,
        )
        _FONT_PAIR = (regular, bold)
        return _FONT_PAIR


# --- 渲染原语的可替换接缝 ---------------------------------------------------
#
# 工作台适配层把 ``legal_workbench.dossier_export`` 上同名的两个页面渲染函数
# 转进来，这样既有的故障注入测试（把矢量排版与位图兜底都改坏）仍然能作用于
# 真正的渲染路径，而不是只改到一个没人调用的空壳。

RenderFn = Callable[[PageObject], tuple[PageObject, tuple[float, float]]]
RasterizeFn = Callable[[bytes, int], tuple[PageObject, tuple[float, float]]]

_render_loader: Callable[[], tuple[RenderFn, RasterizeFn]] | None = None


def install_render_hook_loader(loader: Callable[[], tuple[RenderFn, RasterizeFn]] | None) -> None:
    """Host seam: resolve the page-render primitives at each build (or reset)."""
    global _render_loader
    _render_loader = loader


def _render_functions() -> tuple[RenderFn, RasterizeFn]:
    if _render_loader is None:
        return _place_pdf_page_onto_a4, _rasterize_pdf_page_to_a4
    return _render_loader()


@dataclass(frozen=True)
class _ResolvedBinding:
    page_start: int | None
    page_end: int | None
    material_name: str
    acquisition: str | None
    path: Path
    ext: str


@dataclass
class _ItemAssembly:
    code: str
    group_no: int
    group_title: str
    title: str
    purpose: str | None
    form_label: str
    source_names: list[str]
    standalone_entries: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    content_pages: list[PageObject] = field(default_factory=list)

    @property
    def page_range_text(self) -> str:
        if self.page_start is None or self.page_end is None:
            return "—"
        if self.page_start == self.page_end:
            return f"第 {self.page_start} 页"
        return f"第 {self.page_start}-{self.page_end} 页"


def _clean_pdf_bytes_reader(data: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(data))


def _make_stamp_page(width: float, height: float, *, left_text: str, right_text: str) -> PageObject:
    """Create a transparent overlay page with bottom-left/bottom-right text (top-right blank)."""
    regular, _ = _ensure_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(width, height))
    c.setFont(regular, 8.5)
    c.setFillColor(colors.HexColor("#334155"))
    if left_text:
        c.drawString(LEFT_BIND_MARGIN, FOOTER_Y, left_text)
    if right_text:
        c.drawRightString(width - RIGHT_MARGIN, FOOTER_Y, right_text)
    c.showPage()
    c.save()
    return _clean_pdf_bytes_reader(buf.getvalue()).pages[0]


def _make_a4_image_page(img: Image.Image) -> tuple[PageObject, tuple[float, float]]:
    """Place a raster PIL image inside A4 safe margins, returning an unnumbered PDF page."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        rgb = bg
    elif img.mode != "RGB":
        rgb = img.convert("RGB")
    else:
        rgb = img

    img_w, img_h = rgb.size
    pagesize = A4_LANDSCAPE if img_w > img_h * 1.15 else A4_PORTRAIT
    page_w, page_h = pagesize
    avail_w = page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN
    avail_h = page_h - TOP_MARGIN - BOTTOM_MARGIN
    scale = min(avail_w / max(img_w, 1), avail_h / max(img_h, 1))
    draw_w = img_w * scale
    draw_h = img_h * scale
    x = LEFT_BIND_MARGIN + (avail_w - draw_w) / 2.0
    y = BOTTOM_MARGIN + (avail_h - draw_h) / 2.0

    img_buf = io.BytesIO()
    rgb.save(img_buf, format="PNG")
    img_buf.seek(0)

    pdf_buf = io.BytesIO()
    c = rl_canvas.Canvas(pdf_buf, pagesize=pagesize)
    c.drawImage(ImageReader(img_buf), x, y, width=draw_w, height=draw_h, preserveAspectRatio=True)
    c.showPage()
    c.save()
    return _clean_pdf_bytes_reader(pdf_buf.getvalue()).pages[0], pagesize


def _wrap_paragraphs(text: str, style: ParagraphStyle) -> Paragraph:
    safe = escape(text or "").replace("\n", "<br/>")
    return Paragraph(safe, style)


def _make_notice_page(*, code: str, title: str, badge: str, heading: str,
                      lines: list[str]) -> tuple[PageObject, tuple[float, float]]:
    """Build an A4 portrait notice/placeholder page inside safe margins."""
    regular, bold = _ensure_fonts()
    page_w, page_h = A4_PORTRAIT
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4_PORTRAIT)

    x0 = LEFT_BIND_MARGIN + 8 * mm
    box_w = page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN - 16 * mm
    y_top = page_h - TOP_MARGIN - 26 * mm

    title_style = ParagraphStyle("notice_title", fontName=bold, fontSize=15, leading=21,
                                 textColor=colors.HexColor("#0F172A"))
    p_title = _wrap_paragraphs(title, title_style)
    _, th = p_title.wrap(box_w, 45 * mm)

    body_style = ParagraphStyle("notice_body", fontName=regular, fontSize=10.5, leading=16.5,
                                textColor=colors.HexColor("#334155"))
    wrapped_lines = []
    body_total_h = 0.0
    for line in lines:
        p = _wrap_paragraphs(line, body_style)
        _, ph = p.wrap(box_w, 60 * mm)
        wrapped_lines.append((p, ph))
        body_total_h += ph + 3.5 * mm

    # Size frame box dynamically so long titles or file lists never overflow
    box_height = max(114 * mm, 42 * mm + th + body_total_h)
    box_height = min(box_height, y_top - BOTTOM_MARGIN + 6 * mm)

    c.setStrokeColor(colors.HexColor("#CBD5E1"))
    c.setFillColor(colors.HexColor("#F8FAFC"))
    c.roundRect(LEFT_BIND_MARGIN + 2 * mm, y_top + 6 * mm - box_height,
                page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN - 4 * mm, box_height,
                4, stroke=1, fill=1)

    c.setFillColor(colors.HexColor("#173E35"))
    c.setFont(bold, 11)
    c.drawString(x0, y_top - 4 * mm, f"{code} · {badge}")

    c.setFillColor(colors.HexColor("#0F172A"))
    p_title.drawOn(c, x0, y_top - 14 * mm - th)

    c.setStrokeColor(colors.HexColor("#E2E8F0"))
    line_y = y_top - 18 * mm - th
    c.line(x0, line_y, x0 + box_w, line_y)

    c.setFillColor(colors.HexColor("#1E293B"))
    c.setFont(bold, 11)
    c.drawString(x0, line_y - 9 * mm, heading)

    cur_y = line_y - 16 * mm
    for p, ph in wrapped_lines:
        if cur_y - ph < BOTTOM_MARGIN:
            break
        p.drawOn(c, x0, cur_y - ph)
        cur_y -= ph + 3.5 * mm

    c.showPage()
    c.save()
    return _clean_pdf_bytes_reader(buf.getvalue()).pages[0], A4_PORTRAIT


def _make_divider_page(item: _ItemAssembly) -> PageObject:
    """Build an unnumbered per-item divider page (证据分隔页)."""
    regular, bold = _ensure_fonts()
    page_w, page_h = A4_PORTRAIT
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4_PORTRAIT)

    x0 = LEFT_BIND_MARGIN + 10 * mm
    box_w = page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN - 20 * mm

    # Accent vertical bar inside safe left margin
    c.setFillColor(colors.HexColor("#173E35"))
    c.rect(LEFT_BIND_MARGIN + 2 * mm, page_h * 0.25, 2.2 * mm, page_h * 0.50, stroke=0, fill=1)

    c.setFont(regular, 10.5)
    c.setFillColor(colors.HexColor("#475569"))
    group_style = ParagraphStyle("div_group", fontName=regular, fontSize=10.5, leading=15,
                                 textColor=colors.HexColor("#475569"))
    p_group = _wrap_paragraphs(f"第 {item.group_no} 组 · {item.group_title}", group_style)
    _, gh = p_group.wrap(box_w, 30 * mm)
    y_cursor = page_h * 0.72
    p_group.drawOn(c, x0, y_cursor - gh)
    y_cursor -= gh + 10 * mm

    c.setFillColor(colors.HexColor("#173E35"))
    c.setFont(bold, 28)
    c.drawString(x0, y_cursor - 8 * mm, item.code)
    y_cursor -= 20 * mm

    title_style = ParagraphStyle("div_title", fontName=bold, fontSize=18, leading=26,
                                 textColor=colors.HexColor("#0F172A"))
    p_title = _wrap_paragraphs(item.title, title_style)
    _, th = p_title.wrap(box_w, 60 * mm)
    p_title.drawOn(c, x0, y_cursor - th)
    y_cursor -= th + 10 * mm

    c.setStrokeColor(colors.HexColor("#CBD5E1"))
    c.line(x0, y_cursor, x0 + box_w, y_cursor)
    y_cursor -= 10 * mm

    row_style = ParagraphStyle("div_row", fontName=regular, fontSize=11, leading=17,
                               textColor=colors.HexColor("#1E293B"))
    purpose = item.purpose or "（未填写证明目的）"
    sources = "；".join(item.source_names) if item.source_names else "（未绑定材料）"
    for label, val in (
        ("证明目的：", purpose),
        ("卷内页码：", item.page_range_text),
        ("对应材料：", sources),
    ):
        markup = (
            f"<b>{escape(label)}</b>{escape(val).replace(chr(10), '<br/>')}"
            if bold != regular
            else escape(f"{label}{val}").replace("\n", "<br/>")
        )
        p = Paragraph(markup, row_style)
        avail_h = y_cursor - (BOTTOM_MARGIN + 4 * mm)
        if avail_h <= 12:
            break
        _, mh = p.wrap(box_w, avail_h)
        if mh > avail_h:
            break
        p.drawOn(c, x0, y_cursor - mh)
        y_cursor -= mh + 5 * mm

    c.setFont(regular, 8.5)
    c.setFillColor(colors.HexColor("#64748B"))
    c.drawString(x0, FOOTER_Y, "证据分隔页（不计入证据正文连续页码）")

    c.showPage()
    c.save()
    return _clean_pdf_bytes_reader(buf.getvalue()).pages[0]


def _format_party_summary(plaintiff: str | None, defendant: str | None) -> str:
    parts = []
    if plaintiff:
        parts.append(f"原告：{plaintiff}")
    if defendant:
        parts.append(f"被告：{defendant}")
    return "  /  ".join(parts)


def _make_cover_page(
    case_title: str, group_count: int, item_count: int,
    body_page_count: int, date_cn: str, *,
    case_number: str | None = None,
    plaintiff: str | None = None,
    defendant: str | None = None,
) -> PageObject:
    """Build the A4 court submission cover page, pre-filling case number and parties if set."""
    regular, bold = _ensure_fonts()
    page_w, page_h = A4_PORTRAIT
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4_PORTRAIT)

    center_x = LEFT_BIND_MARGIN + (page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN) / 2.0
    box_w = page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN - 18 * mm
    x0 = LEFT_BIND_MARGIN + 9 * mm

    # Subtle vertical binding-line mark (left 20mm)
    c.setStrokeColor(colors.HexColor("#CBD5E1"))
    c.setDash(3, 3)
    c.line(LEFT_BIND_MARGIN - 2 * mm, 18 * mm, LEFT_BIND_MARGIN - 2 * mm, page_h - 18 * mm)
    c.setDash()

    c.setFillColor(colors.HexColor("#0F172A"))
    c.setFont(bold, 28)
    c.drawCentredString(center_x, page_h - 56 * mm, "证  据  材  料")

    c.setStrokeColor(colors.HexColor("#173E35"))
    c.setLineWidth(1.5)
    c.line(center_x - 38 * mm, page_h - 62 * mm, center_x + 38 * mm, page_h - 62 * mm)

    case_style = ParagraphStyle("cover_case", fontName=bold, fontSize=15, leading=22,
                                alignment=1, textColor=colors.HexColor("#1E293B"))
    p_case = _wrap_paragraphs(case_title, case_style)
    _, ch = p_case.wrap(box_w, 50 * mm)
    p_case.drawOn(c, x0, page_h - 86 * mm - ch)

    fields_y = page_h - 118 * mm - ch
    c.setFont(regular, 12)
    c.setFillColor(colors.HexColor("#1E293B"))
    c.setStrokeColor(colors.HexColor("#64748B"))
    c.setLineWidth(0.7)
    val_style = ParagraphStyle("cover_val", fontName=regular, fontSize=11.5, leading=16,
                               textColor=colors.HexColor("#0F172A"))
    party_summary = _format_party_summary(plaintiff, defendant)
    rows = [
        ("案        号：", case_number or None),
        ("当  事  人：" if party_summary else "提交主体：", party_summary or None),
        ("提交人/代理律师：", None),
    ]
    for label, value in rows:
        c.drawString(x0, fields_y, label)
        if value:
            p_val = _wrap_paragraphs(value, val_style)
            val_w = box_w - 36 * mm
            _, vh = p_val.wrap(val_w, 30 * mm)
            p_val.drawOn(c, x0 + 36 * mm, fields_y - vh + 11.5)
            fields_y -= max(14 * mm, vh + 6 * mm)
        else:
            c.line(x0 + 36 * mm, fields_y - 1.5 * mm, x0 + box_w, fields_y - 1.5 * mm)
            fields_y -= 14 * mm

    c.setFont(regular, 10.5)
    c.setFillColor(colors.HexColor("#475569"))
    summary_y = 60 * mm
    c.drawCentredString(
        center_x, summary_y,
        f"本卷共 {group_count} 组、{item_count} 项已确认证据，正文连续编页共 {body_page_count} 页",
    )
    c.drawCentredString(center_x, summary_y - 9 * mm, f"装卷日期：{date_cn}")
    c.setFont(regular, 9)
    c.setFillColor(colors.HexColor("#64748B"))
    c.drawCentredString(center_x, 22 * mm, "提示：证据封面、目录及分隔页均不占用正文页码；每页右上角已预留法院归档编码区")

    c.showPage()
    c.save()
    return _clean_pdf_bytes_reader(buf.getvalue()).pages[0]


class _NumberedTocCanvas(rl_canvas.Canvas):
    """Two-pass canvas to render `目录 第 X 页 / 共 Y 页` at bottom center."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_page_states)
        regular, _ = _ensure_fonts()
        center_x = LEFT_BIND_MARGIN + (A4_PORTRAIT[0] - LEFT_BIND_MARGIN - RIGHT_MARGIN) / 2.0
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.setFont(regular, 8.5)
            self.setFillColor(colors.HexColor("#475569"))
            self.drawCentredString(center_x, FOOTER_Y, f"目录 第 {self._pageNumber} 页 / 共 {total} 页")
            super().showPage()
        super().save()


def _make_toc_pages(case_title: str, items: list[_ItemAssembly]) -> list[PageObject]:
    """Generate multi-page A4 grouped Evidence Catalog TOC with exact page ranges."""
    regular, bold = _ensure_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4_PORTRAIT,
        leftMargin=LEFT_BIND_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN + 4 * mm,
        bottomMargin=BOTTOM_MARGIN + 4 * mm,
    )

    title_style = ParagraphStyle("toc_title", fontName=bold, fontSize=16, leading=22,
                                 alignment=1, textColor=colors.HexColor("#0F172A"))
    sub_style = ParagraphStyle("toc_sub", fontName=regular, fontSize=9.5, leading=14,
                               alignment=1, textColor=colors.HexColor("#475569"))
    head_style = ParagraphStyle("toc_head", fontName=bold, fontSize=9.5, leading=13,
                                textColor=colors.HexColor("#0F172A"))
    group_style = ParagraphStyle("toc_group", fontName=bold, fontSize=9.5, leading=13,
                                 textColor=colors.HexColor("#173E35"))
    cell_style = ParagraphStyle("toc_cell", fontName=regular, fontSize=9, leading=13,
                                textColor=colors.HexColor("#1E293B"))
    page_cell_style = ParagraphStyle("toc_page", fontName=regular, fontSize=9, leading=13,
                                     alignment=1, textColor=colors.HexColor("#0F172A"))
    sign_style = ParagraphStyle("toc_sign", fontName=regular, fontSize=10.5, leading=18,
                                textColor=colors.HexColor("#0F172A"))

    # Total usable width = 210 - 20 - 15 = 175 mm
    col_widths = [21 * mm, 41 * mm, 26 * mm, 23 * mm, 64 * mm]
    table_data: list[list[Any]] = [[
        Paragraph("证据编号", head_style),
        Paragraph("证据名称", head_style),
        Paragraph("形式/来源", head_style),
        Paragraph("页码", ParagraphStyle("toc_head_c", parent=head_style, alignment=1)),
        Paragraph("证明目的", head_style),
    ]]
    style_cmds: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EFEA")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#94A3B8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4.5),
    ]

    last_group_no: int | None = None
    for item in items:
        if item.group_no != last_group_no:
            last_group_no = item.group_no
            row_idx = len(table_data)
            table_data.append([
                _wrap_paragraphs(f"第 {item.group_no} 组：{item.group_title}", group_style),
                "", "", "", "",
            ])
            style_cmds.append(("SPAN", (0, row_idx), (-1, row_idx)))
            style_cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), colors.HexColor("#F4F6F4")))
        table_data.append([
            _wrap_paragraphs(item.code, cell_style),
            _wrap_paragraphs(item.title, cell_style),
            _wrap_paragraphs(item.form_label, cell_style),
            _wrap_paragraphs(item.page_range_text, page_cell_style),
            _wrap_paragraphs(item.purpose or "—", cell_style),
        ])

    table = LongTable(table_data, colWidths=col_widths, repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle(style_cmds))

    story: list[Any] = [
        _wrap_paragraphs("证  据  目  录", title_style),
        Spacer(1, 2 * mm),
        _wrap_paragraphs(f"案件名称：{case_title}", sub_style),
        Spacer(1, 4 * mm),
        table,
        Spacer(1, 8 * mm),
        _wrap_paragraphs(
            "提交人（签字/盖章）：__________________________    提交日期：____ 年 __ 月 __ 日",
            sign_style,
        ),
    ]
    doc.build(story, canvasmaker=_NumberedTocCanvas)
    return list(_clean_pdf_bytes_reader(buf.getvalue()).pages)


def _place_pdf_page_onto_a4(src_page: PageObject) -> tuple[PageObject, tuple[float, float]]:
    """Vector-scale a PDF page into A4 safe margins, honoring /Rotate and cropbox."""
    tmp_writer = PdfWriter()
    tmp_writer.add_page(src_page)
    cloned = tmp_writer.pages[0]
    cloned.transfer_rotation_to_content()
    box = cloned.cropbox
    src_w = float(box.width)
    src_h = float(box.height)
    if src_w <= 1.0 or src_h <= 1.0:
        box = cloned.mediabox
        src_w = float(box.width)
        src_h = float(box.height)
    if src_w <= 1.0 or src_h <= 1.0:
        raise ValueError("Source PDF page has empty dimensions")

    left = float(box.left)
    bottom = float(box.bottom)
    pagesize = A4_LANDSCAPE if src_w > src_h * 1.15 else A4_PORTRAIT
    page_w, page_h = pagesize
    avail_w = page_w - LEFT_BIND_MARGIN - RIGHT_MARGIN
    avail_h = page_h - TOP_MARGIN - BOTTOM_MARGIN
    scale = min(avail_w / src_w, avail_h / src_h)
    tx = LEFT_BIND_MARGIN + (avail_w - src_w * scale) / 2.0
    ty = BOTTOM_MARGIN + (avail_h - src_h * scale) / 2.0

    target = PageObject.create_blank_page(width=page_w, height=page_h)
    transform = Transformation().translate(-left, -bottom).scale(scale, scale).translate(tx, ty)
    target.merge_transformed_page(cloned, transform)
    return target, pagesize


def _rasterize_pdf_page_to_a4(pdf_bytes: bytes, page_index: int) -> tuple[PageObject, tuple[float, float]]:
    """Fallback renderer using PDFium (under the process-wide lock) when vector merge fails."""
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        page = None
        bitmap = None
        try:
            page = doc[page_index]
            bitmap = page.render(scale=180.0 / 72.0)
            pil_image = bitmap.to_pil()
        finally:
            if bitmap is not None:
                bitmap.close()
            if page is not None:
                page.close()
            doc.close()
    return _make_a4_image_page(pil_image)


def _stamp_page(base_page: PageObject, pagesize: tuple[float, float],
                *, code: str, page_no: int) -> PageObject:
    stamp = _make_stamp_page(pagesize[0], pagesize[1], left_text=code, right_text=f"第 {page_no} 页")
    writer = PdfWriter()
    writer.add_page(base_page)
    cloned = writer.pages[0]
    cloned.merge_page(stamp)
    return cloned


_EXTENDED_PROPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"


def _software_version(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _format_pdf_datetime(dt: datetime) -> str:
    """Format an aware datetime as a PDF date string, e.g. D:20261125193000+08'00'."""
    base = dt.strftime("D:%Y%m%d%H%M%S")
    offset = dt.utcoffset()
    if offset is None:
        return base + "Z"
    total_minutes = int(offset.total_seconds() // 60)
    if total_minutes == 0:
        return base + "Z"
    sign = "+" if total_minutes >= 0 else "-"
    abs_minutes = abs(total_minutes)
    hours, minutes = divmod(abs_minutes, 60)
    return f"{base}{sign}{hours:02d}'{minutes:02d}'"


def _pdf_metadata(
    *,
    producer: str,
    title: str | None = None,
    subject: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, str]:
    meta: dict[str, str] = {
        "/Creator": producer,
        "/Producer": producer,
    }
    if title:
        meta["/Title"] = _sanitize_xml_text(title)
    if subject:
        meta["/Subject"] = _sanitize_xml_text(subject)
    if created_at is not None:
        pdf_dt = _format_pdf_datetime(created_at)
        meta["/CreationDate"] = pdf_dt
        meta["/ModDate"] = pdf_dt
    return meta


def _apply_docx_metadata(
    doc: Any,
    *,
    producer: str,
    title: str,
    subject: str,
    created_at: datetime | None = None,
) -> None:
    """Populate standard OOXML core and extended software properties."""
    core = doc.core_properties
    core.author = SOFTWARE_NAME
    core.last_modified_by = SOFTWARE_NAME
    core.title = _sanitize_xml_text(title)
    core.subject = _sanitize_xml_text(subject)
    core.comments = _sanitize_xml_text(f"Generated by {producer}")
    if created_at is not None:
        naive_utc = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        core.created = naive_utc
        core.modified = naive_utc

    for part in doc.part.package.iter_parts():
        if str(part.partname) == "/docProps/app.xml":
            root = etree.fromstring(part.blob)
            app_el = root.find(f"{{{_EXTENDED_PROPS_NS}}}Application")
            if app_el is None:
                app_el = etree.SubElement(root, f"{{{_EXTENDED_PROPS_NS}}}Application")
            app_el.text = _sanitize_xml_text(producer)
            part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            break


def _pages_to_pdf_bytes(
    pages: list[PageObject],
    *,
    producer: str,
    title: str | None = None,
    subject: str | None = None,
    created_at: datetime | None = None,
) -> bytes:
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    writer.add_metadata(_pdf_metadata(
        producer=producer, title=title, subject=subject, created_at=created_at,
    ))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _set_cell_shading(cell: Any, fill_hex: str) -> None:
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _set_cell_font(run: Any, *, size_pt: float, bold: bool = False,
                   color: tuple[int, int, int] = (30, 41, 59)) -> None:
    run.font.name = "SimSun"
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    run.font.color.rgb = RGBColor(*color)
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), "SimSun")
    rfonts.set(qn("w:ascii"), "SimSun")
    rfonts.set(qn("w:hAnsi"), "SimSun")


def _sanitize_xml_text(text: str | None) -> str:
    """Strip XML 1.0 illegal control characters before writing Word (.docx) runs."""
    if not text:
        return ""
    normalized = text.replace("\x0b", " ").replace("\x0c", " ")
    return _XML_ILLEGAL_RE.sub("", normalized)


def _csv_safe_cell(value: object) -> object:
    """Neutralize spreadsheet formula injection payloads on CSV export."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped and stripped[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{value}"
    return value


def _check_online_size_warnings(warnings: list[str], entry_name: str, byte_len: int) -> None:
    if byte_len > ONLINE_MAX_BYTES:
        warnings.append(
            f"分项文件「{entry_name}」大小为 {byte_len / (1024 * 1024):.1f} MB，已超过常见法院在线平台 100 MB 上限，上传前请拆分分册。"
        )
    elif byte_len > ONLINE_WARN_BYTES:
        warnings.append(
            f"分项文件「{entry_name}」大小为 {byte_len / (1024 * 1024):.1f} MB，超过部分地方法院 50 MB 建议上限。"
        )


def _build_docx_catalog(
    case_title: str, items: list[_ItemAssembly], *,
    producer: str,
    case_number: str | None = None,
    plaintiff: str | None = None,
    defendant: str | None = None,
    created_at: datetime | None = None,
) -> bytes:
    """Create editable court-standard Word Evidence Catalog (.docx)."""
    doc = DocxDocument()
    catalog_title = f"{case_title} - 证据目录" if (case_title or "").strip() else "证据目录"
    _apply_docx_metadata(
        doc, producer=producer, title=catalog_title, subject="证据目录", created_at=created_at,
    )
    sec = doc.sections[0]
    sec.page_width = Mm(210)
    sec.page_height = Mm(297)
    sec.left_margin = Mm(25)
    sec.right_margin = Mm(18)
    sec.top_margin = Mm(20)
    sec.bottom_margin = Mm(20)

    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_title.paragraph_format.space_after = Pt(6)
    r_title = p_title.add_run("证  据  目  录")
    _set_cell_font(r_title, size_pt=18, bold=True, color=(15, 23, 42))

    party_summary = _format_party_summary(plaintiff, defendant)
    meta_lines = [
        f"案件名称：{_sanitize_xml_text(case_title)}",
        f"案    号：{_sanitize_xml_text(case_number) or '__________________________'}    提交主体：{_sanitize_xml_text(party_summary) or '________________'}",
    ]
    for meta_line in meta_lines:
        p_meta = doc.add_paragraph()
        p_meta.paragraph_format.space_after = Pt(3)
        r_meta = p_meta.add_run(_sanitize_xml_text(meta_line))
        _set_cell_font(r_meta, size_pt=10.5)

    widths = (Mm(22), Mm(38), Mm(27), Mm(22), Mm(58))
    headers = ("证据编号", "证据名称", "形式/来源", "页码", "证明目的")
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    hdr_row = table.rows[0]
    tr_pr = hdr_row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:tblHeader"))
    for idx, text in enumerate(headers):
        cell = hdr_row.cells[idx]
        cell.width = widths[idx]
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _set_cell_shading(cell, "E8EFEA")
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if idx == 3 else WD_ALIGN_PARAGRAPH.LEFT
        run = p.add_run(_sanitize_xml_text(text))
        _set_cell_font(run, size_pt=10, bold=True, color=(15, 23, 42))

    last_group_no: int | None = None
    for item in items:
        if item.group_no != last_group_no:
            last_group_no = item.group_no
            grow = table.add_row()
            merged = grow.cells[0].merge(grow.cells[4])
            merged.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            _set_cell_shading(merged, "F4F6F4")
            gp = merged.paragraphs[0]
            grun = gp.add_run(_sanitize_xml_text(f"第 {item.group_no} 组：{item.group_title}"))
            _set_cell_font(grun, size_pt=10, bold=True, color=(23, 62, 53))

        row = table.add_row()
        values = (
            item.code,
            item.title,
            item.form_label,
            item.page_range_text,
            item.purpose or "—",
        )
        for idx, text in enumerate(values):
            cell = row.cells[idx]
            cell.width = widths[idx]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if idx == 3 else WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(_sanitize_xml_text(text))
            _set_cell_font(run, size_pt=9.5)

    p_sign = doc.add_paragraph()
    p_sign.paragraph_format.space_before = Pt(14)
    r_sign = p_sign.add_run("提交人（签字/盖章）：______________________        提交日期：____ 年 __ 月 __ 日")
    _set_cell_font(r_sign, size_pt=10.5)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _build_csv_catalog(items: list[_ItemAssembly]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "组号", "组名", "证据编号", "证据名称", "形式/来源",
        "合订本页码", "证明目的", "对应材料", "分项文件",
    ])
    for item in items:
        writer.writerow([
            item.group_no,
            _csv_safe_cell(item.group_title),
            _csv_safe_cell(item.code),
            _csv_safe_cell(item.title),
            _csv_safe_cell(item.form_label),
            _csv_safe_cell(item.page_range_text),
            _csv_safe_cell(item.purpose or ""),
            _csv_safe_cell("；".join(item.source_names) if item.source_names else "（未绑定）"),
            _csv_safe_cell("；".join(item.standalone_entries) if item.standalone_entries else ""),
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _compose_filename(prefix: str, title: str, ext: str = "") -> str:
    """Flatten directory separators inside titles so safe_name preserves prefix and ext."""
    clean_title = (title or "").replace("/", "_").replace("\\", "_")
    return f"{prefix}_{clean_title}{ext}"


def _unique_entry_name(used: set[str], desired: str) -> str:
    """Generate a unique Windows/macOS-safe path under `02_分项证据材料/`."""
    candidate = safe_name(desired, fallback="evidence")
    if candidate.lower() not in used:
        used.add(candidate.lower())
        return candidate
    stem, dot, ext = candidate.rpartition(".")
    if not dot:
        stem, ext = candidate, ""
    else:
        ext = "." + ext
    idx = 2
    while True:
        suffix = f"_{idx}"
        trimmed = stem[: max(1, 120 - len(suffix) - len(ext))] + suffix + ext
        if trimmed.lower() not in used:
            used.add(trimmed.lower())
            return trimmed
        idx += 1


def _infer_form_label(acquisitions: list[str | None], exts: list[str]) -> str:
    explicit = [a.strip() for a in acquisitions if a and a.strip()]
    if explicit:
        return "、".join(dict.fromkeys(explicit))
    if not exts:
        return "待补充原件"
    has_pdf_img = any(e in PDF_SUFFIXES or e in IMAGE_SUFFIXES for e in exts)
    has_office = any(e not in PDF_SUFFIXES and e not in IMAGE_SUFFIXES for e in exts)
    if has_pdf_img and not has_office:
        return "扫描件/图片"
    if has_office and not has_pdf_img:
        return "电子文档"
    return "混合文件"


def _binding_ext(binding: BindingSpec) -> str:
    if binding.ext:
        cleaned = binding.ext.strip().lower()
        return cleaned if cleaned.startswith(".") else f".{cleaned}"
    return PurePosixPath(str(binding.file_path).replace("\\", "/")).suffix.lower()


def _verify_binding_digest(path: Path, binding: BindingSpec) -> None:
    """独立模式下清单可以自带 SHA-256/大小；对不上就拒绝装卷。"""
    if binding.size is not None:
        actual = path.stat().st_size
        if actual != binding.size:
            raise DossierError(
                "integrity_error",
                f"材料「{binding.material_name}」大小与清单不一致（期望 {binding.size} 字节，实际 {actual} 字节），已阻止装卷",
                {"file_path": str(path), "expected_size": binding.size, "actual_size": actual},
            )
    if binding.sha256:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_hex = digest.hexdigest()
        if actual_hex.lower() != binding.sha256.strip().lower():
            raise DossierError(
                "integrity_error",
                f"材料「{binding.material_name}」完整性校验失败（SHA-256 不匹配），已阻止装卷",
                {"file_path": str(path), "expected_sha256": binding.sha256,
                 "actual_sha256": actual_hex},
            )


def _resolve_binding(
    binding: BindingSpec,
    *,
    path_resolver: Callable[[BindingSpec], Path] | None,
    roots: tuple[Path, ...],
    base_dir: Path | None,
    verify_digest: bool,
) -> _ResolvedBinding:
    if path_resolver is not None:
        resolved = path_resolver(binding)
        path = Path(resolved)
        if not path.is_file():
            raise DossierError(
                "not_found",
                f"材料「{binding.material_name}」的本地文件不存在：{path}",
                {"file_path": str(path)},
            )
    else:
        path = path_fence.resolve_readable_file(binding.file_path, roots, base_dir=base_dir)
    if verify_digest:
        _verify_binding_digest(path, binding)
    return _ResolvedBinding(
        page_start=binding.page_start,
        page_end=binding.page_end,
        material_name=binding.material_name,
        acquisition=binding.acquisition,
        path=path,
        ext=_binding_ext(binding),
    )


def build_dossier(
    spec: DossierSpec,
    *,
    path_resolver: Callable[[BindingSpec], Path] | None = None,
    allowed_roots: tuple[Path, ...] | None = None,
    base_dir: Path | None = None,
    software_version: str | None = None,
    now: datetime | None = None,
) -> DossierResult:
    """Assemble every group/item/binding into a Chinese-court-ready ZIP dossier.

    ``path_resolver`` is the workbench seam (``Storage.verify`` + ``managed_path``);
    without it every binding goes through the 方案 A file fence rooted at
    ``allowed_roots`` (defaults to the process working directory).
    """
    roots: tuple[Path, ...] = tuple(allowed_roots or path_fence.allowed_roots())

    ordered: list[tuple[int, str, Any, list[_ResolvedBinding]]] = []
    for group_index, group in enumerate(spec.groups, start=1):
        if not group.items:
            continue
        for item in group.items:
            bindings = [
                _resolve_binding(
                    binding,
                    path_resolver=path_resolver,
                    roots=roots,
                    base_dir=base_dir,
                    verify_digest=path_resolver is None,
                )
                for binding in item.bindings
            ]
            ordered.append((group_index, group.title, item, bindings))

    if not ordered:
        raise DossierError(
            "invalid_input",
            "暂无已确认的证据条目，请先确认证据目录后再装卷",
            {"group_count": len(spec.groups)},
        )

    producer = f"{SOFTWARE_NAME} {_software_version(software_version)}"
    place_page, rasterize_page = _render_functions()

    # 1. Pass 1: assemble body pages & standalone split files
    now_cn = now or datetime.now(TZ_SHANGHAI)
    date_tag = now_cn.strftime("%Y%m%d")
    date_cn = now_cn.strftime("%Y年%m月%d日")

    assemblies: list[_ItemAssembly] = []
    used_split_names: set[str] = set()
    standalone_blobs: list[tuple[str, bytes]] = []
    reminders: list[str] = []
    warnings: list[str] = []
    next_body_page = 1

    current_group: int | None = None
    item_no = 0
    for grp_no, grp_title, item, item_bindings in ordered:
        if grp_no != current_group:
            current_group = grp_no
            item_no = 0
        item_no += 1
        code = f"证据{grp_no}-{item_no}"

        source_names: list[str] = []
        acquisitions: list[str | None] = []
        exts: list[str] = []
        content_pairs: list[tuple[PageObject, tuple[float, float]]] = []
        standalone_pdf_pages: list[PageObject] = []
        extra_non_pdf_files: list[tuple[str, bytes]] = []

        if not item_bindings:
            page, sz = _make_notice_page(
                code=code, title=item.title, badge="材料缺失占位页",
                heading="本条已确认证据暂未绑定原件材料",
                lines=[
                    "请在「证据目录」中为本条目绑定对应的 PDF、图片或电子文档后重新装卷。",
                ],
            )
            content_pairs.append((page, sz))
            reminders.append(f"{code}（{item.title}）：尚未绑定任何材料，合订本中已插入占位提示页。")
        else:
            for b in item_bindings:
                disk_path = b.path
                ext = b.ext
                acquisitions.append(b.acquisition)
                exts.append(ext)
                if b.page_start is not None:
                    end_txt = b.page_end or b.page_start
                    page_desc = f"（第{b.page_start}-{end_txt}页）" if end_txt != b.page_start else f"（第{b.page_start}页）"
                else:
                    page_desc = ""
                source_names.append(f"{b.material_name}{page_desc}")

                if ext in PDF_SUFFIXES:
                    raw_bytes = disk_path.read_bytes()
                    reader: PdfReader | None = None
                    try:
                        reader = _clean_pdf_bytes_reader(raw_bytes)
                        if reader.is_encrypted:
                            if reader.decrypt("") == 0:
                                reader = None
                    except Exception:
                        reader = None

                    if reader is None or len(reader.pages) == 0:
                        msg = f"材料「{b.material_name}」加密或无法解析为 PDF，请在分项材料或本地查看原件。"
                        page, sz = _make_notice_page(
                            code=code, title=item.title, badge="原件提示页",
                            heading=f"PDF 无法直接合订：{b.material_name}",
                            lines=[msg],
                        )
                        content_pairs.append((page, sz))
                        entry_name = _unique_entry_name(used_split_names, _compose_filename(code, item.title, ext))
                        extra_non_pdf_files.append((entry_name, raw_bytes))
                        warnings.append(f"{code}：{msg}")
                        continue

                    total_src = len(reader.pages)
                    start_p = b.page_start if b.page_start is not None else 1
                    end_p = (b.page_end or start_p) if b.page_start is not None else total_src
                    if start_p > total_src:
                        msg = f"绑定页码（第{start_p}-{end_p}页）超出原件「{b.material_name}」总页数（{total_src}页）。"
                        page, sz = _make_notice_page(
                            code=code, title=item.title, badge="页码越界提示页",
                            heading="绑定的页码范围超出原件实际页数",
                            lines=[msg],
                        )
                        content_pairs.append((page, sz))
                        warnings.append(f"{code}：{msg}")
                        continue
                    if end_p > total_src:
                        warnings.append(
                            f"{code}：原件「{b.material_name}」共 {total_src} 页，已将绑定结束页 {end_p} 自动截断为 {total_src}。"
                        )
                        end_p = total_src

                    had_failed_page = False
                    for p_idx in range(start_p - 1, end_p):
                        try:
                            placed, sz = place_page(reader.pages[p_idx])
                        except Exception:
                            try:
                                placed, sz = rasterize_page(raw_bytes, p_idx)
                            except Exception:
                                had_failed_page = True
                                msg = f"材料「{b.material_name}」第 {p_idx + 1} 页无法渲染，请在分项材料或本地查看原件。"
                                placed, sz = _make_notice_page(
                                    code=code, title=item.title, badge="原件提示页",
                                    heading=f"PDF 页渲染失败：{b.material_name}",
                                    lines=[msg],
                                )
                                warnings.append(f"{code}：{msg}")
                                content_pairs.append((placed, sz))
                                continue
                        # _stamp_page clones `placed` via PdfWriter, so `placed` stays independent
                        content_pairs.append((placed, sz))
                        standalone_pdf_pages.append(placed)

                    if had_failed_page:
                        entry_name = _unique_entry_name(used_split_names, _compose_filename(code, item.title, ext))
                        extra_non_pdf_files.append((entry_name, raw_bytes))
                        warnings.append(
                            f"{code}：PDF「{b.material_name}」存在渲染失败页，已同步保留原 PDF 至 02_分项证据材料/{entry_name}。"
                        )

                elif ext in IMAGE_SUFFIXES:
                    try:
                        frame_content: list[tuple[PageObject, tuple[float, float]]] = []
                        frame_standalone: list[PageObject] = []
                        with Image.open(disk_path) as img:
                            n_frames = getattr(img, "n_frames", 1)
                            if n_frames > MAX_IMAGE_FRAMES:
                                raise ValueError(f"Image exceeds max frame limit ({n_frames} > {MAX_IMAGE_FRAMES})")
                            for frame_idx in range(n_frames):
                                if n_frames > 1:
                                    img.seek(frame_idx)
                                oriented = ImageOps.exif_transpose(img.copy())
                                placed, sz = _make_a4_image_page(oriented)
                                frame_content.append((placed, sz))
                                frame_standalone.append(placed)
                        content_pairs.extend(frame_content)
                        standalone_pdf_pages.extend(frame_standalone)
                    except (EOFError, OSError, Image.DecompressionBombError, ValueError):
                        raw_bytes = disk_path.read_bytes()
                        entry_name = _unique_entry_name(used_split_names, _compose_filename(code, item.title, ext))
                        extra_non_pdf_files.append((entry_name, raw_bytes))
                        msg = f"图片材料「{b.material_name}」损坏、帧数过多或尺寸过大，已附原文件至 02_分项证据材料/{entry_name}。"
                        page, sz = _make_notice_page(
                            code=code, title=item.title, badge="原件提示页",
                            heading=f"图片无法合订：{b.material_name}",
                            lines=[msg],
                        )
                        content_pairs.append((page, sz))
                        warnings.append(f"{code}：{msg}")
                else:
                    raw_bytes = disk_path.read_bytes()
                    entry_name = _unique_entry_name(used_split_names, _compose_filename(code, item.title, ext))
                    extra_non_pdf_files.append((entry_name, raw_bytes))
                    page, sz = _make_notice_page(
                        code=code, title=item.title, badge="电子原件另附提示页",
                        heading="本证据为可编辑电子文档，原件已随卷宗包单独打包",
                        lines=[
                            f"原件名称：{b.material_name}",
                            f"分项文件：02_分项证据材料/{entry_name}",
                            "说明：为完整保留 Word/Excel/文本原始格式与排版，程序不强制转码；打印或网上提交前请查看或用本机办公软件另存为 PDF。",
                        ],
                    )
                    content_pairs.append((page, sz))
                    reminders.append(
                        f"{code}（{item.title}）：包含非 PDF/图片文件「{b.material_name}」，已保留原件至 02_分项证据材料/{entry_name}。"
                    )

        start_num = next_body_page
        stamped_pages: list[PageObject] = []
        for base_page, sz in content_pairs:
            stamped = _stamp_page(base_page, sz, code=code, page_no=next_body_page)
            stamped_pages.append(stamped)
            next_body_page += 1
        end_num = next_body_page - 1

        entry_list: list[str] = []
        if standalone_pdf_pages:
            pdf_name = _unique_entry_name(used_split_names, _compose_filename(code, item.title, ".pdf"))
            pdf_data = _pages_to_pdf_bytes(
                standalone_pdf_pages,
                producer=producer,
                title=f"{code} {item.title}",
                subject="分项证据材料",
                created_at=now_cn,
            )
            standalone_blobs.append((f"02_分项证据材料/{pdf_name}", pdf_data))
            entry_list.append(pdf_name)
            _check_online_size_warnings(warnings, pdf_name, len(pdf_data))
        for non_pdf_name, non_pdf_bytes in extra_non_pdf_files:
            standalone_blobs.append((f"02_分项证据材料/{non_pdf_name}", non_pdf_bytes))
            entry_list.append(non_pdf_name)
            _check_online_size_warnings(warnings, non_pdf_name, len(non_pdf_bytes))

        assemblies.append(_ItemAssembly(
            code=code,
            group_no=grp_no,
            group_title=grp_title,
            title=item.title,
            purpose=item.purpose,
            form_label=_infer_form_label(acquisitions, exts),
            source_names=source_names,
            standalone_entries=entry_list,
            page_start=start_num,
            page_end=end_num,
            content_pages=stamped_pages,
        ))

    total_body_pages = next_body_page - 1
    used_group_count = len({a.group_no for a in assemblies})
    case = spec.case

    # 2. Pass 2: build Cover, TOC, Dividers, Bookmarks and assemble 00_证据合订卷宗.pdf
    cover_page = _make_cover_page(
        case.title, used_group_count, len(assemblies), total_body_pages, date_cn,
        case_number=case.case_number, plaintiff=case.plaintiff, defendant=case.defendant,
    )
    toc_pages = _make_toc_pages(case.title, assemblies)

    merged_writer = PdfWriter()
    merged_writer.add_page(cover_page)
    merged_writer.add_outline_item(title="封面", page_number=0)

    toc_start_idx = len(merged_writer.pages)
    for tp in toc_pages:
        merged_writer.add_page(tp)
    merged_writer.add_outline_item(title="证据目录", page_number=toc_start_idx)

    group_outlines: dict[int, Any] = {}
    for asm in assemblies:
        divider_idx = len(merged_writer.pages)
        merged_writer.add_page(_make_divider_page(asm))
        if asm.group_no not in group_outlines:
            group_outlines[asm.group_no] = merged_writer.add_outline_item(
                title=f"第 {asm.group_no} 组：{asm.group_title}", page_number=divider_idx,
            )
        merged_writer.add_outline_item(
            title=f"{asm.code} {asm.title}（{asm.page_range_text}）",
            page_number=divider_idx,
            parent=group_outlines[asm.group_no],
        )
        for content_page in asm.content_pages:
            merged_writer.add_page(content_page)

    merged_title = f"{case.title} - 证据合订卷宗" if (case.title or "").strip() else "证据合订卷宗"
    merged_writer.add_metadata(_pdf_metadata(
        producer=producer,
        title=merged_title,
        subject="证据合订卷宗",
        created_at=now_cn,
    ))
    merged_buf = io.BytesIO()
    merged_writer.write(merged_buf)
    merged_pdf_bytes = merged_buf.getvalue()
    total_pdf_pages = len(merged_writer.pages)

    if len(merged_pdf_bytes) > ONLINE_MAX_BYTES:
        warnings.append(
            f"整本合订卷宗「00_证据合订卷宗.pdf」为 {len(merged_pdf_bytes) / (1024 * 1024):.1f} MB，网上立案逐条上传建议使用「02_分项证据材料/」中的单份 PDF。"
        )

    # 3. Editable DOCX catalog, CSV catalog & README instructions
    docx_bytes = _build_docx_catalog(
        case.title, assemblies,
        producer=producer,
        case_number=case.case_number, plaintiff=case.plaintiff, defendant=case.defendant,
        created_at=now_cn,
    )
    csv_bytes = _build_csv_catalog(assemblies)

    readme_lines = [
        f"案件名称：{case.title}",
        f"装卷时间：{now_cn.strftime('%Y-%m-%d %H:%M')}",
        f"装卷范围：仅纳入已确认证据（共 {used_group_count} 组 / {len(assemblies)} 项 / 正文 {total_body_pages} 页 / 合订 PDF 共 {total_pdf_pages} 页）",
        "",
        "【压缩包内容说明】",
        "1. 00_证据合订卷宗.pdf —— 已按中国法院常规装订标准排版（A4 幅面、左侧预留 20mm 装订线、封面+精确页码目录+每项分隔页+右下角正文连续页码、右上角严格留白供书记员归档编码），可直接打印装订或整本阅览。",
        "2. 01_证据目录.docx —— 可编辑 Word 版法院表格目录（页码已与合订本完全对齐，可自行补充案号、提交人及落款日期后打印）。",
        "3. 01_证据目录.csv —— 结构化目录表，方便导入 Excel 核对。",
        "4. 02_分项证据材料/ —— 已按「证据组-序_名称」自动命名排好序的单份材料，可直接用于「人民法院在线服务」或各地电子诉讼平台逐条上传。",
    ]
    if reminders:
        readme_lines.extend(["", "【另附原件与待补材料提醒】"])
        readme_lines.extend(f"- {line}" for line in reminders)
    if warnings:
        readme_lines.extend(["", "【格式与上传大小提示】"])
        readme_lines.extend(f"- {line}" for line in warnings)

    readme_bytes = ("\r\n".join(readme_lines) + "\r\n").encode("utf-8")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00_装卷说明.txt", readme_bytes)
        zf.writestr("00_证据合订卷宗.pdf", merged_pdf_bytes)
        zf.writestr("01_证据目录.docx", docx_bytes)
        zf.writestr("01_证据目录.csv", csv_bytes)
        for arc_name, payload in standalone_blobs:
            zf.writestr(arc_name, payload)

    safe_case = (case.title or "").replace("/", "_").replace("\\", "_")
    zip_filename = safe_name(f"证据卷宗_{safe_case}_{date_tag}.zip", fallback=f"证据卷宗_{date_tag}.zip")
    return DossierResult(
        zip_bytes=zip_buf.getvalue(),
        filename=zip_filename,
        item_count=len(assemblies),
        total_body_pages=total_body_pages,
        total_pages=total_pdf_pages,
        warnings=warnings,
        group_count=used_group_count,
        standalone_file_count=len(standalone_blobs),
        reminders=reminders,
    )
