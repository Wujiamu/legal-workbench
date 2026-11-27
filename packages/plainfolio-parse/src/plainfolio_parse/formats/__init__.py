"""格式解析器：TXT / DOCX / XLSX / PDF / 图片，共享同一份结果模型。"""
from __future__ import annotations

from .base import PARSER_VERSION, ParseResult, ParsedUnit, make_result
from .dispatch import (
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    is_supported,
    parse_file,
)
from .ocr import MANIFEST, OcrBox, OcrEngine, OcrPage, ocr_availability, verify_models
from .pdf import PDF_CONFIG, RENDER_SCALE, iter_pdf_units, parse_pdf, pdf_page_count

__all__ = [
    "DOCUMENT_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "MANIFEST",
    "OcrBox",
    "OcrEngine",
    "OcrPage",
    "PARSER_VERSION",
    "PDF_CONFIG",
    "ParseResult",
    "ParsedUnit",
    "RENDER_SCALE",
    "SUPPORTED_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "is_supported",
    "iter_pdf_units",
    "make_result",
    "ocr_availability",
    "parse_file",
    "parse_pdf",
    "pdf_page_count",
    "verify_models",
]
