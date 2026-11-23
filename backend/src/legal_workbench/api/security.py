"""Keep browser requests within the local Workbench origin."""
from fastapi import Request

from ..errors import ErrorCode, WorkbenchError

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def check_local_request(request: Request, allowed_hosts: set[str]) -> None:
    host = request.headers.get("host", "").lower()
    if host not in allowed_hosts:
        raise WorkbenchError(ErrorCode.FORBIDDEN, "仅接受工作台的本机地址")
    if request.method not in SAFE_METHODS:
        expected_origin = f"{request.url.scheme}://{host}"
        if request.headers.get("origin") != expected_origin:
            raise WorkbenchError(ErrorCode.FORBIDDEN, "写请求必须来自工作台所在页面")
