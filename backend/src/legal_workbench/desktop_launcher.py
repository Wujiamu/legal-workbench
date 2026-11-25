"""Single entry point for the frozen desktop build (macOS ``.app`` / Windows ``.exe``).

A frozen executable has no external ``python`` to delegate to, so this module is
also its own server, worker, MCP host and CLI: ``--run-*`` re-enters the same
binary and runs one component in the foreground.  A source checkout keeps using
``python -m …`` so existing scripts and tests are unaffected.

Responsibilities, in the order they must happen:

1. resolve the read-only bundle and the writable data home;
2. take a file lock so a double click cannot start two stacks;
3. first run: create the cluster, roles, database, ``.env`` and schema, then
   register the instance pointer so agent plugins find it with no setup;
4. upgrade: back up (and verify the backup) before applying a pending
   migration; abort instead of starting against a half-migrated database;
5. start PostgreSQL, the API and exactly one worker, then open the browser;
6. on exit or ``--stop``: stop the children first, then the cluster.

Nothing here duplicates business logic: migrations, backups, the app and the
worker are the same modules the command line uses.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from legal_workbench import backup, config
from legal_workbench.config import Settings, load_settings, pg_env

APP_NAME = "PlainFolio"
APP_VERSION = "0.1.0"

SUPERUSER = "lwb_super"
MIGRATOR = "lwb_migrator"
APP_ROLE = "lwb_app"
DATABASE = "legal_workbench"
PG_PORT = 55432

READY = "ready"
FRESH = "fresh"
PARTIAL = "partial"

CHILD_KINDS = ("server", "worker", "mcp", "cli", "doctor", "start-db")


class AlreadyRunning(RuntimeError):
    """Another launcher holds this home's lock; this one only opens the browser."""


# --- layout -----------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """Every path a running install writes to, derived from the home directory."""

    home: Path

    @property
    def var(self) -> Path:
        return self.home / "var"

    @property
    def postgres(self) -> Path:
        return self.var / "postgres"

    @property
    def runtime(self) -> Path:
        return self.var / "runtime"

    @property
    def data(self) -> Path:
        return self.var / "data"

    @property
    def backups(self) -> Path:
        return self.var / "backups"

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def pgpass(self) -> Path:
        return self.runtime / "pgpass"

    @property
    def postgres_log(self) -> Path:
        return self.runtime / "postgres.log"

    @property
    def lock(self) -> Path:
        return self.runtime / "desktop.lock"

    @property
    def state_file(self) -> Path:
        return self.runtime / "desktop-state.json"

    @property
    def failure_log(self) -> Path:
        return self.runtime / "init-failure.log"


def layout_for(home: Path | str) -> Layout:
    return Layout(Path(home).expanduser())


def initialization_state(home: Path | str) -> str:
    """``ready`` / ``fresh`` / ``partial`` for the writable home.

    ``partial`` is the dangerous middle: a cluster directory without ``.env``
    means an earlier initialisation died halfway.  Treating that as ``fresh``
    would run ``initdb`` into a non-empty directory; treating it as ``ready``
    would start a server with no credentials.
    """
    home = Path(home)
    if (home / ".env").is_file():
        return READY
    if (home / "var" / "postgres").is_dir():
        return PARTIAL
    return FRESH


def needs_initialization(home: Path | str) -> bool:
    return initialization_state(home) == FRESH


# --- child processes --------------------------------------------------------

