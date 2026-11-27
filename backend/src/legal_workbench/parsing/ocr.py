"""本地 OCR 封装：委托 ``plainfolio_parse.formats.ocr``。

``OcrEngine`` 是**同一个类对象**，因此既有测试对 ``legal_workbench.parsing.ocr.OcrEngine``
的 monkeypatch（替换 ``recognize``）仍然作用于真实引擎。
"""
from __future__ import annotations

from pathlib import Path

from plainfolio_parse.formats import ocr as _ocr
from plainfolio_parse.formats.ocr import (  # noqa: F401 - 兼容重导出
    MANIFEST,
    MODEL_FILES,
    Deadline,
    OcrBox,
    OcrEngine,
    OcrPage,
    time_limit,
)

from ..config import Settings
from .adapter import call, to_parse_config

# 工作台既有测试通过替换本模块的 ``signal`` 名字来模拟"没有 SIGALRM 的平台"
# （Windows 线程超时回退路径）。保留这个 monkeypatch 接缝：真实引擎走的是包内
# 同名模块的 ``signal``，两种情形都以 TimeoutError 报告超时，行为一致。
signal = _ocr.signal
threading = _ocr.threading


def model_dir(settings: Settings | None = None) -> Path:
    directory = _ocr.model_dir(to_parse_config(settings))
    assert directory is not None  # 工作台设置必有 OCR 模型目录
    return directory


def verify_models(directory: Path) -> dict[str, Path]:
    return call(_ocr.verify_models, directory)
