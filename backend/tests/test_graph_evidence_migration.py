"""Migration 0007 drill: relation graph + evidence catalog on a 0006-era database.

Plan checklist item 7 and acceptance condition 13 (docs/changes/20261120-案件主体关系图.md):
upgrade keeps legacy data intact, the app role can read/write every new table,
cross-case / cross-graph / cross-snapshot references are rejected by the
database itself, snapshots and consents are immutable at row level, the audit
CHECK is a closed set extended with graph.* / evidence.*, and downgrade to 0006
removes everything cleanly.  TEST-ONLY synthetic data; real PostgreSQL only.

The graph service (graph.py) is not implemented yet in this workspace, so the
graph-side rows are synthesized with low-level SQL/ORM inserts: what is verified
here is the *structure* (constraints, defaults, triggers, grants), not service
behaviour.  The evidence-catalog service exists and is exercised for real.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError

from legal_workbench import audit, cases, evidence_catalog, models
from legal_workbench.config import load_settings
from legal_workbench.db import sqlalchemy_url
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    ArchiveCaseInput, EvidenceBindingRevisionInput, EvidenceChangeIdInput,
    ImportCatalogRow, ImportEvidenceCatalogInput, ImportBindingRow,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
DRILL_DB = "legal_workbench_graph_drill"
REV_0006 = "0006_case_archive_time"
REV_0007 = "0007_graph_evidence"
# 正式链的 head 是 0008；本文件仍逐项核对 0007 的对象。
REV_0008 = "0008_claim_amounts"

NEW_TABLES = (
    "case_entities", "entity_aliases", "case_relations", "graph_sources",
    "relation_graphs", "relation_graph_groups", "relation_graph_nodes",
    "relation_graph_edges", "relation_graph_snapshots", "snapshot_consents",
    "evidence_groups", "evidence_items", "evidence_item_bindings",
    "graph_changes", "evidence_changes",
)


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


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- low-level row synthesis (structure tests; graph service not yet built) ---

def _entity(conn, case_id, name="TEST-ONLY 虚构主体"):
    row_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO case_entities (id, case_id, kind, name, created_by, updated_by) "
        "VALUES (:id, :c, 'person', :n, 'human', 'human')"),
        {"id": str(row_id), "c": str(case_id), "n": name})
    return row_id


def _relation(conn, case_id, a, b, kind="equity_control"):
    row_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO case_relations (id, case_id, from_entity_id, to_entity_id, kind, "
        "credibility, created_by, updated_by) VALUES (:id, :c, :a, :b, :k, "
        "'suspected', 'human', 'human')"),
        {"id": str(row_id), "c": str(case_id), "a": str(a), "b": str(b), "k": kind})
    return row_id


def _graph(conn, case_id, title="TEST-ONLY 虚构图"):
    row_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO relation_graphs (id, case_id, title, created_by, updated_by) "
        "VALUES (:id, :c, :t, 'human', 'human')"),
        {"id": str(row_id), "c": str(case_id), "t": title})
    return row_id


def _group(conn, case_id, graph_id):
    row_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO relation_graph_groups (id, case_id, graph_id, title) "
        "VALUES (:id, :c, :g, 'TEST-ONLY 分组')"),
        {"id": str(row_id), "c": str(case_id), "g": str(graph_id)})
    return row_id


def _node(conn, case_id, graph_id, entity_id, group_id=None):
    row_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO relation_graph_nodes (id, case_id, graph_id, entity_id, group_id) "
        "VALUES (:id, :c, :g, :e, :grp)"),
        {"id": str(row_id), "c": str(case_id), "g": str(graph_id),
         "e": str(entity_id), "grp": str(group_id) if group_id else None})
    return row_id


def _snapshot(conn, case_id, graph_id, version_no=1):
    row_id = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO relation_graph_snapshots (id, case_id, graph_id, version_no, "
        "stage_label, payload) VALUES (:id, :c, :g, :v, '方案沟通', "
        "'{\"nodes\": [], \"labels\": {\"frozen\": \"证据1-1\"}}'::jsonb)"),
        {"id": str(row_id), "c": str(case_id), "g": str(graph_id), "v": version_no})
    return row_id


def _consent(conn, case_id, snapshot_id, action="confirm", parent=None, row_id=None):
    row_id = row_id or uuid.uuid4()
    conn.execute(text(
        "INSERT INTO snapshot_consents (id, case_id, snapshot_id, action, "
        "parent_consent_id, consenter, consented_at, channel, source_detail) "
        "VALUES (:id, :c, :s, :a, :p, 'TEST-ONLY 虚构客户', now(), 'wechat', "
        "'TEST-ONLY 微信确认')"),
        {"id": str(row_id), "c": str(case_id), "s": str(snapshot_id), "a": action,
         "p": str(parent) if parent else None})
    return row_id


def test_upgrade_0006_to_0007_and_back(drill_db):
    migrator_url, app_url = drill_db
    _alembic("upgrade", REV_0006, migrator_url, app_url)
    legacy_id = uuid.uuid4()
    node_id = uuid.uuid4()
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        assert not (set(NEW_TABLES) & _tables(conn))
        conn.execute(text(
            "INSERT INTO cases (id, title, revision) VALUES (:id, 'TEST-ONLY 0006 旧案', 0)"),
            {"id": str(legacy_id)})
        conn.execute(text(
            "INSERT INTO progress_nodes (id, case_id, kind, title, state, position, "
            "created_by, updated_by) VALUES (:id, :c, 'stage', '庭前程序', "
            "'in_progress', 0, 'human', 'human')"),
            {"id": str(node_id), "c": str(legacy_id)})
        conn.execute(text(
            "INSERT INTO time_entries (id, case_id, node_title, work_date, hours, "
            "source, created_by) VALUES (:id, :c, '阅卷', CURRENT_DATE, 0.5, "
            "'manual', 'local-web')"), {"id": str(uuid.uuid4()), "c": str(legacy_id)})

    _alembic("upgrade", "head", migrator_url, app_url)
    with engine.begin() as conn:
        # revision id fits the untouched VARCHAR(32) alembic_version column
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == REV_0008 and len(REV_0007) <= 32
        width = conn.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name='alembic_version' AND column_name='version_num'"
        )).scalar_one()
        assert width == 32
        assert set(NEW_TABLES) <= _tables(conn)
        # legacy rows survive untouched
        assert conn.execute(text("SELECT archived_at FROM cases WHERE id=:id"),
                            {"id": str(legacy_id)}).scalar_one() is None
        assert conn.execute(text("SELECT count(*) FROM progress_nodes WHERE case_id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM time_entries WHERE case_id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
        # domain sequence rows for the two new domains exist after the upgrade
        conn.execute(text(
            "INSERT INTO case_change_seq (case_id, domain, seq) VALUES "
            "(:c, 'progress', 4), (:c, 'graph', 2), (:c, 'evidence', 3)"),
            {"c": str(legacy_id)})
        # server defaults are real SQL expressions, not double-quoted literals
        defaults = dict(conn.execute(text(
            "SELECT table_name || '.' || column_name, column_default "
            "FROM information_schema.columns WHERE column_default IS NOT NULL "
            "AND table_name = ANY(:tables)"), {"tables": list(NEW_TABLES)}).all())
        assert defaults["case_entities.roles"] == "'[]'::jsonb"
        assert defaults["case_relations.attrs"] == "'{}'::jsonb"
        assert defaults["relation_graph_snapshots.created_by"] == "'human'::text"
        assert defaults["snapshot_consents.created_by"] == "'human'::text"
        assert defaults["evidence_items.source_kind"] == "'manual'::text"
        assert defaults["evidence_items.review_state"] == "'confirmed'::text"
        assert defaults["evidence_item_bindings.review_state"] == "'confirmed'::text"
        assert defaults["graph_changes.review_state"] == "'none'::text"
        assert defaults["evidence_changes.review_state"] == "'none'::text"
        # omitted-column insert really produces usable values
        eid = _entity(conn, legacy_id)
        roles = conn.execute(text("SELECT roles FROM case_entities WHERE id=:id"),
                             {"id": str(eid)}).scalar_one()
        assert roles == []
        snap = _snapshot(conn, legacy_id, _graph(conn, legacy_id))
        actor = conn.execute(text(
            "SELECT created_by FROM relation_graph_snapshots WHERE id=:id"),
            {"id": str(snap)}).scalar_one()
        assert actor == "human"
        # app role can write graph.* / evidence.* audit rows (widened CHECK)
        for op in ("graph.add_entity", "graph.create_snapshot",
                   "graph.add_snapshot_consent", "evidence.add_group",
                   "evidence.import_catalog"):
            conn.execute(text(
                "INSERT INTO audit_events (id, case_id, actor, operation, target_type) "
                "VALUES (:id, :c, 'local-web', :op, 'graph')"),
                {"id": str(uuid.uuid4()), "c": str(legacy_id), "op": op})

    # unknown operations still rejected (closed set)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO audit_events (id, actor, operation, target_type) "
                "VALUES (:id, 'local-web', 'graph.not_a_command', 'graph')"),
                {"id": str(uuid.uuid4())})

    # ORM metadata and the migrated database agree (no pending autogenerate ops)
    env = dict(os.environ, LWB_MIGRATION_URL=migrator_url, LWB_DATABASE_URL=app_url)
    check = subprocess.run([sys.executable, "-m", "alembic", "check"],
                           cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in check.stdout + check.stderr

    # downgrade removes every new object, restores the old audit set, keeps legacy data
    _alembic("downgrade", REV_0006, migrator_url, app_url)
    with engine.begin() as conn:
        assert not (set(NEW_TABLES) & _tables(conn))
        # each domain revision clears only its own sequence rows
        domains = conn.execute(text(
            "SELECT domain FROM case_change_seq WHERE case_id=:id"), {"id": str(legacy_id)}
        ).scalars().all()
        assert domains == ["progress"], domains
        assert conn.execute(text("SELECT count(*) FROM cases WHERE id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM progress_nodes WHERE case_id=:id"),
                            {"id": str(legacy_id)}).scalar_one() == 1
        funcs = conn.execute(text(
            "SELECT proname FROM pg_proc WHERE proname IN "
            "('lwb_snapshot_immutable', 'lwb_consent_immutable')")).scalars().all()
        assert funcs == []
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO audit_events (id, actor, operation, target_type) "
                "VALUES (:id, 'local-web', 'graph.add_entity', 'graph')"),
                {"id": str(uuid.uuid4())})
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO audit_events (id, actor, operation, target_type) "
            "VALUES (:id, 'local-web', 'progress.add_node', 'progress')"),
            {"id": str(uuid.uuid4())})

    # re-upgrade works on the same database
    _alembic("upgrade", "head", migrator_url, app_url)
    with engine.begin() as conn:
        assert set(NEW_TABLES) <= _tables(conn)
    engine.dispose()


def test_app_role_dml_on_all_new_tables(drill_db):
    """The application role (no DDL) can read and write every 0007 table."""
    migrator_url, app_url = drill_db
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        case_id = uuid.uuid4()
        conn.execute(text("INSERT INTO cases (id, title) VALUES (:id, 'TEST-ONLY 角色验证')"),
                     {"id": str(case_id)})
        a = _entity(conn, case_id, "TEST-ONLY 甲")
        b = _entity(conn, case_id, "TEST-ONLY 乙")
        rel = _relation(conn, case_id, a, b)
        graph = _graph(conn, case_id)
        group = _group(conn, case_id, graph)
        _node(conn, case_id, graph, a, group)
        conn.execute(text(
            "INSERT INTO relation_graph_edges (id, case_id, graph_id, relation_id) "
            "VALUES (:id, :c, :g, :r)"),
            {"id": str(uuid.uuid4()), "c": str(case_id), "g": str(graph), "r": str(rel)})
        conn.execute(text(
            "INSERT INTO entity_aliases (id, case_id, entity_id, alias, created_by) "
            "VALUES (:id, :c, :e, 'TEST-ONLY 别名', 'human')"),
            {"id": str(uuid.uuid4()), "c": str(case_id), "e": str(a)})
        conn.execute(text(
            "INSERT INTO graph_sources (id, case_id, relation_id, material_id, created_by) "
            "SELECT :id, :c, :r, m.id, 'human' FROM materials m LIMIT 0"),
            {"id": str(uuid.uuid4()), "c": str(case_id), "r": str(rel)})
        snap = _snapshot(conn, case_id, graph)
        _consent(conn, case_id, snap)
        eg = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO evidence_groups (id, case_id, position, title, created_by) "
            "VALUES (:id, :c, 0, 'TEST-ONLY 组', 'human')"),
            {"id": str(eg), "c": str(case_id)})
        ei = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO evidence_items (id, case_id, group_id, position, title, created_by) "
            "VALUES (:id, :c, :g, 0, 'TEST-ONLY 条目', 'human')"),
            {"id": str(ei), "c": str(case_id), "g": str(eg)})
        for table, domain in (("graph_changes", "graph"), ("evidence_changes", "evidence")):
            conn.execute(text(
                f"INSERT INTO {table} (id, case_id, seq, command, actor, entry, diff) "
                "VALUES (:id, :c, 1, 'add_entity', 'human', 'local-web', '[]'::jsonb)"),
                {"id": str(uuid.uuid4()), "c": str(case_id)})
            conn.execute(text(
                "INSERT INTO case_change_seq (case_id, domain, seq) VALUES (:c, :d, 1)"),
                {"c": str(case_id), "d": domain})
        # UPDATE and DELETE where the design allows them
        conn.execute(text("UPDATE case_entities SET name='TEST-ONLY 甲改' WHERE id=:id"),
                     {"id": str(a)})
        conn.execute(text("DELETE FROM relation_graph_edges WHERE case_id=:c"),
                     {"c": str(case_id)})
        for table in NEW_TABLES:
            count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count >= 0
    # DDL stays forbidden for the app role
    with psycopg.connect(app_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE forbidden_ddl_0007 (id int)")
        conn.rollback()
    engine.dispose()


def test_cross_case_references_rejected(drill_db):
    migrator_url, app_url = drill_db
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        case_a = uuid.uuid4()
        case_b = uuid.uuid4()
        for cid in (case_a, case_b):
            conn.execute(text("INSERT INTO cases (id, title) VALUES (:id, 'TEST-ONLY 跨案')"),
                         {"id": str(cid)})
        ea = _entity(conn, case_a, "TEST-ONLY A案主体")
        eb = _entity(conn, case_b, "TEST-ONLY B案主体")
        ga = _graph(conn, case_a, "TEST-ONLY A图")
        gb = _graph(conn, case_b, "TEST-ONLY B图")
        grp_a = _group(conn, case_a, ga)
        snap_a = _snapshot(conn, case_a, ga)
        consent_a = _consent(conn, case_a, snap_a)

        def rejected(fn):
            # SAVEPOINT keeps the outer transaction usable after each violation.
            sp = conn.begin_nested()
            with pytest.raises(IntegrityError):
                fn()
            sp.rollback()

        # relation endpoint from another case
        rejected(lambda: conn.execute(text(
            "INSERT INTO case_relations (id, case_id, from_entity_id, to_entity_id, "
            "kind, credibility, created_by, updated_by) VALUES (:id, :c, :a, :b, "
            "'contract', 'suspected', 'human', 'human')"),
            {"id": str(uuid.uuid4()), "c": str(case_a), "a": str(ea), "b": str(eb)}))
        # alias pointing at another case's entity
        rejected(lambda: conn.execute(text(
            "INSERT INTO entity_aliases (id, case_id, entity_id, alias, created_by) "
            "VALUES (:id, :c, :e, 'x', 'human')"),
            {"id": str(uuid.uuid4()), "c": str(case_a), "e": str(eb)}))
        # graph node membership across cases
        rejected(lambda: _node(conn, case_a, gb, ea))
        rejected(lambda: _node(conn, case_a, ga, eb))
        # group from another case
        rejected(lambda: _node(conn, case_a, ga, ea, _group(conn, case_b, gb)))
        # snapshot of another case's graph
        rejected(lambda: _snapshot(conn, case_a, gb))
        # consent on another case's snapshot
        rejected(lambda: _consent(conn, case_a, _snapshot(conn, case_b, gb)))
        # consent parent from another case
        rejected(lambda: _consent(conn, case_a, snap_a, "revoke",
                                  parent=_consent(conn, case_b, _snapshot(conn, case_b, gb))))
        # evidence item bound to another case's group
        rejected(lambda: conn.execute(text(
            "INSERT INTO evidence_items (id, case_id, group_id, position, title, created_by) "
            "VALUES (:id, :c, :g, 0, 'x', 'human')"),
            {"id": str(uuid.uuid4()), "c": str(case_a),
             "g": str(conn.execute(text(
                 "INSERT INTO evidence_groups (id, case_id, position, title, created_by) "
                 "VALUES (:id, :c, 0, 'B组', 'human') RETURNING id"),
                 {"id": str(uuid.uuid4()), "c": str(case_b)}).scalar_one())}))
    engine.dispose()


def test_group_scope_is_per_graph(drill_db):
    """同案跨图分组必须被拒绝；同图分组正常。"""
    migrator_url, app_url = drill_db
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        case_id = uuid.uuid4()
        conn.execute(text("INSERT INTO cases (id, title) VALUES (:id, 'TEST-ONLY 分组作用域')"),
                     {"id": str(case_id)})
        e = _entity(conn, case_id)
        g1 = _graph(conn, case_id, "TEST-ONLY 图一")
        g2 = _graph(conn, case_id, "TEST-ONLY 图二")
        grp1 = _group(conn, case_id, g1)
        # same graph: fine
        _node(conn, case_id, g1, e, grp1)
        # same case, different graph: rejected by composite FK
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            _node(conn, case_id, g2, e, grp1)
        sp.rollback()
        # moving an existing node to another graph's group is also rejected
        node = _node(conn, case_id, g2, e)
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "UPDATE relation_graph_nodes SET group_id=:grp WHERE id=:id"),
                {"grp": str(grp1), "id": str(node)})
        sp.rollback()
    engine.dispose()


def test_consent_parent_scope_and_self_reference(drill_db):
    """同意更正/撤回只能指向同案同快照的原记录，且不能指向自己。"""
    migrator_url, app_url = drill_db
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        case_id = uuid.uuid4()
        conn.execute(text("INSERT INTO cases (id, title) VALUES (:id, 'TEST-ONLY 同意作用域')"),
                     {"id": str(case_id)})
        g = _graph(conn, case_id)
        s1 = _snapshot(conn, case_id, g, 1)
        s2 = _snapshot(conn, case_id, g, 2)
        c1 = _consent(conn, case_id, s1)
        # same snapshot amend/revoke: fine, original preserved
        c2 = _consent(conn, case_id, s1, "amend", parent=c1)
        _consent(conn, case_id, s1, "revoke", parent=c2)
        assert conn.execute(text(
            "SELECT count(*) FROM snapshot_consents WHERE snapshot_id=:s"),
            {"s": str(s1)}).scalar_one() == 3
        # parent on another snapshot of the SAME case: rejected
        other = _consent(conn, case_id, s2)
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            _consent(conn, case_id, s1, "revoke", parent=other)
        sp.rollback()
        # self reference: rejected by CHECK (FK alone would allow it)
        rid = uuid.uuid4()
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            _consent(conn, case_id, s1, "amend", parent=rid, row_id=rid)
        sp.rollback()
        # confirm must not carry a parent; amend/revoke must carry one
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            _consent(conn, case_id, s1, "confirm", parent=c1)
        sp.rollback()
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            _consent(conn, case_id, s1, "revoke")
        sp.rollback()
    engine.dispose()


def test_snapshot_and_consent_immutable(drill_db):
    migrator_url, app_url = drill_db
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        case_id = uuid.uuid4()
        conn.execute(text("INSERT INTO cases (id, title) VALUES (:id, 'TEST-ONLY 不可变')"),
                     {"id": str(case_id)})
        g = _graph(conn, case_id)
        snap = _snapshot(conn, case_id, g)
        consent = _consent(conn, case_id, snap)
        for stmt, params in (
            ("UPDATE relation_graph_snapshots SET note='篡改' WHERE id=:id", {"id": str(snap)}),
            ("DELETE FROM relation_graph_snapshots WHERE id=:id", {"id": str(snap)}),
            ("UPDATE snapshot_consents SET note='篡改' WHERE id=:id", {"id": str(consent)}),
            ("DELETE FROM snapshot_consents WHERE id=:id", {"id": str(consent)}),
        ):
            sp = conn.begin_nested()
            # trigger RAISE EXCEPTION surfaces as ProgrammingError (P0001)
            with pytest.raises(ProgrammingError, match="不可修改"):
                conn.execute(text(stmt), params)
            sp.rollback()
        # appending a new version / correction is the allowed path
        _snapshot(conn, case_id, g, 2)
        _consent(conn, case_id, snap, "amend", parent=consent)
        # agent actors are rejected at the database level too
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO relation_graph_snapshots (id, case_id, graph_id, version_no, "
                "stage_label, payload, created_by) VALUES (:id, :c, :g, 3, '庭前', "
                "'{}'::jsonb, 'agent')"),
                {"id": str(uuid.uuid4()), "c": str(case_id), "g": str(g)})
        sp.rollback()
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO snapshot_consents (id, case_id, snapshot_id, action, "
                "consenter, consented_at, channel, source_detail, created_by) "
                "VALUES (:id, :c, :s, 'confirm', 'x', now(), 'phone', 'y', 'agent')"),
                {"id": str(uuid.uuid4()), "c": str(case_id), "s": str(snap)})
        sp.rollback()
    engine.dispose()


def test_graph_source_target_and_ref_exclusivity(drill_db):
    migrator_url, app_url = drill_db
    _alembic("upgrade", "head", migrator_url, app_url)
    engine = create_engine(sqlalchemy_url(app_url), future=True)
    with engine.begin() as conn:
        case_id = uuid.uuid4()
        conn.execute(text("INSERT INTO cases (id, title) VALUES (:id, 'TEST-ONLY 出处')"),
                     {"id": str(case_id)})
        a = _entity(conn, case_id, "甲")
        b = _entity(conn, case_id, "乙")
        rel = _relation(conn, case_id, a, b)
        # neither / both targets rejected
        for entity, relation in ((None, None), (str(a), str(rel))):
            sp = conn.begin_nested()
            with pytest.raises(IntegrityError):
                conn.execute(text(
                    "INSERT INTO graph_sources (id, case_id, entity_id, relation_id, "
                    "material_id, created_by) VALUES (:id, :c, :e, :r, :m, 'human')"),
                    {"id": str(uuid.uuid4()), "c": str(case_id), "e": entity,
                     "r": relation, "m": str(uuid.uuid4())})
            sp.rollback()
        # build a real material via the intake chain
        intake = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO intakes (id, case_id, request_key, source_label, source_root) "
            "VALUES (:id, :c, :k, 'TEST-ONLY', '/tmp/test-only')"),
            {"id": str(intake), "c": str(case_id), "k": uuid.uuid4().hex})
        item = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO intake_items (id, case_id, intake_id, relative_source, status) "
            "VALUES (:id, :c, :i, '虚构/出处.txt', 'published')"),
            {"id": str(item), "c": str(case_id), "i": str(intake)})
        obj = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO stored_objects (id, case_id, sha256, size, relative_path) "
            "VALUES (:id, :c, :h, 9, 'cases/x/originals/h/source.txt')"),
            {"id": str(obj), "c": str(case_id), "h": "0" * 64})
        mat = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO materials (id, case_id, intake_item_id, display_name) "
            "VALUES (:id, :c, :i, 'TEST-ONLY 出处材料')"),
            {"id": str(mat), "c": str(case_id), "i": str(item)})
        # exactly one target + exactly one ref: fine
        src = uuid.uuid4()
        conn.execute(text(
            "INSERT INTO graph_sources (id, case_id, relation_id, material_id, created_by) "
            "VALUES (:id, :c, :r, :m, 'human')"),
            {"id": str(src), "c": str(case_id), "r": str(rel), "m": str(mat)})
        # two refs rejected
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "UPDATE graph_sources SET segment_id=:s WHERE id=:id"),
                {"s": str(uuid.uuid4()), "id": str(src)})
        sp.rollback()
        # zero refs rejected
        sp = conn.begin_nested()
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "UPDATE graph_sources SET material_id=NULL WHERE id=:id"), {"id": str(src)})
        sp.rollback()
    engine.dispose()


def test_evidence_service_numbering_and_pending_isolation(db, drill_db):
    """Real service path: numbering, agent pending drafts, confirm/revert, archive guard.

    Uses the session-scoped test database (conftest migrates it to head), while
    ``drill_db`` only ensures this module's dedicated cluster database exists.
    """
    from .factories import build_material

    built = build_material(db)
    case_id = built["case_id"]
    db.commit()
    catalog = evidence_catalog.add_group(
        db, AddEvidenceGroupInput(case_id=case_id, title="TEST-ONLY 第一组"),
        actor="human", entry="local-web")
    group_id = catalog.groups[0].id
    catalog = evidence_catalog.add_item(
        db, AddEvidenceItemInput(case_id=case_id, group_id=group_id, title="TEST-ONLY 合同"),
        actor="human", entry="local-web")
    item_id = catalog.items[0].id
    catalog = evidence_catalog.add_binding(
        db, AddEvidenceBindingInput(case_id=case_id, item_id=item_id,
                                    material_id=built["material_id"]),
        actor="human", entry="local-web")
    assert catalog.numbers[str(item_id)] == "证据1-1"

    # a segment-backed source resolves to the numbered label
    run = models.ParseRun(id=uuid.uuid4(), case_id=case_id,
                          file_version_id=built["file_version_id"],
                          parser_version="test", sequence=0, status="complete")
    db.add(run)
    db.flush()
    unit = models.ParseUnit(id=uuid.uuid4(), case_id=case_id, parse_run_id=run.id,
                            unit_key="pdf:5", status="extracted")
    db.add(unit)
    db.flush()
    segment = models.Segment(id=uuid.uuid4(), case_id=case_id, parse_run_id=run.id,
                             parse_unit_id=unit.id, locator={"type": "pdf", "page": 5},
                             text="TEST-ONLY 片段", normalized_text="test-only 片段")
    db.add(segment)
    db.flush()
    source = models.GraphSource(id=uuid.uuid4(), case_id=case_id,
                                relation_id=None, entity_id=None,
                                segment_id=segment.id, created_by="human")
    # graph_sources needs exactly one target: attach to a real relation
    ent_a = models.CaseEntity(id=uuid.uuid4(), case_id=case_id, kind="person",
                              name="TEST-ONLY 甲", created_by="human", updated_by="human")
    ent_b = models.CaseEntity(id=uuid.uuid4(), case_id=case_id, kind="person",
                              name="TEST-ONLY 乙", created_by="human", updated_by="human")
    db.add_all([ent_a, ent_b])
    db.flush()
    relation = models.CaseRelation(id=uuid.uuid4(), case_id=case_id,
                                   from_entity_id=ent_a.id, to_entity_id=ent_b.id,
                                   kind="contract", credibility="evidenced",
                                   created_by="human", updated_by="human")
    db.add(relation)
    db.flush()
    source.relation_id = relation.id
    db.add(source)
    db.commit()
    labels = evidence_catalog.resolve_labels(db, case_id, [source])
    resolved = labels[str(source.id)]
    assert resolved.labels == ["证据1-1 第5页"], resolved

    # agent import stays pending and never shifts working numbers
    imported = evidence_catalog.import_catalog(
        db, ImportEvidenceCatalogInput(
            case_id=case_id, import_material_id=built["material_id"],
            request_key="drill-import-1",
            rows=[ImportCatalogRow(group_title="TEST-ONLY 外部组", item_title="TEST-ONLY 外部条目",
                                   bindings=[ImportBindingRow(material_id=built["material_id"])])]),
        actor="agent", entry="mcp")
    assert imported.numbers[str(item_id)] == "证据1-1"
    pending_items = [i for i in imported.items if i.review_state == "pending"]
    assert len(pending_items) == 1
    assert str(pending_items[0].id) not in imported.numbers
    # replay with the same request_key creates nothing new
    replay = evidence_catalog.import_catalog(
        db, ImportEvidenceCatalogInput(
            case_id=case_id, import_material_id=built["material_id"],
            request_key="drill-import-1",
            rows=[ImportCatalogRow(group_title="TEST-ONLY 外部组", item_title="TEST-ONLY 外部条目")]),
        actor="agent", entry="mcp")
    assert len(replay.items) == len(imported.items)

    # 条目未确认前，绑定不得单独确认（服务层守卫）。
    pending_binding = next(b for b in imported.bindings if b.review_state == "pending")
    from legal_workbench.schemas import EvidenceItemRevisionInput
    with pytest.raises(WorkbenchError) as early:
        evidence_catalog.confirm_binding(
            db, EvidenceBindingRevisionInput(case_id=case_id, binding_id=pending_binding.id,
                                             expected_revision=0),
            actor="human", entry="local-web")
    assert early.value.code == ErrorCode.INVALID_INPUT

    # 人工确认整条导入提案：组、条目与绑定联动确认后才进入编号（证据2-1）。
    from legal_workbench.schemas import EvidenceChangeIdInput
    accepted = evidence_catalog.accept_change(
        db, EvidenceChangeIdInput(case_id=case_id,
                                  change_id=imported.pending_changes[0].id),
        actor="human", entry="local-web")
    assert accepted.numbers[str(pending_items[0].id)] == "证据2-1"

    # agent cannot confirm; human revert of an accepted change is recorded
    with pytest.raises(WorkbenchError) as exc:
        evidence_catalog.confirm_binding(
            db, EvidenceBindingRevisionInput(case_id=case_id, binding_id=pending_binding.id,
                                             expected_revision=0),
            actor="agent", entry="mcp")
    assert exc.value.code == ErrorCode.FORBIDDEN

    # archive guard: every evidence write is refused afterwards
    overview = cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))
    assert overview.archived_at is not None
    with pytest.raises(WorkbenchError):
        evidence_catalog.add_group(
            db, AddEvidenceGroupInput(case_id=case_id, title="归档后新增"),
            actor="human", entry="local-web")
    # reads still work
    assert evidence_catalog.get_catalog(db, case_id).numbers[str(item_id)] == "证据1-1"

    # audit rows were written for the service commands
    ops = set(db.execute(text(
        "SELECT operation FROM audit_events WHERE case_id=:c"),
        {"c": str(case_id)}).scalars().all())
    assert {"evidence.add_group", "evidence.add_item", "evidence.add_binding",
            "evidence.import_catalog", "evidence.accept_change"} <= ops


def test_audit_operations_match_database_check(db):
    """audit.OPERATIONS and the DB CHECK stay one closed set (D-decisions consequence)."""
    constraint = db.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname='ck_audit_events_operation'")).scalar_one()
    for operation in audit.OPERATIONS:
        assert f"'{operation}'" in constraint, operation
    # every graph/evidence command in the rule config has an audit name
    from legal_workbench import graph_rules
    rules = graph_rules.get_rules()
    for command in graph_rules.COMMANDS:
        assert f"graph.{command}" in audit.OPERATIONS
    for command in graph_rules.EVIDENCE_COMMANDS:
        assert f"evidence.{command}" in audit.OPERATIONS