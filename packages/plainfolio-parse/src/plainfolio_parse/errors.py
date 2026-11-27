"""统一的业务错误契约（CLI / MCP / 工作台适配层共用）。

错误码是**闭集**：工作台按码映射回自己的 ``ErrorCode``，CLI 按码决定退出码。
自由字符串会让其中一跳悄悄退化成"某种其他错误"。

``details["reason"]`` 是附加的机器可读细分原因，只有 ``ocr_unavailable`` 一个取值：
工作台据此把"缺 OCR 依赖/模型"精确映射回 ``dependency_missing``（既有错误码契约），
而不必新增错误码。
"""
from __future__ import annotations

from typing import Any

INVALID_INPUT = "invalid_input"
NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"
UNSUPPORTED_FORMAT = "unsupported_format"
PARSE_FAILED = "parse_failed"
INTERNAL_ERROR = "internal_error"

VALID_CODES = frozenset({
    INVALID_INPUT, NOT_FOUND, FORBIDDEN, UNSUPPORTED_FORMAT, PARSE_FAILED, INTERNAL_ERROR,
})

REASON_OCR_UNAVAILABLE = "ocr_unavailable"


class ParseError(Exception):
    """带稳定错误码、用户可读消息与结构化细节的业务失败。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        if code not in VALID_CODES:
            raise ValueError(f"unknown ParseError code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    @property
    def reason(self) -> str | None:
        value = self.details.get("reason")
        return value if isinstance(value, str) else None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # 日志可读且不泄露正文
        return f"ParseError(code={self.code!r}, message={self.message!r})"


def ocr_unavailable(message: str, **details: Any) -> ParseError:
    """缺 OCR 依赖或模型：统一带 ``reason=ocr_unavailable``，供上层精确降级/映射。"""
    return ParseError(
        PARSE_FAILED, message, {"reason": REASON_OCR_UNAVAILABLE, **details}
    )


def is_ocr_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, ParseError) and exc.reason == REASON_OCR_UNAVAILABLE
