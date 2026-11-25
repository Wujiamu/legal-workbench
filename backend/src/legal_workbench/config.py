"""Local configuration; secrets must not appear in representations.

The workbench can be found in two ways: an explicit ``LWB_HOME`` environment
variable, or a small machine-wide pointer file (``~/.plainfolio/instance.json``)
written by ``scripts/lwb register-instance``.  When neither names a usable
directory a source checkout falls back to the repository that contains this
module, which keeps a plain ``git clone`` working with no registration step at
all.  A frozen desktop build has no checkout to fall back to: its program files
are read-only, so the writable home lives under the platform's user-data
directory (see ``runtime_paths``).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from . import runtime_paths

def _source_root() -> Path:
    """Repository root for a checkout; the bundle root for a frozen build.

    ``parents[3]`` is meaningless inside a PyInstaller bundle (``__file__``
    points into ``_MEIPASS``), so the frozen branch resolves the resource root
    instead of indexing past it.
    """
    if getattr(sys, "frozen", False):
        return runtime_paths.resolve_bundle_root(root=Path.cwd(), frozen=True)
    return Path(__file__).resolve().parents[3]


ROOT = _source_root()

INSTANCE_CONFIG_ENV = "LWB_INSTANCE_CONFIG"
HOME_ENV = "LWB_HOME"
DEFAULT_INSTANCE_CONFIG_PARTS = (".plainfolio", "instance.json")


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


def instance_config_path() -> Path:
    """Where this machine records which workbench installation to use.

    ``LWB_INSTANCE_CONFIG`` overrides the default so tests and portable
    launchers never touch the real ``~/.plainfolio/instance.json``.
    """
    override = os.environ.get(INSTANCE_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home().joinpath(*DEFAULT_INSTANCE_CONFIG_PARTS)


def read_instance_pointer(path: Path | None = None) -> dict[str, object]:
    """Read the pointer file, treating anything unusable as "no pointer".

    A half-written, truncated or hand-edited file must never stop the workbench
    from starting: the caller simply falls back to the next candidate.
    """
    target = instance_config_path() if path is None else Path(path).expanduser()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pointer_home(value: object, base: Path) -> Path | None:
    """Accept a pointer value only when it names an existing directory."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate if candidate.is_dir() else None


def is_packaged_mode() -> bool:
    """True inside a frozen desktop build (or when ``LWB_PACKAGED`` says so)."""
    return runtime_paths.is_packaged_mode()


def resolve_bundle_root() -> Path:
    """Read-only program directory: bundle root when packaged, else ``ROOT``."""
    return runtime_paths.resolve_bundle_root(root=ROOT)


def default_user_data_dir(platform_name: str | None = None) -> Path:
    """Writable per-user home a packaged install uses when nothing is registered."""
    return runtime_paths.default_user_data_dir(platform_name)


def resolve_frontend_dist() -> Path:
    """Built single-page app; bundle copy first, source checkout as fallback."""
    return runtime_paths.resolve_frontend_dist(root=ROOT, bundle_root=resolve_bundle_root())


def resolve_alembic_ini() -> Path:
    return runtime_paths.resolve_alembic_ini(root=ROOT, bundle_root=resolve_bundle_root())


def resolve_migrations_dir() -> Path:
    return runtime_paths.resolve_migrations_dir(root=ROOT, bundle_root=resolve_bundle_root())


def resolve_home_dir() -> Path:
    """Resolve the workbench home directory (writable data + ``.env``).

    Priority: ``LWB_HOME`` → the ``home`` field of the instance pointer (only
    when it names an existing directory) → the platform user-data directory in
    packaged mode → this module's checkout (``ROOT``) in source mode.
    ``ROOT`` is read at call time so callers that replace it (tests, embedded
    launchers) still get the value they set.
    """
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_absolute() else ROOT / candidate
    found = _pointer_home(read_instance_pointer().get("home"), ROOT)
    if found is not None:
        return found
    if is_packaged_mode():
        return default_user_data_dir()
    return ROOT


