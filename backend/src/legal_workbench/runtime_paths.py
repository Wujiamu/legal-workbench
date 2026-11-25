"""Where the program lives (read-only) versus where the data lives (writable).

A source checkout uses one directory for both.  A frozen desktop build cannot:
the application bundle is read-only (``/Applications``, ``%LOCALAPPDATA%\\Programs``
or a mounted ``.dmg``) while the database cluster, managed case files and OCR
models must live in the user's profile.  Every decision the workbench makes
about its *own* files goes through this module, so the two modes cannot drift
apart and ``config.py`` stays a thin façade that callers already import.

Resolution order is deliberately environment-first: packaging scripts, tests and
portable installs must be able to point at a staged bundle without editing code.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys

PACKAGED_ENV = "LWB_PACKAGED"
BUNDLE_ROOT_ENV = "LWB_BUNDLE_ROOT"
FRONTEND_DIST_ENV = "LWB_FRONTEND_DIST"
ALEMBIC_INI_ENV = "LWB_ALEMBIC_INI"
MIGRATIONS_DIR_ENV = "LWB_MIGRATIONS_DIR"

#: Directory name used under the platform's user-data location.
APP_DIR_NAME = "PlainFolio"

TRUTHY = frozenset({"1", "true", "yes", "on"})

#: PostgreSQL binaries a bundle must carry to initialise and dump a cluster.
PG_REQUIRED_TOOLS = ("initdb", "pg_ctl", "postgres", "psql", "createdb",
                     "pg_dump", "pg_restore")


def truthy(value: object) -> bool:
    """Accept the usual shell spellings of "yes" and nothing else."""
    return isinstance(value, str) and value.strip().lower() in TRUTHY


def is_windows_platform(platform_name: str | None = None) -> bool:
    name = sys.platform if platform_name is None else platform_name
    return name in ("win32", "nt", "windows", "cygwin")


def is_packaged_mode(env: Mapping[str, str] | None = None, *,
                     frozen: bool | None = None) -> bool:
    """True for a frozen build, or when a launcher/script declares packaged mode.

    ``LWB_PACKAGED`` exists because a PyInstaller onedir build of a *script*
    (or an integration test) cannot set ``sys.frozen`` for another process.
    """
    values = os.environ if env is None else env
    if truthy(values.get(PACKAGED_ENV)):
        return True
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    return bool(frozen)


def _frozen_resource_roots() -> tuple[Path, ...]:
    """Candidate roots for a frozen build, most specific first.

    PyInstaller sets ``sys._MEIPASS`` to the directory that holds collected
    data files (``_internal`` for a 6.x onedir build).  Older layouts put the
    data next to the executable, and a macOS ``.app`` puts it under
    ``Contents/Resources``; probing all three keeps the launcher independent of
    the exact bundler version.
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(str(meipass)))
    try:
        executable = Path(sys.executable).resolve()
    except OSError:  # pragma: no cover - defensive, sys.executable is always set
        return tuple(roots)
    roots.append(executable.parent)
    roots.append(executable.parent / "_internal")
    roots.append(executable.parent.parent / "Resources")
    return tuple(roots)


