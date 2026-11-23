"""Migration 0006 drill: archive column + time_entries on a 0005-era database.

Verifies the upgrade keeps legacy cases intact (archived_at NULL), that the app
role can record time, that the widened audit CHECK accepts the new operations,
and that downgrading to 0005 removes the column/table while leaving progress
data and the legacy case untouched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from legal_workbench.config import load_settings
from legal_workbench.db import sqlalchemy_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
DRILL_DB = "legal_workbench_archive_drill"


def _urls():
    settings = load_settings()
    migrator = make_url(settings.migration_url).set(database=DRILL_DB)
    app = make_url(settings.database_url).set(database=DRILL_DB)
    return (migrator.render_as_string(hide_password=False),
            app.render_as_string(hide_password=False))


def _alembic(action: str, target: str, migrator_url: str, app_url: str) -> None:
    env = dict(os.environ, LWB_MIGRATION_URL=migrator_url, LWB_DATABASE_URL=app_url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, target],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"alembic {action} {target} failed: {result.stderr}")


@pytest.fixture
def drill_db():
    migrator_url, app_url = _urls()
    admin = make_url(migrator_url).set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DRILL_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DRILL_DB}"')
    yield migrator_url, app_url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DRILL_DB}" WITH (FORCE)')


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t"), {"t": table}).scalars().all())


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all())


def test_upgrade_0005_to_head_and_back(drill_db):
    migrator_url, app_url = drill_db
    _alembic("upgrade", "0005_case_progress", migrator_url, app_url)
    legacy_id = uuid.uuid4()
    node_id = uuid.uuid4()
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        assert "archived_at" not in _columns(conn, "cases")
        assert "time_entries" not in _tables(conn)
        conn.execute(text(
            "INSERT INTO cases (id, title, description, revision, stage, next_action) "
            "VALUES (:id, 'TEST-ONLY 归档钻取', NULL, 0, '立案', NULL)"), {"id": str(legacy_id)})
        conn.execute(text(
            "INSERT INTO progress_nodes (id, case_id, kind, title, state, position, "
            "created_by, updated_by) VALUES (:id, :case, 'stage', '庭前程序', "
            "'in_progress', 0, 'human', 'human')"), {"id": str(node_id), "case": str(legacy_id)})

    _alembic("upgrade", "head", migrator_url, app_url)
    with engine.begin() as conn:
        assert "archived_at" in _columns(conn, "cases")
        assert "time_entries" in _tables(conn)
        # 旧案件保留，归档时间默认为空
        assert conn.execute(text("SELECT archived_at FROM cases WHERE id=:id"),
                            {"id": str(legacy_id)}).scalar_one() is None
        # app 角色可写工时与归档审计
        entry_id = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO time_entries (id, case_id, node_id, node_title, work_date, hours, "
            "source, started_at, created_by) VALUES (:id, :case, :node, '阅卷', "
            "CURRENT_DATE, 0.25, 'timer', now(), 'local-web')"),
            {"id": str(entry_id), "case": str(legacy_id), "node": str(node_id)})
        conn.execute(text(
            "INSERT INTO audit_events (id, case_id, actor, operation, target_type, target_id) "
            "VALUES (:id, :case, 'local-web', 'case.archive', 'case', :case)"),
            {"id": str(uuid.uuid4()), "case": str(legacy_id)})
        # 手动工时不需要 started_at
        conn.execute(text(
            "INSERT INTO time_entries (id, case_id, node_title, work_date, hours, source, "
            "created_by) VALUES (:id, :case, '补录', CURRENT_DATE, 1.5, 'manual', 'local-web')"),
            {"id": str(uuid.uuid4()), "case": str(legacy_id)})

    _alembic("downgrade", "0005_case_progress", migrator_url, app_url)
    with engine.begin() as conn:
        assert "archived_at" not in _columns(conn, "cases")
        assert "time_entries" not in _tables(conn)
        # 进度数据与旧案件不受影响
        assert "progress_nodes" in _tables(conn)
        assert conn.execute(text("SELECT count(*) FROM cases WHERE id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM progress_nodes WHERE case_id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1

    _alembic("upgrade", "head", migrator_url, app_url)
    with engine.begin() as conn:
        assert "time_entries" in _tables(conn)
        assert conn.execute(text("SELECT count(*) FROM time_entries")).scalar_one() == 0
    engine.dispose()
