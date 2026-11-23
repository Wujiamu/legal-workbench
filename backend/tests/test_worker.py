"""T07: single worker lock, crash recovery, maintenance gate, retry."""
from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import select, text

from legal_workbench import imports, jobs, materials, models, worker
from legal_workbench.config import load_settings
from legal_workbench.db import get_engine, get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def import_root(tmp_path):
    root = tmp_path / "import-root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"TEST-ONLY a")
    (root / "b.txt").write_bytes(b"TEST-ONLY b")
    return root


@pytest.fixture
def settings_for(import_root, tmp_path):
    return dataclasses.replace(load_settings(), import_roots=(import_root,),
                               data_root=tmp_path / "data")


@pytest.fixture
def store(settings_for):
    return Storage(root=settings_for.data_root)


@pytest.fixture
def queued_job(db, store, settings_for, import_root):
    case_id = factories.create_case(db)
    created = imports.create_intake(
        db, store, case_id=case_id, source_path=str(import_root),
        source_label="TEST-ONLY", request_key="req-1", settings=settings_for,
    )
    return created, case_id


def test_second_worker_exits_without_processing(db, store, queued_job):
    created, _ = queued_job
    engine = get_engine()
    lock = worker.try_acquire_worker_lock(engine)
    assert lock is not None
    try:
        assert worker.run_worker(engine, store, once=True) == 0
        assert db.get(models.Job, created.job_id).status == "queued"
    finally:
        worker.release_worker_lock(lock)
    # The import job queues one parse job per material, so more than one job runs.
    assert worker.run_worker(engine, store, once=True) >= 1
    assert db.get(models.Job, created.job_id).status == "succeeded"


def test_running_job_is_recovered_and_completed(db, store, queued_job):
    created, case_id = queued_job
    db.execute(text("UPDATE jobs SET status='running' WHERE id=:id"), {"id": created.job_id})
    db.execute(text("UPDATE intake_items SET status='copying' WHERE intake_id=:id"),
               {"id": created.intake_id})
    db.commit()
    assert worker.run_worker(get_engine(), store, once=True) >= 1
    job = db.get(models.Job, created.job_id)
    assert job.status == "succeeded" and job.completed_units == 2
    assert db.execute(select(models.Material).where(models.Material.case_id == case_id)).scalars().all().__len__() == 2


def test_crash_mid_job_then_worker_resumes(db, store, queued_job):
    created, case_id = queued_job
    calls = {"n": 0}

    def fail_first(stage):
        if stage == "copying":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        imports.process_import_job(get_session_factory(), store, created.job_id, fault_inject=fail_first)
    assert db.get(models.Job, created.job_id).status == "running"
    assert worker.run_worker(get_engine(), store, once=True) >= 1
    assert db.get(models.Job, created.job_id).status == "succeeded"
    assert len(db.execute(select(models.Material).where(models.Material.case_id == case_id)).scalars().all()) == 2


def test_maintenance_flag_rejects_new_writes(db, store, settings_for, import_root):
    case_id = factories.create_case(db)
    built = factories.build_material(db, case_id=case_id)
    db.execute(text("UPDATE maintenance_state SET active=true, reason='test' WHERE id=1"))
    db.commit()
    with pytest.raises(WorkbenchError) as exc:
        imports.create_intake(db, store, case_id=case_id, source_path=str(import_root),
                              source_label="TEST-ONLY", request_key="m", settings=settings_for)
    assert exc.value.code == ErrorCode.MAINTENANCE
    db.rollback()
    with pytest.raises(WorkbenchError) as exc:
        materials.update_classification(db, case_id=case_id, material_id=built["material_id"],
                                        category="x", expected_revision=0)
    assert exc.value.code == ErrorCode.MAINTENANCE
    db.rollback()
    db.execute(text("UPDATE maintenance_state SET active=false, reason=NULL WHERE id=1"))
    db.commit()


def test_failed_job_can_be_retried_with_same_id(db, store, settings_for, import_root, monkeypatch):
    case_id = factories.create_case(db)
    created = imports.create_intake(db, store, case_id=case_id, source_path=str(import_root),
                                    source_label="TEST-ONLY", request_key="retry", settings=settings_for)
    original = Storage.stage_path

    def broken(self, source, original_name=None, operation_id=None, max_bytes=None):
        raise WorkbenchError(ErrorCode.CONFLICT, "simulated conflict")

    monkeypatch.setattr(Storage, "stage_path", broken)
    assert worker.run_worker(get_engine(), store, once=True) >= 1
    assert db.get(models.Job, created.job_id).status == "failed"
    status = jobs.get_job_status(db, case_id=case_id, job_id=created.job_id)
    assert status.status.value == "failed" and len(status.errors) == 2
    assert {error.error_code for error in status.errors} == {"conflict"}
    monkeypatch.setattr(Storage, "stage_path", original)
    retried = jobs.retry_job(db, case_id=case_id, job_id=created.job_id)
    assert retried.id == created.job_id and retried.status == "queued"
    assert worker.run_worker(get_engine(), store, once=True) >= 1
    db.expire_all()
    assert db.get(models.Job, created.job_id).status == "succeeded"
    assert db.get(models.Job, created.job_id).attempt >= 2
    assert len(db.execute(select(models.Material).where(models.Material.case_id == case_id)).scalars().all()) == 2


def test_one_failing_job_does_not_block_next(db, store, settings_for, import_root):
    case_id = factories.create_case(db)
    bad = models.Job(id=uuid.uuid4(), case_id=case_id, kind="bogus", target_id=uuid.uuid4(),
                     status="queued", total_units=0)
    db.add(bad)
    db.commit()
    created = imports.create_intake(db, store, case_id=case_id, source_path=str(import_root),
                                    source_label="TEST-ONLY", request_key="after", settings=settings_for)
    assert worker.run_worker(get_engine(), store, once=True) >= 2
    assert db.get(models.Job, bad.id).status == "failed"
    assert db.get(models.Job, created.job_id).status == "succeeded"


def test_claim_next_job_marks_running(db, store, queued_job):
    created, _ = queued_job
    session = db
    claimed = worker.claim_next_job(session)
    assert claimed.id == created.job_id and claimed.status == "running" and claimed.attempt == 1
    assert worker.claim_next_job(session) is None
