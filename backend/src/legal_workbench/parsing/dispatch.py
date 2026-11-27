"""格式分派：委托 ``plainfolio_parse.formats.dispatch``。"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats.dispatch import (  # noqa: F401 - 兼容重导出
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
)
from plainfolio_parse.formats.dispatch import parse_file as _parse_file

from ..config import Settings
from .adapter import call, to_parse_config


def parse_file(path: Path, settings: Settings | None = None):
    return call(_parse_file, path, to_parse_config(settings))
