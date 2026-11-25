"""Local application shell: loopback-only HTTP API plus the built frontend."""
from __future__ import annotations

import logging
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import analyses, cases, files, materials, progress, search, time as time_api
from .api import evidence as evidence_api
from .api import graph as graph_api
from .api import chart_export as chart_export_api
from .api import claims as claims_api
from .api import source_notes as annotation_api
from .api.deps import guard, request_id
from .config import load_settings, resolve_frontend_dist
from .errors import ErrorCode, WorkbenchError
from .logging_config import configure_logging

configure_logging()
logger = logging.getLogger("legal_workbench.api")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_settings = load_settings()
app.state.allowed_hosts = {f"{host}:{_settings.port}" for host in ("127.0.0.1", "localhost")}

for module in (cases, materials, search, analyses, files, annotation_api, progress, time_api,
               graph_api, evidence_api, claims_api, chart_export_api):
    app.include_router(module.router)


@app.get("/api/v1/health", include_in_schema=False, dependencies=[Depends(guard)])
async def health():
    return {"app": "legal-workbench"}


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request.state.request_id = uuid.uuid4()
    response = await call_next(request)
    response.headers["X-Request-Id"] = str(request.state.request_id)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(WorkbenchError)
async def handle_workbench_error(request: Request, exc: WorkbenchError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status,
                        content=exc.to_response(request_id(request)).model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    error = WorkbenchError(ErrorCode.INVALID_INPUT, "请求参数校验失败",
                           {"errors": [{"loc": list(item["loc"]), "msg": item["msg"],
                                        "type": item["type"]} for item in exc.errors()]})
    return JSONResponse(status_code=error.http_status,
                        content=error.to_response(request_id(request)).model_dump(mode="json"))


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Log the type only; messages can carry paths and file content.
    logger.error("unhandled error: %s", type(exc).__name__, exc_info=exc)
    error = WorkbenchError(ErrorCode.INTERNAL_ERROR, "服务器内部错误")
    return JSONResponse(status_code=error.http_status,
                        content=error.to_response(request_id(request)).model_dump(mode="json"))


dist = resolve_frontend_dist().resolve()
if dist.is_dir():
    # 带内容哈希的构建产物可长期缓存；index.html 绝不缓存，
    # 否则浏览器启发式新鲜度会让用户停留在旧壳上（看不到新构建）。
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.middleware("http")
    async def cache_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif not path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """Serve the single-page shell; unknown /api paths stay 404 JSON."""
        if full_path.startswith("api/"):
            raise WorkbenchError(ErrorCode.NOT_FOUND, "接口不存在",
                                 {"path": "/" + full_path})
        candidate = dist / full_path
        if full_path and candidate.is_file() and candidate.resolve().is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html", media_type="text/html")
