"""Original, preview and download endpoints with strict MIME handling.

Only server-detected PDFs and safe raster images may be served inline.  Anything
else is an attachment; HTML, SVG and other active content never render inline.
"""
from __future__ import annotations

import re
from io import BytesIO

import pypdfium2 as pdfium
import uuid
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..errors import ErrorCode, WorkbenchError
from ..pdf_preview import pdf_lock
from ..schemas import GetOriginalOutput
from ..storage import Storage, safe_name
from .deps import Guarded, get_db, get_storage
from .ids import uuid_of

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["files"], dependencies=[Guarded])

INLINE_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".gif": "image/gif", ".webp": "image/webp"}
# Plain-text formats can be previewed straight from the stored bytes: no parsing
# is needed, and text/plain + nosniff cannot execute anything.  Without this a
# .txt original showed a JSON error inside the preview frame until it had been
# parsed.
INLINE_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log"}
# Above this size the preview would be a memory spike for no benefit; the user
# can download instead.
MAX_INLINE_TEXT_BYTES = 2 * 1024 * 1024
ATTACHMENT_TYPE = "application/octet-stream"


def _version_or_error(session: Session, case_id: uuid.UUID,
                      file_version_id: uuid.UUID) -> models.FileVersion:
    version = session.get(models.FileVersion, file_version_id)
    if version is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "文件版本不存在",
                             {"file_version_id": str(file_version_id)})
    if version.case_id != case_id:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "文件版本不属于该案件",
                             {"file_version_id": str(file_version_id)})
    return version


def _stored_or_error(session: Session, version: models.FileVersion) -> models.StoredObject:
    stored = session.get(models.StoredObject, version.stored_object_id)
    if stored is None or stored.case_id != version.case_id:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "文件内容记录缺失",
                             {"file_version_id": str(version.id)})
    return stored


def _suffix(stored: models.StoredObject) -> str:
    return PurePosixPath(stored.relative_path).suffix.lower()


def _content_disposition(kind: str, name: str) -> str:
    """ASCII fallback plus RFC 5987 form; the header never carries raw user text."""
    clean = safe_name(name)
    suffix = PurePosixPath(clean).suffix
    ascii_stem = clean.encode("ascii", "ignore").decode("ascii").replace('"', "_").strip()
    if not ascii_stem or ascii_stem == suffix:
        ascii_name = f"material{suffix if suffix.isascii() else ''}"
    else:
        ascii_name = ascii_stem
    quoted = quote(clean, safe="")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


@router.get("/materials/{material_id}/original", response_model=GetOriginalOutput)
def get_original(case_id: str, material_id: str,
                 session: Session = Depends(get_db)) -> GetOriginalOutput:
    case_uuid = uuid_of(case_id, "case_id")
    material = session.get(models.Material, uuid_of(material_id, "material_id"))
    if material is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "材料不存在", {"material_id": material_id})
    if material.case_id != case_uuid:
        raise WorkbenchError(ErrorCode.CASE_MISMATCH, "材料不属于该案件",
                             {"material_id": material_id})
    if material.original_file_version_id is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "材料尚无原件版本",
                             {"material_id": material_id})
    version = _version_or_error(session, case_uuid, material.original_file_version_id)
    stored = _stored_or_error(session, version)
    return GetOriginalOutput(
        file_version_id=version.id, integrity_status=stored.integrity_status,
        browse_url=f"/api/v1/cases/{case_uuid}/file-versions/{version.id}/preview",
    )


def _check_readable(storage: Storage, stored: models.StoredObject) -> None:
    if stored.integrity_status != "ok":
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "原件校验失败，禁止读取",
                             {"stored_object_id": str(stored.id)})
    if not storage.verify(stored.relative_path, stored.sha256, stored.size):
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "文件内容与登记哈希不一致",
                             {"stored_object_id": str(stored.id)})


def _extracted_text(session: Session, version: models.FileVersion) -> str | None:
    if version.active_parse_run_id is None:
        return None
    segments = session.execute(
        select(models.Segment)
        .join(models.ParseUnit, models.Segment.parse_unit_id == models.ParseUnit.id)
        .where(models.Segment.parse_run_id == version.active_parse_run_id)
    ).scalars().all()
    if not segments:
        return None
    ordered = sorted(segments, key=lambda row: [int(part) if part.isdigit() else part
                                                for part in re.split(r"(\d+)",
                                                                     row.locator.get("unit_key", ""))])
    return "\n\n".join(row.text for row in ordered)


