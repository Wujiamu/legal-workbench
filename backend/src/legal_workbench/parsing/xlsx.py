"""XLSX 解析：委托 ``plainfolio_parse.formats.xlsx``。"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats import xlsx as _xlsx

from ..config import Settings
from .adapter import call, to_parse_config

PARSER = _xlsx.PARSER


def parse_xlsx(path: Path, settings: Settings | None = None):
    return call(_xlsx.parse_xlsx, path, to_parse_config(settings))
