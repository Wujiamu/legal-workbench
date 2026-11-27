"""Windows cross-platform compatibility tests (no real Windows host required)."""
from __future__ import annotations

import dataclasses
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
import uuid

from PIL import Image
import pytest

from legal_workbench import backup, config, imports
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import ocr as ocr_module
from legal_workbench import storage as storage_module
from legal_workbench.storage import Storage, safe_name

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_pg_bin_and_windows_env(monkeypatch, tmp_path):
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    fake_pg = tmp_path / "Program Files" / "PostgreSQL" / "18" / "bin"
    fake_pg.mkdir(parents=True)
    (fake_pg / "pg_ctl.exe").write_bytes(b"")
    (fake_root / ".env").write_text(f"LWB_PG_BIN={fake_pg}\n", encoding="utf-8")

    monkeypatch.setattr(config, "ROOT", fake_root)
    monkeypatch.delenv("LWB_PG_BIN", raising=False)
    loaded = config.load_settings()
    assert loaded.pg_bin == fake_pg
    assert config.resolve_pg_bin(settings=loaded) == fake_pg

    # Auto-probe Windows ProgramFiles when LWB_PG_BIN is unset and PATH misses pg_ctl.
    (fake_root / ".env").unlink()
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)
    assert config.resolve_pg_bin() == fake_pg

    win_env = config.pg_env(base={"PATH": "x"})
    assert win_env["PGCLIENTENCODING"] == "UTF8"
    assert win_env["PYTHONUTF8"] == "1"
    assert "LC_ALL" not in win_env


def test_ocr_thread_timeout_fallback_without_sigalrm(monkeypatch):
    monkeypatch.setattr(ocr_module, "signal", SimpleNamespace())

    class _SlowEngine:
        def __call__(self, _image):
            threading.Event().wait(1.0)
            return None, 0.0

    class _FailingEngine:
        def __call__(self, _image):
            raise RuntimeError("ResizeImgError: genuine resize failure")

    class _OkEngine:
        def __call__(self, _image):
            return [([[0, 0], [10, 0], [10, 10], [0, 10]], "测试文字", 0.99)], 0.01

    def _wrap(stub):
        engine = object.__new__(ocr_module.OcrEngine)
        engine.settings = dataclasses.replace(config.load_settings(), parse_timeout_seconds=1)
        engine._engine = stub
        return engine

    img = Image.new("RGB", (8, 8), "white")
    with pytest.raises(TimeoutError, match="parse timeout"):
        _wrap(_SlowEngine()).recognize(img, timeout=0.05)

    with pytest.raises(RuntimeError, match="genuine resize failure"):
        _wrap(_FailingEngine()).recognize(img, timeout=1.0)

    page = _wrap(_OkEngine()).recognize(img, timeout=1.0)
    assert page.text == "测试文字"
    assert page.mean_score == pytest.approx(0.99)


