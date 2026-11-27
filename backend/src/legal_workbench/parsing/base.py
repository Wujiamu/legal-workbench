"""解析结果模型：由零数据库独立包 ``plainfolio_parse.formats.base`` 提供。"""
from __future__ import annotations

from plainfolio_parse.formats.base import (  # noqa: F401 - 兼容重导出
    PARSER_VERSION,
    ParseResult,
    ParsedUnit,
    make_result,
)

__all__ = ["PARSER_VERSION", "ParseResult", "ParsedUnit", "make_result"]
