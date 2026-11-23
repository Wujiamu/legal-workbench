"""Audit trail: every audited operation writes exactly one event, with no secrets."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from legal_workbench import (analysis, audit, cases, imports, materials, models, parsing,
                             search, storage as storage_module)
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import ReviewStatus, SearchMode, StatementKind
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def events(db) -> list[models.AuditEvent]:
    db.expire_all()
    return db.execute(select(models.AuditEvent).order_by(models.AuditEvent.created_at)
                      ).scalars().all()


def test_case_creation_is_audited(db, store):
    audit.set_context(actor="test", request_id=uuid.uuid4())
    created = cases.create_case(db, title="TEST-ONLY 审计案件")
    rows = events(db)
    # K14: creating a case also instantiates the default progress fragment,
    # so two events are expected: case.create + progress.insert_fragment.
    assert len(rows) == 2
    row = next(r for r in rows if r.operation == "case.create")
    assert row.target_type == "case"
    assert row.target_id == created.case_id
    assert row.case_id == created.case_id
    assert row.actor == "test"
    assert row.request_id is not None
    fragment_row = next(r for r in rows if r.operation == "progress.insert_fragment")
    assert fragment_row.case_id == created.case_id


def test_material_operations_are_audited(db, store):
    audit.set_context(actor="test")
    built = factories.build_material_with_storage(
        db, store, content="TEST-ONLY 正文 金额100元。".encode(), name="合同.txt")
    case_id, material_id = built["case_id"], built["material_id"]

    materials.update_classification(db, case_id=case_id, material_id=material_id,
                                    category="合同", expected_revision=0)
    materials.update_display_name(db, case_id=case_id, material_id=material_id,
                                  display_name="合同（改名）.txt", expected_revision=1)
    working_id, _relative = materials.create_working_copy(
        db, store, case_id=case_id, file_version_id=built["file_version_id"],
        display_name="副本.txt")

    operations = [row.operation for row in events(db)]
    assert "material.classify" in operations
    assert "material.rename" in operations
    assert "material.working_copy" in operations
    assert all(row.case_id == case_id for row in events(db))


def test_analysis_save_and_review_are_audited(db, store):
    audit.set_context(actor="agent")
    built = factories.build_material_with_storage(
        db, store, content="TEST-ONLY 正文。".encode(), name="合同.txt")
    parsing.create_parse_run(db, case_id=built["case_id"],
                             file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store,
                               db.execute(select(models.Job).where(
                                   models.Job.kind == "parse")).scalars().first().id)
    db.expire_all()
    segment = db.execute(select(models.Segment)).scalars().first()

    saved = analysis.save_analysis(db, store, case_id=built["case_id"],
                                   title="TEST-ONLY 分析", content="结论：有效。",
                                   statement_kind=StatementKind.AGENT_ANALYSIS,
                                   source_refs=[segment.id])
    analysis.review_analysis(db, case_id=built["case_id"], analysis_id=saved.analysis_id,
                             version_id=saved.version_id, expected_revision=0,
                             review_status=ReviewStatus.APPROVED)

    operations = [row.operation for row in events(db)]
    assert operations.count("analysis.save") == 1
    assert operations.count("analysis.review") == 1
    assert all(row.actor == "agent" for row in events(db))


def test_audit_never_records_content_or_secrets(db, store):
    """Only identifiers and operation names are stored: no text, no key, no URL."""
    audit.set_context(actor="test")
    secret_text = "TEST-ONLY 极度敏感正文 SHOULD-NOT-APPEAR"
    built = factories.build_material_with_storage(
        db, store, content=secret_text.encode(), name="敏感.txt")
    materials.update_classification(db, case_id=built["case_id"],
                                    material_id=built["material_id"],
                                    category="合同", expected_revision=0)
    dumped = json.dumps([row.__dict__ for row in events(db)], default=str, ensure_ascii=False)
    assert "SHOULD-NOT-APPEAR" not in dumped
    assert "postgresql://" not in dumped
    assert "lwb_app" not in dumped


def test_unknown_operation_is_rejected(db):
    with pytest.raises(ValueError):
        audit.record(db, operation="made.up", target_type="x")


def test_non_uuid_target_id_is_hashed_stably(db):
    """Backup digests and maintenance rows are not UUIDs; target_id must still work."""
    audit.set_context(actor="test")
    audit.record(db, operation="backup.create", target_type="backup",
                 target_id="d9b9f9c3243aaa56", commit=True)
    audit.record(db, operation="maintenance.clear", target_type="maintenance",
                 target_id="1", commit=True)
    rows = events(db)
    assert len(rows) == 2
    assert all(isinstance(row.target_id, uuid.UUID) for row in rows)
    # Same input must produce the same id, so events stay correlatable.
    audit.record(db, operation="backup.create", target_type="backup",
                 target_id="d9b9f9c3243aaa56", commit=True)
    rows = events(db)
    assert rows[0].target_id == rows[-1].target_id


def test_operation_check_constraint_rejects_unknown_value(db):
    """The database, not just the Python helper, enforces the closed set."""
    from sqlalchemy.exc import IntegrityError

    db.add(models.AuditEvent(id=uuid.uuid4(), actor="test", operation="bogus.operation",
                             target_type="x"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
