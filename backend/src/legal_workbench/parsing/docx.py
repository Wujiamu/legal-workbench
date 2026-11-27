"""DOCX 解析：委托 ``plainfolio_parse.formats.docx``。"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats import docx as _docx

from ..config import Settings
from .adapter import call, to_parse_config

PARSER = _docx.PARSER


def parse_docx(path: Path, settings: Settings | None = None):
    return call(_docx.parse_docx, path, to_parse_config(settings))
