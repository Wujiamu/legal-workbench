"""Managed file storage: streaming hashes, staged writes, no-overwrite publish.

Layout (relative to DATA_ROOT):
  cases/<case_id>/originals/<sha256>/source<ext>
  cases/<case_id>/versions/<file_version_id>/source<ext>
  cases/<case_id>/working/<working_copy_id>/<safe_name>
  cases/<case_id>/derived/<parse_run_id>/
  cases/<case_id>/analysis/<analysis_version_id>/content.md
  staging/<operation_id>/
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Iterable, Iterator

from .config import load_settings
from .errors import ErrorCode, WorkbenchError

CHUNK = 1024 * 1024
_SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,10}$")
_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def is_link_or_junction(path: Path) -> bool:
    """Detect POSIX/NTFS symlinks and Windows NTFS directory junctions."""
    return path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


def hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def hash_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        return hash_stream(handle)


def _is_windows_reserved(name: str) -> bool:
    head = name.partition(".")[0].rstrip(" ").upper()
    if head in _WINDOWS_RESERVED_NAMES:
        return True
    return hasattr(ntpath, "isreserved") and ntpath.isreserved(name)


def _truncate_component(name: str, limit: int = 120) -> str:
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 10 and len(ext) + 2 <= limit:
        return stem[: limit - len(ext) - 1] + "." + ext
    return name[:limit]


def safe_name(name: str, fallback: str = "material") -> str:
    """Display-name to a single safe path component (no directories)."""
    name = unicodedata.normalize("NFC", name).replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = _WINDOWS_INVALID_CHARS.sub("_", name)
    name = name.lstrip(". ").rstrip(" .") or fallback
    name = _truncate_component(name, 120).lstrip(". ").rstrip(" .") or fallback
    if _is_windows_reserved(name):
        name = _truncate_component(f"_{name}", 120).lstrip(". ").rstrip(" .") or fallback
    return name


def safe_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if _SAFE_EXT.match(suffix) else ".bin"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StagedFile:
    path: Path
    meta_path: Path
    relative_staging: str
    sha256: str
    size: int
    operation_id: uuid.UUID
    original_name: str
    extension: str


@dataclass(frozen=True)
class PublishResult:
    relative_path: str
    sha256: str
    size: int
    created: bool


class Storage:
    def __init__(self, root: Path | None = None):
        settings = load_settings()
        self.root = (root or settings.data_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.root.resolve()
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def staging_root(self) -> Path:
        return self.root / "staging"

    # ---- path safety -------------------------------------------------
    def managed_path(self, relative: str) -> Path:
        """Resolve a stored relative path; reject escapes and any symlink component."""
        if "\\" in relative:
            raise WorkbenchError(ErrorCode.INVALID_PATH, "受管路径不合法", {"path": relative})
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or any(
            part in ("", ".", "..") or ":" in part for part in pure.parts
        ) or PureWindowsPath(relative).is_absolute():
            raise WorkbenchError(ErrorCode.INVALID_PATH, "受管路径不合法", {"path": relative})
        current = self.root
        for part in pure.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or is_link_or_junction(current):
                raise WorkbenchError(ErrorCode.INVALID_PATH, "受管路径包含符号链接", {"path": relative})
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkbenchError(ErrorCode.INVALID_PATH, "受管路径越界", {"path": relative})
        return current

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    # ---- case layout -------------------------------------------------
    def case_dir(self, case_id: uuid.UUID) -> Path:
        path = self.root / "cases" / str(case_id)
        for child in ("originals", "versions", "working", "derived", "analysis"):
            (path / child).mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def original_relative(self, case_id: uuid.UUID, sha256: str, extension: str) -> str:
        return f"cases/{case_id}/originals/{sha256}/source{extension}"

    def version_relative(self, case_id: uuid.UUID, version_id: uuid.UUID, extension: str) -> str:
        return f"cases/{case_id}/versions/{version_id}/source{extension}"

    def working_relative(self, case_id: uuid.UUID, working_copy_id: uuid.UUID, name: str) -> str:
        return f"cases/{case_id}/working/{working_copy_id}/{safe_name(name)}"

    def analysis_relative(self, case_id: uuid.UUID, version_id: uuid.UUID) -> str:
        return f"cases/{case_id}/analysis/{version_id}/content.md"

    def derived_relative(self, case_id: uuid.UUID, parse_run_id: uuid.UUID) -> str:
        return f"cases/{case_id}/derived/{parse_run_id}"

    # ---- staging -----------------------------------------------------
    def stage_bytes(self, data: bytes, original_name: str = "upload.bin",
                    operation_id: uuid.UUID | None = None) -> StagedFile:
        return self._stage(_BytesReader(data), original_name, operation_id)

    def stage_stream(self, stream: BinaryIO, original_name: str,
                     operation_id: uuid.UUID | None = None,
                     max_bytes: int | None = None) -> StagedFile:
        return self._stage(stream, original_name, operation_id, max_bytes)

    def stage_path(self, source: Path, original_name: str | None = None,
                   operation_id: uuid.UUID | None = None,
                   max_bytes: int | None = None) -> StagedFile:
        source = Path(source)
        if is_link_or_junction(source):
            raise WorkbenchError(ErrorCode.INVALID_PATH, "不接受符号链接来源", {"path": str(source)})
        before = source.stat()
        with source.open("rb") as handle:
            staged = self._stage(handle, original_name or source.name, operation_id, max_bytes)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino
        ):
            self.discard(staged)
            raise WorkbenchError(ErrorCode.CONFLICT, "源文件在复制期间发生变化",
                                 {"path": str(source)})
        return staged

    def _stage(self, stream: BinaryIO, original_name: str,
               operation_id: uuid.UUID | None, max_bytes: int | None = None) -> StagedFile:
        settings = load_settings()
        limit = max_bytes if max_bytes is not None else settings.max_file_bytes
        operation_id = operation_id or uuid.uuid4()
        folder = self.staging_root / str(operation_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = uuid.uuid4().hex
        part = folder / f"{token}.part"
        meta_path = folder / f"{token}.json"
        digest = hashlib.sha256()
        size = 0
        try:
            with part.open("xb") as out:
                while chunk := stream.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise WorkbenchError(
                            ErrorCode.INVALID_INPUT, "文件超过大小限制",
                            {"limit_bytes": limit},
                        )
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        except WorkbenchError:
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise
        except BaseException as exc:
            # Keep the partial bytes plus a diagnostic sidecar for later inspection.
            meta_path.write_text(json.dumps({
                "operation_id": str(operation_id), "status": "failed", "error": type(exc).__name__,
                "part": part.name, "size_written": size, "created_at": _utcnow(),
            }, ensure_ascii=False), encoding="utf-8")
            raise
        staged = StagedFile(
            path=part, meta_path=meta_path,
            relative_staging=f"staging/{operation_id}/{part.name}",
            sha256=digest.hexdigest(), size=size, operation_id=operation_id,
            original_name=safe_name(original_name), extension=safe_extension(original_name),
        )
        meta_path.write_text(json.dumps({
            "operation_id": str(operation_id), "status": "complete", "sha256": staged.sha256,
            "size": size, "part": part.name, "original_name": staged.original_name,
            "created_at": _utcnow(),
        }, ensure_ascii=False), encoding="utf-8")
        return staged

    def discard(self, staged: StagedFile) -> None:
        staged.path.unlink(missing_ok=True)
        staged.meta_path.unlink(missing_ok=True)
        try:
            staged.path.parent.rmdir()
        except OSError:
            pass

    def iter_staging(self) -> Iterator[dict]:
        for meta in sorted(self.staging_root.glob("*/*.json")):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {"status": "unreadable"}
            payload["relative_path"] = self.relative(meta)
            yield payload

    # ---- publish -----------------------------------------------------
    def _publish(self, staged: StagedFile, relative: str) -> PublishResult:
        target = self.managed_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = True
        try:
            os.link(staged.path, target)
        except FileExistsError:
            created = False
            existing_hash, existing_size = hash_file(target)
            if existing_hash != staged.sha256 or existing_size != staged.size:
                raise WorkbenchError(
                    ErrorCode.INTEGRITY_ERROR, "已存在同名但内容不同的受管对象",
                    {"path": relative, "expected_sha256": staged.sha256,
                     "actual_sha256": existing_hash},
                )
        else:
            os.unlink(staged.path)
        # Read the published bytes back; a mismatch is never silently accepted.
        actual_hash, actual_size = hash_file(target)
        if actual_hash != staged.sha256 or actual_size != staged.size:
            raise WorkbenchError(
                ErrorCode.INTEGRITY_ERROR, "发布后回读校验失败",
                {"path": relative, "expected_sha256": staged.sha256, "actual_sha256": actual_hash},
            )
        staged.meta_path.unlink(missing_ok=True)
        try:
            staged.path.parent.rmdir()
        except OSError:
            pass
        return PublishResult(relative_path=relative, sha256=staged.sha256, size=staged.size,
                             created=created)

    def publish_original(self, case_id: uuid.UUID, staged: StagedFile) -> PublishResult:
        self.case_dir(case_id)
        return self._publish(staged, self.original_relative(case_id, staged.sha256, staged.extension))

    def publish_version(self, case_id: uuid.UUID, version_id: uuid.UUID,
                        staged: StagedFile) -> PublishResult:
        self.case_dir(case_id)
        return self._publish(staged, self.version_relative(case_id, version_id, staged.extension))

    def write_immutable(self, relative: str, data: bytes) -> PublishResult:
        staged = self.stage_bytes(data, original_name=PurePosixPath(relative).name)
        return self._publish(staged, relative)

    def copy_to_working(self, case_id: uuid.UUID, working_copy_id: uuid.UUID,
                        source_relative: str, name: str) -> PublishResult:
        """Independent byte copy (never a hard link) with verification."""
        source = self.managed_path(source_relative)
        staged = self.stage_path(source, original_name=name)
        return self._publish(staged, self.working_relative(case_id, working_copy_id, name))

    # ---- verification ------------------------------------------------
    def verify(self, relative: str, sha256: str, size: int) -> bool:
        path = self.managed_path(relative)
        try:
            actual_hash, actual_size = hash_file(path)
        except OSError:
            return False
        return actual_hash == sha256 and actual_size == size

    def delete_unpublished(self, relative: str) -> None:
        """Remove a file only when it has no database reference (caller decides)."""
        path = self.managed_path(relative)
        if path.is_file():
            path.unlink()

    def iter_case_files(self, case_id: uuid.UUID) -> Iterable[Path]:
        base = self.root / "cases" / str(case_id)
        if not base.is_dir():
            return []
        return (p for p in base.rglob("*") if p.is_file() and not is_link_or_junction(p))


class _BytesReader:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        end = len(self._data) if size < 0 else self._offset + size
        chunk = self._data[self._offset:end]
        self._offset += len(chunk)
        return chunk
