"""本地 RapidOCR 封装。模型必须已存在于配置目录，运行期绝不下载。

工作台安装脚本把模型复制到 ``<ocr_model_dir>/rapidocr`` 并在 ``manifest.json`` 记录
哈希；本模块在使用前校验清单。缺依赖或缺模型时抛 :func:`ocr_unavailable` 形式的
:class:`ParseError`，由调用方按 ``ParseConfig.ocr_missing_policy`` 决定降级或报错。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import ParseConfig, default_parse_config
from ..errors import ocr_unavailable

MANIFEST = "manifest.json"
MODEL_FILES = ("det.onnx", "rec.onnx", "cls.onnx")
OCR_EXTRA = "plainfolio-parse[ocr]"


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
    """记录闹钟是否真的响过，即使第三方库把异常吞掉了。"""

    expired: bool = False


@contextmanager
def time_limit(seconds: float):
    """SIGALRM hard limit; only effective on the main thread.

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


def model_dir(config: ParseConfig | None = None) -> Path | None:
    """``<ocr_model_dir>/rapidocr``；未配置模型目录时为 ``None``。"""
    return (config or default_parse_config()).rapidocr_dir


def dependency_installed() -> bool:
    try:
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except (ImportError, ValueError):  # pragma: no cover - 损坏的安装
        return False


def verify_models(directory: Path) -> dict[str, Path]:
    """校验模型清单与哈希；不通过时抛 ``ocr_unavailable``。"""
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        raise ocr_unavailable("OCR 模型未安装；请先运行 scripts/install_ocr_models.py",
                              model_dir=str(directory))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ocr_unavailable("OCR 模型清单无法读取", model_dir=str(directory)) from exc
    paths: dict[str, Path] = {}
    for name in MODEL_FILES:
        entry = manifest.get("files", {}).get(name)
        path = directory / name
        if entry is None or not path.is_file():
            raise ocr_unavailable(f"OCR 模型文件缺失: {name}", model_dir=str(directory))
        if _sha256(path) != entry.get("sha256"):
            raise ocr_unavailable(f"OCR 模型哈希不匹配: {name}", model_dir=str(directory))
        paths[name] = path
    return paths


def ocr_availability(config: ParseConfig | None = None) -> dict[str, Any]:
    """不做哈希校验的快速可用性探测，供 ``policy`` 与告警文案使用。"""
    config = config or default_parse_config()
    directory = config.rapidocr_dir
    dependency = dependency_installed()
    models_present = bool(
        directory is not None
        and (directory / MANIFEST).is_file()
        and all((directory / name).is_file() for name in MODEL_FILES)
    )
    reason = None
    if not config.enable_ocr:
        reason = "已在配置中关闭 OCR（enable_ocr=false）"
    elif not dependency:
        reason = f"未安装 OCR 依赖；请 pip install '{OCR_EXTRA}'"
    elif directory is None:
        reason = ("未配置 OCR 模型目录；请设置 PLAINFOLIO_OCR_MODEL_DIR"
                  "（或工作台的 LWB_OCR_MODEL_DIR）")
    elif not models_present:
        reason = f"OCR 模型目录缺少 manifest.json 或 {', '.join(MODEL_FILES)}：{directory}"
    return {
        "available": reason is None,
        "enabled": config.enable_ocr,
        "dependency_installed": dependency,
        "model_dir": str(directory) if directory is not None else None,
        "models_present": models_present,
        "missing_policy": config.ocr_missing_policy,
        "reason": reason,
    }


class OcrEngine:
    _cache: dict[str, "OcrEngine"] = {}

    def __init__(self, config: ParseConfig | None = None):
        # 属性名沿用 ``settings``：工作台既有测试用 ``object.__new__`` 注入
        # ``settings`` 与 ``_engine`` 后直接调用 ``recognize``。
        self.settings = config or default_parse_config()
        if not self.settings.enable_ocr:
            raise ocr_unavailable("已关闭 OCR（enable_ocr=false）")
        directory = self.settings.rapidocr_dir
        if directory is None:
            raise ocr_unavailable(
                "未配置 OCR 模型目录；请设置 PLAINFOLIO_OCR_MODEL_DIR 或 LWB_OCR_MODEL_DIR")
        if not dependency_installed():
            raise ocr_unavailable(
                f"未安装 OCR 依赖；请 pip install '{OCR_EXTRA}'", model_dir=str(directory))
        paths = verify_models(directory)
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR(
            det_model_path=str(paths["det.onnx"]),
            rec_model_path=str(paths["rec.onnx"]),
            cls_model_path=str(paths["cls.onnx"]),
            print_verbose=False,
        )

    @classmethod
    def get(cls, config: ParseConfig | None = None) -> "OcrEngine":
        config = config or default_parse_config()
        directory = config.rapidocr_dir
        key = str(directory) if directory is not None else "<unconfigured>"
        if key not in cls._cache:
            cls._cache[key] = cls(config)
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