def child_command(kind: str, *, settings: Settings | None = None,
                  frozen: bool | None = None, executable: str | None = None,
                  argv: Sequence[str] = ()) -> list[str]:
    """Command line for one component, in this build's own terms.

    Frozen builds re-enter the binary (``--run-server`` …) because there is no
    interpreter to import the package; checkouts keep the ``-m`` module form so
    a developer's ``python -m uvicorn`` keeps working exactly as before.
    """
    if kind not in CHILD_KINDS:
        raise ValueError(f"unknown child kind: {kind}")
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = executable or sys.executable
    if is_frozen:
        if kind == "cli":
            return [exe, "--run-cli", *argv]
        return [exe, f"--run-{kind}" if kind != "start-db" else "--start-db"]
    settings = settings or load_settings()
    if kind == "server":
        return [exe, "-m", "uvicorn", "legal_workbench.app:app",
                "--host", settings.host, "--port", str(settings.port), "--no-access-log"]
    if kind == "worker":
        return [exe, "-m", "legal_workbench.worker"]
    if kind == "mcp":
        return [exe, "-m", "legal_workbench.mcp_server"]
    if kind == "doctor":
        return [exe, "-m", "legal_workbench.doctor"]
    if kind == "cli":
        return [exe, "-m", "legal_workbench.cli", *argv]
    # start-db has no module of its own; the lifecycle script owns the cluster.
    return [exe, str(config.ROOT / "scripts" / "lwb"), "start-db"]


