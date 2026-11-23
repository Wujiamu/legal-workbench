"""Read-only diagnostics except for a temporary data-directory write probe."""
import shutil
import subprocess
import sys
import tempfile
from .config import load_settings

CLEAR_MAINTENANCE = (
    "python -m legal_workbench.cli clear-maintenance"
)


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
    settings = load_settings()
    ok = True
    def report(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        print(f"{'OK' if passed else 'MISSING'} {label}: {detail}")
        ok &= passed
    report("Python", sys.version_info[:2] == (3, 13), sys.version.split()[0])
    for name in ("node", "npm"):
        path = shutil.which(name)
        version = subprocess.run([path, "--version"], capture_output=True, text=True).stdout.strip() if path else "not found"
        report(name, bool(path and version), version)
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
