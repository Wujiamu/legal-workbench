"""Shared error envelope and HTTP status mapping; no transport implementation."""
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    FORBIDDEN = "forbidden"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CASE_MISMATCH = "case_mismatch"
    CONFLICT = "conflict"
    INVALID_PATH = "invalid_path"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INTEGRITY_ERROR = "integrity_error"
    PARSE_FAILED = "parse_failed"
    MAINTENANCE = "maintenance"
    DEPENDENCY_MISSING = "dependency_missing"
    INTERNAL_ERROR = "internal_error"


HTTP_ERROR_STATUS = {
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CASE_MISMATCH: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.INVALID_PATH: 400,
    ErrorCode.UNSUPPORTED_FORMAT: 422,
    ErrorCode.INTEGRITY_ERROR: 409,
    ErrorCode.PARSE_FAILED: 422,
    ErrorCode.MAINTENANCE: 503,
    ErrorCode.DEPENDENCY_MISSING: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    details: dict[str, Any]
    request_id: UUID


class WorkbenchError(Exception):
    """Domain error carried unchanged to HTTP, CLI and MCP transports."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_ERROR_STATUS[self.code]

    def to_response(self, request_id: UUID) -> ErrorResponse:
        return ErrorResponse(
            code=self.code, message=self.message, details=self.details, request_id=request_id
        )