def _hidden_kwargs(*, capture: bool = False) -> dict:
    if os.name != "nt":
        return {}
    if capture:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _spawn_kwargs() -> dict:
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if hasattr(stream, "reconfigure") and (sys.platform == "win32" or encoding != "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _pg_tool(pg_bin: Path, name: str) -> str:
    found = shutil.which(name, path=str(pg_bin))
    if found:
        return found
    suffix = ".exe" if os.name == "nt" else ""
    return str(Path(pg_bin) / f"{name}{suffix}")


def initdb_command(pg_bin: Path | str, data_dir: Path | str, pwfile: Path | str, *,
                   windows: bool | None = None) -> list[str]:
    """``initdb`` arguments for a fresh cluster.

    Windows ANSI code pages (e.g. CP936) conflict with ``--encoding=UTF8``
    unless a UTF-8 builtin locale is requested explicitly; POSIX keeps the
    system locale so existing installs behave exactly as before.
    """
    is_windows = (os.name == "nt") if windows is None else windows
    suffix = ".exe" if is_windows else ""
    binary = (shutil.which(f"initdb{suffix}", path=str(pg_bin))
              or str(Path(pg_bin) / f"initdb{suffix}"))
    command = [binary, "-D", str(data_dir), "-U", SUPERUSER,
               "--auth=scram-sha-256", "--pwfile", str(pwfile), "--encoding=UTF8"]
    if is_windows:
        command.extend(["--locale-provider=builtin", "--builtin-locale=C.UTF-8"])
    return command


def require_pg_bin(settings: Settings | None = None) -> Path:
    pg_bin = config.resolve_pg_bin(settings=settings)
    if pg_bin is None:
        raise RuntimeError(
            "找不到 PostgreSQL 18 工具目录：打包版应内置 runtime/postgres，"
            "源码版请安装 PostgreSQL 18 或在 .env 中设置 LWB_PG_BIN"
        )
    return Path(pg_bin)


def database_running(layout: Layout, pg_bin: Path) -> bool:
    if not layout.postgres.is_dir():
        return False
    status = subprocess.run(
        [_pg_tool(pg_bin, "pg_ctl"), "-D", str(layout.postgres), "status"],
        capture_output=True, env=pg_env(), **_hidden_kwargs(capture=True),
    )
    return status.returncode == 0


def _mkdir_private(path: Path) -> None:
    """Create a private directory without tripping Python 3.13's Windows 0o700 DACL.

    On Windows, Python 3.13 ``mkdir(mode=0o700)`` builds a restrictive DACL for
    SYSTEM + Administrators + OWNER RIGHTS and strips inherited user ACEs; when
    an elevated process launches PostgreSQL tools, ``CreateRestrictedToken``
    disables Administrators and locks PostgreSQL out of its own directory.
    """
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


def start_database(layout: Layout, pg_bin: Path) -> None:
    """Start the cluster if it is not already running (idempotent)."""
    if database_running(layout, pg_bin):
        return
    _mkdir_private(layout.runtime)
    subprocess.run(
        [_pg_tool(pg_bin, "pg_ctl"), "-D", str(layout.postgres),
         "-l", str(layout.postgres_log), "-w", "start"],
        check=True, env=pg_env(), **_hidden_kwargs(),
    )


def stop_database(layout: Layout, pg_bin: Path | None) -> bool:
    """Fast shutdown; ``False`` when the cluster was already stopped."""
    if pg_bin is None or not layout.postgres.is_dir():
        return False
    pg_ctl = _pg_tool(Path(pg_bin), "pg_ctl")
    status = subprocess.run(
        [pg_ctl, "-D", str(layout.postgres), "status"],
        capture_output=True, env=pg_env(), **_hidden_kwargs(capture=True),
    )
    if status.returncode != 0:
        return False
    subprocess.run(
        [pg_ctl, "-D", str(layout.postgres), "-w", "-m", "fast", "stop"],
        check=False, capture_output=True, env=pg_env(), **_hidden_kwargs(capture=True),
    )
    return True


# --- first run --------------------------------------------------------------

def _write_private(path: Path, text: str) -> None:
    """Create ``path`` with ``0600`` and never overwrite an existing file."""
    _mkdir_private(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _failure_text(exc: BaseException) -> str:
    """Describe a failure without echoing command lines that carry passwords."""
    if isinstance(exc, subprocess.CalledProcessError):
        tool = Path(str(exc.cmd[0])).name if exc.cmd else "<command>"
        return f"命令 {tool} 退出码 {exc.returncode}"
    return f"{type(exc).__name__}: {exc}"


def _default_runner(args, **kwargs):
    return subprocess.run([str(item) for item in args], check=True,
                          env=pg_env(kwargs.pop("env", None)), **kwargs)


def _abort_initialization(layout: Layout, stop: Callable[[], object],
                          exc: BaseException) -> None:
    """Stop the temporary cluster and leave no half-built home behind.

    A stray ``var/postgres`` next to no ``.env`` is exactly the state that would
    make the next start believe the install is broken (or, worse, usable).  The
    directory holds only bytes this function just created, so removing it is
    safe; the reason is kept in ``init-failure.log`` for support.
    """
    try:
        stop()
    except Exception:  # noqa: BLE001 - shutdown must never mask the real error
        pass
    for target in (layout.env_file, layout.pgpass, layout.runtime / "init-password"):
        target.unlink(missing_ok=True)
    shutil.rmtree(layout.postgres, ignore_errors=True)
    _mkdir_private(layout.runtime)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        layout.failure_log.write_text(
            f"at={stamp}\nhome={layout.home}\nreason={_failure_text(exc)[:2000]}\n",
            encoding="utf-8")
    except OSError:
        pass


def initialize_home(home: Path | str, *, pg_bin: Path | str,
                    runner: Callable[..., object] | None = None,
                    start_db: Callable[[], object] | None = None,
                    stop_db: Callable[[], object] | None = None,
                    migrate: Callable[[], object] | None = None,
                    pointer_path: Path | None = None,
                    executable: str | Path | None = None,
                    mode: str | None = None) -> Path:
    """Create a complete, working installation under ``home``.

    Everything a first run needs happens here — cluster, roles, database,
    ``.env``, schema and the instance pointer — because a desktop user has no
    terminal to run four more commands in.
    """
    layout = layout_for(home)
    if initialization_state(layout.home) != FRESH:
        raise RuntimeError(f"{layout.home} 已有 .env 或数据库目录，拒绝重复初始化")
    pg_bin = Path(pg_bin)
    run = _default_runner if runner is None else runner
    start = (lambda: start_database(layout, pg_bin)) if start_db is None else start_db
    stop = (lambda: stop_database(layout, pg_bin)) if stop_db is None else stop_db
    migrate_db = apply_migrations if migrate is None else migrate

    _mkdir_private(layout.runtime)
    _mkdir_private(layout.data)
    _mkdir_private(layout.backups)
    super_pw, migrator_pw, app_pw = (secrets.token_urlsafe(32) for _ in range(3))
    pwfile = layout.runtime / "init-password"
    pwfile.unlink(missing_ok=True)  # a hard-killed earlier attempt may have left one
    _write_private(pwfile, super_pw + "\n")
    try:
        run(initdb_command(pg_bin, layout.postgres, pwfile))
    finally:
        pwfile.unlink(missing_ok=True)

    with (layout.postgres / "postgresql.conf").open("a", encoding="utf-8") as handle:
        handle.write(f"\nlisten_addresses = '127.0.0.1'\nport = {PG_PORT}\n")
    _write_private(
        layout.pgpass,
        f"127.0.0.1:{PG_PORT}:*:{SUPERUSER}:{super_pw}\n"
        f"127.0.0.1:{PG_PORT}:*:{MIGRATOR}:{migrator_pw}\n"
        f"127.0.0.1:{PG_PORT}:*:{APP_ROLE}:{app_pw}\n",
    )
    try:
        start()
        # Every client below must address the fresh cluster: without this env,
        # psql falls back to the default socket on 5432 and the roles land in
        # whatever instance happens to be listening there.
        env = pg_env(extra={
            "PGHOST": "127.0.0.1", "PGPORT": str(PG_PORT),
            "PGUSER": SUPERUSER, "PGPASSFILE": str(layout.pgpass),
        })
        psql = _pg_tool(pg_bin, "psql")
        # The migration role owns schema objects; the application role gets DML
        # only (granted by the migrations themselves).
        run([psql, "-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c",
             f"CREATE ROLE {MIGRATOR} LOGIN CREATEDB PASSWORD '{migrator_pw}'"], env=env)
        run([psql, "-v", "ON_ERROR_STOP=1", "-d", "postgres", "-c",
             f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{app_pw}'"], env=env)
        run([_pg_tool(pg_bin, "createdb"), "-O", MIGRATOR, DATABASE], env=env)
        _write_private(
            layout.env_file,
            f"LWB_DATABASE_URL=postgresql://{APP_ROLE}:{app_pw}@127.0.0.1:{PG_PORT}/{DATABASE}\n"
            f"LWB_MIGRATION_URL=postgresql://{MIGRATOR}:{migrator_pw}@127.0.0.1:{PG_PORT}/{DATABASE}\n"
            f"LWB_PG_BIN={pg_bin}\n",
        )
        migrate_db()
    except BaseException as exc:
        _abort_initialization(layout, stop, exc)
        raise
    config.write_instance_pointer(
        layout.home, config.resolve_bundle_root(), config_path=pointer_path,
        executable=executable, mode=mode,
    )
    return layout.home


# --- migrations -------------------------------------------------------------

def alembic_config():
    """Alembic config pointing at the bundle's (or checkout's) migration chain."""
    from alembic.config import Config

    ini = config.resolve_alembic_ini()
    cfg = Config(str(ini)) if ini.is_file() else Config()
    cfg.set_main_option("script_location", str(config.resolve_migrations_dir()))
    source = config.resolve_bundle_root() / "backend" / "src"
    cfg.set_main_option("prepend_sys_path", str(source) if source.is_dir() else "")
    return cfg


def code_head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head() or "head"


def database_revision(settings: Settings | None = None) -> str | None:
    """Applied Alembic revision, or ``None`` when the schema was never migrated.

    Connection failures propagate: a database that cannot be reached must abort
    startup, not look like "needs migration".
    """
    import psycopg

    settings = settings or load_settings()
    url = settings.migration_url or settings.database_url
    if not url:
        raise RuntimeError("LWB_DATABASE_URL 未配置，无法检查数据库版本")
    with psycopg.connect(url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.alembic_version')")
            present = cursor.fetchone()
            if not present or present[0] is None:
                return None
            cursor.execute("SELECT version_num FROM alembic_version")
            row = cursor.fetchone()
    return row[0] if row else None


def _backfill_progress() -> int:
    from legal_workbench import progress
    from legal_workbench.db import session_scope

    with session_scope() as session:
        return progress.backfill_default_fragments(session)


def apply_migrations() -> None:
    """``alembic upgrade head`` plus the idempotent progress backfill."""
    from alembic import command

    command.upgrade(alembic_config(), "head")
    _backfill_progress()


def backup_before_upgrade(settings: Settings, destination: Path) -> backup.BackupResult:
    """Full, verified backup of the database and every managed file."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from legal_workbench.db import sqlalchemy_url
    from legal_workbench.storage import Storage

    engine = create_engine(sqlalchemy_url(settings.database_url), future=True)
    try:
        with Session(bind=engine) as session:
            result = backup.create_backup(
                session, Storage(settings.data_root), destination,
                database_url=settings.database_url,
                pg_bin=str(settings.pg_bin) if settings.pg_bin else None,
            )
    finally:
        engine.dispose()
    if not result.complete:
        raise RuntimeError("升级前备份未标记完成，已中止启动")
    # A dump that pg_dump wrote is not a backup until every hash re-verifies.
    backup.verify_backup(destination)
    return result


@dataclass(frozen=True)
class UpgradeReport:
    current: str | None
    head: str
    backup: Path | None = None
    migrated: bool = False


def ensure_up_to_date(home: Path | str, *, settings: Settings | None = None,
                      revision_fn: Callable[[Settings], str | None] | None = None,
                      head_fn: Callable[[], str] | None = None,
                      backup_fn: Callable[[Path], object] | None = None,
                      migrate_fn: Callable[[], object] | None = None) -> UpgradeReport:
    """Back up before a pending migration; never migrate without a good backup."""
    settings = settings or load_settings()
    current = (revision_fn or database_revision)(settings)
    head = (head_fn or code_head_revision)()
    if current == head:
        return UpgradeReport(current=current, head=head)
    destination = (layout_for(home).backups
                   / f"pre-upgrade-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    make_backup = backup_fn or (lambda target: backup_before_upgrade(settings, target))
    make_backup(destination)
    (migrate_fn or apply_migrations)()
    return UpgradeReport(current=current, head=head, backup=destination, migrated=True)


# --- single instance --------------------------------------------------------

def _acquire_lock(handle, *, timeout: float, sleep: Callable[[float], None]) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise AlreadyRunning("另一个 PlainFolio 实例正在启动")
            sleep(0.1)


def _release_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def instance_lock(path: Path, *, timeout: float = 15.0,
                  sleep: Callable[[float], None] = time.sleep):
    """Exclusive per-home lock; a second double click waits, then gives up."""
    _mkdir_private(path.parent)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        _acquire_lock(handle, timeout=timeout, sleep=sleep)
        try:
            yield
        finally:
            _release_lock(handle)
    finally:
        handle.close()


# --- runtime ----------------------------------------------------------------

def base_url(settings: Settings) -> str:
    return f"http://{settings.host}:{settings.port}"


def service_ready(settings: Settings, *, timeout: float = 2.0) -> bool:
    """A live HTTP process is not ready until it can read from the database."""
    local = build_opener(ProxyHandler({}))
    try:
        with local.open(base_url(settings) + "/api/v1/health", timeout=timeout) as response:
            if response.read(128) != b'{"app":"legal-workbench"}':
                return False
    except (URLError, TimeoutError, OSError):
        return False
    try:
        with local.open(base_url(settings) + "/api/v1/cases?limit=1", timeout=timeout) as response:
            return response.status == 200
    except (URLError, TimeoutError, OSError):
        return False


def wait_until_ready(server, probe: Callable[[], bool], *,
                     timeout: float = 90.0, sleep: Callable[[float], None] = time.sleep) -> None:
    deadline = time.monotonic() + timeout
    while not probe():
        if getattr(server, "poll", lambda: None)() is not None:
            raise RuntimeError("工作台服务启动失败，请查看 var/runtime 下的日志")
        if time.monotonic() >= deadline:
            raise RuntimeError("工作台服务未能及时启动，请查看 var/runtime 下的日志")
        sleep(0.25)


def open_browser(url: str) -> None:
    webbrowser.open(url)


def _terminate(children: list) -> None:
    for child in children:
        if getattr(child, "poll", lambda: 0)() is None:
            try:
                child.terminate()
            except OSError:
                pass
    for child in children:
        try:
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001 - a stuck child must not block exit
            try:
                child.kill()
            except OSError:
                pass


def _write_state(path: Path, payload: dict) -> None:
    _mkdir_private(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _raise_keyboard_interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def _install_signal_handlers() -> dict:
    installed = {}
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            installed[number] = signal.signal(number, _raise_keyboard_interrupt)
        except (ValueError, OSError, RuntimeError):
            continue
    return installed


def _restore_signal_handlers(installed: dict) -> None:
    for number, previous in installed.items():
        try:
            signal.signal(number, previous)
        except (ValueError, OSError, RuntimeError):
            continue


def start_stack(layout: Layout, settings: Settings, *, pg_bin: Path,
                headless: bool = False,
                popen: Callable[..., object] | None = None,
                probe: Callable[[], bool] | None = None,
                open_browser_fn: Callable[[str], object] | None = None) -> int:
    """Run the API and one worker until the API exits; always stop both."""
    spawn = subprocess.Popen if popen is None else popen
    ready = (lambda: service_ready(settings)) if probe is None else probe
    opener = open_browser if open_browser_fn is None else open_browser_fn
    children: list = []
    installed = _install_signal_handlers()
    try:
        worker = spawn(child_command("worker", settings=settings), cwd=str(layout.home),
                       stdin=subprocess.DEVNULL, **_spawn_kwargs())
        children.append(worker)
        server = spawn(child_command("server", settings=settings), cwd=str(layout.home),
                       stdin=subprocess.DEVNULL, **_spawn_kwargs())
        children.append(server)
        _write_state(layout.state_file, {
            "launcher_pid": os.getpid(),
            "server_pid": getattr(server, "pid", None),
            "worker_pid": getattr(worker, "pid", None),
            "home": str(layout.home),
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        })
        wait_until_ready(server, ready)
        if not headless:
            opener(base_url(settings) + "/")
        return int(server.wait())  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        return 0
    finally:
        _restore_signal_handlers(installed)
        _terminate(children)
        try:
            stop_database(layout, pg_bin)
        except OSError:
            pass
        layout.state_file.unlink(missing_ok=True)


# --- commands ---------------------------------------------------------------

def _run_server() -> int:
    import uvicorn

    from legal_workbench.app import app

    settings = load_settings()
    uvicorn.run(app, host=settings.host, port=settings.port,
                access_log=False, log_config=None)
    return 0


def _run_worker() -> int:
    from legal_workbench.db import get_engine
    from legal_workbench.storage import Storage
    from legal_workbench.worker import run_worker

    settings = load_settings()
    run_worker(get_engine(), Storage(settings.data_root), once=False)
    return 0


def _run_mcp() -> int:
    from legal_workbench.mcp_server import main as mcp_main

    mcp_main()
    return 0


def _run_cli(argv: Sequence[str]) -> int:
    from legal_workbench.cli import main as cli_main

    return int(cli_main(list(argv)) or 0)


def _run_doctor() -> int:
    from legal_workbench.doctor import main as doctor_main

    return int(doctor_main() or 0)


def _prepare_home() -> Path:
    """Resolve the writable home, create it, and pin it for every child."""
    home = config.resolve_home_dir()
    _mkdir_private(home)
    os.environ[config.HOME_ENV] = str(home)
    return home


def _start_db_command() -> int:
    home = _prepare_home()
    layout = layout_for(home)
    start_database(layout, require_pg_bin())
    return 0


def _packaged_executable() -> str | None:
    """Path recorded in the instance pointer for plugin launchers.

    Only a frozen build writes it: under ``LWB_PACKAGED=1`` in a checkout
    ``sys.executable`` is a plain interpreter, and advertising it as a desktop
    binary would send the MCP wrapper to the wrong program.
    """
    if not getattr(sys, "frozen", False):
        return None
    return str(Path(sys.executable).resolve())


def _mode_name() -> str:
    return "packaged" if config.is_packaged_mode() else "source"


def launch(home: Path, *, headless: bool = False, init_only: bool = False) -> int:
    """Bring the install up: initialise if needed, upgrade safely, then serve."""
    layout = layout_for(home)
    try:
        with instance_lock(layout.lock):
            state = initialization_state(home)
            if state == PARTIAL:
                raise RuntimeError(
                    f"{layout.postgres} 存在但缺少 .env：上次初始化未完成。"
                    f"请查看 {layout.failure_log} 后重试。")
            pg_bin = require_pg_bin()
            if state == FRESH:
                initialize_home(home, pg_bin=pg_bin,
                                executable=_packaged_executable(), mode=_mode_name())
            if init_only:
                return 0
            settings = load_settings()
            start_database(layout, pg_bin)
            ensure_up_to_date(home, settings=settings)
            return start_stack(layout, settings, pg_bin=pg_bin, headless=headless)
    except AlreadyRunning:
        # Another window won the race; show the workbench it is starting.
        settings = load_settings()
        open_browser(base_url(settings) + "/")
        return 0


def status(home: Path) -> int:
    layout = layout_for(home)
    settings = load_settings()
    pg_bin = config.resolve_pg_bin(settings=settings)
    running = database_running(layout, pg_bin) if pg_bin is not None else False
    revision = None
    if running:
        try:
            revision = database_revision(settings)
        except Exception:  # noqa: BLE001 - status is diagnostics, never fatal
            revision = "unavailable"
    payload = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "home": str(layout.home),
        "bundle_root": str(config.resolve_bundle_root()),
        "packaged": config.is_packaged_mode(),
        "port": settings.port,
        "postgres": running,
        "service": service_ready(settings),
        "revision": revision,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["postgres"] and payload["service"] else 1


def _pid_alive(pid: int) -> bool:
    """Liveness probe that is safe on Windows.

    ``os.kill(pid, 0)`` is not a probe on Windows — CPython maps any non-console
    signal to ``TerminateProcess`` — so the process handle is queried instead.
    """
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def stop(home: Path) -> int:
    """Stop the running stack: launcher first (it stops its children), then PG."""
    layout = layout_for(home)
    state = _read_state(layout.state_file)
    pid = state.get("launcher_pid")
    terminated = False
    if isinstance(pid, int) and pid > 0 and pid != os.getpid():
        terminated = _terminate_pid(pid)
        deadline = time.monotonic() + 15
        while terminated and time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
    stopped = stop_database(layout, config.resolve_pg_bin())
    layout.state_file.unlink(missing_ok=True)
    print(json.dumps({"launcher_terminated": terminated, "postgres_stopped": stopped},
                     ensure_ascii=False))
    return 0


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lwb-desktop",
        description="素章 PlainFolio 桌面工作台（打包入口；也用于源码态调试）")
    parser.add_argument("--headless", action="store_true",
                        help="只启动数据库、迁移、服务与 worker，不打开浏览器")
    parser.add_argument("--init-only", action="store_true",
                        help="只完成首次初始化，不启动服务")
    parser.add_argument("--status", action="store_true", help="打印运行状态（JSON）")
    parser.add_argument("--stop", action="store_true", help="停止服务、worker 与数据库")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    # Child modes come first: they must not take the lock, initialise, or
    # migrate again — the parent already did all of that before spawning them.
    if args[:1] == ["--run-server"]:
        return _run_server()
    if args[:1] == ["--run-worker"]:
        return _run_worker()
    if args[:1] == ["--run-mcp"]:
        return _run_mcp()
    if args[:1] == ["--run-doctor"]:
        return _run_doctor()
    if args[:1] == ["--run-cli"]:
        return _run_cli(args[1:])
    if args[:1] == ["--start-db"]:
        return _start_db_command()

    parsed = _parse(args)
    if parsed.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    home = _prepare_home()
    if parsed.status:
        return status(home)
    if parsed.stop:
        return stop(home)
    return launch(home, headless=parsed.headless, init_only=parsed.init_only)


if __name__ == "__main__":
    raise SystemExit(main())
