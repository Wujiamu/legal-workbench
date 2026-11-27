"""素章（PlainFolio）零数据库离线阅卷解析包。

公开入口只有四类概念：

* :func:`~plainfolio_parse.formats.parse_file` / :func:`~plainfolio_parse.formats.iter_pdf_units`
  / :func:`~plainfolio_parse.formats.pdf_page_count`：TXT / DOCX / XLSX / PDF / 图片解析；
* :func:`~plainfolio_parse.exact_values.extract_exact_values`：日期、分币种金额与编号；
* :mod:`plainfolio_parse.identifiers`：强指纹（手机号 / USCC / 银行账号 / 邮箱）、
  机构名与跨文件碰撞排查；
* :mod:`plainfolio_parse.path_fence`：方案 A 文件访问围栏（默认只允许当前工作目录，
  ``PLAINFOLIO_ALLOWED_ROOTS`` 显式放行）。

本包不导入 sqlalchemy / psycopg / fastapi / alembic / uvicorn，也不读写任何数据库；
OCR 是可选组 ``plainfolio-parse[ocr]``，缺失时按 ``ParseConfig.ocr_missing_policy``
降级（默认把需要 OCR 的页面/图片标为 failed 并写明原因）。
"""
from __future__ import annotations

from .config import (
    PLAINFOLIO_OCR_MODEL_DIR_ENV,
    WORKBENCH_OCR_MODEL_DIR_ENV,
    ParseConfig,
    default_parse_config,
)
from .errors import ParseError
from .exact_values import ExactValueHit, extract_exact_values
from .formats import (
    ParseResult,
    ParsedUnit,
    is_supported,
    iter_pdf_units,
    make_result,
    parse_file,
    parse_pdf,
    pdf_page_count,
)
from .identifiers import (
    IdentifierHit,
    OrgMention,
    extract_identifiers,
    extract_org_names,
    scan_identifier_bridges,
)
from .markdown_export import render_parse_result_markdown
from .path_fence import (
    PLAINFOLIO_ALLOWED_ROOTS_ENV,
    allowed_roots,
    resolve_readable_dir,
    resolve_readable_file,
    resolve_writable_output,
)
from .tables import preview_table

__version__ = "0.1.0"

__all__ = [
    "ExactValueHit",
    "IdentifierHit",
    "OrgMention",
    "PLAINFOLIO_ALLOWED_ROOTS_ENV",
    "PLAINFOLIO_OCR_MODEL_DIR_ENV",
    "ParseConfig",
    "ParseError",
    "ParseResult",
    "ParsedUnit",
    "WORKBENCH_OCR_MODEL_DIR_ENV",
    "__version__",
    "allowed_roots",
    "default_parse_config",
    "extract_exact_values",
    "extract_identifiers",
    "extract_org_names",
    "is_supported",
    "iter_pdf_units",
    "make_result",
    "parse_file",
    "parse_pdf",
    "pdf_page_count",
    "preview_table",
    "render_parse_result_markdown",
    "resolve_readable_dir",
    "resolve_readable_file",
    "resolve_writable_output",
    "scan_identifier_bridges",
]
