"""T06 regression: identical bytes must reuse one StoredObject, not insert a duplicate."""
from __future__ import annotations

import pytest

from legal_workbench import materials, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def test_registering_unchanged_working_copy_reuses_stored_object(db, store):
    """An unedited working copy has the same sha256 as the original.

    The (case_id, sha256) unique constraint means registering it must reuse the
    existing StoredObject rather than inserting a duplicate row.
    """
    content = "TEST-ONLY 合同正文".encode()
    built = factories.build_material_with_storage(db, store, content=content, name="合同.txt")
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="合同.txt")
    version_id, job_id = materials.register_file_version(
        db, store, case_id=built["case_id"], working_copy_id=working_id, expected_revision=0)
    version = db.get(models.FileVersion, version_id)
    original = db.get(models.FileVersion, built["file_version_id"])
    assert version.kind == "working"
    assert version.stored_object_id == original.stored_object_id
    assert version.parent_version_id == original.id
    objects = db.query(models.StoredObject).filter(
        models.StoredObject.case_id == built["case_id"]).all()
    assert len(objects) == 1


def test_edited_working_copy_gets_its_own_stored_object(db, store):
    built = factories.build_material_with_storage(
        db, store, content="TEST-ONLY 原始条款".encode(), name="条款.txt")
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="条款.txt")
    store.managed_path(relative).write_bytes("TEST-ONLY 修改后条款".encode())
    version_id, _job = materials.register_file_version(
        db, store, case_id=built["case_id"], working_copy_id=working_id, expected_revision=0)
    version = db.get(models.FileVersion, version_id)
    original = db.get(models.FileVersion, built["file_version_id"])
    assert version.stored_object_id != original.stored_object_id
    stored = db.get(models.StoredObject, version.stored_object_id)
    assert store.managed_path(stored.relative_path).read_bytes() == "TEST-ONLY 修改后条款".encode()
    # The original bytes are untouched.
    original_stored = db.get(models.StoredObject, original.stored_object_id)
    assert store.managed_path(original_stored.relative_path).read_bytes() == \
        "TEST-ONLY 原始条款".encode()
