#!/usr/bin/env python3
"""Copy bundled RapidOCR models into the configured model directory.

This script is the only place models are installed; parsing never downloads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legal_workbench.config import load_settings  # noqa: E402

FILES = {
    "det.onnx": "ch_PP-OCRv4_det_infer.onnx",
    "rec.onnx": "ch_PP-OCRv4_rec_infer.onnx",
    "cls.onnx": "ch_ppocr_mobile_v2.0_cls_infer.onnx",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Install local OCR models")
    parser.add_argument("--force", action="store_true", help="overwrite an existing install")
    args = parser.parse_args()
    import rapidocr_onnxruntime

    source = Path(rapidocr_onnxruntime.__file__).parent / "models"
    settings = load_settings()
    target = settings.ocr_model_dir / "rapidocr"
    manifest_path = target / "manifest.json"
    if manifest_path.exists() and not args.force:
        print(f"模型已存在: {target}（使用 --force 覆盖）")
        return 0
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = {"source": f"rapidocr_onnxruntime {getattr(rapidocr_onnxruntime, '__version__', 'unknown')}",
                "files": {}}
    for name, bundled in FILES.items():
        origin = source / bundled
        if not origin.is_file():
            print(f"缺少随包模型: {origin}", file=sys.stderr)
            return 1
        destination = target / name
        shutil.copyfile(origin, destination)
        manifest["files"][name] = {"sha256": sha256(destination), "size": destination.stat().st_size}
        print(f"{name} sha256={manifest['files'][name]['sha256']}")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(f"已安装到 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
