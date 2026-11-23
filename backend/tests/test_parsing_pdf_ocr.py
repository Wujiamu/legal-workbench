"""T09: PDF native/OCR routing, 40+ page coverage, images/TIFF, model and limit errors."""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from legal_workbench.config import load_settings
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing.images import parse_image
from legal_workbench.parsing.ocr import MANIFEST, model_dir
from legal_workbench.parsing.pdf import parse_pdf

ROOT = Path(__file__).resolve().parents[2]
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


@pytest.fixture(scope="session", autouse=True)
def ensure_models():
    if not (model_dir() / MANIFEST).is_file():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "install_ocr_models.py")],
                       check=True)


def _text_image(text: str, size=(1240, 600)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 56)
    draw.text((40, 60), text, font=font, fill="black")
    return image


def _build_mixed_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    scanned = _text_image("扫描件 合同金额100.10元")
    for page in range(1, 43):
        if 11 <= page <= 15:
            pdf.drawImage(ImageReader(scanned), 0, 0, width=width, height=height * 0.55)
        elif 16 <= page <= 20:
            pass
        elif 21 <= page <= 25:
            pdf.setPageRotation(90)
            pdf.setFont("STSong-Light", 12)
            pdf.drawString(40, 40, f"旋转页 TEST-ONLY 第{page}页 金额1000元")
        else:
            pdf.setFont("STSong-Light", 12)
            pdf.drawString(40, height - 60, f"TEST-ONLY 第{page}页 合同正文 金额100.10元")
            pdf.drawString(40, height - 90, "甲方与乙方约定于2024年1月1日履行。")
        pdf.showPage()
        pdf.setPageRotation(0)
    pdf.save()


@pytest.fixture(scope="module")
def mixed_pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "mixed-42p.pdf"
    _build_mixed_pdf(path)
    return path


def test_pdf_42_pages_native_scan_blank_rotation(mixed_pdf):
    result = parse_pdf(mixed_pdf)
    assert len(result.units) == 42
    by_page = {unit.locator["page"]: unit for unit in result.units}
    assert by_page[1].method == "native" and "合同正文" in by_page[1].text
    assert by_page[40].method == "native" and "第40页" in by_page[40].text
    for page in range(11, 16):
        unit = by_page[page]
        assert unit.method == "ocr" and unit.status == "ocr_done"
        assert "100.10" in unit.text
        assert unit.locator["ocr_boxes"] and all("score" in box for box in unit.locator["ocr_boxes"])
        assert unit.locator["ocr_mean_score"] > 0.5
        assert unit.locator["native_text"] == ""
    for page in range(16, 21):
        assert by_page[page].status == "no_text_detected"
    assert by_page[21].locator["rotation"] == 90
    assert result.config["version"] == "pdf-quality-1.0"


def test_pdf_text_only_works_without_models(tmp_path):
    path = tmp_path / "text-only.pdf"
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setFont("STSong-Light", 12)
    pdf.drawString(40, 700, "TEST-ONLY 纯文字PDF 合同金额100元")
    pdf.save()
    settings = dataclasses.replace(load_settings(), ocr_model_dir=tmp_path / "missing")
    result = parse_pdf(path, settings=settings)
    assert result.units[0].method == "native"
    assert "合同金额" in result.units[0].text


def test_pdf_missing_models_on_scanned_page(tmp_path, mixed_pdf):
    settings = dataclasses.replace(load_settings(), ocr_model_dir=tmp_path / "missing")
    with pytest.raises(WorkbenchError) as exc:
        parse_pdf(mixed_pdf, settings=settings)
    assert exc.value.code == ErrorCode.DEPENDENCY_MISSING


def test_pdf_pixel_limit_reported(mixed_pdf):
    settings = dataclasses.replace(load_settings(), max_image_pixels=1000)
    result = parse_pdf(mixed_pdf, settings=settings)
    scanned = [u for u in result.units if u.locator["page"] == 11][0]
    assert scanned.status == "failed" and scanned.error.startswith("max_image_pixels")
    assert result.units[0].method == "native"


def test_pdf_timeout_reported(mixed_pdf):
    settings = dataclasses.replace(load_settings(), parse_timeout_seconds=0.001)
    result = parse_pdf(mixed_pdf, settings=settings)
    scanned = [u for u in result.units if u.locator["page"] == 11][0]
    assert scanned.status == "failed" and scanned.error == "timeout"


def test_tiff_multiframe_all_frames(tmp_path):
    path = tmp_path / "multi.tiff"
    frames = [_text_image("第一帧 TEST-ONLY 金额100元", (1000, 300)),
              _text_image("第二帧 TEST-ONLY 金额1000元", (1000, 300))]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    result = parse_image(path)
    assert len(result.units) == 2
    assert all(unit.method == "ocr" and unit.status == "ocr_done" for unit in result.units)
    assert "第一帧" in result.units[0].text and "第二帧" in result.units[1].text
    assert all(unit.locator["ocr_boxes"] for unit in result.units)


def test_image_pixel_limit(tmp_path):
    path = tmp_path / "big.png"
    Image.new("RGB", (200, 200), "white").save(path)
    settings = dataclasses.replace(load_settings(), max_image_pixels=100)
    result = parse_image(path, settings=settings)
    assert result.units[0].status == "failed"
    assert result.units[0].error.startswith("max_image_pixels")


def test_model_hash_mismatch_rejected(tmp_path):
    from legal_workbench.parsing.ocr import verify_models
    source = model_dir()
    target = tmp_path / "models"
    target.mkdir()
    for name in ("det.onnx", "rec.onnx", "cls.onnx", MANIFEST):
        (target / name).write_bytes((source / name).read_bytes())
    with (target / "rec.onnx").open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(WorkbenchError) as exc:
        verify_models(target)
    assert exc.value.code == ErrorCode.DEPENDENCY_MISSING


class _WrappingEngine:
    """Stands in for RapidOCR's resize path, which wraps whatever it catches."""

    def __init__(self, spin: bool):
        self.spin = spin
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        if self.spin:
            # The SIGALRM handler raises TimeoutError mid-loop; this except is
            # exactly what the library does before raising its own error type.
            try:
                while True:
                    pass
            except TimeoutError:
                raise RuntimeError("ResizeImgError: wrapped") from None
        raise RuntimeError("ResizeImgError: genuine resize failure")


def _engine_with(stub):
    from legal_workbench.parsing import ocr as ocr_module
    engine = object.__new__(ocr_module.OcrEngine)
    engine.settings = dataclasses.replace(load_settings(), parse_timeout_seconds=1)
    engine._engine = stub
    return engine


def test_wrapped_timeout_is_reported_as_timeout():
    """A library-wrapped TimeoutError must still be classified as a timeout.

    PDF parsing only catches TimeoutError, so a wrapped error surfaced as
    ResizeImgError and the page was reported as a generic failure.
    """
    from PIL import Image

    engine = _engine_with(_WrappingEngine(spin=True))
    with pytest.raises(TimeoutError):
        engine.recognize(Image.new("RGB", (8, 8), "white"), timeout=0.2)


def test_genuine_resize_error_is_not_reported_as_timeout():
    """A real resize failure must keep its own type: no blanket relabelling."""
    from PIL import Image

    engine = _engine_with(_WrappingEngine(spin=False))
    with pytest.raises(RuntimeError) as caught:
        engine.recognize(Image.new("RGB", (8, 8), "white"), timeout=30)
    assert "ResizeImgError" in str(caught.value)