@router.get("/file-versions/{file_version_id}/preview")
def preview_file_version(case_id: str, file_version_id: str,
                         frame: int | None = Query(None, ge=0),
                         session: Session = Depends(get_db),
                         storage: Storage = Depends(get_storage)):
    case_uuid = uuid_of(case_id, "case_id")
    version = _version_or_error(session, case_uuid, uuid_of(file_version_id, "file_version_id"))
    stored = _stored_or_error(session, version)
    _check_readable(storage, stored)
    path = storage.managed_path(stored.relative_path)
    suffix = _suffix(stored)
    if suffix == ".pdf":
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    if suffix in {".tif", ".tiff", ".bmp"} or (frame is not None and suffix in INLINE_IMAGE_TYPES):
        try:
            with Image.open(path) as image:
                image.seek(frame or 0)
                if image.width * image.height > 50_000_000:
                    raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "图片过大，请下载查看")
                image.thumbnail((1800, 1800))
                output = BytesIO()
                image.convert("RGB").save(output, format="PNG")
        except (EOFError, OSError, Image.DecompressionBombError) as exc:
            raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "无法读取图片帧") from exc
        return Response(output.getvalue(), media_type="image/png")
    if suffix in INLINE_IMAGE_TYPES:
        return FileResponse(path, media_type=INLINE_IMAGE_TYPES[suffix],
                            headers={"Content-Disposition": "inline"})
    if suffix in INLINE_TEXT_SUFFIXES:
        if stored.size > MAX_INLINE_TEXT_BYTES:
            raise WorkbenchError(
                ErrorCode.UNSUPPORTED_FORMAT, "文件过大，无法内联预览，请使用下载接口",
                {"file_version_id": str(version.id), "size": stored.size})
        raw = path.read_bytes()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body = raw.decode("gb18030", errors="replace")
        return PlainTextResponse(body, media_type="text/plain; charset=utf-8",
                                 headers={"X-Content-Type-Options": "nosniff",
                                          "Content-Security-Policy": "default-src 'none'"})
    text = _extracted_text(session, version)
    if text is None:
        raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "该类型没有安全的内联预览，请使用下载接口",
                             {"file_version_id": str(version.id)})
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8",
                             headers={"X-Content-Type-Options": "nosniff",
                                      "Content-Security-Policy": "default-src 'none'"})


@router.get("/file-versions/{file_version_id}/page-preview")
def preview_pdf_page(case_id: str, file_version_id: str, page: int = Query(ge=1),
                     session: Session = Depends(get_db),
                     storage: Storage = Depends(get_storage)) -> Response:
    version = _version_or_error(session, uuid_of(case_id), uuid_of(file_version_id))
    stored = _stored_or_error(session, version)
    if _suffix(stored) != ".pdf":
        raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "只有 PDF 支持按页预览")
    _check_readable(storage, stored)
    path = storage.managed_path(stored.relative_path)
    # PDFium calls in the web process must be serialized.
    with pdf_lock:
        try:
            with pdfium.PdfDocument(path) as document:
                if page > len(document):
                    raise WorkbenchError(ErrorCode.NOT_FOUND, "PDF 中没有该页")
                sheet = document[page - 1]
                try:
                    width, height = sheet.get_size()
                    bitmap = sheet.render(scale=min(2, 1800 / max(width, height, 1)))
                    try:
                        output = BytesIO()
                        bitmap.to_pil().save(output, format="PNG")
                    finally:
                        bitmap.close()
                finally:
                    sheet.close()
        except (pdfium.PdfiumError, ValueError, FileNotFoundError) as exc:
            raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "无法读取 PDF 页面") from exc
    return Response(output.getvalue(), media_type="image/png")


@router.get("/file-versions/{file_version_id}/download")
def download_file_version(case_id: str, file_version_id: str,
                          session: Session = Depends(get_db),
                          storage: Storage = Depends(get_storage)):
    case_uuid = uuid_of(case_id, "case_id")
    version = _version_or_error(session, case_uuid, uuid_of(file_version_id, "file_version_id"))
    stored = _stored_or_error(session, version)
    _check_readable(storage, stored)
    material = session.get(models.Material, version.material_id)
    name = material.display_name if material else f"{version.id}{_suffix(stored)}"
    return FileResponse(
        storage.managed_path(stored.relative_path), media_type=ATTACHMENT_TYPE,
        headers={"Content-Disposition": _content_disposition("attachment", name),
                 "X-Content-Type-Options": "nosniff"},
    )
