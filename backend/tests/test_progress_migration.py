"""Migration 0005 upgrade drill on a 0004-era database (plan checklist item 7).

Builds a database at 0004 with legacy cases carrying stage/next_action/deadlines,
upgrades to head, then verifies: progress tables exist, default fragments are
backfilled (K14), legacy scheduling fields survive, audit CHECK accepts progress.*
and the app role can write progress. Downgrade returns to 0004 cleanly.
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
DRILL_DB = "legal_workbench_progress_drill"


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


def test_upgrade_from_0004_backfills_and_preserves_legacy(drill_db):
    migrator_url, app_url = drill_db
    # 1. migrate to 0004 only
    _alembic("upgrade", "0004_case_workbench", migrator_url, app_url)
    # 2. insert a legacy case with scheduling fields, as a pre-progress install would have
    legacy_id = uuid.uuid4()
    with psycopg.connect(app_url) as conn:
        conn.execute(
            "INSERT INTO cases (id, title, stage, next_action, deadlines, revision) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, 0)",
            (str(legacy_id), "TEST-ONLY 旧案", "庭审准备", "核对原件",
             '[{"title":"提交证据","due":"2026-12-01","kind":"external","completed":false}]'))
        conn.commit()
    # 3. upgrade to head (0005)
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all())
        assert {"progress_nodes", "progress_relations", "progress_links",
                "progress_changes", "case_change_seq"} <= tables
        # legacy scheduling preserved
        row = conn.execute(text(
            "SELECT stage, next_action, deadlines FROM cases WHERE id=:id"),
            {"id": str(legacy_id)}).one()
        assert row[0] == "庭审准备"
        assert row[1] == "核对原件"
        assert row[2][0]["title"] == "提交证据"
    engine.dispose()
    # 4. backfill (as scripts/lwb migrate does) and verify default fragment
    env = dict(os.environ, LWB_DATABASE_URL=app_url, LWB_MIGRATION_URL=migrator_url)
    result = subprocess.run(
        [sys.executable, "-m", "legal_workbench.backfill_progress"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        titles = set(conn.execute(text(
            "SELECT title FROM progress_nodes WHERE case_id=:id"),
            {"id": str(legacy_id)}).scalars().all())
        assert {"诉前准备", "一审", "立案", "庭前程序", "庭审", "裁判"} <= titles
        assert "结案归档" not in titles
        seq = conn.execute(text(
            "SELECT seq FROM case_change_seq WHERE case_id=:id AND domain='progress'"),
            {"id": str(legacy_id)}).scalar_one()
        assert seq >= 1
        # audit CHECK now accepts progress.* operations
        conn.execute(text(
            "INSERT INTO audit_events (id, case_id, actor, operation, target_type, target_id) "
            "VALUES (:id, :cid, 'local-web', 'progress.add_node', 'progress', :tid)"),
            {"id": str(uuid.uuid4()), "cid": str(legacy_id), "tid": str(uuid.uuid4())})
    engine.dispose()
    # 5. idempotent backfill
    result2 = subprocess.run(
        [sys.executable, "-m", "legal_workbench.backfill_progress"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert "0 case(s)" in result2.stdout
    # 6. downgrade returns to 0004 and drops progress tables
    _alembic("downgrade", "0004_case_workbench", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all())
        assert "progress_nodes" not in tables
        # legacy case still intact after downgrade
        assert conn.execute(text("SELECT count(*) FROM cases WHERE id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
    engine.dispose()