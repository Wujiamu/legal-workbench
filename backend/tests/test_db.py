"""T03: real PostgreSQL schema, constraints, role separation and CAS."""
from __future__ import annotations

import subprocess
import sys
import uuid

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from legal_workbench import models
from legal_workbench.db import update_with_revision

from . import factories


def test_schema_and_extension(db):
    tables = set(db.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )).scalars())
    expected = {
        "cases", "intakes", "intake_items", "stored_objects", "materials", "file_versions",
        "working_copies", "parse_runs", "parse_units", "segments", "exact_values", "jobs",
        "analyses", "analysis_versions", "audit_events", "maintenance_state",
    }
    assert expected <= tables
    assert db.execute(text("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'")).scalar() == 1
    active = db.execute(text("SELECT active FROM maintenance_state WHERE id=1")).scalar()
    assert active is False


def test_upgrade_rerun_is_noop(test_database):
    from pathlib import Path
    import os
    backend_dir = Path(__file__).resolve().parents[1]
    env = dict(os.environ, LWB_MIGRATION_URL=test_database[0], LWB_DATABASE_URL=test_database[1])
    before = subprocess.run(
        [sys.executable, "-m", "alembic", "current"], cwd=backend_dir, env=env,
        capture_output=True, text=True, check=True,
    ).stdout
    again = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend_dir, env=env,
        capture_output=True, text=True,
    )
    assert again.returncode == 0, again.stderr
    after = subprocess.run(
        [sys.executable, "-m", "alembic", "current"], cwd=backend_dir, env=env,
        capture_output=True, text=True, check=True,
    ).stdout
    assert before.strip() and before == after


def test_cross_case_material_reference_rejected(db):
    a = factories.build_material(db)
    other_case = factories.create_case(db, "另一虚构案件")
    with pytest.raises(IntegrityError):
        db.add(models.FileVersion(
            id=uuid.uuid4(), case_id=other_case, material_id=a["material_id"],
            stored_object_id=a["stored_object_id"], kind="working",
        ))
        db.flush()
    db.rollback()


def test_intake_request_key_unique_per_case(db):
    case_a = factories.create_case(db)
    case_b = factories.create_case(db)
    factories.create_intake(db, case_a, "same-key")
    db.commit()
    with pytest.raises(IntegrityError):
        factories.create_intake(db, case_a, "same-key")
    db.rollback()
    factories.create_intake(db, case_b, "same-key")
    db.commit()


def test_content_hash_unique_per_case(db):
    case_a = factories.create_case(db)
    case_b = factories.create_case(db)
    factories.create_stored_object(db, case_a, b"same synthetic bytes")
    db.commit()
    with pytest.raises(IntegrityError):
        factories.create_stored_object(db, case_a, b"same synthetic bytes")
    db.rollback()
    factories.create_stored_object(db, case_b, b"same synthetic bytes")
    db.commit()


def test_original_version_must_belong_to_same_material(db):
    case_id = factories.create_case(db)
    intake_id = factories.create_intake(db, case_id)
    item_a = factories.create_intake_item(db, case_id, intake_id, "虚构/a.txt")
    item_b = factories.create_intake_item(db, case_id, intake_id, "虚构/b.txt")
    object_id = factories.create_stored_object(db, case_id)
    material_a = factories.create_material(db, case_id, item_a, "a.txt")
    material_b = factories.create_material(db, case_id, item_b, "b.txt")
    version_b = factories.create_file_version(db, case_id, material_b, object_id)
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE materials SET original_file_version_id=:fv WHERE id=:m"),
                   {"fv": version_b, "m": material_a})
        db.flush()
    db.rollback()
    version_a = factories.create_file_version(db, case_id, material_a, object_id)
    db.execute(text("UPDATE materials SET original_file_version_id=:fv WHERE id=:m"),
               {"fv": version_a, "m": material_a})
    db.commit()


def test_intake_item_rejects_foreign_case_intake(db):
    case_a = factories.create_case(db)
    case_b = factories.create_case(db)
    intake_b = factories.create_intake(db, case_b)
    with pytest.raises(IntegrityError):
        db.add(models.IntakeItem(id=uuid.uuid4(), case_id=case_a, intake_id=intake_b,
                                 relative_source="虚构/x.txt", status="pending"))
        db.flush()
    db.rollback()


def test_parse_unit_key_unique_per_run(db):
    built = factories.build_material(db)
    run = models.ParseRun(
        id=uuid.uuid4(), case_id=built["case_id"], file_version_id=built["file_version_id"],
        parser_version="test-1", sequence=0, status="queued",
    )
    db.add(run)
    db.flush()
    db.add(models.ParseUnit(id=uuid.uuid4(), case_id=built["case_id"], parse_run_id=run.id,
                            unit_key="page:1", status="pending"))
    db.flush()
    with pytest.raises(IntegrityError):
        db.add(models.ParseUnit(id=uuid.uuid4(), case_id=built["case_id"], parse_run_id=run.id,
                                unit_key="page:1", status="pending"))
        db.flush()
    db.rollback()


def test_revision_cas_only_one_connection_wins(app_engine, db):
    case_id = factories.create_case(db)
    db.commit()
    with Session(app_engine) as first, Session(app_engine) as second:
        assert update_with_revision(first, models.Case, case_id, 0, title="第一次修改")
        first.commit()
        assert not update_with_revision(second, models.Case, case_id, 0, title="过期修改")
        second.rollback()
    row = db.execute(text("SELECT title, revision FROM cases WHERE id=:id"), {"id": case_id}).one()
    assert tuple(row) == ("第一次修改", 1)


def test_app_role_cannot_create_table(app_database_url):
    with psycopg.connect(app_database_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE forbidden_ddl (id int)")
        conn.rollback()


def test_app_role_can_write_business_rows(db):
    case_id = factories.create_case(db)
    db.commit()
    assert db.execute(text("SELECT count(*) FROM cases WHERE id=:id"), {"id": case_id}).scalar() == 1