def _require_directory(value: Path | str, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if not candidate.is_dir():
        raise ValueError(f"{label}不存在或不是目录：{candidate}")
    return candidate.resolve()


def _write_json_atomic(target: Path, payload: dict[str, object]) -> None:
    """Write ``payload`` to ``target`` without ever exposing a partial file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle_fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


def write_instance_pointer(home_dir: Path, project_root: Path | None = None, *,
                           config_path: Path | None = None,
                           executable: str | Path | None = None,
                           mode: str | None = None) -> Path:
    """Record where this workbench lives so other agents can find it.

    Only non-sensitive paths and a timestamp are written.  Database URLs,
    passwords and case data must never be placed here: the pointer file is
    readable metadata, not a credential store.

    ``executable`` and ``mode`` are written only for a frozen install, where a
    plugin launcher cannot infer an interpreter from ``backend/.venv`` and has
    to re-enter the desktop binary with ``--run-mcp``.
    """
    home = _require_directory(home_dir, "工作台目录")
    root = _require_directory(ROOT if project_root is None else project_root, "项目根目录")
    target = (Path(config_path).expanduser() if config_path is not None
              else instance_config_path())
    payload: dict[str, object] = {
        "home": str(home),
        "project_root": str(root),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z"),
    }
    if executable is not None:
        payload["executable"] = str(Path(executable).expanduser())
    if mode is not None:
        payload["mode"] = str(mode)
    _write_json_atomic(target, payload)
    return target


def load_settings() -> Settings:
    # Explicit environment variables override the local, permission-restricted file.
    home = resolve_home_dir()
    values: dict[str, str] = {}
    path = home / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    def get(name: str, default: str = "") -> str:
        return os.environ.get("LWB_" + name, values.get("LWB_" + name, default))
    def local_path(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else home / path
    roots = get("IMPORT_ROOTS")
    pg_bin_raw = get("PG_BIN").strip()
    return Settings(
        database_url=get("DATABASE_URL"),
        migration_url=get("MIGRATION_URL") or get("DATABASE_URL"),
        data_root=local_path(get("DATA_ROOT", "var/data")),
        import_roots=tuple(local_path(p) for p in roots.split(os.pathsep) if p),
        host=get("HOST", "127.0.0.1"), port=int(get("PORT", "8765")),
        ocr_model_dir=_resolve_ocr_model_dir(home, get("OCR_MODEL_DIR").strip(), local_path),
        max_file_bytes=int(get("MAX_FILE_BYTES", str(2 * 1024**3))),
        parse_timeout_seconds=int(get("PARSE_TIMEOUT_SECONDS", "120")),
        max_image_pixels=int(get("MAX_IMAGE_PIXELS", "50000000")),
        pg_bin=local_path(pg_bin_raw) if pg_bin_raw else None,
    )


def _resolve_ocr_model_dir(home: Path, configured: str, local_path) -> Path:
    """Explicit configuration wins; a shipped model directory beats ``var/models``.

    A packaged build ships the ONNX models inside the read-only bundle, and the
    user has no ``var/models`` to fill.  An explicit ``LWB_OCR_MODEL_DIR`` (env
    or ``.env``) still overrides that, so a checkout can point at a larger or
    newer model set without touching the bundle.
    """
    if configured:
        return local_path(configured)
    embedded = runtime_paths.embedded_models_dir(resolve_bundle_root())
    if runtime_paths.directory_has_content(embedded):
        return embedded
    return home / "var" / "models"


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
    """Resolve PostgreSQL 18 binaries across macOS, Windows, and Linux.

    Order: explicit argument → configured ``LWB_PG_BIN`` → the portable cluster
    shipped inside a bundle → ``PATH`` → well-known system install locations.
    The bundled copy comes before ``PATH`` so a packaged install can never pick
    up an unrelated PostgreSQL that happens to be first on ``PATH``.
    """
    if configured is not None and str(configured).strip():
        return Path(str(configured).strip()).expanduser()
    cfg = settings or load_settings()
    if cfg.pg_bin is not None:
        return cfg.pg_bin
    embedded = runtime_paths.embedded_pg_bin(resolve_bundle_root())
    if embedded is not None:
        return embedded
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
