"""T05: intake batches, idempotency, per-item states, crash recovery."""
from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import func, select

from legal_workbench import imports, models
from legal_workbench.config import load_settings
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.storage import Storage, hash_file


@pytest.fixture
def import_root(tmp_path):
    root = tmp_path / "import-root"
    (root / "sub").mkdir(parents=True)
    (root / "合同.txt").write_bytes("TEST-ONLY 合同正文".encode())
    (root / "empty.txt").write_bytes(b"")
    (root / "broken.docx").write_bytes(b"PK-not-a-real-zip")
    (root / "sub" / "unknown.xyz").write_bytes(b"unsupported but stored")
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "link.txt").symlink_to("/etc/hosts")
    (root / "linkdir").symlink_to("/tmp")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_bytes(b"junk dir")
    return root


@pytest.fixture
def settings_for(import_root, tmp_path):
    base = load_settings()
    return dataclasses.replace(base, import_roots=(import_root,), data_root=tmp_path / "data")


@pytest.fixture
def store(settings_for):
    return Storage(root=settings_for.data_root)


def make_intake(db, store, settings, case_id, root, key="req-1"):
    return imports.create_intake(
        db, store, case_id=case_id, source_path=str(root), source_label="TEST-ONLY",
        request_key=key, settings=settings,
    )


def items_of(db, intake_id):
    return db.execute(
        select(models.IntakeItem).where(models.IntakeItem.intake_id == intake_id)
        .order_by(models.IntakeItem.relative_source)
    ).scalars().all()


def test_mixed_batch_all_items_terminal(db, store, settings_for, import_root):
    from tests import factories
    case_id = factories.create_case(db)
    created = make_intake(db, store, settings_for, case_id, import_root)
    assert created.created and created.item_count == 8
    outcome = imports.process_import_job(get_session_factory(), store, created.job_id)
    assert (outcome.ready, outcome.failed, outcome.skipped) == (4, 0, 4)
    assert outcome.status == "succeeded"
    rows = items_of(db, created.intake_id)
    assert {row.status for row in rows} <= {"ready", "failed", "skipped"}
    assert {row.error_code for row in rows if row.status == "skipped"} == {
        "ignored_system_file", "symlink_skipped", "ignored_system_directory",
    }
    materials = db.execute(select(models.Material).where(models.Material.case_id == case_id)).scalars().all()
    assert len(materials) == 4
    objects = db.execute(select(models.StoredObject).where(models.StoredObject.case_id == case_id)).scalars().all()
    assert len(objects) == 4
    by_name = {row.relative_source: row for row in rows}
    broken = by_name["broken.docx"]
    assert broken.material_id is not None
    material = db.get(models.Material, broken.material_id)
    version = db.get(models.FileVersion, material.original_file_version_id)
    stored = db.get(models.StoredObject, version.stored_object_id)
    assert store.managed_path(stored.relative_path).read_bytes() == b"PK-not-a-real-zip"
    empty_item = by_name["empty.txt"]
    empty_material = db.get(models.Material, empty_item.material_id)
    empty_version = db.get(models.FileVersion, empty_material.original_file_version_id)
    empty_object = db.get(models.StoredObject, empty_version.stored_object_id)
    assert empty_object.size == 0
    assert empty_object.sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_same_request_key_is_idempotent(db, store, settings_for, import_root):
    from tests import factories
    case_id = factories.create_case(db)
    first = make_intake(db, store, settings_for, case_id, import_root)
    second = make_intake(db, store, settings_for, case_id, import_root)
    assert first.intake_id == second.intake_id and first.job_id == second.job_id
    assert second.created is False and second.item_count == first.item_count
    assert db.execute(select(func.count()).select_from(models.Intake)).scalar() == 1


def test_new_batch_shares_object_but_creates_new_materials(db, store, settings_for, import_root):
    from tests import factories
    case_id = factories.create_case(db)
    first = make_intake(db, store, settings_for, case_id, import_root, "batch-1")
    imports.process_import_job(get_session_factory(), store, first.job_id)
    second = make_intake(db, store, settings_for, case_id, import_root, "batch-2")
    assert second.created is True
    imports.process_import_job(get_session_factory(), store, second.job_id)
    materials = db.execute(select(models.Material).where(models.Material.case_id == case_id)).scalars().all()
    objects = db.execute(select(models.StoredObject).where(models.StoredObject.case_id == case_id)).scalars().all()
    assert len(materials) == 8 and len(objects) == 4
    assert len({m.id for m in materials}) == 8


def test_crash_at_copying_then_retry_has_no_duplicates(db, store, settings_for, import_root):
    from tests import factories
    case_id = factories.create_case(db)
    created = make_intake(db, store, settings_for, case_id, import_root)
    calls = {"n": 0}

    def fail_first(stage):
        if stage == "copying":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash during copying")

    with pytest.raises(RuntimeError):
        imports.process_import_job(get_session_factory(), store, created.job_id, fault_inject=fail_first)
    job = db.get(models.Job, created.job_id)
    assert job.status == "running"
    outcome = imports.process_import_job(get_session_factory(), store, created.job_id)
    assert outcome.ready == 4 and outcome.failed == 0
    materials = db.execute(select(models.Material).where(models.Material.case_id == case_id)).scalars().all()
    assert len(materials) == 4


