"""Local RapidOCR wrapper. Models must already exist in the configured directory.

No runtime downloads: the install script copies bundled models and records
their hashes in manifest.json; this module verifies the manifest before use.
"""
from __future__ import annotations

import hashlib
import json
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from ..config import Settings, load_settings
from ..errors import ErrorCode, WorkbenchError

MANIFEST = "manifest.json"
MODEL_FILES = ("det.onnx", "rec.onnx", "cls.onnx")


@dataclass
class OcrBox:
    box: list[list[int]]
    text: str
    score: float


@dataclass
class OcrPage:
    text: str
    boxes: list[OcrBox] = field(default_factory=list)
    mean_score: float | None = None
    elapsed: float | None = None


@dataclass
class Deadline:
    """Records that the alarm fired, even if the library swallowed the exception."""
    expired: bool = False


@contextmanager
def time_limit(seconds: float):
    """SIGALRM-based hard limit; only effective on the main thread.

    The handler raises ``TimeoutError``, but RapidOCR wraps everything that
    happens inside its own resize step, so the caller may only see
    ``ResizeImgError``.  The flag on the yielded :class:`Deadline` is what lets
    the caller tell a real timeout from a real resize failure.
    """
    deadline = Deadline()
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield deadline
        return

    def handler(signum, frame):
        deadline.expired = True
        raise TimeoutError("parse timeout")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield deadline
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _call_with_thread_timeout(fn, seconds: float):
    """Windows / non-main-thread fallback when SIGALRM is unavailable."""
    done = threading.Event()
    outcome: list = [None, None]

    def runner() -> None:
        try:
            outcome[0] = fn()
        except BaseException as exc:  # noqa: BLE001 - propagated to caller
            outcome[1] = exc
        finally:
            done.set()

    thread = threading.Thread(target=runner, name="lwb-ocr-timeout", daemon=True)
    thread.start()
    if not done.wait(seconds):
        raise TimeoutError("parse timeout")
    if outcome[1] is not None:
        raise outcome[1]
    return outcome[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def model_dir(settings: Settings | None = None) -> Path:
    return (settings or load_settings()).ocr_model_dir / "rapidocr"


def verify_models(directory: Path) -> dict[str, Path]:
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        raise WorkbenchError(ErrorCode.DEPENDENCY_MISSING,
                             "OCR 模型未安装；请先运行 scripts/install_ocr_models.py",
                             {"model_dir": str(directory)})
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WorkbenchError(ErrorCode.DEPENDENCY_MISSING, "OCR 模型清单无法读取",
                             {"model_dir": str(directory)}) from exc
    paths: dict[str, Path] = {}
    for name in MODEL_FILES:
        entry = manifest.get("files", {}).get(name)
        path = directory / name
        if entry is None or not path.is_file():
            raise WorkbenchError(ErrorCode.DEPENDENCY_MISSING, f"OCR 模型文件缺失: {name}",
                                 {"model_dir": str(directory)})
        if _sha256(path) != entry.get("sha256"):
            raise WorkbenchError(ErrorCode.DEPENDENCY_MISSING, f"OCR 模型哈希不匹配: {name}",
                                 {"model_dir": str(directory)})
        paths[name] = path
    return paths


class OcrEngine:
    _cache: dict[str, "OcrEngine"] = {}

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        directory = model_dir(self.settings)
        paths = verify_models(directory)
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR(
            det_model_path=str(paths["det.onnx"]),
            rec_model_path=str(paths["rec.onnx"]),
            cls_model_path=str(paths["cls.onnx"]),
            print_verbose=False,
        )

    @classmethod
    def get(cls, settings: Settings | None = None) -> "OcrEngine":
        settings = settings or load_settings()
        key = str(model_dir(settings))
        if key not in cls._cache:
            cls._cache[key] = cls(settings)
        return cls._cache[key]

    def recognize(self, image: Image.Image, timeout: float | None = None) -> OcrPage:
        limit = self.settings.parse_timeout_seconds if timeout is None else timeout
        use_sigalrm = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
        if limit > 0 and not use_sigalrm:
            result, _elapse = _call_with_thread_timeout(lambda: self._engine(image), limit)
        else:
            with time_limit(limit) as deadline:
                try:
                    result, _elapse = self._engine(image)
                except TimeoutError:
                    raise
                except Exception:
                    # The alarm fired but the library wrapped the TimeoutError:
                    # report the timeout, not the wrapper's type.
                    if deadline.expired:
                        raise TimeoutError("parse timeout") from None
                    raise
        if not result:
            return OcrPage(text="", boxes=[])
        boxes: list[OcrBox] = []
        lines: list[str] = []
        for item in result:
            points, text, score = item[0], item[1], item[2]
            boxes.append(OcrBox(
                box=[[int(x), int(y)] for x, y in points],
                text=str(text), score=float(score),
            ))
            lines.append(str(text))
        mean = sum(box.score for box in boxes) / len(boxes) if boxes else None
        return OcrPage(text="\n".join(lines), boxes=boxes, mean_score=mean)