def test_storage_windows_safe_name_and_path_guards(monkeypatch, tmp_path):
    assert safe_name("CON") == "_CON"
    assert safe_name("con.txt") == "_con.txt"
    assert safe_name("CON .txt") == "_CON .txt"
    assert safe_name("CON" + " " * 120 + ".verylongextension") == "_CON"
    assert safe_name(". hidden") == "hidden"
    assert safe_name("CONIN$") == "_CONIN$"
    assert safe_name("COM¹.txt") == "_COM¹.txt"
    assert safe_name("NUL.pdf") == "_NUL.pdf"
    assert safe_name("COM1.docx") == "_COM1.docx"
    assert safe_name("lpt9") == "_lpt9"
    assert safe_name("合同:第1号?*.pdf") == "合同_第1号__.pdf"
    assert safe_name("report. . ") == "report"
    assert safe_name("合同.v2.docx") == "合同.v2.docx"
    assert safe_name("2026.11.23.合同（终版）.pdf") == "2026.11.23.合同（终版）.pdf"

    store = Storage(root=tmp_path / "data")
    for bad in (r"cases\a\b.txt", "C:foo.txt", "C:/foo.txt", r"C:\foo.txt"):
        with pytest.raises(WorkbenchError) as exc:
            store.managed_path(bad)
        assert exc.value.code == ErrorCode.INVALID_PATH

    case_id = uuid.uuid4()
    junction_dir = store.root / "cases" / str(case_id) / "junction"
    junction_dir.mkdir(parents=True)
    monkeypatch.setattr(
        storage_module, "is_link_or_junction",
        lambda p: p.name == "junction" or p.is_symlink(),
    )
    with pytest.raises(WorkbenchError) as exc:
        store.managed_path(f"cases/{case_id}/junction/file.txt")
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_imports_rejects_junction_source_and_entry(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("hello", encoding="utf-8")
    jdir = root / "jdir"
    jdir.mkdir()
    (jdir / "nested.txt").write_text("skip", encoding="utf-8")

    monkeypatch.setattr(
        imports, "is_link_or_junction",
        lambda p: p.name == "jdir" or p.is_symlink(),
    )
    entries = imports.scan_source(root, root)
    by_rel = {rel: reason for rel, _path, reason in entries}
    assert by_rel["ok.txt"] is None
    assert by_rel["jdir"] == "symlink_skipped"
    assert "jdir/nested.txt" not in by_rel

    settings = dataclasses.replace(config.load_settings(), import_roots=(root,))
    with pytest.raises(WorkbenchError) as exc:
        imports.resolve_source(str(jdir), settings)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_lwb_windows_lifecycle_branches(monkeypatch, tmp_path):
    lwb = _load_script("lwb_win_test", REPO_ROOT / "scripts" / "lwb")
    fake_root = tmp_path / "workspace"
    pg_bin = tmp_path / "pg18" / "bin"
    pg_bin.mkdir(parents=True)
    for exe in ("initdb.exe", "pg_ctl.exe", "psql.exe", "createdb.exe"):
        (pg_bin / exe).write_bytes(b"")
    venv_py = fake_root / "backend" / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")

    monkeypatch.setattr(lwb, "ROOT", fake_root)
    monkeypatch.setattr(lwb, "DATA", fake_root / "var" / "postgres")
    monkeypatch.setattr(lwb, "RUNTIME", fake_root / "var" / "runtime")
    monkeypatch.setattr(lwb, "ENV_FILE", fake_root / ".env")
    monkeypatch.setattr(lwb, "_is_windows", lambda: True)
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setattr(lwb, "resolve_pg_bin", lambda: pg_bin)
    monkeypatch.setenv("LWB_PG_BIN", str(pg_bin))

    commands: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        cmd = [str(x) for x in args]
        commands.append((cmd, kwargs))
        if cmd[0].endswith("initdb.exe"):
            data_dir = Path(cmd[cmd.index("-D") + 1])
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "postgresql.conf").write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(lwb, "run", fake_run)
    monkeypatch.setattr(lwb, "start_db", lambda: None)
    monkeypatch.setattr(lwb, "migrate", lambda: None)
    monkeypatch.setattr(lwb, "start", lambda: 0)

    assert lwb.initialize() == 0
    initdb_cmd = commands[0][0]
    assert initdb_cmd[0].endswith("initdb.exe")
    assert "--locale-provider=builtin" in initdb_cmd
    assert "--builtin-locale=C.UTF-8" in initdb_cmd
    env_text = (fake_root / ".env").read_text(encoding="utf-8")
    assert f"LWB_PG_BIN={pg_bin}" in env_text
    assert lwb._backend_cmd(["-m", "legal_workbench.worker"], ["python"]) == [
        str(venv_py), "-m", "legal_workbench.worker",
    ]

    ran_exec: list[list[str]] = []
    monkeypatch.setattr(
        lwb.subprocess, "run",
        lambda cmd, **_kw: ran_exec.append(cmd) or SimpleNamespace(returncode=0),
    )
    assert lwb._exec_or_run([str(venv_py), "-m", "legal_workbench.doctor"]) == 0
    assert ran_exec == [[str(venv_py), "-m", "legal_workbench.doctor"]]

    err = subprocess.CalledProcessError(
        1, ["psql", "-c", "CREATE ROLE lwb_app PASSWORD 'top-secret-pw'"]
    )
    msg = lwb._format_failure(err)
    assert "top-secret-pw" not in msg
    assert "psql" in msg and "退出码 1" in msg


