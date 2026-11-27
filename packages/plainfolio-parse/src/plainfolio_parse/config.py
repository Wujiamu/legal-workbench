"""显式解析配置。

工作台原来在解析器内部直接 ``load_settings()``，读的是进程级全局配置；独立包没有
``Settings``，所以把这些值收成显式参数：不传时用 :func:`default_parse_config`,
它只读两个环境变量，不碰任何配置文件或数据库。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLAINFOLIO_OCR_MODEL_DIR_ENV = "PLAINFOLIO_OCR_MODEL_DIR"
WORKBENCH_OCR_MODEL_DIR_ENV = "LWB_OCR_MODEL_DIR"

# OCR 缺失时的两种处置：
# * ``degrade``（默认）：纯文本/DOCX/XLSX/原生文字层 PDF 照常返回，需要 OCR 的
#   页面或图片标为 failed 并写明 ``ocr_unavailable: …``，整份文档仍能交付；
# * ``error``：直接抛 ``ParseError``。工作台沿用这个模式，以保持
#   ``dependency_missing`` 的既有错误码契约。
OCR_MISSING_POLICIES = ("degrade", "error")


@dataclass
class ParseConfig:
    ocr_model_dir: Path | None = None
    parse_timeout_seconds: int = 120
    max_image_pixels: int = 50_000_000
    enable_ocr: bool = True
    ocr_missing_policy: str = "degrade"

    def __post_init__(self) -> None:
        if self.ocr_model_dir is not None:
            self.ocr_model_dir = Path(self.ocr_model_dir).expanduser()
        if self.parse_timeout_seconds < 0:
            raise ValueError("parse_timeout_seconds 不能为负数")
        if self.max_image_pixels < 0:
            raise ValueError("max_image_pixels 不能为负数")
        if self.ocr_missing_policy not in OCR_MISSING_POLICIES:
            raise ValueError(
                f"ocr_missing_policy 必须是 {OCR_MISSING_POLICIES} 之一，"
                f"收到 {self.ocr_missing_policy!r}"
            )

    @property
    def rapidocr_dir(self) -> Path | None:
        """RapidOCR 模型目录（``<ocr_model_dir>/rapidocr``，与工作台布局一致）。"""
        if self.ocr_model_dir is None:
            return None
        return self.ocr_model_dir / "rapidocr"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ocr_model_dir": str(self.ocr_model_dir) if self.ocr_model_dir else None,
            "parse_timeout_seconds": self.parse_timeout_seconds,
            "max_image_pixels": self.max_image_pixels,
            "enable_ocr": self.enable_ocr,
            "ocr_missing_policy": self.ocr_missing_policy,
        }


def default_parse_config() -> ParseConfig:
    """OCR 模型目录优先 ``PLAINFOLIO_OCR_MODEL_DIR``，其次工作台的 ``LWB_OCR_MODEL_DIR``."""
    for name in (PLAINFOLIO_OCR_MODEL_DIR_ENV, WORKBENCH_OCR_MODEL_DIR_ENV):
        value = (os.environ.get(name) or "").strip()
        if value:
            return ParseConfig(ocr_model_dir=Path(value).expanduser())
    return ParseConfig()
