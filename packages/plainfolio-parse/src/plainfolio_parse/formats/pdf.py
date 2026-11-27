"""PDF 解析：原生文字层、质量启发式与逐页本地 OCR。"""
from __future__ import annotations

import re
from pathlib import Path

import pypdfium2 as pdfium

from ..config import ParseConfig, default_parse_config
from ..errors import ParseError, is_ocr_unavailable
from .base import ParseResult, ParsedUnit, make_result
from .ocr import OcrEngine

PARSER = "pdf-1.0"
RENDER_SCALE = 2.0
PDF_CONFIG = {
    "version": "pdf-quality-1.0",
    "min_native_chars": 20,
    "replacement_ratio": 0.02,
    "image_area_ratio": 0.25,
    "image_text_chars": 80,
    "render_scale": RENDER_SCALE,
}
_WHITESPACE = re.compile(r"\s+")


def _native_quality(text: str, image_ratio: float) -> tuple[bool, dict]:
    stripped = _WHITESPACE.sub("", text)
    replacement = text.count("\ufffd") / max(len(text), 1)
    needs_ocr = (
        len(stripped) < PDF_CONFIG["min_native_chars"]
        or replacement > PDF_CONFIG["replacement_ratio"]
        or (image_ratio > PDF_CONFIG["image_area_ratio"]
            and len(stripped) < PDF_CONFIG["image_text_chars"])
    )
    return needs_ocr, {
        "native_chars": len(stripped),
        "replacement_ratio": round(replacement, 4),
        "image_area_ratio": round(image_ratio, 4),
    }


def _image_area_ratio(page) -> float:
    width, height = page.get_size()
    page_area = max(width * height, 1.0)
    total = 0.0
    try:
        objects = page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE])
    except Exception:
        return 0.0
    for obj in objects:
        try:
            left, bottom, right, top = obj.get_bounds()
        except Exception:
            continue
        clipped_w = max(0.0, min(right, width) - max(left, 0.0))
        clipped_h = max(0.0, min(top, height) - max(bottom, 0.0))
        total += clipped_w * clipped_h
    return min(total / page_area, 1.0)


def parse_pdf(path: Path, config: ParseConfig | None = None,
              force_ocr_pages: set[int] | None = None) -> ParseResult:
    config = config or default_parse_config()
    units = list(iter_pdf_units(path, config, force_ocr_pages))
    warnings = [f"第 {unit.locator.get('page')} 页失败: {unit.error}"
                for unit in units if unit.status == "failed"]
    if not units:
        units = [ParsedUnit(unit_key="pdf:0", locator={"type": "pdf"}, text="",
                            status="no_text_detected")]
    result = make_result(units, PARSER, dict(PDF_CONFIG), warnings)
    if warnings and result.status == "complete":
        result.status = "partial"
    return result


def pdf_page_count(path: Path) -> int:
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception:
        return 0
    try:
        return len(document)
    finally:
        document.close()


def iter_pdf_units(path: Path, config: ParseConfig | None = None,
                   force_ocr_pages: set[int] | None = None,
                   skip_keys: set[str] | None = None):
    """Yield one unit per page so the pipeline can checkpoint after each page.

    ``skip_keys`` is consulted *before* the page is parsed, so a resumed run does
    not render or OCR pages it already has.  Deciding afterwards would still pay
    the OCR cost and could let a missing model abort an otherwise complete
    recovery.
    """
    config = config or default_parse_config()
    skip = skip_keys or set()
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise ParseError("parse_failed", "PDF 无法打开（可能损坏或加密）",
                         {"error": type(exc).__name__}) from exc
    try:
        for index in range(len(document)):
            unit_key = f"pdf:{index + 1}"
            if unit_key in skip:
                continue
            try:
                page = document[index]
                unit = _parse_page(page, index, config,
                                   force_ocr=bool(force_ocr_pages and index + 1 in force_ocr_pages))
                yield unit
            except ParseError:
                raise
            except Exception as exc:
                yield ParsedUnit(
                    unit_key=unit_key, locator={"type": "pdf", "page": index + 1},
                    text="", status="failed", error=f"{type(exc).__name__}: {exc}",
                )
    finally:
        document.close()


def _parse_page(page, index: int, config: ParseConfig, force_ocr: bool = False) -> ParsedUnit:
    page_no = index + 1
    rotation = page.get_rotation()
    width, height = page.get_size()
    textpage = page.get_textpage()
    native = textpage.get_text_range()
    image_ratio = _image_area_ratio(page)
    needs_ocr, quality = _native_quality(native, image_ratio)
    if force_ocr:
        needs_ocr = True
    locator = {"type": "pdf", "page": page_no, "rotation": rotation,
               "width": round(width, 2), "height": round(height, 2),
               "native_text": native, "forced_ocr": force_ocr, **quality}
    if not needs_ocr:
        return ParsedUnit(unit_key=f"pdf:{page_no}", locator=locator, text=native,
                          method="native", status="extracted" if native.strip() else "no_text_detected")
    expected_pixels = (width * RENDER_SCALE) * (height * RENDER_SCALE)
    if expected_pixels > config.max_image_pixels:
        return ParsedUnit(
            unit_key=f"pdf:{page_no}", locator=locator, text="", method="ocr",
            status="failed", error=f"max_image_pixels: {int(expected_pixels)}",
        )
    try:
        engine = OcrEngine.get(config)
    except ParseError as exc:
        if not is_ocr_unavailable(exc) or config.ocr_missing_policy == "error":
            raise
        return ParsedUnit(
            unit_key=f"pdf:{page_no}", locator=locator, text="", method="ocr",
            status="failed", error=f"ocr_unavailable: {exc.message}",
        )
    bitmap = page.render(scale=RENDER_SCALE)
    image = bitmap.to_pil().convert("RGB")
    if image.width * image.height > config.max_image_pixels:
        return ParsedUnit(
            unit_key=f"pdf:{page_no}", locator=locator, text="", method="ocr",
            status="failed", error=f"max_image_pixels: {image.width * image.height}",
        )
    try:
        ocr = engine.recognize(image, timeout=config.parse_timeout_seconds)
    except TimeoutError:
        return ParsedUnit(unit_key=f"pdf:{page_no}", locator=locator, text="", method="ocr",
                          status="failed", error="timeout")
    locator["ocr_boxes"] = [{"box": box.box, "text": box.text, "score": round(box.score, 4)}
                            for box in ocr.boxes]
    locator["ocr_mean_score"] = round(ocr.mean_score, 4) if ocr.mean_score is not None else None
    status = "ocr_done" if ocr.text.strip() else "no_text_detected"
    return ParsedUnit(unit_key=f"pdf:{page_no}", locator=locator, text=ocr.text,
                      method="ocr", status=status)
