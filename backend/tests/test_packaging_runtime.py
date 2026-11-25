"""Packaging stage: bundle/data separation, desktop launcher, build pipeline.

Nothing here needs a frozen build, a real ``initdb`` or PyInstaller.  The
launcher is driven through injected runners and callables, and the build
scripts through their ``--dry-run`` contract, which is exactly the surface a
build machine exercises before it produces an artifact.

The three properties these tests protect, in order of importance:

1. a source checkout keeps behaving exactly as before (``config.ROOT``
   monkeypatching still works, no packaged path leaks into it);
2. a packaged install never writes into its read-only bundle and never starts
   against a database whose migration has no verified backup;
3. no installer or uninstaller step can ever delete the user's case data.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from legal_workbench import config, desktop_launcher, doctor, runtime_paths
from legal_workbench.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGING = REPO_ROOT / "packaging"
PLUGIN_LAUNCHER = REPO_ROOT / "plugins" / "plainfolio" / "scripts" / "run-mcp.py"
ISS = PACKAGING / "windows" / "plainfolio.iss"

FRONTEND_BUILT = (REPO_ROOT / "frontend" / "dist" / "index.html").is_file()


@pytest.fixture(autouse=True)
def _clean_packaging_env(monkeypatch):
    """No test may inherit a developer's packaged-mode or bundle overrides."""
    for name in ("LWB_PACKAGED", "LWB_BUNDLE_ROOT", "LWB_FRONTEND_DIST",
                 "LWB_ALEMBIC_INI", "LWB_MIGRATIONS_DIR", "LWB_PG_BIN",
                 "LWB_OCR_MODEL_DIR", "LWB_DATA_ROOT", "LWB_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    yield


def _settings(tmp_path: Path) -> Settings:
    """A Settings value that cannot reach the developer's real database."""
    return Settings(
        database_url="postgresql://lwb_app:pw@127.0.0.1:55432/legal_workbench",
        migration_url="postgresql://lwb_migrator:pw@127.0.0.1:55432/legal_workbench",
        data_root=tmp_path / "var" / "data",
        import_roots=(),
        host="127.0.0.1",
        port=8765,
        ocr_model_dir=tmp_path / "var" / "models",
        max_file_bytes=1024,
        parse_timeout_seconds=5,
        max_image_pixels=1024,
        pg_bin=None,
    )


def _fake_pg_bin(tmp_path: Path, *, windows: bool = False) -> Path:
    pg_bin = tmp_path / "pg18" / "bin"
    pg_bin.mkdir(parents=True)
    suffix = ".exe" if windows else ""
    for tool in ("initdb", "pg_ctl", "postgres", "psql", "createdb", "pg_dump", "pg_restore"):
        (pg_bin / f"{tool}{suffix}").write_bytes(b"")
    return pg_bin


def _load_plugin_launcher():
    """Load the standalone wrapper without leaving bytecode in the package."""
    module = types.ModuleType("plainfolio_run_mcp_packaged")
    module.__file__ = str(PLUGIN_LAUNCHER)
    sys.modules[module.__name__] = module
    try:
        source = PLUGIN_LAUNCHER.read_text(encoding="utf-8")
        exec(compile(source, str(PLUGIN_LAUNCHER), "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


# --- 打包态判定 -----------------------------------------------------------

def test_is_packaged_mode_accepts_the_documented_truthy_values(monkeypatch):
    assert config.is_packaged_mode() is False

    for value in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("LWB_PACKAGED", value)
        assert config.is_packaged_mode() is True, value

    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("LWB_PACKAGED", value)
        assert config.is_packaged_mode() is False, value

    monkeypatch.delenv("LWB_PACKAGED")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.is_packaged_mode() is True


def test_resolve_bundle_root_prefers_env_then_frozen_then_checkout(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(config, "ROOT", checkout)
    assert config.resolve_bundle_root() == checkout

    explicit = tmp_path / "staged-bundle"
    explicit.mkdir()
    monkeypatch.setenv("LWB_BUNDLE_ROOT", str(explicit))
    assert config.resolve_bundle_root() == explicit.resolve()

    monkeypatch.delenv("LWB_BUNDLE_ROOT")
    meipass = tmp_path / "_internal"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    assert config.resolve_bundle_root() == meipass.resolve()


def test_resolve_bundle_root_falls_back_to_the_executable_directory(monkeypatch, tmp_path):
    """A layout without ``_MEIPASS`` still resolves next to the binary."""
    app_dir = tmp_path / "PlainFolio"
    app_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(app_dir / "PlainFolio"))
    assert config.resolve_bundle_root() == app_dir.resolve()


@pytest.mark.parametrize("platform_name,expected", [
    ("darwin", Path("Library") / "Application Support" / "PlainFolio"),
    ("linux", Path(".local") / "share" / "plainfolio"),
])
def test_default_user_data_dir_uses_the_platform_convention(tmp_path, platform_name, expected):
    home = tmp_path / "home"
    assert runtime_paths.default_user_data_dir(platform_name, home=home) == home / expected


def test_default_user_data_dir_uses_localappdata_on_windows(tmp_path):
    home = tmp_path / "home"
    local = tmp_path / "LocalAppData"
    assert runtime_paths.default_user_data_dir(
        "win32", home=home, env={"LOCALAPPDATA": str(local)}) == local / "PlainFolio"
    # No LOCALAPPDATA (service account, stripped environment): still per-user.
    assert runtime_paths.default_user_data_dir(
        "win32", home=home, env={}) == home / "AppData" / "Local" / "PlainFolio"


def test_resolve_home_dir_keeps_source_mode_on_the_patched_root(monkeypatch, tmp_path):
    """The compatibility promise of stage 1: tests may replace ``config.ROOT``."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(config, "ROOT", checkout)
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.delenv("LWB_HOME", raising=False)

    assert config.resolve_home_dir() == checkout


def test_resolve_home_dir_uses_the_platform_data_dir_only_when_packaged(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(config, "ROOT", checkout)
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.delenv("LWB_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    monkeypatch.setenv("LWB_PACKAGED", "1")
    assert config.resolve_home_dir() == config.default_user_data_dir()
    assert config.resolve_home_dir() != checkout


def test_resolve_home_dir_still_prefers_env_and_pointer_when_packaged(monkeypatch, tmp_path):
    monkeypatch.setenv("LWB_PACKAGED", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env_home = tmp_path / "env-home"
    env_home.mkdir()
    pointer_home = tmp_path / "pointer-home"
    pointer_home.mkdir()
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({"home": str(pointer_home)}), encoding="utf-8")
    monkeypatch.setenv("LWB_INSTANCE_CONFIG", str(pointer))

    monkeypatch.setenv("LWB_HOME", str(env_home))
    assert config.resolve_home_dir() == env_home.resolve()

    monkeypatch.delenv("LWB_HOME")
    assert config.resolve_home_dir() == pointer_home.resolve()


# --- 只读程序目录与静态资源 -----------------------------------------------

def test_resolve_frontend_dist_prefers_override_then_bundle_then_checkout(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "frontend" / "dist").mkdir(parents=True)
    (checkout / "frontend" / "dist" / "index.html").write_text("<html>", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", checkout)

    assert config.resolve_frontend_dist() == checkout / "frontend" / "dist"

    bundle = tmp_path / "bundle"
    (bundle / "frontend" / "dist").mkdir(parents=True)
    (bundle / "frontend" / "dist" / "index.html").write_text("<html>", encoding="utf-8")
    monkeypatch.setenv("LWB_BUNDLE_ROOT", str(bundle))
    assert config.resolve_frontend_dist() == bundle / "frontend" / "dist"

    explicit = tmp_path / "explicit-dist"
    explicit.mkdir()
    monkeypatch.setenv("LWB_FRONTEND_DIST", str(explicit))
    assert config.resolve_frontend_dist() == explicit


def test_resolve_migrations_and_alembic_ini_prefer_the_bundle(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "backend" / "migrations").mkdir(parents=True)
    (checkout / "backend" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", checkout)

    assert config.resolve_migrations_dir() == checkout / "backend" / "migrations"
    assert config.resolve_alembic_ini() == checkout / "backend" / "alembic.ini"

    bundle = tmp_path / "bundle"
    (bundle / "backend" / "migrations").mkdir(parents=True)
    (bundle / "backend" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    monkeypatch.setenv("LWB_BUNDLE_ROOT", str(bundle))
    assert config.resolve_migrations_dir() == bundle / "backend" / "migrations"
    assert config.resolve_alembic_ini() == bundle / "backend" / "alembic.ini"


def test_load_settings_prefers_embedded_ocr_models_unless_configured(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LWB_HOME", str(home))

    # No bundle: a checkout keeps using its own var/models.
    assert config.load_settings().ocr_model_dir == home / "var" / "models"

    bundle = tmp_path / "bundle"
    models = bundle / "runtime" / "models"
    (models / "rapidocr").mkdir(parents=True)
    (models / "rapidocr" / "det.onnx").write_bytes(b"onnx")
    monkeypatch.setenv("LWB_BUNDLE_ROOT", str(bundle))
    assert config.load_settings().ocr_model_dir == models

    # An explicit setting (environment or .env) still wins over the bundle.
    explicit = tmp_path / "custom-models"
    monkeypatch.setenv("LWB_OCR_MODEL_DIR", str(explicit))
    assert config.load_settings().ocr_model_dir == explicit


def test_resolve_pg_bin_prefers_configured_then_the_embedded_cluster(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    pg_bin = bundle / "runtime" / "postgres" / "bin"
    pg_bin.mkdir(parents=True)
    (pg_bin / "pg_ctl").write_bytes(b"")
    monkeypatch.setenv("LWB_BUNDLE_ROOT", str(bundle))
    monkeypatch.setenv("LWB_HOME", str(tmp_path / "home"))

    assert config.resolve_pg_bin() == pg_bin

    configured = tmp_path / "configured-pg"
    monkeypatch.setenv("LWB_PG_BIN", str(configured))
    assert config.resolve_pg_bin() == configured
    assert config.resolve_pg_bin(configured=tmp_path / "explicit") == tmp_path / "explicit"


def test_doctor_skips_source_tooling_only_in_packaged_mode():
    packaged = doctor.tooling_status(packaged=True, which=lambda _name: None)
    assert [entry[0] for entry in packaged] == ["SKIP", "SKIP"]
    assert all(entry[1] for entry in packaged), "missing node must not fail a packaged build"

    source = doctor.tooling_status(packaged=False, which=lambda _name: None)
    assert [entry[0] for entry in source] == ["MISSING", "MISSING"]
    assert not any(entry[1] for entry in source)

    present = doctor.tooling_status(
        packaged=True, which=lambda name: f"/usr/bin/{name}",
        run=lambda *_a, **_k: types.SimpleNamespace(stdout="v22.1.0\n"))
    assert [entry[0] for entry in present] == ["OK", "OK"]
    assert present[0][2] == "node: v22.1.0"


# --- 桌面入口：子进程构造 -------------------------------------------------

def test_child_command_reenters_the_frozen_binary(tmp_path):
    executable = str(tmp_path / "PlainFolio")
    settings = _settings(tmp_path)

    assert desktop_launcher.child_command(
        "server", settings=settings, frozen=True, executable=executable
    ) == [executable, "--run-server"]
    assert desktop_launcher.child_command(
        "worker", settings=settings, frozen=True, executable=executable
    ) == [executable, "--run-worker"]
    assert desktop_launcher.child_command(
        "mcp", settings=settings, frozen=True, executable=executable
    ) == [executable, "--run-mcp"]
    assert desktop_launcher.child_command(
        "doctor", settings=settings, frozen=True, executable=executable
    ) == [executable, "--run-doctor"]
    assert desktop_launcher.child_command(
        "cli", settings=settings, frozen=True, executable=executable,
        argv=["list-cases", "--limit", "5"],
    ) == [executable, "--run-cli", "list-cases", "--limit", "5"]
    assert desktop_launcher.child_command(
        "start-db", settings=settings, frozen=True, executable=executable
    ) == [executable, "--start-db"]


def test_child_command_uses_modules_in_a_source_checkout(tmp_path):
    settings = replace(_settings(tmp_path), port=8123)

    assert desktop_launcher.child_command("server", settings=settings, frozen=False) == [
        sys.executable, "-m", "uvicorn", "legal_workbench.app:app",
        "--host", "127.0.0.1", "--port", "8123", "--no-access-log",
    ]
    assert desktop_launcher.child_command("worker", settings=settings, frozen=False) == [
        sys.executable, "-m", "legal_workbench.worker",
    ]
    assert desktop_launcher.child_command("mcp", settings=settings, frozen=False) == [
        sys.executable, "-m", "legal_workbench.mcp_server",
    ]
    assert desktop_launcher.child_command("doctor", settings=settings, frozen=False) == [
        sys.executable, "-m", "legal_workbench.doctor",
    ]
    assert desktop_launcher.child_command(
        "cli", settings=settings, frozen=False, argv=["list-cases"]) == [
        sys.executable, "-m", "legal_workbench.cli", "list-cases",
    ]
    assert desktop_launcher.child_command("start-db", settings=settings, frozen=False) == [
        sys.executable, str(config.ROOT / "scripts" / "lwb"), "start-db",
    ]


def test_child_command_rejects_an_unknown_component(tmp_path):
    with pytest.raises(ValueError):
        desktop_launcher.child_command("database", settings=_settings(tmp_path), frozen=True)


def test_initdb_command_requests_a_utf8_builtin_locale_on_windows(tmp_path):
    windows = desktop_launcher.initdb_command(
        tmp_path / "pg", tmp_path / "data", tmp_path / "pw", windows=True)
    assert windows[0].endswith("initdb.exe")
    assert "--locale-provider=builtin" in windows
    assert "--builtin-locale=C.UTF-8" in windows
    assert "--auth=scram-sha-256" in windows
    assert "--encoding=UTF8" in windows

    posix = desktop_launcher.initdb_command(
        tmp_path / "pg", tmp_path / "data", tmp_path / "pw", windows=False)
    assert posix[0].endswith("initdb")
    assert "--locale-provider=builtin" not in posix


# --- 首次运行自动初始化 ---------------------------------------------------

def test_initialization_state_separates_fresh_partial_and_ready(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert desktop_launcher.initialization_state(home) == desktop_launcher.FRESH
    assert desktop_launcher.needs_initialization(home) is True

    (home / "var" / "postgres").mkdir(parents=True)
    assert desktop_launcher.initialization_state(home) == desktop_launcher.PARTIAL
    assert desktop_launcher.needs_initialization(home) is False

    (home / ".env").write_text("LWB_DATABASE_URL=\n", encoding="utf-8")
    assert desktop_launcher.initialization_state(home) == desktop_launcher.READY


def _install_fake_initdb(commands: list[tuple[list[str], dict]]):
    """Record commands and make ``initdb`` create the directory it was given."""
    def runner(args, **kwargs):
        command = [str(item) for item in args]
        commands.append((command, kwargs))
        if Path(command[0]).name.startswith("initdb"):
            data_dir = Path(command[command.index("-D") + 1])
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "postgresql.conf").write_text("", encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    return runner


def test_initialize_home_writes_private_env_and_a_credential_free_pointer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    pg_bin = _fake_pg_bin(tmp_path)
    commands: list[tuple[list[str], dict]] = []
    started: list[int] = []
    stopped: list[int] = []
    migrated: list[int] = []
    pointer = tmp_path / "state" / "instance.json"

    desktop_launcher.initialize_home(
        home, pg_bin=pg_bin, runner=_install_fake_initdb(commands),
        start_db=lambda: started.append(1), stop_db=lambda: stopped.append(1),
        migrate=lambda: migrated.append(1), pointer_path=pointer,
        executable="/Applications/PlainFolio.app/Contents/MacOS/PlainFolio",
        mode="packaged",
    )

    initdb_cmd = commands[0][0]
    assert Path(initdb_cmd[0]).name == "initdb"
    assert "--pwfile" in initdb_cmd
    pwfile = Path(initdb_cmd[initdb_cmd.index("--pwfile") + 1])
    assert not pwfile.exists(), "the temporary password file must not survive"
    assert started == [1] and migrated == [1] and stopped == []

    conf = (home / "var" / "postgres" / "postgresql.conf").read_text(encoding="utf-8")
    assert "listen_addresses = '127.0.0.1'" in conf
    assert "port = 55432" in conf

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "LWB_DATABASE_URL=postgresql://lwb_app:" in env_text
    assert "LWB_MIGRATION_URL=postgresql://lwb_migrator:" in env_text
    app_password = env_text.split("LWB_DATABASE_URL=postgresql://lwb_app:")[1].split("@")[0]
    migration_password = env_text.split("LWB_MIGRATION_URL=postgresql://lwb_migrator:")[1].split("@")[0]
    assert app_password != migration_password, "each role needs its own random password"
    assert len(app_password) >= 32 and len(migration_password) >= 32
    if os.name == "posix":
        assert (home / ".env").stat().st_mode & 0o077 == 0
        assert (home / "var" / "runtime" / "pgpass").stat().st_mode & 0o077 == 0
    pgpass = (home / "var" / "runtime" / "pgpass").read_text(encoding="utf-8")
    assert pgpass.count("\n") == 3
    assert app_password in pgpass and migration_password in pgpass

    payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert payload["home"] == str(home.resolve())
    assert payload["mode"] == "packaged"
    assert payload["executable"] == "/Applications/PlainFolio.app/Contents/MacOS/PlainFolio"
    pointer_text = pointer.read_text(encoding="utf-8")
    for secret in ("postgresql://", "PASSWORD", "password", "lwb_app", "lwb_migrator",
                   "lwb_super", app_password, migration_password):
        assert secret not in pointer_text
    if os.name == "posix":
        assert pointer.stat().st_mode & 0o077 == 0


def test_initialize_home_stops_postgres_and_leaves_no_half_built_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    pg_bin = _fake_pg_bin(tmp_path)
    stopped: list[int] = []
    pointer = tmp_path / "instance.json"

    def failing_runner(args, **kwargs):
        command = [str(item) for item in args]
        if Path(command[0]).name.startswith("initdb"):
            data_dir = Path(command[command.index("-D") + 1])
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "postgresql.conf").write_text("", encoding="utf-8")
            return types.SimpleNamespace(returncode=0)
        if Path(command[0]).name.startswith("createdb"):
            raise subprocess.CalledProcessError(1, command)
        return types.SimpleNamespace(returncode=0)

    with pytest.raises(subprocess.CalledProcessError):
        desktop_launcher.initialize_home(
            home, pg_bin=pg_bin, runner=failing_runner,
            start_db=lambda: None, stop_db=lambda: stopped.append(1),
            migrate=lambda: pytest.fail("migrations must not run after a failed initdb step"),
            pointer_path=pointer, mode="packaged",
        )

    assert stopped == [1], "the temporary cluster must be stopped"
    assert not (home / ".env").exists(), "no .env may claim this home is initialised"
    assert not (home / "var" / "postgres").exists(), "no stray cluster directory may remain"
    assert not pointer.exists(), "a failed install must not be registered"
    assert desktop_launcher.initialization_state(home) == desktop_launcher.FRESH

    log = (home / "var" / "runtime" / "init-failure.log").read_text(encoding="utf-8")
    assert "createdb" in log
    assert "PASSWORD" not in log and "postgresql://" not in log


def test_initialize_home_removes_the_env_when_migration_fails(tmp_path):
    """A failure after ``.env`` is written must not leave a "ready" home."""
    home = tmp_path / "home"
    home.mkdir()
    pg_bin = _fake_pg_bin(tmp_path)
    commands: list[tuple[list[str], dict]] = []
    stopped: list[int] = []
    pointer = tmp_path / "instance.json"

    def failing_migrate() -> None:
        raise RuntimeError("alembic upgrade head 失败")

    with pytest.raises(RuntimeError, match="alembic"):
        desktop_launcher.initialize_home(
            home, pg_bin=pg_bin, runner=_install_fake_initdb(commands),
            start_db=lambda: None, stop_db=lambda: stopped.append(1),
            migrate=failing_migrate, pointer_path=pointer, mode="packaged",
        )

    assert (home / ".env").exists() is False
    assert stopped == [1]
    assert desktop_launcher.initialization_state(home) == desktop_launcher.FRESH
    assert not pointer.exists()


def test_initialize_home_refuses_to_overwrite_an_existing_installation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("LWB_DATABASE_URL=x\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="拒绝重复初始化"):
        desktop_launcher.initialize_home(home, pg_bin=_fake_pg_bin(tmp_path))


# --- 升级前备份 + 迁移 ----------------------------------------------------

def test_ensure_up_to_date_skips_backup_when_the_schema_is_current(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls: list[str] = []

    report = desktop_launcher.ensure_up_to_date(
        home, settings=_settings(tmp_path),
        revision_fn=lambda _settings: "0009_case_metadata",
        head_fn=lambda: "0009_case_metadata",
        backup_fn=lambda _dest: calls.append("backup"),
        migrate_fn=lambda: calls.append("migrate"),
    )

    assert calls == [], "no backup and no migration when there is nothing to apply"
    assert report.migrated is False and report.backup is None
    assert report.current == report.head == "0009_case_metadata"


def test_ensure_up_to_date_backs_up_before_applying_a_pending_migration(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    order: list[str] = []
    destinations: list[Path] = []

    def backup(destination: Path) -> None:
        order.append("backup")
        destinations.append(destination)

    report = desktop_launcher.ensure_up_to_date(
        home, settings=_settings(tmp_path),
        revision_fn=lambda _settings: "0008_claim_amounts",
        head_fn=lambda: "0009_case_metadata",
        backup_fn=backup, migrate_fn=lambda: order.append("migrate"),
    )

    assert order == ["backup", "migrate"], "the backup must complete before the migration"
    assert report.migrated is True
    assert report.current == "0008_claim_amounts"
    assert destinations[0].parent == home / "var" / "backups"
    assert destinations[0].name.startswith("pre-upgrade-")
    assert report.backup == destinations[0]


def test_ensure_up_to_date_aborts_when_the_backup_fails(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    migrated: list[int] = []

    def failing_backup(_destination: Path) -> None:
        raise RuntimeError("备份失败：pg_dump 退出码 1")

    with pytest.raises(RuntimeError, match="备份失败"):
        desktop_launcher.ensure_up_to_date(
            home, settings=_settings(tmp_path),
            revision_fn=lambda _settings: "0008_claim_amounts",
            head_fn=lambda: "0009_case_metadata",
            backup_fn=failing_backup,
            migrate_fn=lambda: migrated.append(1),
        )

    assert migrated == [], "a failed backup must abort before any migration"


def test_ensure_up_to_date_treats_a_missing_alembic_table_as_pending(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    order: list[str] = []

    report = desktop_launcher.ensure_up_to_date(
        home, settings=_settings(tmp_path),
        revision_fn=lambda _settings: None, head_fn=lambda: "0009_case_metadata",
        backup_fn=lambda _dest: order.append("backup"),
        migrate_fn=lambda: order.append("migrate"),
    )

    assert order == ["backup", "migrate"]
    assert report.current is None and report.migrated is True


# --- 打包态 MCP 兼容派发 -------------------------------------------------

def _packaged_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "data-home"
    home.mkdir()
    executable = home / "PlainFolio"
    executable.write_bytes(b"")
    pointer = tmp_path / "instance.json"
    pointer.write_text(json.dumps({
        "home": str(home), "mode": "packaged", "executable": str(executable),
    }), encoding="utf-8")
    return home, executable, pointer


def test_plugin_launcher_dispatches_a_packaged_install(tmp_path):
    module = _load_plugin_launcher()
    home, executable, pointer = _packaged_home(tmp_path)
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    dirs: list[str] = []

    code = module.main(
        [], env={"LWB_INSTANCE_CONFIG": str(pointer)}, script_path=PLUGIN_LAUNCHER,
        exec_fn=lambda path, args, env: calls.append((path, list(args), dict(env))),
        chdir_fn=dirs.append,
    )

    assert code == 0
    path, args, env = calls[0]
    assert path == str(executable)
    assert args == [str(executable), "--run-mcp"]
    assert env["LWB_HOME"] == str(home)
    assert "PYTHONPATH" not in env, "a frozen binary must not import a source tree"
    assert dirs == [str(home)]


def test_plugin_launcher_resolves_a_packaged_home_from_the_env(tmp_path):
    module = _load_plugin_launcher()
    home, _executable, _pointer = _packaged_home(tmp_path)

    resolution = module.resolve_home(
        PLUGIN_LAUNCHER,
        {"LWB_HOME": str(home), "LWB_INSTANCE_CONFIG": str(tmp_path / "missing.json")},
    )

    assert resolution.home == home.resolve()


def test_plugin_launcher_print_plan_reports_a_packaged_interpreter(tmp_path, capsys):
    module = _load_plugin_launcher()
    _home, executable, pointer = _packaged_home(tmp_path)

    code = module.main(
        ["--print-plan"], env={"LWB_INSTANCE_CONFIG": str(pointer)},
        script_path=PLUGIN_LAUNCHER,
        exec_fn=lambda *_args: pytest.fail("--print-plan must not launch the server"),
    )

    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["interpreter"] == "packaged"
    assert plan["command"] == [str(executable), "--run-mcp"]


def test_plugin_launcher_does_not_guess_when_a_packaged_binary_moved(tmp_path):
    module = _load_plugin_launcher()
    home, _executable, _pointer = _packaged_home(tmp_path)
    stale = {"home": str(home), "mode": "packaged",
             "executable": str(tmp_path / "gone" / "PlainFolio")}

    # The directory still holds a binary, but the pointer says this install was
    # moved: launching a different program would be worse than failing.
    assert module.packaged_executable(home, stale) is None


def test_plugin_launcher_auto_start_uses_the_packaged_binary(tmp_path):
    module = _load_plugin_launcher()
    _home, executable, pointer = _packaged_home(tmp_path)
    runs: list[list[str]] = []

    code = module.main(
        [], env={"LWB_INSTANCE_CONFIG": str(pointer), "LWB_MCP_AUTO_START_DB": "1"},
        script_path=PLUGIN_LAUNCHER,
        exec_fn=lambda *_args: None,
        run_fn=lambda command, **_kwargs: runs.append(list(command)) or types.SimpleNamespace(
            returncode=0, stdout=""),
        chdir_fn=lambda _path: None,
    )

    assert code == 0
    assert runs == [[str(executable), "--start-db"]]


# --- 组装与校验脚本 -------------------------------------------------------

def _fake_bundle_source(tmp_path: Path, *, platform_tools: tuple[str, ...] = ()) -> Path:
    root = tmp_path / "repo"
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    versions = root / "backend" / "migrations" / "versions"
    versions.mkdir(parents=True)
    (root / "backend" / "migrations" / "env.py").write_text("", encoding="utf-8")
    (versions / "0001_initial.py").write_text(
        'revision = "0001"\ndown_revision = None\n', encoding="utf-8")
    (root / "backend" / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    plugin = root / "plugins" / "plainfolio"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "plugin.json").write_text("{}", encoding="utf-8")
    (plugin / "scripts" / "run-mcp.py").write_text("", encoding="utf-8")
    return root


def _fake_runtime(tmp_path: Path) -> tuple[Path, Path]:
    pg_root = tmp_path / "pg18"
    (pg_root / "bin").mkdir(parents=True)
    (pg_root / "share").mkdir()
    for tool in ("initdb", "pg_ctl", "postgres", "psql", "createdb", "pg_dump", "pg_restore"):
        (pg_root / "bin" / tool).write_bytes(tool.encode())
    models = tmp_path / "models"
    models.mkdir()
    (models / "manifest.json").write_text("{}", encoding="utf-8")
    (models / "det.onnx").write_bytes(b"onnx")
    return pg_root, models


def _run_script(name: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PACKAGING / name), *args],
        cwd=str(cwd or REPO_ROOT), capture_output=True, text=True, timeout=300,
    )


def test_stage_bundle_validates_and_writes_a_manifest(tmp_path):
    root = _fake_bundle_source(tmp_path)
    pg_root, models = _fake_runtime(tmp_path)
    staging = tmp_path / "staging"

    result = _run_script(
        "stage_bundle.py", "--source-root", str(root), "--staging-dir", str(staging),
        "--platform", "macos", "--pg-dir", str(pg_root), "--models-dir", str(models),
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["schema"] == "plainfolio-bundle/1"
    assert manifest["platform"] == "macos"
    assert manifest["components"]["migrations"]["head"] == "0001"
    assert manifest["components"]["postgres"]["included"] is True
    assert manifest["components"]["models"]["included"] is True

    written = json.loads((staging / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert written["app_version"] == manifest["app_version"]
    assert (staging / "frontend" / "dist" / "index.html").is_file()
    assert (staging / "backend" / "alembic.ini").is_file()
    assert (staging / "backend" / "migrations" / "versions" / "0001_initial.py").is_file()
    assert (staging / "plugins" / "plainfolio" / "plugin.json").is_file()
    assert (staging / "runtime" / "postgres" / "bin" / "pg_ctl").is_file()
    assert (staging / "runtime" / "models" / "rapidocr" / "det.onnx").is_file()

    digest = hashlib.sha256((pg_root / "bin" / "pg_ctl").read_bytes()).hexdigest()
    tools = {entry["name"]: entry for entry in written["components"]["postgres"]["tools"]}
    assert tools["pg_ctl"]["sha256"] == digest
    assert written["components"]["frontend_dist"]["index_sha256"] == hashlib.sha256(
        b"<html></html>").hexdigest()


def test_stage_bundle_rejects_a_missing_postgres_binary(tmp_path):
    root = _fake_bundle_source(tmp_path)
    pg_root, models = _fake_runtime(tmp_path)
    (pg_root / "bin" / "pg_restore").unlink()
    staging = tmp_path / "staging"

    result = _run_script(
        "stage_bundle.py", "--source-root", str(root), "--staging-dir", str(staging),
        "--platform", "macos", "--pg-dir", str(pg_root), "--models-dir", str(models),
    )

    assert result.returncode == 2
    assert "pg_restore" in result.stderr
    assert not (staging / "bundle-manifest.json").exists(), "a rejected payload must not be staged"


def test_stage_bundle_requires_runtime_unless_explicitly_allowed(tmp_path):
    root = _fake_bundle_source(tmp_path)
    staging = tmp_path / "staging"

    strict = _run_script("stage_bundle.py", "--source-root", str(root),
                         "--staging-dir", str(staging), "--platform", "macos")
    assert strict.returncode == 2
    assert "PostgreSQL" in strict.stderr

    relaxed = _run_script("stage_bundle.py", "--source-root", str(root),
                          "--staging-dir", str(staging), "--platform", "macos",
                          "--allow-missing-runtime", "--dry-run")
    assert relaxed.returncode == 0, relaxed.stderr
    manifest = json.loads(relaxed.stdout)
    assert manifest["components"]["postgres"]["included"] is False
    assert not (staging / "bundle-manifest.json").exists(), "--dry-run writes nothing"


def test_stage_bundle_rejects_a_branched_migration_chain(tmp_path):
    root = _fake_bundle_source(tmp_path)
    versions = root / "backend" / "migrations" / "versions"
    (versions / "0002_a.py").write_text('revision = "0002"\ndown_revision = "0001"\n',
                                        encoding="utf-8")
    (versions / "0002_b.py").write_text('revision = "0002b"\ndown_revision = "0001"\n',
                                        encoding="utf-8")

    result = _run_script("stage_bundle.py", "--source-root", str(root),
                         "--staging-dir", str(tmp_path / "staging"),
                         "--platform", "macos", "--allow-missing-runtime", "--dry-run")

    assert result.returncode == 2
    assert "多个 head" in result.stderr


@pytest.mark.skipif(not FRONTEND_BUILT, reason="frontend/dist not built in this checkout")
def test_build_scripts_dry_run_cleanly_on_this_checkout():
    for script in ("build_macos_dmg.py", "build_windows_exe.py"):
        result = _run_script(script, "--dry-run")
        assert result.returncode == 0, f"{script}: {result.stderr}"
        plan = json.loads(result.stdout)
        assert plan["mode"] == "dry-run"
        assert plan["app_version"]
        assert plan["stages"], "a build plan must name its stages"
        assert plan["paths"]["source_root"] == str(REPO_ROOT)


def test_build_macos_dry_run_reports_the_expected_artifacts():
    if not FRONTEND_BUILT:
        pytest.skip("frontend/dist not built in this checkout")
    result = _run_script("build_macos_dmg.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["platform"] == "macos"
    assert plan["paths"]["dmg"].endswith(f"PlainFolio-{plan['app_version']}-macos-{plan['arch']}.dmg")
    assert plan["paths"]["app_bundle"].endswith("PlainFolio.app")
    assert "hdiutil" in plan["tools"]


def test_build_windows_dry_run_validates_the_installer_template():
    if not FRONTEND_BUILT:
        pytest.skip("frontend/dist not built in this checkout")
    result = _run_script("build_windows_exe.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["platform"] == "windows"
    assert plan["paths"]["installer"].endswith(f"PlainFolio-Setup-{plan['app_version']}-x64.exe")
    assert plan["tools"]["iscc"] is None or isinstance(plan["tools"]["iscc"], str)


def test_build_windows_dry_run_rejects_an_unsafe_installer(tmp_path):
    """A template that deletes the user data directory must stop the build."""
    source = tmp_path / "repo"
    (source / "packaging" / "windows").mkdir(parents=True)
    unsafe = ISS.read_text(encoding="utf-8").replace(
        "Type: files; Name: \"{app}\\*.log\"",
        "Type: filesandordirs; Name: \"{localappdata}\\PlainFolio\"",
    )
    (source / "packaging" / "windows" / "plainfolio.iss").write_text(unsafe, encoding="utf-8")

    result = _run_script("build_windows_exe.py", "--dry-run", "--source-root", str(source))
    assert result.returncode == 2
    assert "不得删除用户数据目录" in result.stderr


# --- Inno Setup 模板静态检查 ---------------------------------------------

def test_inno_setup_template_installs_per_user_and_never_touches_case_data():
    text = ISS.read_text(encoding="utf-8")
    assert "DefaultDirName={localappdata}\\Programs\\PlainFolio" in text
    assert "PrivilegesRequired=lowest" in text
    assert "legal-workbench.ico" in text
    assert "chinesesimplified" in text
    assert "addtopath" in text

    sys.path.insert(0, str(PACKAGING))
    try:
        import bundle_common
    finally:
        sys.path.pop(0)
    assert bundle_common.validate_iss(text) == []

    sections = bundle_common.iss_sections(text)
    uninstall = "\n".join(
        line for line in sections.get("uninstalldelete", [])
        if not line.lstrip().startswith(";")).lower()
    assert "{localappdata}" not in uninstall
    assert "var\\data" not in uninstall and "var/data" not in uninstall
    assert "{app}" in uninstall, "only the install directory itself may be cleaned"


def test_gitignore_keeps_build_artifacts_out_but_not_the_tracked_app_bundle():
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("packaging/build/", "packaging/dist/", "packaging/staging/",
                    "*.dmg", "*.exe"):
        assert pattern in text, pattern

    def ignored(path: str) -> bool:
        return subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=str(REPO_ROOT),
            capture_output=True).returncode == 0

    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert ignored("packaging/dist/PlainFolio-0.1.0-macos-arm64.dmg")
    assert ignored("packaging/staging/macos/PlainFolio.app/Contents/Info.plist")
    assert not ignored("法律工作台.app/Contents/Info.plist")


def test_mkdir_private_omits_0o700_on_windows_and_keeps_it_on_posix(monkeypatch, tmp_path):
    calls: list[tuple[Path, dict]] = []

    def fake_mkdir(self, parents=False, exist_ok=False, mode=0o777):
        calls.append((self, {"parents": parents, "exist_ok": exist_ok, "mode": mode}))

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    target = tmp_path / "var" / "runtime"

    monkeypatch.setattr(desktop_launcher.os, "name", "nt")
    desktop_launcher._mkdir_private(target)
    assert calls[-1] == (target, {"parents": True, "exist_ok": True, "mode": 0o777})

    monkeypatch.setattr(desktop_launcher.os, "name", "posix")
    desktop_launcher._mkdir_private(target)
    assert calls[-1] == (target, {"parents": True, "exist_ok": True, "mode": 0o700})