def test_crash_after_publish_then_retry_associates(db, store, settings_for, import_root):
    from tests import factories
    case_id = factories.create_case(db)
    created = make_intake(db, store, settings_for, case_id, import_root)
    calls = {"n": 0}

    def fail_first(stage):
        if stage == "published":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after publish")

    with pytest.raises(RuntimeError):
        imports.process_import_job(get_session_factory(), store, created.job_id, fault_inject=fail_first)
    published = db.execute(select(models.IntakeItem).where(
        models.IntakeItem.intake_id == created.intake_id,
        models.IntakeItem.status == "published",
    )).scalars().all()
    assert len(published) == 1 and published[0].stored_object_id is not None
    outcome = imports.process_import_job(get_session_factory(), store, created.job_id)
    assert outcome.ready == 4
    assert db.execute(select(func.count()).select_from(models.StoredObject)
                      .where(models.StoredObject.case_id == case_id)).scalar() == 4


def test_crash_after_association_then_retry_no_duplicate_material(db, store, settings_for, import_root):
    from tests import factories
    case_id = factories.create_case(db)
    created = make_intake(db, store, settings_for, case_id, import_root)
    calls = {"n": 0}

    def fail_first(stage):
        if stage == "associated":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash after association")

    with pytest.raises(RuntimeError):
        imports.process_import_job(get_session_factory(), store, created.job_id, fault_inject=fail_first)
    outcome = imports.process_import_job(get_session_factory(), store, created.job_id)
    assert outcome.ready == 4
    assert db.execute(select(func.count()).select_from(models.Material)
                      .where(models.Material.case_id == case_id)).scalar() == 4


def test_source_outside_import_roots_rejected(db, store, settings_for, tmp_path):
    from tests import factories
    case_id = factories.create_case(db)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_bytes(b"x")
    with pytest.raises(WorkbenchError) as exc:
        imports.create_intake(db, store, case_id=case_id, source_path=str(outside),
                              source_label="TEST-ONLY", request_key="bad", settings=settings_for)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_symlink_source_rejected(db, store, settings_for, import_root, tmp_path):
    from tests import factories
    case_id = factories.create_case(db)
    link = tmp_path / "link-to-root"
    link.symlink_to(import_root)
    with pytest.raises(WorkbenchError) as exc:
        imports.create_intake(db, store, case_id=case_id, source_path=str(link),
                              source_label="TEST-ONLY", request_key="link", settings=settings_for)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_subdirectory_source_resolves_against_import_root(db, store, settings_for, import_root):
    """A subdirectory import must stay resolvable: items are stored relative to the root."""
    from tests import factories
    case_id = factories.create_case(db)
    created = imports.create_intake(
        db, store, case_id=case_id, source_path=str(import_root / "sub"),
        source_label="TEST-ONLY 子目录", request_key="subdir", settings=settings_for)
    items = items_of(db, created.intake_id)
    assert [item.relative_source for item in items] == ["sub/unknown.xyz"]
    imports.process_import_job(get_session_factory(), store, created.job_id)
    db.expire_all()
    item = db.get(models.IntakeItem, items[0].id)
    assert item.status == "ready", (item.error_code, item.error_message)


def test_source_change_fails_item_and_retry_reuses_job(db, store, settings_for, tmp_path, monkeypatch):
    from tests import factories
    root = tmp_path / "single"
    root.mkdir()
    (root / "a.txt").write_bytes(b"content")
    settings = dataclasses.replace(settings_for, import_roots=(root,))
    case_id = factories.create_case(db)
    created = imports.create_intake(db, store, case_id=case_id, source_path=str(root),
                                    source_label="TEST-ONLY", request_key="one", settings=settings)
    original = Storage.stage_path

    def tampered(self, source, original_name=None, operation_id=None, max_bytes=None):
        raise WorkbenchError(ErrorCode.CONFLICT, "源文件在复制期间发生变化")

    monkeypatch.setattr(Storage, "stage_path", tampered)
    outcome = imports.process_import_job(get_session_factory(), store, created.job_id)
    assert outcome.status == "failed"
    item = items_of(db, created.intake_id)[0]
    assert item.status == "failed" and item.error_code == "conflict"
    monkeypatch.setattr(Storage, "stage_path", original)
    imports.reset_job_for_retry(db, created.job_id)
    retried = imports.process_import_job(get_session_factory(), store, created.job_id)
    assert retried.status == "succeeded"
    assert db.execute(select(func.count()).select_from(models.Material)).scalar() == 1
    assert db.get(models.Job, created.job_id).attempt == 1
