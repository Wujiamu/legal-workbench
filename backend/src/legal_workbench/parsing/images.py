"""Image parsing with local OCR, including multi-frame TIFF."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..config import Settings, load_settings
from ..errors import ErrorCode, WorkbenchError
from .base import ParseResult, ParsedUnit, make_result
from .ocr import OcrEngine

PARSER = "image-1.0"
IMAGE_CONFIG = {"version": "image-1.0", "frames": "all"}


def parse_image(path: Path, settings: Settings | None = None) -> ParseResult:
    settings = settings or load_settings()
    units: list[ParsedUnit] = []
    warnings: list[str] = []
    try:
        image = Image.open(path)
    except Exception as exc:
        raise WorkbenchError(ErrorCode.UNSUPPORTED_FORMAT, "图片无法打开",
                             {"error": type(exc).__name__}) from exc
    engine = None
    frames = getattr(image, "n_frames", 1)
    try:
        for frame_index in range(frames):
            image.seek(frame_index)
            frame = image.convert("RGB")
            unit_key = f"image:{frame_index}"
            locator = {"type": "image", "frame": frame_index, "format": image.format,
                       "width": frame.width, "height": frame.height}
            if frame.width * frame.height > settings.max_image_pixels:
                units.append(ParsedUnit(
                    unit_key=unit_key, locator=locator, text="", method="ocr",
                    status="failed", error=f"max_image_pixels: {frame.width * frame.height}",
                ))
                continue
            if engine is None:
                engine = OcrEngine.get(settings)
            try:
                ocr = engine.recognize(frame, timeout=settings.parse_timeout_seconds)
            except TimeoutError:
                units.append(ParsedUnit(unit_key=unit_key, locator=locator, text="",
                                        method="ocr", status="failed", error="timeout"))
                continue
            locator["ocr_boxes"] = [{"box": box.box, "text": box.text,
                                     "score": round(box.score, 4)} for box in ocr.boxes]
            locator["ocr_mean_score"] = (round(ocr.mean_score, 4)
                                         if ocr.mean_score is not None else None)
            units.append(ParsedUnit(
                unit_key=unit_key, locator=locator, text=ocr.text, method="ocr",
                status="ocr_done" if ocr.text.strip() else "no_text_detected",
            ))
    finally:
        image.close()
    if not units:
        units = [ParsedUnit(unit_key="image:0", locator={"type": "image"}, text="",
                            status="no_text_detected")]
    result = make_result(units, PARSER, dict(IMAGE_CONFIG), warnings)
    if any(unit.status == "failed" for unit in units) and result.status == "complete":
        result.status = "partial"
    return result
