"""Path parameter parsing helpers shared by routers."""
from __future__ import annotations

import uuid

from ..errors import ErrorCode, WorkbenchError


def uuid_of(value: str, field: str = "id") -> uuid.UUID:
    """Reject malformed identifiers as invalid input instead of a 500."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "标识符格式不合法",
                             {"field": field, "value": str(value)})
