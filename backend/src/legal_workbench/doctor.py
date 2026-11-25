"""Read-only diagnostics except for a temporary data-directory write probe."""
import os
import shutil
import subprocess
import sys
import tempfile
from .config import (is_packaged_mode, load_settings, resolve_bundle_root,
                     resolve_home_dir, resolve_pg_bin)

CLEAR_MAINTENANCE = (
    "python -m legal_workbench.cli clear-maintenance"
)


def tooling_status(*, packaged: bool, which=None, run=None) -> list[tuple[str, bool, str]]:
    """``(label, ok, detail)`` for the source-build toolchain.

    A packaged build ships the compiled frontend and the bundled interpreter, so
    a missing ``node``/``npm`` is not a defect there; in a checkout it still is,
    because ``npm run build`` is how the frontend gets built.
    """
    finder = shutil.which if which is None else which
    runner = subprocess.run if run is None else run
    win_kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    entries: list[tuple[str, bool, str]] = []
    for name in ("node", "npm"):
        path = finder(name)
        version = (
            runner(
                [path, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", **win_kwargs,
            ).stdout.strip()
            if path else ""
        )
        if path and version:
            entries.append(("OK", True, f"{name}: {version}"))
        elif packaged:
            entries.append(("SKIP", True,
                            f"{name}: not required by a packaged build"
                            + (f" (found {path})" if path else "")))
        else:
            entries.append(("MISSING", False, f"{name}: not found"))
    return entries


def _report_maintenance(settings) -> tuple[bool, str]:
    """Surface a stuck maintenance flag: it blocks every write, and the operator
    needs to know both that it is on and how to clear it safely."""
    if not settings.database_url:
        return False, "skipped; database not configured"
    try:
        import psycopg
        with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.maintenance_state')")
                if cursor.fetchone()[0] is None:
                    return False, "skipped; schema not migrated yet"
                cursor.execute("SELECT active, reason FROM maintenance_state WHERE id = 1")
                row = cursor.fetchone()
    except Exception:
        return False, "skipped; database unavailable"
    if row is None or not row[0]:
        return True, "inactive; writes allowed"
    reason = row[1] or "unknown"
    return False, (
        f"ACTIVE (reason: {reason}) — all writes are rejected with 'maintenance'. "
        "If no backup is running, clear it with: " + CLEAR_MAINTENANCE)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if hasattr(stream, "reconfigure") and (sys.platform == "win32" or encoding != "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    settings = load_settings()
    ok = True
    def report(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        print(f"{'OK' if passed else 'MISSING'} {label}: {detail}")
        ok &= passed
    packaged = is_packaged_mode()
    bundle_root = resolve_bundle_root()
    print(f"OK PACKAGED: {'yes (frozen desktop build)' if packaged else 'no (source checkout)'}")
    report("BUNDLE_ROOT", bundle_root.is_dir(), str(bundle_root))
    report("HOME", resolve_home_dir().is_dir(), str(resolve_home_dir()))
    report("Python", sys.version_info[:2] == (3, 13), sys.version.split()[0])
    for state, passed, detail in tooling_status(packaged=packaged):
        # ``state`` is already OK / MISSING / SKIP; ``detail`` names the tool.
        print(f"{state} {detail}")
        ok &= passed
    pg_bin = resolve_pg_bin(settings=settings)
    report("PG_BIN", bool(pg_bin), str(pg_bin) if pg_bin else "not found (install PostgreSQL 18 or set LWB_PG_BIN)")
    try:
        with tempfile.TemporaryFile(dir=settings.data_root):
            pass
        report("DATA_ROOT", True, "writable")
    except OSError:
        report("DATA_ROOT", False, "missing or not writable")
    models = settings.ocr_model_dir
    report("OCR_MODEL_DIR", models.is_dir() and any(models.iterdir()), "nonempty directory" if models.is_dir() and any(models.iterdir()) else "missing or empty; OCR models are not installed automatically")
    if not settings.database_url:
        report("PostgreSQL", False, "LWB_DATABASE_URL is not configured")
    else:
        try:
            import psycopg
            with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SHOW server_version_num")
                    version = int(cursor.fetchone()[0])
            report("PostgreSQL", 180000 <= version < 190000, f"server major version {version // 10000}")
        except Exception:
            report("PostgreSQL", False, "connection failed; check configuration and server")
    maintenance_ok, maintenance_detail = _report_maintenance(settings)
    report("MAINTENANCE", maintenance_ok, maintenance_detail)
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
