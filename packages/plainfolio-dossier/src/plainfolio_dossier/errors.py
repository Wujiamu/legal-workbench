"""Error contract shared by every entry point (CLI / MCP / 工作台适配层).

Codes are a closed set on purpose: the workbench maps each one back to its own
``ErrorCode``, and the CLI maps each one to an exit code.  A free-form string
would silently become "some other error" on one of those two hops.
"""
from __future__ import annotations

from typing import Any

INVALID_INPUT = "invalid_input"
NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"
INTEGRITY_ERROR = "integrity_error"
INTERNAL_ERROR = "internal_error"

VALID_CODES = frozenset({INVALID_INPUT, NOT_FOUND, FORBIDDEN, INTEGRITY_ERROR, INTERNAL_ERROR})


class DossierError(Exception):
    """Business failure with a stable code, a user-facing message and details."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        if code not in VALID_CODES:
            raise ValueError(f"unknown DossierError code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # keeps log lines readable without leaking payloads
        return f"DossierError(code={self.code!r}, message={self.message!r})"