def test_lwb_launch_windows_lock_spawn_and_browser(monkeypatch, tmp_path):
    mod = _load_script("lwb_launch_win_test", REPO_ROOT / "scripts" / "lwb-launch")
    fake_root = tmp_path / "workspace"
    (fake_root / "frontend" / "dist").mkdir(parents=True)
    (fake_root / "frontend" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    runtime = fake_root / "var" / "runtime"
    monkeypatch.setattr(mod, "ROOT", fake_root)
    monkeypatch.setattr(mod, "RUNTIME", runtime)
    monkeypatch.setattr(mod, "LOG", runtime / "launcher.log")

    monkeypatch.setattr(mod, "_is_windows", lambda: True)
    monkeypatch.setattr(mod, "_is_macos", lambda: False)
    monkeypatch.setattr(mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    lock_ops: list[tuple[int, int]] = []
    fake_msvcrt = ModuleType("msvcrt")
    fake_msvcrt.LK_NBLCK = 2
    fake_msvcrt.LK_UNLCK = 0
    fake_msvcrt.locking = lambda _fd, mode, size: lock_ops.append((mode, size))
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    started_urls: list[str] = []
    monkeypatch.setattr(mod.os, "startfile", started_urls.append, raising=False)

    popen_calls: list[tuple[list[str], dict]] = []
    probes = iter([(False, False), (True, True)])

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return SimpleNamespace(poll=lambda: None)

    mod.open_workbench(
        probe_fn=lambda: next(probes),
        popen_fn=fake_popen,
        sleep_fn=lambda _s: None,
    )

    assert lock_ops == [(2, 1), (0, 1)]
    assert len(popen_calls) == 1
    kwargs = popen_calls[0][1]
    assert "start_new_session" not in kwargs
    assert kwargs["creationflags"] == (0x200 | 0x08000000)
    assert kwargs["env"]["LWB_BACKGROUND"] == "1"
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert len(started_urls) == 1 and started_urls[0].startswith("http://127.0.0.1:")

    alerts: list[tuple] = []
    fake_ctypes = ModuleType("ctypes")
    fake_ctypes.windll = SimpleNamespace(
        user32=SimpleNamespace(MessageBoxW=lambda *a: alerts.append(a) or 1),
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    mod._notify_failure("测试失败", runtime / "launcher.log")
    assert alerts == [(None, "测试失败", "无法启动素章", 0x10)]


def test_backup_windows_pg_env_and_manifest_path_validation(monkeypatch, tmp_path):
    fake_pg = tmp_path / "pg18" / "bin"
    fake_pg.mkdir(parents=True)
    fake_dump = fake_pg / "pg_dump.exe"
    fake_dump.write_bytes(b"")
    monkeypatch.setattr(backup, "_is_windows", lambda: True)
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setattr(backup.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("LWB_PG_BIN", str(fake_pg))
    monkeypatch.setattr(
        backup.shutil, "which",
        lambda name, path=None: str(Path(path) / f"{name}.exe") if path else None,
    )
    assert backup._pg_tool("pg_dump", "") == str(fake_dump)

    captured: list[dict] = []

    def fake_run(cmd, **kwargs):
        captured.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    backup._pg_dump("postgresql://user:pw@127.0.0.1:55432/db", tmp_path / "a.dump")
    assert len(captured) == 1
    assert captured[0]["cmd"][0] == str(fake_dump)
    assert captured[0]["encoding"] == "utf-8"
    assert captured[0]["creationflags"] == 0x08000000
    assert "LC_ALL" not in captured[0]["env"]
    assert captured[0]["env"]["PGCLIENTENCODING"] == "UTF8"

    dest = tmp_path / "bk"
    dest.mkdir()
    (dest / backup.DB_DUMP_NAME).write_bytes(b"dump")
    import hashlib
    dump_sha = hashlib.sha256(b"dump").hexdigest()
    (dest / backup.MANIFEST_NAME).write_text(
        json.dumps({
            "schema_version": "0008_claim_amounts",
            "app_version": "0.1.0",
            "created_at": "2026-11-23T00:00:00Z",
            "database": {"file": backup.DB_DUMP_NAME, "sha256": dump_sha, "size": 4},
            "files": [{"relative_path": r"..\escape.txt", "size": 1, "sha256": "0"}],
            "complete": True,
        }),
        encoding="utf-8",
    )
    with pytest.raises(WorkbenchError) as exc:
        backup.verify_backup(dest)
    assert exc.value.code == ErrorCode.INVALID_PATH


@pytest.mark.skipif(os.name != "nt", reason="real NTFS junction only exists on Windows")
def test_real_windows_ntfs_junction_rejected(tmp_path):
    import _winapi  # type: ignore[import-not-found]

    store = Storage(root=tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    case_id = uuid.uuid4()
    store.case_dir(case_id)
    jdir = store.root / "cases" / str(case_id) / "working" / "junc"
    _winapi.CreateJunction(str(outside), str(jdir))
    with pytest.raises(WorkbenchError) as exc:
        store.managed_path(f"cases/{case_id}/working/junc/secret.txt")
    assert exc.value.code == ErrorCode.INVALID_PATH
