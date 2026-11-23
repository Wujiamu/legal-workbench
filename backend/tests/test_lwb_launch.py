"""Regression tests for scripts/lwb-launch recovery when PostgreSQL was stopped."""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    launcher_path = REPO_ROOT / "scripts" / "lwb-launch"
    loader = importlib.machinery.SourceFileLoader("lwb_launch_mod", str(launcher_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake_root = tmp_path / "workspace"
    (fake_root / "frontend" / "dist").mkdir(parents=True)
    (fake_root / "frontend" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    runtime = fake_root / "var" / "runtime"
    monkeypatch.setattr(module, "ROOT", fake_root)
    monkeypatch.setattr(module, "RUNTIME", runtime)
    monkeypatch.setattr(module, "LOG", runtime / "launcher.log")
    return module


def test_lwb_launch_already_ready_opens_browser_without_spawning(monkeypatch, tmp_path):
    mod = _load_launcher(monkeypatch, tmp_path)
    opened: list[str] = []
    runs: list[list[str]] = []
    popens: list[list[str]] = []

    mod.open_workbench(
        probe_fn=lambda: (True, True),
        run_fn=lambda cmd, **_kw: runs.append(cmd) or SimpleNamespace(returncode=0),
        popen_fn=lambda cmd, **_kw: popens.append(cmd) or SimpleNamespace(poll=lambda: None),
        open_browser_fn=opened.append,
        sleep_fn=lambda _s: None,
    )

    assert runs == []
    assert popens == []
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")


def test_lwb_launch_starts_service_when_not_running(monkeypatch, tmp_path):
    mod = _load_launcher(monkeypatch, tmp_path)
    probes = iter([(False, False), (True, True)])
    opened: list[str] = []
    popens: list[list[str]] = []

    mod.open_workbench(
        probe_fn=lambda: next(probes),
        popen_fn=lambda cmd, **_kw: popens.append(cmd) or SimpleNamespace(poll=lambda: None),
        open_browser_fn=opened.append,
        sleep_fn=lambda _s: None,
    )

    assert len(popens) == 1
    assert popens[0][-2:] == [str(tmp_path / "workspace" / "scripts" / "lwb"), "start"]
    assert len(opened) == 1


def test_lwb_launch_recovers_db_and_worker_when_web_running_but_db_down(monkeypatch, tmp_path):
    mod = _load_launcher(monkeypatch, tmp_path)
    probes = iter([(True, False), (True, True)])
    runs: list[list[str]] = []
    popens: list[list[str]] = []
    opened: list[str] = []

    mod.open_workbench(
        probe_fn=lambda: next(probes),
        run_fn=lambda cmd, **_kw: runs.append(cmd) or SimpleNamespace(returncode=0),
        popen_fn=lambda cmd, **_kw: popens.append(cmd) or SimpleNamespace(poll=lambda: None),
        open_browser_fn=opened.append,
        sleep_fn=lambda _s: None,
    )

    assert len(runs) == 1
    assert runs[0][-1] == "start-db"
    assert len(popens) == 1
    assert popens[0][-1] == "worker"
    assert len(opened) == 1
    log_text = (tmp_path / "workspace" / "var" / "runtime" / "launcher.log").read_text(encoding="utf-8")
    assert "尝试拉起 PostgreSQL 并恢复解析 worker" in log_text


def test_lwb_launch_raises_immediately_if_db_recovery_fails(monkeypatch, tmp_path):
    mod = _load_launcher(monkeypatch, tmp_path)
    popens: list[list[str]] = []
    opened: list[str] = []

    with pytest.raises(RuntimeError, match="自动拉起 PostgreSQL 失败"):
        mod.open_workbench(
            probe_fn=lambda: (True, False),
            run_fn=lambda _cmd, **_kw: SimpleNamespace(returncode=1),
            popen_fn=lambda cmd, **_kw: popens.append(cmd) or SimpleNamespace(poll=lambda: None),
            open_browser_fn=opened.append,
            sleep_fn=lambda _s: None,
        )

    assert popens == []
    assert opened == []
