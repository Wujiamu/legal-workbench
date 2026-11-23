"""Migration 0007 upgrade drill on a 0006-era database (checklist item 7).

Builds a database at 0006 with real progress rows, upgrades to head, then checks
that the new tables exist, that ``progress_changes`` and friends are untouched
(column set, constraints and rows), that the extended audit CHECK accepts
``graph.*`` / ``evidence.*``, and that the app role can write.  Downgrade returns
to 0006 and leaves progress intact.

TEST-ONLY synthetic data; a throwaway database on the isolated cluster.
"""
from __future__ import annotations

import os
import re
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
DRILL_DB = "legal_workbench_graph_drill"

NEW_TABLES = {
    "case_entities", "entity_aliases", "case_relations", "graph_sources",
    "relation_graphs", "relation_graph_nodes", "relation_graph_edges",
    "relation_graph_groups", "relation_graph_snapshots", "snapshot_consents",
    "graph_changes", "evidence_groups", "evidence_items", "evidence_item_bindings",
    "evidence_changes",
}
PROGRESS_TABLES = {"progress_nodes", "progress_relations", "progress_links",
                   "progress_changes", "case_change_seq"}


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


def _schema(engine) -> dict[str, set[str]]:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public'")).all()
    schema: dict[str, set[str]] = {}
    for table, column in rows:
        schema.setdefault(table, set()).add(column)
    return schema


def _constraints(engine, table: str) -> list[str]:
    with engine.begin() as conn:
        return sorted(conn.execute(text(
            "SELECT conname || ':' || pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = cast(:t AS regclass)"), {"t": table}).scalars().all())


def _operations(engine) -> set[str]:
    """The operation literals inside the audit CHECK, as a set."""
    with engine.begin() as conn:
        definition = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_audit_events_operation'")).scalar_one()
    return set(re.findall(r"'([a-z]+\.[a-z_]+)'", definition))


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


def test_upgrade_0006_to_0007_preserves_progress(drill_db):
    migrator_url, app_url = drill_db
    _alembic("upgrade", "0006_case_archive_time", migrator_url, app_url)

    # 1. a 0006 install with a case, a progress node and a progress change row
    case_id = uuid.uuid4()
    node_id = uuid.uuid4()
    change_id = uuid.uuid4()
    with psycopg.connect(app_url) as conn:
        conn.execute(
            "INSERT INTO cases (id, title, stage, revision) VALUES (%s, %s, %s, 0)",
            (str(case_id), "TEST-ONLY 旧案", "庭审准备"))
        conn.execute(
            "INSERT INTO progress_nodes (id, case_id, kind, title, state, position, "
            "created_by, updated_by, revision) VALUES (%s, %s, 'stage', "
            "'TEST-ONLY 一审', 'in_progress', 0, 'human', 'human', 0)",
            (str(node_id), str(case_id)))
        conn.execute(
            "INSERT INTO case_change_seq (case_id, domain, seq) VALUES (%s, 'progress', 1)",
            (str(case_id),))
        conn.execute(
            "INSERT INTO progress_changes (id, case_id, seq, command, actor, entry, diff) "
            "VALUES (%s, %s, 1, 'add_node', 'human', 'local-web', '[]'::jsonb)",
            (str(change_id), str(case_id)))
        conn.commit()

    before_engine = create_engine(sqlalchemy_url(app_url), future=True)
    try:
        before_schema = _schema(before_engine)
        before_constraints = {table: _constraints(before_engine, table)
                              for table in PROGRESS_TABLES | {"audit_events", "cases"}}
        before_operations = _operations(before_engine)
    finally:
        before_engine.dispose()
    assert NEW_TABLES.isdisjoint(before_schema)

    # 2. upgrade to head
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    try:
        _assert_upgraded(engine, before_schema, before_constraints, before_operations,
                         case_id, change_id)
    finally:
        engine.dispose()

    # 3. downgrade returns to 0006 and drops only the new tables
    _alembic("downgrade", "0006_case_archive_time", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    try:
        _assert_downgraded(engine, before_schema, before_constraints, before_operations,
                           case_id)
    finally:
        engine.dispose()


def _assert_upgraded(engine, before_schema, before_constraints, before_operations,
                     case_id, change_id) -> None:
    after_schema = _schema(engine)

    # new tables exist and every new table carries case_id (checklist item 6)
    assert NEW_TABLES <= set(after_schema)
    for table in NEW_TABLES:
        assert "case_id" in after_schema[table], table

    # existing tables keep exactly their columns and constraints
    for table in PROGRESS_TABLES | {"cases"}:
        assert after_schema[table] == before_schema[table], table
    for table in PROGRESS_TABLES - {"audit_events"}:
        assert _constraints(engine, table) == before_constraints[table], table
    # only the audit CHECK changed, and it changed by pure extension
    assert _operations(engine) > before_operations
    assert {op for op in _operations(engine) if op.startswith(("graph.", "evidence."))}

    with engine.begin() as conn:
        # legacy rows survive untouched
        assert conn.execute(text("SELECT count(*) FROM progress_nodes WHERE case_id=:c"),
                            {"c": str(case_id)}).scalar_one() == 1
        assert conn.execute(text("SELECT command FROM progress_changes WHERE id=:i"),
                            {"i": str(change_id)}).scalar_one() == "add_node"
        assert conn.execute(text("SELECT stage FROM cases WHERE id=:c"),
                            {"c": str(case_id)}).scalar_one() == "庭审准备"
        # the audit CHECK accepts the new operations and still rejects rubbish
        for operation in ("graph.add_entity", "graph.create_snapshot", "graph.add_snapshot_consent",
                          "evidence.add_item", "evidence.confirm_binding"):
            conn.execute(text(
                "INSERT INTO audit_events (id, case_id, actor, operation, target_type, "
                "target_id) VALUES (:id, :c, 'local-web', :op, 'graph', :t)"),
                {"id": str(uuid.uuid4()), "c": str(case_id), "op": operation,
                 "t": str(uuid.uuid4())})
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO audit_events (id, case_id, actor, operation, target_type, "
                "target_id) VALUES (:id, :c, 'local-web', 'graph.not_a_command', "
                "'graph', :t)"),
                {"id": str(uuid.uuid4()), "c": str(case_id), "t": str(uuid.uuid4())})
    # the app role can write the new tables (no DDL, but DML yes)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO evidence_groups (id, case_id, position, title, created_by, revision) "
            "VALUES (:id, :c, 0, 'TEST-ONLY 组', 'human', 0)"),
            {"id": str(uuid.uuid4()), "c": str(case_id)})
        conn.execute(text(
            "INSERT INTO case_entities (id, case_id, kind, name, created_by, updated_by, "
            "revision) VALUES (:id, :c, 'org', 'TEST-ONLY 主体', 'human', 'human', 0)"),
            {"id": str(uuid.uuid4()), "c": str(case_id)})


def _assert_downgraded(engine, before_schema, before_constraints, before_operations,
                       case_id) -> None:
    final_schema = _schema(engine)
    assert NEW_TABLES.isdisjoint(final_schema)
    assert PROGRESS_TABLES <= set(final_schema)
    for table in PROGRESS_TABLES | {"cases"}:
        assert final_schema[table] == before_schema[table], table
    for table in PROGRESS_TABLES:
        assert _constraints(engine, table) == before_constraints[table], table
    assert _constraints(engine, "audit_events") == before_constraints["audit_events"]
    assert _operations(engine) == before_operations
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM progress_changes WHERE case_id=:c"),
                            {"c": str(case_id)}).scalar_one() == 1
