"""纯文本解析：委托 ``plainfolio_parse.formats.text``。"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats import text as _text

from ..config import Settings
from .adapter import call, to_parse_config

PARSER = _text.PARSER


def decode_text(data: bytes) -> str:
    return call(_text.decode_text, data)


def parse_text(path: Path, settings: Settings | None = None):
    return call(_text.parse_text, path, to_parse_config(settings))
