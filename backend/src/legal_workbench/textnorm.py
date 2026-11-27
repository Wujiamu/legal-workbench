"""检索归一化与偏移映射。

规则实现已迁到零数据库独立包 :mod:`plainfolio_parse.textnorm`，本模块只做重导出，
保证既有 ``from legal_workbench.textnorm import normalize_with_map, original_index``
调用点与常量引用一字不变。
"""
from __future__ import annotations

from plainfolio_parse.textnorm import (  # noqa: F401 - 兼容重导出
    FULLWIDTH_END,
    FULLWIDTH_OFFSET,
    FULLWIDTH_START,
    normalize_with_map,
    original_index,
)

__all__ = [
    "FULLWIDTH_END",
    "FULLWIDTH_OFFSET",
    "FULLWIDTH_START",
    "normalize_with_map",
    "original_index",
]
