"""0008_claim_amounts 迁移演练：从交付文件声明的父版本升到 head，再降回父版本。

父版本从磁盘上的迁移文件读取（`ScriptDirectory.get_revision("0008_claim_amounts")`），
所以同一测试在两种检出状态下都有效：

* 本副本（0007 尚未合入）——实测变体把 `down_revision` 临时改为
  `0006_case_archive_time`，演练的是 0006 → 诉请结构；
* 合并后（0007 已存在）——演练 0007 → 0008，并核对 0007 的审计操作在升级后
  全部保留、降级后原样恢复。

两者都不把临时父版本当成正式链：交付文件的 `down_revision` 仍指向 0007。
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from legal_workbench.config import load_settings
from legal_workbench.db import sqlalchemy_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
DRILL_DB = "legal_workbench_claims_drill"
CLAIM_OPERATIONS = {
    "claims.add_group", "claims.update_group", "claims.reorder_groups",
    "claims.remove_group", "claims.add_item", "claims.update_item",
    "claims.reorder_items", "claims.remove_item", "claims.restore_item",
    "claims.accept_change", "claims.accept_all", "claims.revert_change",
}


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


def _parent_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("0008_claim_amounts")
    assert revision.down_revision and isinstance(revision.down_revision, str)
    return revision.down_revision


def _audit_operations(engine) -> set[str]:
    with engine.begin() as conn:
        definition = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_audit_events_operation' "
            "AND conrelid = 'audit_events'::regclass")).scalar_one()
    import re
    return set(re.findall(r"'([^']+)'", definition))


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


def test_upgrade_from_parent_adds_claims_and_downgrade_restores(drill_db):
    migrator_url, app_url = drill_db
    parent = _parent_revision()
    _alembic("upgrade", parent, migrator_url, app_url)

    engine = create_engine(sqlalchemy_url(migrator_url), future=True)
    parent_operations = _audit_operations(engine)
    assert "claims.add_item" not in parent_operations
    legacy_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO cases (id, title, revision) VALUES (:id, 'TEST-ONLY 迁移旧案', 0)"),
            {"id": str(legacy_id)})
    engine.dispose()

    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(migrator_url), future=True)
    with engine.begin() as conn:
        tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all())
        assert {"claim_groups", "claim_items", "claim_changes"} <= tables
        amount_type = conn.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'claim_items' AND column_name = 'amount_cents'")).scalar_one()
        assert amount_type == "bigint"
        constraints = {row[0] for row in conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'claim_items'::regclass")).all()}
        assert {"fk_claim_items_group", "fk_claim_items_segment",
                "fk_claim_items_material", "fk_claim_items_annotation",
                "ck_claim_items_amount", "ck_claim_items_one_source"} <= constraints
        segment_delete = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'fk_claim_items_segment'")).scalar_one()
        # 引用不可变：不得带 ON DELETE SET NULL/SET NULL (column)/CASCADE。
        assert "ON DELETE" not in segment_delete, segment_delete
        assert "REFERENCES segments(case_id, id)" in segment_delete
        amount_check = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_claim_items_amount'")).scalar_one()
        # 数据库上限与公开契约一致（JS 安全整数）。
        assert "9007199254740991" in amount_check, amount_check
        assert "amount_cents >= 0" in amount_check, amount_check
        fingerprint = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'claim_changes' AND column_name = 'request_fingerprint'"
        )).scalar_one_or_none()
        assert fingerprint == "request_fingerprint"
    operations = _audit_operations(engine)
    # 父版本（0007 或 0006）的操作全部保留，只增加 claims.*。
    assert parent_operations <= operations
    assert CLAIM_OPERATIONS <= operations
    assert operations - parent_operations == CLAIM_OPERATIONS
    engine.dispose()

    # 应用角色（仅 DML）能写入并读回新表：迁移的默认权限覆盖了新表。
    group_id, item_id, change_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(app_url) as conn:
        conn.execute(
            "INSERT INTO claim_groups (id, case_id, title, currency, include_in_total, "
            "position, created_by, updated_by, revision) "
            "VALUES (%s, %s, 'TEST-ONLY 迁移组', 'CNY', true, 0, 'human', 'human', 0)",
            (str(group_id), str(legacy_id)))
        conn.execute(
            "INSERT INTO claim_items (id, case_id, group_id, title, category, amount_cents, "
            "position, created_by, updated_by, revision) "
            "VALUES (%s, %s, %s, 'TEST-ONLY 迁移项', 'principal', 100, 0, 'human', 'human', 0)",
            (str(item_id), str(legacy_id), str(group_id)))
        conn.execute(
            "INSERT INTO claim_changes (id, case_id, seq, command, actor, entry, diff) "
            "VALUES (%s, %s, 1, 'add_item', 'human', 'local-web', '[]'::jsonb)",
            (str(change_id), str(legacy_id)))
        conn.commit()
        assert conn.execute("SELECT amount_cents FROM claim_items WHERE id = %s",
                            (str(item_id),)).fetchone()[0] == 100
        conn.execute("UPDATE claim_items SET amount_cents = 250 WHERE id = %s", (str(item_id),))
        conn.commit()

    _alembic("downgrade", parent, migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(migrator_url), future=True)
    with engine.begin() as conn:
        tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all())
        assert not {"claim_groups", "claim_items", "claim_changes"} & tables
        assert conn.execute(text("SELECT count(*) FROM cases WHERE id = :id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
        # 降级只清理 claims 领域：其它领域的 seq 行与审计原样保留。
        domains = set(conn.execute(text(
            "SELECT DISTINCT domain FROM case_change_seq")).scalars().all())
        assert "claims" not in domains
        assert conn.execute(text(
            "SELECT count(*) FROM audit_events WHERE operation LIKE 'claims.%'"
        )).scalar_one() == 0
    assert _audit_operations(engine) == parent_operations
    engine.dispose()


def test_downgrade_keeps_other_domains(drill_db):
    """0008 降级只删 claims 的 seq 行与审计，进度等其它领域不受影响。"""
    migrator_url, app_url = drill_db
    parent = _parent_revision()
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(migrator_url), future=True)
    case_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO cases (id, title, revision) VALUES (:id, 'TEST-ONLY 降级案', 0)"),
            {"id": str(case_id)})
        conn.execute(text(
            "INSERT INTO case_change_seq (case_id, domain, seq) VALUES (:id, 'claims', 4)"),
            {"id": str(case_id)})
        conn.execute(text(
            "INSERT INTO case_change_seq (case_id, domain, seq) VALUES (:id, 'progress', 9)"),
            {"id": str(case_id)})
        conn.execute(text(
            "INSERT INTO audit_events (id, case_id, operation, target_type, target_id, actor, "
            "request_id) VALUES (:id, :case_id, 'claims.add_item', 'claims', :target, "
            "'human', :rid)"),
            {"id": str(uuid.uuid4()), "case_id": str(case_id), "target": str(uuid.uuid4()),
             "rid": str(uuid.uuid4())})
        conn.execute(text(
            "INSERT INTO audit_events (id, case_id, operation, target_type, target_id, actor, "
            "request_id) VALUES (:id, :case_id, 'case.create', 'case', :target, "
            "'human', :rid)"),
            {"id": str(uuid.uuid4()), "case_id": str(case_id), "target": str(case_id),
             "rid": str(uuid.uuid4())})
    engine.dispose()

    _alembic("downgrade", parent, migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(migrator_url), future=True)
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT domain, seq FROM case_change_seq WHERE case_id = :id"),
            {"id": str(case_id)}).all()
        assert rows == [("progress", 9)]
        operations = conn.execute(text(
            "SELECT operation FROM audit_events WHERE case_id = :id"),
            {"id": str(case_id)}).scalars().all()
        assert operations == ["case.create"]
        assert conn.execute(text("SELECT count(*) FROM cases WHERE id = :id"),
                            {"id": str(case_id)}).scalar_one() == 1
    engine.dispose()
