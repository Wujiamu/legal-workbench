"""图片解析：委托 ``plainfolio_parse.formats.images``。"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats import images as _images

from ..config import Settings
from .adapter import call, to_parse_config

PARSER = _images.PARSER
IMAGE_CONFIG = _images.IMAGE_CONFIG


def parse_image(path: Path, settings: Settings | None = None):
    return call(_images.parse_image, path, to_parse_config(settings))
