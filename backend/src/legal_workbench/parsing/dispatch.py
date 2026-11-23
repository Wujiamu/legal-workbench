"""Format dispatch for material parsing."""
from __future__ import annotations

from pathlib import Path

from ..errors import ErrorCode, WorkbenchError
from .base import ParseResult

TEXT_EXTENSIONS = {".txt", ".md", ".text"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def parse_file(path: Path) -> ParseResult:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        from .text import parse_text
        return parse_text(path)
    if suffix == ".docx":
        from .docx import parse_docx
        return parse_docx(path)
    if suffix == ".xlsx":
        from .xlsx import parse_xlsx
        return parse_xlsx(path)
    if suffix == ".pdf":
        from .pdf import parse_pdf
        return parse_pdf(path)
    if suffix in IMAGE_EXTENSIONS:
        from .images import parse_image
        return parse_image(path)
    raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "不支持的文件格式", {"suffix": suffix})