def resolve_bundle_root(*, root: Path | str, env: Mapping[str, str] | None = None,
                        frozen: bool | None = None) -> Path:
    """Directory holding the program's own read-only files.

    Priority: ``LWB_BUNDLE_ROOT`` → frozen resource roots → the source checkout.
    """
    values = os.environ if env is None else env
    explicit = (values.get(BUNDLE_ROOT_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if is_packaged_mode(values, frozen=frozen):
        for candidate in _frozen_resource_roots():
            if candidate.is_dir():
                return candidate.resolve()
    return Path(root)


def default_user_data_dir(platform_name: str | None = None, *,
                          env: Mapping[str, str] | None = None,
                          home: Path | str | None = None) -> Path:
    """Writable per-user directory for a packaged install.

    macOS and Windows both reserve a documented location for application data;
    Linux follows the XDG data directory.  ``LOCALAPPDATA`` (not ``APPDATA``) is
    used on Windows because the cluster must not roam with a domain profile.
    """
    values = os.environ if env is None else env
    base_home = Path.home() if home is None else Path(home)
    if platform_name is None:
        platform_name = sys.platform
    if platform_name == "darwin":
        return base_home / "Library" / "Application Support" / APP_DIR_NAME
    if is_windows_platform(platform_name):
        local = (values.get("LOCALAPPDATA") or "").strip()
        base = Path(local).expanduser() if local else base_home / "AppData" / "Local"
        return base / APP_DIR_NAME
    return base_home / ".local" / "share" / "plainfolio"


def _first_existing(candidates: tuple[Path, ...], probe) -> Path:
    """Return the first candidate ``probe`` accepts, else the last one.

    Returning a non-existent fallback instead of raising keeps the source-mode
    behaviour identical: callers already check ``is_dir()`` / ``is_file()`` and
    report a precise error themselves.
    """
    for candidate in candidates:
        if probe(candidate):
            return candidate
    return candidates[-1]


def resolve_frontend_dist(*, root: Path | str, bundle_root: Path | str | None = None,
                          env: Mapping[str, str] | None = None) -> Path:
    """Built single-page app: explicit override → bundle → source checkout."""
    values = os.environ if env is None else env
    explicit = (values.get(FRONTEND_DIST_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    bundle = Path(bundle_root) if bundle_root is not None else resolve_bundle_root(
        root=root, env=values)
    return _first_existing(
        (bundle / "frontend" / "dist", Path(root) / "frontend" / "dist"),
        lambda candidate: (candidate / "index.html").is_file())


def resolve_alembic_ini(*, root: Path | str, bundle_root: Path | str | None = None,
                        env: Mapping[str, str] | None = None) -> Path:
    """``alembic.ini`` for the migration chain (bundle layout first)."""
    values = os.environ if env is None else env
    explicit = (values.get(ALEMBIC_INI_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    bundle = Path(bundle_root) if bundle_root is not None else resolve_bundle_root(
        root=root, env=values)
    return _first_existing(
        (bundle / "backend" / "alembic.ini", bundle / "alembic.ini",
         Path(root) / "backend" / "alembic.ini"),
        lambda candidate: candidate.is_file())


def resolve_migrations_dir(*, root: Path | str, bundle_root: Path | str | None = None,
                           env: Mapping[str, str] | None = None) -> Path:
    """Alembic ``versions/`` parent directory (bundle layout first)."""
    values = os.environ if env is None else env
    explicit = (values.get(MIGRATIONS_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    bundle = Path(bundle_root) if bundle_root is not None else resolve_bundle_root(
        root=root, env=values)
    return _first_existing(
        (bundle / "backend" / "migrations", bundle / "migrations",
         Path(root) / "backend" / "migrations"),
        lambda candidate: candidate.is_dir())


def embedded_runtime_dir(bundle_root: Path | str) -> Path:
    return Path(bundle_root) / "runtime"


def embedded_pg_bin(bundle_root: Path | str, *, platform_name: str | None = None) -> Path | None:
    """Portable PostgreSQL ``bin`` shipped inside the bundle, when present."""
    candidate = embedded_runtime_dir(bundle_root) / "postgres" / "bin"
    suffix = ".exe" if is_windows_platform(platform_name) else ""
    for tool in ("pg_ctl", "pg_dump"):
        if (candidate / f"{tool}{suffix}").is_file():
            return candidate
    return None


def embedded_models_dir(bundle_root: Path | str) -> Path:
    return embedded_runtime_dir(bundle_root) / "models"


def directory_has_content(path: Path | str) -> bool:
    """True when ``path`` is a directory with at least one entry."""
    candidate = Path(path)
    try:
        return candidate.is_dir() and next(candidate.iterdir(), None) is not None
    except OSError:
        return False


def frozen_resource_root(root: Path | str) -> Path:
    """Resource root used to import bundled Python packages in a frozen build."""
    return resolve_bundle_root(root=root)
