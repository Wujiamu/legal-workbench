"""薄适配层：``Settings`` → :class:`plainfolio_parse.ParseConfig`，
:class:`~plainfolio_parse.ParseError` → :class:`~legal_workbench.errors.WorkbenchError`。

解析规则全部在 `packages/plainfolio-parse`，工作台这里只做三件不涉及业务判断的事：

1. 把进程级 ``Settings`` 的三个解析参数显式传给包（不传则包自己读环境变量）；
2. 把包的错误码精确映射回既有 ``ErrorCode``，消息与细节**原样保留**——上游 API 与
   CLI 的错误文案、故障注入测试都依赖这一点；
3. 缺 OCR 依赖/模型（``details["reason"] == "ocr_unavailable"``）映射回
   ``DEPENDENCY_MISSING``，这是工作台既有契约（``dependency_missing`` → HTTP 503）。
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, NoReturn, TypeVar

from plainfolio_parse import ParseConfig, ParseError
from plainfolio_parse.errors import REASON_OCR_UNAVAILABLE

from ..config import Settings, load_settings
from ..errors import ErrorCode, WorkbenchError

T = TypeVar("T")

_CODE_MAP = {
    "invalid_input": ErrorCode.INVALID_INPUT,
    "not_found": ErrorCode.NOT_FOUND,
    "forbidden": ErrorCode.FORBIDDEN,
    "unsupported_format": ErrorCode.UNSUPPORTED_FORMAT,
    "parse_failed": ErrorCode.PARSE_FAILED,
    "internal_error": ErrorCode.INTERNAL_ERROR,
}


def to_parse_config(settings: Settings | None = None) -> ParseConfig:
    """把工作台设置翻译成包的显式配置。

    ``ocr_missing_policy="error"``：工作台在缺模型时必须让解析任务以
    ``dependency_missing`` 失败（既有测试与 HTTP 状态码都按这个契约），而不是把
    每一页静默标成 failed。
    """
    settings = settings or load_settings()
    return ParseConfig(
        ocr_model_dir=settings.ocr_model_dir,
        parse_timeout_seconds=settings.parse_timeout_seconds,
        max_image_pixels=settings.max_image_pixels,
        enable_ocr=True,
        ocr_missing_policy="error",
    )


def to_workbench_error(exc: ParseError) -> WorkbenchError:
    details: dict[str, Any] = {key: value for key, value in exc.details.items()
                               if key != "reason"}
    if exc.reason == REASON_OCR_UNAVAILABLE:
        return WorkbenchError(ErrorCode.DEPENDENCY_MISSING, exc.message,
                              details or None)
    code = _CODE_MAP.get(exc.code, ErrorCode.INTERNAL_ERROR)
    return WorkbenchError(code, exc.message, details or None)


def raise_workbench(exc: ParseError) -> NoReturn:
    raise to_workbench_error(exc) from exc


def call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ParseError as exc:
        raise_workbench(exc)


def iterate(units: Iterable[Any]) -> Iterator[Any]:
    """把生成器里的 ``ParseError`` 转成 ``WorkbenchError``（逐页产出仍保持惰性）。"""
    try:
        yield from units
    except ParseError as exc:
        raise_workbench(exc)
