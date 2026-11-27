"""PDF 解析：委托 ``plainfolio_parse.formats.pdf``。"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats import pdf as _pdf

from ..config import Settings
from .adapter import call, iterate, to_parse_config

PARSER = _pdf.PARSER
RENDER_SCALE = _pdf.RENDER_SCALE
PDF_CONFIG = _pdf.PDF_CONFIG


def parse_pdf(path: Path, settings: Settings | None = None,
              force_ocr_pages: set[int] | None = None):
    return call(_pdf.parse_pdf, path, to_parse_config(settings), force_ocr_pages)


def pdf_page_count(path: Path) -> int:
    return _pdf.pdf_page_count(path)


def iter_pdf_units(path: Path, settings: Settings | None = None,
                   force_ocr_pages: set[int] | None = None,
                   skip_keys: set[str] | None = None):
    """逐页产出单元；``skip_keys`` 在渲染/OCR **之前**生效（恢复时不重做已完成页）。"""
    return iterate(_pdf.iter_pdf_units(path, to_parse_config(settings),
                                       force_ocr_pages, skip_keys))
