"""T06: classification, display names, working copies, registered versions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from legal_workbench import materials, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


@pytest.fixture
def built(db, store):
    return factories.build_material_with_storage(db, store, content=b"TEST-ONLY original bytes",
                                                 name="合同原件.txt")


def test_classification_does_not_touch_original(db, store, built):
    before = db.get(models.Material, built["material_id"])
    original_path = before.original_file_version_id
    object_before = db.get(models.StoredObject, built["stored_object_id"])
    path_before = object_before.relative_path
    revision = materials.update_classification(
        db, case_id=built["case_id"], material_id=built["material_id"],
        category="合同", expected_revision=0,
    )
    assert revision == 1
    after = db.get(models.Material, built["material_id"])
    assert after.category == "合同" and after.original_file_version_id == original_path
    object_after = db.get(models.StoredObject, built["stored_object_id"])
    assert object_after.relative_path == path_before
    assert store.managed_path(path_before).read_bytes() == b"TEST-ONLY original bytes"


def test_blank_category_becomes_default(db, store, built):
    materials.update_classification(db, case_id=built["case_id"], material_id=built["material_id"],
                                    category="   ", expected_revision=0)
    assert db.get(models.Material, built["material_id"]).category == "待分类"


def test_stale_revision_conflicts(db, store, built):
    materials.update_classification(db, case_id=built["case_id"], material_id=built["material_id"],
                                    category="合同", expected_revision=0)
    with pytest.raises(WorkbenchError) as exc:
        materials.update_classification(db, case_id=built["case_id"],
                                        material_id=built["material_id"],
                                        category="证据", expected_revision=0)
    assert exc.value.code == ErrorCode.CONFLICT


def test_display_name_is_sanitized(db, store, built):
    revision = materials.update_display_name(
        db, case_id=built["case_id"], material_id=built["material_id"],
        display_name="../../新名字.docx", expected_revision=0,
    )
    assert revision == 1
    assert db.get(models.Material, built["material_id"]).display_name == "新名字.docx"


def test_working_copy_is_editable_without_touching_history(db, store, built):
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="编辑稿.txt",
    )
    working_path = store.managed_path(relative)
    original_path = store.managed_path(built["relative_path"])
    assert working_path.read_bytes() == original_path.read_bytes()
    working_path.write_bytes(b"externally edited bytes")
    assert original_path.read_bytes() == b"TEST-ONLY original bytes"
    version = db.get(models.FileVersion, built["file_version_id"])
    stored = db.get(models.StoredObject, version.stored_object_id)
    assert store.managed_path(stored.relative_path).read_bytes() == b"TEST-ONLY original bytes"


def test_register_version_snapshots_current_bytes(db, store, built):
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="编辑稿.txt",
    )
    working_path = store.managed_path(relative)
    working_path.write_bytes(b"first edit")
    version_id, job_id = materials.register_file_version(
        db, store, case_id=built["case_id"], working_copy_id=working_id, expected_revision=0,
    )
    version = db.get(models.FileVersion, version_id)
    stored = db.get(models.StoredObject, version.stored_object_id)
    assert version.kind == "working" and version.parent_version_id == built["file_version_id"]
    assert store.managed_path(stored.relative_path).read_bytes() == b"first edit"
    assert db.get(models.WorkingCopy, working_id).revision == 1
    job = db.get(models.Job, job_id)
    run = db.get(models.ParseRun, job.target_id)
    assert job.kind == "parse" and job.status == "queued"
    assert run.file_version_id == version_id and run.sequence == 0
    working_path.write_bytes(b"second edit not registered")
    assert store.managed_path(stored.relative_path).read_bytes() == b"first edit"
    with pytest.raises(WorkbenchError) as exc:
        materials.register_file_version(
            db, store, case_id=built["case_id"], working_copy_id=working_id, expected_revision=0,
        )
    assert exc.value.code == ErrorCode.CONFLICT
    second_version, _ = materials.register_file_version(
        db, store, case_id=built["case_id"], working_copy_id=working_id, expected_revision=1,
    )
    second = db.get(models.FileVersion, second_version)
    assert second.parent_version_id == version_id


def test_cross_case_file_version_rejected(db, store, built):
    other_case = factories.create_case(db, "另一案件")
    with pytest.raises(WorkbenchError) as exc:
        materials.create_working_copy(db, store, case_id=other_case,
                                      file_version_id=built["file_version_id"],
                                      display_name="x.txt")
    assert exc.value.code == ErrorCode.CASE_MISMATCH


def test_missing_working_file_rejected(db, store, built):
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="编辑稿.txt",
    )
    store.managed_path(relative).unlink()
    with pytest.raises(WorkbenchError) as exc:
        materials.register_file_version(db, store, case_id=built["case_id"],
                                        working_copy_id=working_id, expected_revision=0)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_symlinked_working_file_rejected(db, store, built, tmp_path):
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="编辑稿.txt",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    path = store.managed_path(relative)
    path.unlink()
    factories.symlink_or_skip(path, outside)
    with pytest.raises(WorkbenchError) as exc:
        materials.register_file_version(db, store, case_id=built["case_id"],
                                        working_copy_id=working_id, expected_revision=0)
    assert exc.value.code == ErrorCode.INVALID_PATH


def test_list_materials_filters_and_pagination(db, store, built):
    second = factories.build_material_with_storage(db, store, case_id=built["case_id"],
                                                   content=b"second", name="第二份.txt")
    materials.update_classification(db, case_id=built["case_id"], material_id=second["material_id"],
                                    category="证据", expected_revision=0)
    page, total, _case_total, _cats = materials.list_materials(
        db, case_id=built["case_id"], limit=1, offset=0)
    assert total == 2 and len(page) == 1 and page[0].coverage.pending == 1
    filtered, filtered_total, _ct, _cs = materials.list_materials(
        db, case_id=built["case_id"], category="证据")
    assert filtered_total == 1 and filtered[0].material_id == second["material_id"]
    assert filtered[0].source_label == "TEST-ONLY"
    assert filtered[0].relative_source == "虚构/第二份.txt"
    none, none_total, _ct2, _cs2 = materials.list_materials(
        db, case_id=built["case_id"], category="不存在")
    assert none_total == 0 and none == []
    with pytest.raises(WorkbenchError):
        materials.list_materials(db, case_id=uuid.uuid4())


def test_numbered_material_names_sort_naturally(db, store):
    """TEST-ONLY: inside one import batch, numbered names paginate in numeric order.

    Rows of a single batch share created_at, so the tiebreaker decides the whole
    list.  Pure lexical order would put 材料10 before 材料2 and bury 材料101 far
    from 材料100.
    """
    case = factories.create_case(db)
    stamp = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    for number in (1, 2, 9, 10, 11, 99, 100, 101, 120):
        built = factories.build_material_with_storage(
            db, store, content=f"TEST-ONLY {number}".encode(), name=f"材料{number}.txt",
            case_id=case)
        db.get(models.Material, built["material_id"]).created_at = stamp
    db.commit()

    names = [m.display_name for m in materials.list_materials(
        db, case_id=case, limit=100)[0]]
    assert names == [f"材料{n}.txt" for n in (1, 2, 9, 10, 11, 99, 100, 101, 120)], names
