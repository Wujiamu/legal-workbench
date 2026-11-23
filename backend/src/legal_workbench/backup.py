"""Backup and restore with a persistent maintenance flag.

Order is fixed and must not be reordered:
  set maintenance flag -> refuse new writes -> take exclusive lock (waits for
  running writers) -> pg_dump -> copy managed files with hash verification ->
  write manifest -> mark complete -> clear maintenance.

A backup is never "successful" because pg_dump returned 0; the manifest must be
complete and every copied file must re-verify.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import audit, models
from .config import _is_windows, pg_env, resolve_pg_bin
from .db import MAINTENANCE_KEY, get_engine
from .errors import ErrorCode, WorkbenchError
from .storage import hash_file

# The manifest used to carry a hard-coded "0002" long after the schema had moved
# to 0003, so it could not identify the migration state of the dump it described.
# The revision is read from the database being backed up instead.
APP_VERSION = "0.1.0"
MANIFEST_NAME = "manifest.json"
INCOMPLETE_NAME = "INCOMPLETE"
DB_DUMP_NAME = "database.dump"
FILES_DIR = "files"
REQUIRED_MANIFEST_KEYS = {"schema_version", "app_version", "created_at", "database",
                          "files", "complete"}


def _schema_revision(session) -> str:
    """Return the alembic revision actually applied to this database."""
    try:
        value = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:  # table missing on a database that was never migrated
        return "unknown"
    return value or "unknown"


@dataclass
class BackupResult:
    destination: Path
    database_sha256: str
    file_count: int
    total_bytes: int
    complete: bool


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_destination_outside_data(destination: Path, data_root: Path) -> Path:
    destination = destination.expanduser().resolve()
    data_root = data_root.expanduser().resolve()
    try:
        destination.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise WorkbenchError(ErrorCode.INVALID_PATH,
                             "备份目的地不得位于 DATA_ROOT 之内",
                             {"destination": str(destination), "data_root": str(data_root)})
    if destination.exists() and any(destination.iterdir()):
        raise WorkbenchError(ErrorCode.CONFLICT, "备份目的地必须为空目录",
                             {"destination": str(destination)})
    return destination


def _set_maintenance(session: Session, active: bool, reason: str | None) -> None:
    session.execute(
        text("INSERT INTO maintenance_state (id, active, reason) VALUES (1, :active, :reason) "
             "ON CONFLICT (id) DO UPDATE SET active = :active, reason = :reason, "
             "updated_at = now()"),
        {"active": active, "reason": reason},
    )
    session.commit()


def take_exclusive_lock(session: Session, *, timeout_ms: int = 60000) -> None:
    """Wait for in-flight writers to leave the shared lock, then hold it exclusively."""
    # SET does not accept a bind parameter for the value; the integer is validated
    # here so no caller-supplied text reaches the statement.
    session.execute(text(f"SET LOCAL lock_timeout = {int(timeout_ms)}"))
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MAINTENANCE_KEY})
    except Exception as exc:  # noqa: BLE001 - surfaced as a maintenance error
        session.rollback()
        raise WorkbenchError(ErrorCode.MAINTENANCE, "无法取得排他锁，仍有写入在进行",
                             {"error": type(exc).__name__})


def _pg_tool(name: str, pg_bin: str | None) -> str:
    resolved = resolve_pg_bin(pg_bin)
    tool = shutil.which(name, path=str(resolved)) if resolved else shutil.which(name)
    if not tool:
        raise WorkbenchError(ErrorCode.DEPENDENCY_MISSING, f"找不到 {name}",
                             {"pg_bin": str(resolved) if resolved else (pg_bin or "")})
    return tool


def _pg_dump(database_url: str, target: Path, pg_bin: str | None = None) -> None:
    pg_dump = _pg_tool("pg_dump", pg_bin)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if _is_windows() else {}
    result = subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", "--no-acl", "--file", str(target),
         database_url],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=pg_env(), **kwargs,
    )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise WorkbenchError(ErrorCode.INTERNAL_ERROR, "pg_dump 失败",
                             {"stderr": result.stderr[-2000:]})


def create_backup(session: Session, storage, destination: Path, *,
                  database_url: str, pg_bin: str | None = None) -> BackupResult:
    """Full backup: database dump plus every referenced managed file."""
    destination = _ensure_destination_outside_data(destination, storage.root)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = destination / INCOMPLETE_NAME
    marker.write_text(f"started_at={_utcnow()}\n", encoding="utf-8")

    _set_maintenance(session, True, "backup in progress")
    try:
        take_exclusive_lock(session)
        dump_path = destination / DB_DUMP_NAME
        _pg_dump(database_url, dump_path, pg_bin)

        files_root = destination / FILES_DIR
        files_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        entries: list[dict] = []
        total_bytes = 0
        object_rows = session.execute(select(models.StoredObject)).scalars().all()
        analysis_rows = session.execute(select(models.AnalysisVersion)).scalars().all()
        working_rows = session.execute(select(models.WorkingCopy)).scalars().all()
        sources: list[tuple[str, str | None, int | None]] = []
        for stored in object_rows:
            sources.append((stored.relative_path, stored.sha256, stored.size))
        for version in analysis_rows:
            sources.append((version.relative_path, version.sha256, 0))
        # Working copies carry no recorded hash: they are user-editable drafts, so
        # they are copied as-is and only checked for stability during the copy.
        for working in working_rows:
            sources.append((working.relative_path, None, None))
        seen: set[str] = set()
        for relative, expected_sha, expected_size in sources:
            if relative in seen:
                continue
            seen.add(relative)
            source = storage.managed_path(relative)
            if not source.is_file():
                raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "受管文件缺失，备份中止",
                                     {"relative_path": relative})
            before_sha, before_size = hash_file(source)
            if expected_sha and before_sha != expected_sha:
                raise WorkbenchError(
                    ErrorCode.INTEGRITY_ERROR, "文件与登记哈希不符，备份中止",
                    {"relative_path": relative, "expected": expected_sha, "actual": before_sha})
            target = files_root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, target)
            after_sha, after_size = hash_file(target)
            # The source may be edited by external software between the two reads.
            reread_sha, _reread_size = hash_file(source)
            if after_sha != before_sha or after_size != before_size or reread_sha != before_sha:
                raise WorkbenchError(
                    ErrorCode.CONFLICT, "源文件在备份期间发生变化，本次备份失败",
                    {"relative_path": relative})
            entries.append({"relative_path": relative, "size": after_size,
                            "sha256": after_sha})
            total_bytes += after_size

        database_sha = _sha256_path(dump_path)
        manifest = {
            "schema_version": _schema_revision(session),
            "app_version": APP_VERSION,
            "created_at": _utcnow(),
            "database": {"file": DB_DUMP_NAME, "sha256": database_sha,
                         "size": dump_path.stat().st_size},
            "files": entries,
            "complete": True,
        }
        (destination / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        marker.unlink(missing_ok=True)
        # Recorded after the manifest is durable: an audit row for a backup that
        # never completed would be misleading.
        audit.record(session, operation="backup.create", target_type="backup",
                     target_id=database_sha, commit=True)
        return BackupResult(destination=destination, database_sha256=database_sha,
                            file_count=len(entries), total_bytes=total_bytes, complete=True)
    finally:
        session.rollback()
        _set_maintenance(session, False, None)


def clear_maintenance(session: Session) -> None:
    """Explicit operator action after a crashed backup; never automatic."""
    _set_maintenance(session, False, None)


def _load_manifest(destination: Path) -> dict:
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.is_file():
        raise WorkbenchError(ErrorCode.NOT_FOUND, "备份清单不存在",
                             {"destination": str(destination)})
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "备份清单不是合法 JSON")
    if not REQUIRED_MANIFEST_KEYS <= set(manifest):
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "备份清单缺少必需字段",
                             {"missing": sorted(REQUIRED_MANIFEST_KEYS - set(manifest))})
    if not manifest.get("complete"):
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "备份未标记完成，不可恢复")
    return manifest


def verify_backup(destination: Path) -> dict:
    """Check the manifest, every file hash and the dump hash; read-only."""
    destination = destination.expanduser().resolve()
    manifest = _load_manifest(destination)
    dump = destination / manifest["database"]["file"]
    if not dump.is_file():
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "数据库备份文件缺失")
    actual = _sha256_path(dump)
    if actual != manifest["database"]["sha256"]:
        raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "数据库备份哈希不符",
                             {"expected": manifest["database"]["sha256"], "actual": actual})
    files_root = (destination / FILES_DIR).resolve()
    for entry in manifest["files"]:
        relative = entry["relative_path"]
        pure = PurePosixPath(relative)
        if (not relative or "\\" in relative or pure.is_absolute()
                or PureWindowsPath(relative).is_absolute()
                or any(part in ("", ".", "..") or ":" in part for part in pure.parts)):
            raise WorkbenchError(ErrorCode.INVALID_PATH, "备份清单包含越界路径",
                                 {"relative_path": relative})
        path = files_root / relative
        if not path.resolve(strict=False).is_relative_to(files_root):
            raise WorkbenchError(ErrorCode.INVALID_PATH, "备份清单包含越界路径",
                                 {"relative_path": relative})
        if not path.is_file():
            raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "备份文件缺失",
                                 {"relative_path": relative})
        digest, size = hash_file(path)
        if digest != entry["sha256"] or size != entry["size"]:
            raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "备份文件哈希不符",
                                 {"relative_path": relative})
    return manifest


def restore_backup(destination: Path, *, data_root: Path, database_url: str,
                   pg_bin: str | None = None) -> dict:
    """Restore into an empty database and an empty DATA_ROOT; never clears targets."""
    destination = destination.expanduser().resolve()
    data_root = data_root.expanduser().resolve()
    manifest = verify_backup(destination)
    if data_root.exists() and any(data_root.iterdir()):
        raise WorkbenchError(ErrorCode.CONFLICT, "DATA_ROOT 非空，拒绝恢复",
                             {"data_root": str(data_root)})
    if not _database_is_empty(database_url, pg_bin):
        raise WorkbenchError(ErrorCode.CONFLICT, "目标数据库非空，拒绝恢复")
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in manifest["files"]:
        source = destination / FILES_DIR / entry["relative_path"]
        target = data_root / entry["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
        digest, size = hash_file(target)
        if digest != entry["sha256"] or size != entry["size"]:
            raise WorkbenchError(ErrorCode.INTEGRITY_ERROR, "恢复后回读校验失败",
                                 {"relative_path": entry["relative_path"]})
    _pg_restore(destination / manifest["database"]["file"], database_url, pg_bin)
    _grant_app_role(database_url)
    reset_running_jobs(database_url)
    return {"restored_files": len(manifest["files"]),
            "schema_version": manifest["schema_version"],
            "database_sha256": manifest["database"]["sha256"]}


APP_ROLE = "lwb_app"


def _grant_app_role(database_url: str) -> None:
    """The dump is taken with --no-acl, so the application role must be re-granted.

    Without this the restored database is unusable by the app role even though
    every row is present.
    """
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            if cursor.fetchone() is None:
                raise WorkbenchError(ErrorCode.DEPENDENCY_MISSING,
                                     "目标集群缺少应用角色，无法恢复权限",
                                     {"role": APP_ROLE})
            cursor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                           f"IN SCHEMA public TO {APP_ROLE}")
            cursor.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                           f"TO {APP_ROLE}")
            cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                           f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}")
            cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                           f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}")
        connection.commit()


def _database_is_empty(database_url: str, pg_bin: str | None) -> bool:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
            )
            # A custom-format dump creates its own schema, so even alembic_version
            # in the target means the restore would collide.
            return cursor.fetchone()[0] == 0


def _pg_restore(dump: Path, database_url: str, pg_bin: str | None) -> None:
    pg_restore = _pg_tool("pg_restore", pg_bin)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if _is_windows() else {}
    result = subprocess.run(
        [pg_restore, "--no-owner", "--no-acl", "--exit-on-error", "--dbname", database_url,
         str(dump)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=pg_env(), **kwargs,
    )
    if result.returncode != 0:
        raise WorkbenchError(ErrorCode.INTERNAL_ERROR, "pg_restore 失败",
                             {"stderr": result.stderr[-2000:]})


def reset_running_jobs(database_url: str) -> int:
    """Restored running jobs are not actually running: requeue them."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            count = cursor.rowcount
            cursor.execute("UPDATE maintenance_state SET active=false, reason=NULL WHERE id=1")
        connection.commit()
    return count
