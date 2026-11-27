"""按扩展名分派解析器。"""
from __future__ import annotations

from pathlib import Path

from ..config import ParseConfig
from ..errors import ParseError
from .base import ParseResult

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".text"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})
DOCUMENT_EXTENSIONS = frozenset({".docx", ".xlsx", ".pdf"})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def parse_file(path: Path, config: ParseConfig | None = None,
               force_ocr_pages: set[int] | None = None) -> ParseResult:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        from .text import parse_text
        return parse_text(path, config)
    if suffix == ".docx":
        from .docx import parse_docx
        return parse_docx(path, config)
    if suffix == ".xlsx":
        from .xlsx import parse_xlsx
        return parse_xlsx(path, config)
    if suffix == ".pdf":
        from .pdf import parse_pdf
        return parse_pdf(path, config, force_ocr_pages)
    if suffix in IMAGE_EXTENSIONS:
        from .images import parse_image
        return parse_image(path, config)
    raise ParseError("unsupported_format", "不支持的文件格式", {"suffix": suffix})
