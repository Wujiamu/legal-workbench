"""Local configuration; secrets must not appear in representations."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    migration_url: str = field(repr=False)
    data_root: Path
    import_roots: tuple[Path, ...]
    host: str
    port: int
    ocr_model_dir: Path
    max_file_bytes: int
    parse_timeout_seconds: int
    max_image_pixels: int
    pg_bin: Path | None = None


def load_settings() -> Settings:
    # Explicit environment variables override the local, permission-restricted file.
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    def get(name: str, default: str = "") -> str:
        return os.environ.get("LWB_" + name, values.get("LWB_" + name, default))
    def local_path(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else ROOT / path
    roots = get("IMPORT_ROOTS")
    pg_bin_raw = get("PG_BIN").strip()
    return Settings(
        database_url=get("DATABASE_URL"),
        migration_url=get("MIGRATION_URL") or get("DATABASE_URL"),
        data_root=local_path(get("DATA_ROOT", "var/data")),
        import_roots=tuple(local_path(p) for p in roots.split(os.pathsep) if p),
        host=get("HOST", "127.0.0.1"), port=int(get("PORT", "8765")),
        ocr_model_dir=local_path(get("OCR_MODEL_DIR", "var/models")),
        max_file_bytes=int(get("MAX_FILE_BYTES", str(2 * 1024**3))),
        parse_timeout_seconds=int(get("PARSE_TIMEOUT_SECONDS", "120")),
        max_image_pixels=int(get("MAX_IMAGE_PIXELS", "50000000")),
        pg_bin=local_path(pg_bin_raw) if pg_bin_raw else None,
    )


def _is_windows() -> bool:
    return os.name == "nt"


def _pg_bin_candidates(*, windows: bool | None = None, darwin: bool | None = None) -> tuple[Path, ...]:
    is_win = _is_windows() if windows is None else windows
    is_mac = (sys.platform == "darwin") if darwin is None else darwin
    if is_win:
        bases = []
        for env_key, default_dir in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            base = os.environ.get(env_key, default_dir)
            if base and base not in bases:
                bases.append(base)
        return tuple(Path(base) / "PostgreSQL" / "18" / "bin" for base in bases)
    if is_mac:
        return (
            Path("/opt/homebrew/opt/postgresql@18/bin"),
            Path("/usr/local/opt/postgresql@18/bin"),
            Path("/Applications/Postgres.app/Contents/Versions/18/bin"),
        )
    return (
        Path("/usr/lib/postgresql/18/bin"),
        Path("/usr/pgsql-18/bin"),
    )


def resolve_pg_bin(configured: str | Path | None = None, *, settings: Settings | None = None) -> Path | None:
    """Resolve PostgreSQL 18 binary directory across macOS, Windows, and Linux."""
    if configured is not None and str(configured).strip():
        return Path(str(configured).strip()).expanduser()
    cfg = settings or load_settings()
    if cfg.pg_bin is not None:
        return cfg.pg_bin
    for tool in ("pg_ctl", "pg_dump"):
        found = shutil.which(tool)
        if found:
            return Path(found).parent
    exe_suffix = ".exe" if _is_windows() else ""
    for candidate in _pg_bin_candidates():
        if (candidate / f"pg_ctl{exe_suffix}").is_file() or (candidate / f"pg_dump{exe_suffix}").is_file():
            return candidate
    return None


def pg_env(base: dict[str, str] | None = None, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build subprocess environment for PostgreSQL CLI tools across POSIX and Windows."""
    env = dict(os.environ if base is None else base)
    if extra:
        env.update(extra)
    if _is_windows():
        env.setdefault("PGCLIENTENCODING", "UTF8")
        env.setdefault("PYTHONUTF8", "1")
    else:
        # PostgreSQL 18 on macOS aborts startup when the postmaster becomes
        # multithreaded under an unset/invalid locale; force a stable one.
        env.setdefault("LC_ALL", "en_US.UTF-8")
    return env
